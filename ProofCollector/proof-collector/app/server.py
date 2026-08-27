"""HTTP server — keyed ingest + read API (SPEC §9, §13). Stdlib only.

Run:  python3 -m app.server        (reads env / CONFIG_DIR)
Binds 127.0.0.1 in local mode (loopback enforced).
"""
import json
import pathlib
import queue
import re
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import capture as capmod
from . import config as C
from . import matcher
from . import normalize
from . import stream as streammod

BLAND = b'{"ok":true}'
INGEST_METHODS = ("GET", "POST", "PUT", "PATCH")
HEADER_ALLOW_PREFIXES = ("x-pc-",)
HEADER_ALLOW_EXACT = ("user-agent",)

CFG = None
STORE = None
BROKER = None
_UI_DIR = pathlib.Path(__file__).parent / "ui"


def _structural_snippet(content_type, raw):
    """Dropped-request snippet: structural metadata ONLY, never verbatim
    content (dropped requests matched no canary -> no synthetic guarantee)."""
    info = f"ct={content_type or '?'}"
    try:
        obj = json.loads(raw.decode("utf-8", "replace"))
        if isinstance(obj, dict):
            info += " json.keys=" + ",".join(list(obj.keys())[:8])
        else:
            info += f" json.{type(obj).__name__}"
    except Exception:
        info += " non-json"
    return info[:120]


def _card(observation, capture):
    """Compact 'event card' pushed to the live dashboard over SSE (already sanitized)."""
    obs = observation["observation"]
    card = {
        "event_id": observation["event_id"],
        "sequence": observation.get("sequence"),
        "received_at": observation.get("received_at"),
        "type": obs["type"],
        "summary": obs["summary"],
        "method": observation["transport"]["method"],
        "content_type": observation["transport"].get("content_type", ""),
        "body_bytes": observation["transport"]["body_bytes"],
        "matches": [{"label": m["label"], "encoding": m["encoding_path"]}
                    for m in observation.get("matches", [])],
    }
    if capture:
        md = capture.get("match_detail") or []
        if md:
            card["canary"] = md[0].get("canary_value")   # synthetic; local only
        if capture.get("document_preview"):
            card["preview"] = capture["document_preview"][:800]
            card["truncated"] = bool(capture.get("document_truncated"))
            card["preview_encoding"] = capture.get("document_encoding_path") or []
    return card


