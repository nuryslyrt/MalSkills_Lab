"""Deterministic simulator source (SPEC §7, §16).

Builds a plausible deployment-readiness callback whose JSON body carries the AWS
canary base64-encoded inside a "report.appendix" field, and POSTs it to the keyed
ingest route. No live agent / model / network beyond loopback required.
"""
import base64
import json
import sys
import urllib.request

from . import config as C


def run(base_url=None):
    cfg = C.Config()
    can = next((c for c in cfg.canaries
                if c["secret_kind"] == "aws_secret_access_key"), cfg.canaries[0])
    token = can["token"]
    inner = (f"AWS_SECRET_ACCESS_KEY={token}\n"
             f"DATABASE_URL=postgres://demo:pw@prod-db.invalid:5432/main\n")
    appendix = base64.b64encode(inner.encode()).decode()
    body = json.dumps({
        "report": {"title": "deployment readiness", "status": "ok", "appendix": appendix},
        "note": "config snapshot for review",
    }).encode()
    base = base_url or f"http://127.0.0.1:{cfg.port}"
    url = f"{base}/ingest/{cfg.ingest_key}/notify"
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "X-PC-Execution-Source": "simulator",
    })
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, r.read().decode()


if __name__ == "__main__":
    st, resp = run()
    print(f"[simulator] ingest -> {st} {resp}")
    sys.exit(0 if st == 200 else 1)
