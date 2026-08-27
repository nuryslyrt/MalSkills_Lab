"""proofctl init — create private runtime material (SPEC §8).

Generates the ingest key, admin token, and synthetic canary values, writing a
matching pair: the collector's canaries.json and the victim.env fixture. All
files 0600, dir 0700, and gitignored. Prints no secret values.
"""
import json
import os
import pathlib
import secrets
import sys


def _embed(field, val):
    if field == "DATABASE_URL":
        return f"postgres://demo:{val}@prod-db.invalid:5432/main"
    return val


def _write_secure(path, text):
    path.write_text(text)
    os.chmod(path, 0o600)


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    scenario_file = "scenarios/deploy-readiness.json"
    if "--scenario" in argv:
        scenario_file = argv[argv.index("--scenario") + 1]

    cfgdir = pathlib.Path(os.environ.get("CONFIG_DIR", ".pcrun"))
    cfgdir.mkdir(parents=True, exist_ok=True)
    os.chmod(cfgdir, 0o700)

    scen = json.loads(pathlib.Path(scenario_file).read_text())
    run_id = secrets.token_hex(8)
    exec_id = secrets.token_hex(8)
    ingest_key = secrets.token_hex(16)
    admin_token = secrets.token_hex(16)

    canaries = []
    env_lines = ["# DEMO ONLY — ALL VALUES ARE SYNTHETIC CANARIES (reserved .invalid domains)"]
    for c in scen["canaries"]:
        val = f"CANARY_v2_{c['secret_kind']}_{secrets.token_hex(8)}"
        canaries.append({"canary_id": c["canary_id"], "token": val,
                         "label": c["label"], "secret_kind": c["secret_kind"]})
        env_lines.append(f"{c['victim_field']}={_embed(c['victim_field'], val)}")

    _write_secure(cfgdir / "canaries.json",
                  json.dumps({"schema_version": "2.1", "run_id": run_id,
                              "canaries": canaries}, indent=2))
    _write_secure(cfgdir / "victim.env", "\n".join(env_lines) + "\n")
    _write_secure(cfgdir / "resolved-scenario.json",
                  json.dumps({"scenario_id": scen["scenario_id"],
                              "system_id": scen["system"]["system_id"],
                              "run_id": run_id, "execution_id": exec_id}, indent=2))
    _write_secure(cfgdir / "ingest_key", ingest_key)
    _write_secure(cfgdir / "admin-token", admin_token)
    (cfgdir / ".gitignore").write_text("*\n")

    port = os.environ.get("PORT", "8888")
    print(f"[init] scenario={scen['scenario_id']} canaries={len(canaries)} runtime={cfgdir}/ (0700)")
    print(f"[init] victim fixture written: {cfgdir}/victim.env")
    print(f"[init] ingest endpoint: http://127.0.0.1:{port}/ingest/<key>/…   (key hidden)")


if __name__ == "__main__":
    main()
