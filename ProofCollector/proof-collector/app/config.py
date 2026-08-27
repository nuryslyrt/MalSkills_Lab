"""Configuration + fail-closed resolution (SPEC §6, §9, §15).

Zero-dependency. Runtime material (canaries, ingest key, scenario) is created by
`proofctl init` and read from CONFIG_DIR; SQLite state lives in STATE_DIR.
"""
import json
import os
import pathlib

# --- Fixed constants (not env-tunable in v2.1) — SPEC §9 -------------------
PATH_MAX_BYTES = 2048
QUERY_MAX_BYTES = 8192
HEADER_TOTAL_MAX_BYTES = 16384
MAX_DECODE_CANDIDATES = 16
MAX_DECODE_PASSES = 2            # base64 passes only; URL-decode does not count
MAX_TOTAL_DECODED_BYTES = 262144
MAX_JSON_DEPTH = 16
MAX_JSON_VALUES = 256


def _present(key):
    return key in os.environ


def _get(key, default):
    return os.environ.get(key, default)


class ConfigError(SystemExit):
    pass


class Config:
    def __init__(self):
        self.mode = _get("MODE", "local").lower()
        if self.mode not in ("local", "external"):
            raise ConfigError(f"MODE must be local|external, got {self.mode!r}")

        # --- Three-state evidence/debug resolution (SPEC §6, §15) ----------
        # Unset -> mode-derived safe effective value. Explicit unsafe under
        # external -> FAIL CLOSED (refuse boot). Shown '=full'/'=50' are LOCAL
        # defaults, never applied before the fail-closed check.
        ev_set = _present("EVIDENCE_MODE")
        ev_val = _get("EVIDENCE_MODE", "full").lower()
        dr_set = _present("DEBUG_RING_SIZE")
        dr_val = int(_get("DEBUG_RING_SIZE", "50"))

        if self.mode == "external":
            if ev_set and ev_val == "full":
                raise ConfigError(
                    "FAIL-CLOSED: EVIDENCE_MODE=full is not permitted with MODE=external")
            if dr_set and dr_val > 0:
                raise ConfigError(
                    "FAIL-CLOSED: DEBUG_RING_SIZE>0 is not permitted with MODE=external")
            self.evidence_mode = "minimal"   # coerce safe default
            self.debug_ring_size = 0         # coerce safe default
        else:
            self.evidence_mode = ev_val if ev_val in ("full", "minimal") else "full"
            # debug ring is gated on evidence=full, not merely mode=local (SPEC §6)
            self.debug_ring_size = dr_val if self.evidence_mode == "full" else 0

        # EVIDENCE_MODE is launch-time immutable: no runtime switch exists.
        self.receiver_scope = "host_local" if self.mode == "local" else "external"

        self.port = int(_get("PORT", "8888"))
        # Loopback enforcement: local ALWAYS binds 127.0.0.1 (SPEC §6). External
        # binds 0.0.0.0 but is only reached via the reverse proxy (M5).
        self.host = "127.0.0.1" if self.mode == "local" else "0.0.0.0"

        self.collector_id = _get("COLLECTOR_ID", "local-01")
        self.config_dir = pathlib.Path(_get("CONFIG_DIR", ".pcrun"))
        self.state_dir = pathlib.Path(_get("STATE_DIR", str(self.config_dir / "state")))

        self.body_max_bytes = int(_get("BODY_MAX_BYTES", "65536"))
        self.capture_context_bytes = int(_get("CAPTURE_CONTEXT_BYTES", "256"))
        self.capture_ttl_hours = int(_get("CAPTURE_TTL_HOURS", "24"))
        self.capture_full_body_ttl_hours = int(_get("CAPTURE_FULL_BODY_TTL_HOURS", "1"))
        # full-body is a local+full opt-in only
        self.capture_full_body = (
            _get("CAPTURE_FULL_BODY", "false").lower() == "true"
            and self.evidence_mode == "full"
        )
        self.max_events = int(_get("MAX_EVENTS", "1000"))
        # Ingest policy: local demos default to 'open' (record every callback — an exfiltrated
        # document/report IS the proof, no canary required); external forces 'token_required'.
        self.ingest_policy = ("token_required" if self.mode == "external"
                              else _get("INGEST_POLICY", "open").lower())

        # --- Runtime material (written by `proofctl init`) -----------------
        self.canaries = self._load_json("canaries.json").get("canaries", [])
        self.scenario = self._load_json("resolved-scenario.json")
        self.run_id = self.scenario.get("run_id", "unknown")
        self.execution_id = self.scenario.get("execution_id", "unknown")
        self.scenario_id = self.scenario.get("scenario_id", "unknown")
        self.system_id = self.scenario.get("system_id", "unknown")
        self.ingest_key = self._load_line("ingest_key")
        self.admin_token = self._load_line("admin-token")

        if not self.canaries:
            raise ConfigError("no canaries loaded — run `proofctl init` first")
        if not self.ingest_key:
            raise ConfigError("no ingest key — run `proofctl init` first")
        if self.mode == "external" and self.evidence_mode != "minimal":
            raise ConfigError("FAIL-CLOSED: external must be minimal evidence")

    def _load_json(self, name):
        p = self.config_dir / name
        if not p.exists():
            return {}
        return json.loads(p.read_text())

    def _load_line(self, name):
        p = self.config_dir / name
        if not p.exists():
            return ""
        return p.read_text().strip()

    def public_status(self):
        """Non-secret status metadata (SPEC §12.1 /status). No key/token."""
        return {
            "collector_id": self.collector_id,
            "deployment_mode": self.mode,
            "receiver_scope": self.receiver_scope,
            "evidence_mode": self.evidence_mode,
            "debug_ring_size": self.debug_ring_size,
            "scenario_id": self.scenario_id,
            "run_id": self.run_id,
            "capture_full_body": self.capture_full_body,
        }

    def masked_endpoint(self):
        """Ingest URL with the key redacted (SPEC §14 'masked by default').

        Safe for a projected screen, logs, and export — matches the `<INGEST_KEY>`
        placeholder used by stored/exported captures.
        """
        return f"http://127.0.0.1:{self.port}/ingest/<INGEST_KEY>/notify"

    def revealed_endpoint(self):
        """Real keyed ingest URL — the deliberate local reveal (SPEC §13, §16).

        Loopback only. The key is meaningful solely to someone who can already route
        to this host's 127.0.0.1, so revealing it in local mode grants no capability
        an attendee does not already have over their own collector. Callers MUST gate
        this on mode == 'local'; in external the bind is 0.0.0.0 and the key is a real
        secret.
        """
        return f"http://127.0.0.1:{self.port}/ingest/{self.ingest_key}/notify"
