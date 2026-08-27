"""Observation + windowed capture builders (SPEC §10).

- Observation: minimal, external-safe, NO token values, NO raw body, NO source IP.
- Capture (local/full only): windowed. EVERY window field bounded by
  CAPTURE_CONTEXT_BYTES — including decoded_form — plus a bounded raw_window
  (pre-decode) that drives reproduction. The full body is NOT stored unless the
  CAPTURE_FULL_BODY opt-in is on (local+full).
"""
import hashlib
import uuid

from . import config as C


def _window_str(text: str, pos: int, tok_len: int, ctx: int) -> str:
    start = max(0, pos - ctx)
    end = min(len(text), pos + tok_len + ctx)
    return text[start:end]


def _window_bytes(raw: bytes, span, ctx: int):
    if not span or span[0] is None or span[0] < 0:
        return None, "", ""
    s = max(0, span[0] - ctx)
    e = min(len(raw), span[1] + ctx)
    before = raw[max(0, span[0] - ctx):span[0]].decode("utf-8", "replace")
    after = raw[span[1]:min(len(raw), span[1] + ctx)].decode("utf-8", "replace")
    return raw[s:e].decode("utf-8", "replace"), before, after


def _printable_ratio(text: str) -> float:
    if not text:
        return 0.0
    ok = sum(1 for ch in text
             if ch in "\n\r\t" or 32 <= ord(ch) < 127 or ord(ch) > 159)
    return ok / len(text)


def _best_preview(raw_body: bytes, candidates, cap: int):
    """Pick the most legible rendering of an unmatched callback body.

    An encoded payload (base64 inside a JSON envelope, say) is unreadable raw, and the
    raw view proves nothing to a room. The decoded candidate is the artifact worth
    showing, so prefer the longest decoded one that still looks like text and report
    the transform chain that produced it. Falls back to the raw body.
    """
    raw_text = raw_body[:cap].decode("utf-8", "replace")
    best = None
    for cand in candidates or ():
        if not cand.get("encoding_path"):
            continue                      # undecoded; raw view already covers it
        text = cand.get("text") or ""
        if len(text) < 32 or _printable_ratio(text) < 0.9:
            continue
        if best is None or len(text) > len(best.get("text") or ""):
            best = cand
    if best is None:
        return raw_text, [], len(raw_body) > cap
    text = best["text"]
    return text[:cap], list(best["encoding_path"]), len(text) > cap


def build(cfg: C.Config, req, raw_body: bytes, matched, execution_source,
          candidates=None):
    """Return (observation, capture_or_None)."""
    ctx = cfg.capture_context_bytes
    event_id = str(uuid.uuid4())
    body_sha256 = hashlib.sha256(raw_body).hexdigest()

    obs_matches = []
    cap_matches = []
    for h in matched:
        obs_matches.append({
            "canary_id": h["canary_id"],
            "label": h["label"],
            "secret_kind": h["secret_kind"],
            "matched_source": h["matched_source"],
            "encoding_path": h["encoding_path"],
            # NB: no token value in the observation.
        })
        if cfg.evidence_mode == "full":
            cand = h["cand"]
            tok = h["token"]
            decoded_form = _window_str(cand["text"], h["pos"], len(tok), ctx)
            raw_window, cb, ca = _window_bytes(raw_body, cand.get("raw_span"), ctx)
            if raw_window is None:
                # query/header source: no raw body span; fall back to the
                # (bounded) decoded window for both.
                raw_window = decoded_form
            cap_matches.append({
                "canary_id": h["canary_id"],
                "label": h["label"],
                "secret_kind": h["secret_kind"],
                "canary_value": tok,              # synthetic; LOCAL ONLY
                "matched_source": h["matched_source"],
                "json_path": None,                # M1: not yet resolved
                "encoding_path": h["encoding_path"],
                "raw_window": raw_window,         # bounded, pre-decode
                "decoded_form": decoded_form,     # bounded around the canary
                "context_before": cb,
                "context_after": ca,
            })

    if matched:
        obs_type = "canary_disclosed"
        obs_summary = (f"Planted {matched[0]['label']} marker reached the "
                       f"{'host-local' if cfg.mode == 'local' else 'external'} receiver.")
    else:
        obs_type = "callback_received"
        obs_summary = (f"Callback received ({len(raw_body)} bytes, "
                       f"{req.get('content_type','') or 'unknown type'}) at ingest path "
                       f"/{req.get('path_tail','')}.")
    observation = {
        "schema_version": C.__dict__.get("SCHEMA_VERSION", "2.1"),
        "event_id": event_id,
        "received_at": None,   # set by store on insert (server wall clock)
        "collector": {
            "collector_id": cfg.collector_id,
            "deployment_mode": cfg.mode,
            "receiver_scope": cfg.receiver_scope,
        },
        "scenario": {
            "scenario_id": cfg.scenario_id,
            "system_id": cfg.system_id,
            "run_id": cfg.run_id,
            "execution_id": cfg.execution_id,
            "execution_source": execution_source,
            "skill_id": None,   # optional; scenario-level correlation when absent
        },
        "transport": {
            "type": "http",
            "method": req["method"],
            "content_type": req.get("content_type", ""),
            "body_bytes": len(raw_body),
            "body_sha256": body_sha256,
            "path_tail_present": bool(req.get("path_tail")),
            "query_present": bool(req.get("query")),
            "truncated": req.get("truncated", False),
        },
        "matches": obs_matches,
        "observation": {
            "type": obs_type,
            "proof_level": "local_callback" if cfg.mode == "local" else "network_callback",
            "summary": obs_summary,
        },
    }

    capture = None
    if cfg.evidence_mode == "full":
        capture = {
            "schema_version": "2.1",
            "event_id": event_id,
            "request": {
                "method": req["method"],
                "path_tail": req.get("path_tail", ""),
                "query_present": bool(req.get("query")),
                "content_type": req.get("content_type", ""),
                "body_bytes": len(raw_body),
            },
            "match_detail": cap_matches,
            "reproduction": {
                # Stored/exported curl MASKS the key. Real key only via the
                # deliberate local copy action (M2). Body replayed = raw_window.
                "curl_masked": _curl_masked(cfg, req, cap_matches),
            },
            "full_body_present": bool(cfg.capture_full_body),
        }
        if not matched:
            # no canary — the whole callback body IS the exfiltrated artifact; show a bounded
            # preview, decoding it first when the payload arrived encoded.
            preview_cap = max(2000, cfg.capture_context_bytes * 8)
            preview, enc_path, truncated = _best_preview(raw_body, candidates, preview_cap)
            capture["document_preview"] = preview
            capture["document_encoding_path"] = enc_path
            capture["document_truncated"] = truncated
        if cfg.capture_full_body:
            capture["request"]["body_raw"] = raw_body.decode("utf-8", "replace")

    return observation, capture


def _curl_masked(cfg, req, cap_matches):
    tail = req.get("path_tail", "")
    path = f"/ingest/<INGEST_KEY>" + (f"/{tail}" if tail else "")
    window = cap_matches[0]["raw_window"] if cap_matches else ""
    # single-quote-escape the replayed window (shell-safe)
    safe = window.replace("'", "'\\''")
    ct = req.get("content_type", "application/json")
    return (f"curl -X {req['method']} 'http://127.0.0.1:{cfg.port}{path}' "
            f"-H 'Content-Type: {ct}' --data-raw '{safe}'")
