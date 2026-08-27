"""proofctl self-test — drive one simulator callback and verify the loop (SPEC §16).

Hard-gate checks (M1): observation recorded; canary token ABSENT from the
observation; base64 carrier detected; windowed capture present WITH the fake
value (money shot); proof level + execution source correct.
Exits nonzero on any failure.
"""
import json
import sys
import urllib.request

from . import config as C
from . import simulator


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, json.loads(r.read().decode())


def main():
    cfg = C.Config()
    base = f"http://127.0.0.1:{cfg.port}"
    aws = next(c for c in cfg.canaries if c["secret_kind"] == "aws_secret_access_key")

    st, _ = simulator.run(base)
    assert st == 200, f"ingest returned {st}, expected bland 200"

    _, evs = _get(base + "/api/v1/events")
    events = evs["events"]
    assert events, "no observation recorded"
    ev = events[-1]

    obs_text = json.dumps(ev)
    assert aws["token"] not in obs_text, "LEAK: canary token found in the observation"

    m = ev["matches"]
    assert any(x["canary_id"] == "aws-secret-01" and x["encoding_path"] == ["base64"]
               for x in m), f"expected base64 aws-secret-01 match, got {m}"
    assert ev["observation"]["proof_level"] == "local_callback", ev["observation"]
    assert ev["scenario"]["execution_source"] == "simulator", ev["scenario"]

    _, cap = _get(base + f"/api/v1/events/{ev['event_id']}/capture")
    md = next(x for x in cap["match_detail"] if x["canary_id"] == "aws-secret-01")
    assert md["canary_value"] == aws["token"], "capture missing the fake canary value"
    assert md["raw_window"], "capture missing raw_window (reproduction)"
    assert md["encoding_path"] == ["base64"], md["encoding_path"]
    assert len(md["decoded_form"]) <= 2 * cfg.capture_context_bytes + len(aws["token"]) + 8, \
        "decoded_form not bounded by CAPTURE_CONTEXT_BYTES"

    print("SELF-TEST PASS")
    print(f"  observation  seq={ev['sequence']}  proof={ev['observation']['proof_level']}  "
          f"source={ev['scenario']['execution_source']}")
    print(f"  match        {md['label']} via {md['matched_source']} encoding={md['encoding_path']}")
    print("  token absent from observation: OK")
    print("  windowed capture bounded:      OK")
    print(f"  money-shot value (fake):       {md['canary_value']}")
    print(f"  reproduction (masked key):     {cap['reproduction']['curl_masked'][:90]}…")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"SELF-TEST FAIL: {e}", file=sys.stderr)
        sys.exit(1)