class Handler(BaseHTTPRequestHandler):
    server_version = "ProofCollector/M1"
    protocol_version = "HTTP/1.1"

    # ---- helpers ----------------------------------------------------------
    def _send(self, code, payload, ctype="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _bland(self):
        self._send(200, BLAND)

    def _allow_headers(self):
        out = {}
        for k, v in self.headers.items():
            lk = k.lower()
            if lk in HEADER_ALLOW_EXACT or any(lk.startswith(p) for p in HEADER_ALLOW_PREFIXES):
                out[lk] = v
        return out

    def _admin_ok(self):
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {CFG.admin_token}" and CFG.admin_token

    def log_message(self, fmt, *args):
        pass  # no access logging (SPEC §16.5)

    # ---- dispatch ---------------------------------------------------------
    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    def do_PATCH(self):
        self._route("PATCH")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _route(self, method):
        parts = urllib.parse.urlsplit(self.path)
        path = parts.path
        query = urllib.parse.parse_qs(parts.query)

        # ingest — keyed route, no catch-all
        m = re.match(r"^/ingest/([^/]+)(?:/(.*))?$", path)
        if m:
            return self._ingest(method, m.group(1), m.group(2) or "", query)

        if method != "GET":
            if path == "/api/v1/admin/clear" and method == "POST":
                return self._admin_clear()
            return self._send(404, {"error": "not_found"})

        # read API (GET)
        if path == "/healthz":
            return self._send(200, {"status": "ok"})
        if path == "/readyz":
            return self._send(200, {"ready": True, "events": STORE.count()})
        if path == "/dashboard":
            return self._dashboard()
        if path == "/api/v1/stream":
            return self._stream()
        if path == "/api/v1/status":
            return self._send(200, CFG.public_status())
        if path == "/api/v1/endpoint":
            # Deliberate reveal of the keyed ingest URL (SPEC §13). Local only: in
            # external the server binds 0.0.0.0, so serving the real key here would
            # hand the ingest capability to anyone who can reach the port. No dashboard
            # auth exists on read routes in M1, so external fails closed to 404 —
            # indistinguishable from unimplemented, matching capture/debug.
            if CFG.mode != "local":
                return self._send(404, {"error": "not_found"})
            return self._send(200, {
                "masked": CFG.masked_endpoint(),
                "endpoint": CFG.revealed_endpoint(),
                "ingest_policy": CFG.ingest_policy,
                "receiver_scope": CFG.receiver_scope,
                "notice": "Loopback only. Rotate after a public demo.",
            })
        if path == "/api/v1/canaries":
            return self._send(200, {"canaries": [
                {"canary_id": c["canary_id"], "label": c["label"],
                 "secret_kind": c["secret_kind"]} for c in CFG.canaries]})
        if path == "/api/v1/events":
            return self._send(200, {"events": STORE.list_events()})
        mc = re.match(r"^/api/v1/events/([^/]+)/capture$", path)
        if mc:
            if not (CFG.mode == "local" and CFG.evidence_mode == "full"):
                return self._send(404, {"error": "not_found"})   # 404 outside local+full
            cap = STORE.get_capture(mc.group(1))
            return self._send(200, cap) if cap else self._send(404, {"error": "not_found"})
        if path == "/api/v1/debug/dropped":
            if not (CFG.mode == "local" and CFG.evidence_mode == "full"):
                return self._send(404, {"error": "not_found"})
            return self._send(200, {"dropped": STORE.dropped()})
        return self._send(404, {"error": "not_found"})

    # ---- ingest -----------------------------------------------------------
    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > CFG.body_max_bytes:
            return None, True
        raw = self.rfile.read(length) if length else b""
        return raw, False

    def _ingest(self, method, key, tail, query):
        if method not in INGEST_METHODS:
            return self._send(404, {"error": "not_found"})
        raw, oversize = self._read_body()
        if oversize:
            return self._send(413, {"error": "too_large"})
        if len(tail.encode()) > C.PATH_MAX_BYTES:
            return self._send(413, {"error": "path_too_large"})

        # trusted execution-source header only on loopback/local (M1)
        exec_source = "live_agent"
        hdr = self.headers.get("X-PC-Execution-Source", "").lower()
        if CFG.mode == "local" and hdr in ("simulator", "replay", "live_agent"):
            exec_source = hdr

        content_type = self.headers.get("Content-Type", "")

        # admission: (1) ingest key valid, (2) >=1 canary matches
        if key != CFG.ingest_key:
            self._drop(method, tail, raw, content_type, reason="bad_key")
            return self._bland()

        cands = normalize.candidates(raw, query, self._allow_headers())
        matched = matcher.match(cands, CFG.canaries)
        if not matched and CFG.ingest_policy == "token_required":
            self._drop(method, tail, raw, content_type, reason="no_canary")
            return self._bland()
        # open policy (local default): record the callback even with no canary —
        # the exfiltrated document (e.g. a code-review report) is itself the proof.

        req = {"method": method, "path_tail": tail, "query": query,
               "content_type": content_type, "truncated": False}
        observation, cap = capmod.build(CFG, req, raw, matched, exec_source, cands)
        STORE.insert(observation, cap)
        if BROKER is not None:
            BROKER.publish(_card(observation, cap))
        return self._bland()   # never reflect

    def _drop(self, method, tail, raw, content_type, reason):
        # local+full only debug ring; structural snippet only
        if CFG.mode == "local" and CFG.evidence_mode == "full" and CFG.debug_ring_size > 0:
            STORE.add_dropped(method, f"/ingest/<key>/{tail}", len(raw),
                              f"{reason}: {_structural_snippet(content_type, raw)}")

    def _admin_clear(self):
        if not self._admin_ok():
            return self._send(401, {"error": "unauthorized"})
        STORE.clear()
        return self._send(200, {"cleared": True})

    # ---- M2 dashboard + live stream --------------------------------------
    def _send_html(self, body: bytes):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        # the dashboard needs its own inline CSS/JS + same-origin SSE/fetch
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; "
                         "script-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _dashboard(self):
        try:
            body = (_UI_DIR / "dashboard.html").read_bytes()
        except OSError:
            return self._send(404, {"error": "dashboard_not_found"})
        return self._send_html(body)

    def _stream(self):
        q = BROKER.subscribe() if BROKER is not None else None
        if q is None:
            return self._send(429, {"error": "too_many_stream_clients"})
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self.wfile.write(b": connected\n\n"); self.wfile.flush()
            while True:
                try:
                    data = q.get(timeout=15)
                    self.wfile.write(("data: " + data + "\n\n").encode()); self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": ping\n\n"); self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            BROKER.unsubscribe(q)


def main():
    global CFG, STORE, BROKER
    from . import store as storemod
    CFG = C.Config()
    if CFG.mode == "local" and CFG.host != "127.0.0.1":
        raise C.ConfigError("loopback enforcement: local mode must bind 127.0.0.1")
    STORE = storemod.Store(CFG)
    BROKER = streammod.Broker(getattr(CFG, "max_sse_clients", 8))
    httpd = ThreadingHTTPServer((CFG.host, CFG.port), Handler)
    print(f"[proof-collector] up on http://{CFG.host}:{CFG.port}  "
          f"mode={CFG.mode} evidence={CFG.evidence_mode} scenario={CFG.scenario_id}",
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
