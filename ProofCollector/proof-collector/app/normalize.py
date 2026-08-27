"""Bounded normalization (SPEC §9).

Produces a bounded set of decoded candidate strings from a request, tracking:
  - text          : the candidate string to scan for canaries
  - encoding_path : ordered transforms, e.g. []  ['url']  ['base64']  ['url','base64']
  - source        : body | body.json | query | header
  - raw_span      : (start, end) byte offsets in the RAW pre-decode body that
                    produced this candidate (None for query/header sources).
                    Used to build the raw reproduction window (SPEC §10.3).

All work is bounded BEFORE and during decode: candidate count, base64 passes,
and total decoded bytes are capped.
"""
import base64
import json
import re
import urllib.parse

from . import config as C

_B64_RUN = re.compile(rb"[A-Za-z0-9+/]{16,}={0,2}")


def _safe_text(b: bytes) -> str:
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("latin-1", "replace")


def candidates(raw_body: bytes, query: dict, headers_allow: dict):
    """Return a bounded list of candidate dicts (see module docstring)."""
    out = []
    decoded_budget = [0]  # mutable closure counter

    def add(text, path, source, raw_span):
        if len(out) >= C.MAX_DECODE_CANDIDATES:
            return False
        out.append({"text": text, "encoding_path": path,
                    "source": source, "raw_span": raw_span})
        return True

    raw_text = _safe_text(raw_body)

    # (1) raw body
    add(raw_text, [], "body", (0, len(raw_body)))

    # (2) one URL-decode (does not count toward base64 pass budget)
    url_dec = urllib.parse.unquote_plus(raw_text)
    if url_dec != raw_text:
        add(url_dec, ["url"], "body", (0, len(raw_body)))

    # (3) JSON scalar string values (bounded), with best-effort raw span
    try:
        obj = json.loads(raw_text)
        vals = []

        def walk(o, depth):
            if depth > C.MAX_JSON_DEPTH or len(vals) >= C.MAX_JSON_VALUES:
                return
            if isinstance(o, dict):
                for v in o.values():
                    walk(v, depth + 1)
            elif isinstance(o, list):
                for v in o:
                    walk(v, depth + 1)
            elif isinstance(o, str):
                vals.append(o)

        walk(obj, 0)
        for v in vals:
            idx = raw_text.find(v)
            span = (idx, idx + len(v)) if idx >= 0 else None
            add(v, [], "body.json", span)
    except (ValueError, TypeError):
        pass

    # (4) query values
    for k, vlist in (query or {}).items():
        for v in vlist:
            add(v, [], "query", None)

    # (5) allowlisted header values
    for k, v in (headers_allow or {}).items():
        add(v, [], "header", None)

    # (6) base64 passes — up to MAX_DECODE_PASSES, on prior-stage outputs.
    frontier = list(out)
    for _pass in range(C.MAX_DECODE_PASSES):
        new_frontier = []
        for item in frontier:
            btext = item["text"].encode("utf-8", "replace")
            for m in _B64_RUN.finditer(btext):
                frag = m.group(0)
                if len(frag) % 4 != 0:
                    continue
                try:
                    dec = base64.b64decode(frag, validate=True)
                except (ValueError, Exception):
                    continue
                if decoded_budget[0] + len(dec) > C.MAX_TOTAL_DECODED_BYTES:
                    continue
                decoded_budget[0] += len(dec)
                dtext = _safe_text(dec)
                # raw span: prefer the parent's raw span (the carrier slice we
                # can repost to re-trigger the same decode); for a base64 run in
                # the raw body itself, use the run's own offsets.
                if item["source"] == "body" and item["encoding_path"] == []:
                    raw_span = (m.start(), m.end())
                else:
                    raw_span = item.get("raw_span")
                nc = {"text": dtext, "encoding_path": item["encoding_path"] + ["base64"],
                      "source": item["source"], "raw_span": raw_span}
                if add(dtext, nc["encoding_path"], nc["source"], nc["raw_span"]):
                    new_frontier.append(nc)
        frontier = new_frontier
        if not frontier:
            break

    return out
