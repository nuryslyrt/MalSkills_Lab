# Proof Collector

Passive HTTP receiver that records **proof** that a planted **synthetic canary** was disclosed and
caused a callback — and preserves a bounded, windowed capture you can see and reproduce.

> **Safety.** All seeded values are synthetic (`CANARY_` prefix, reserved `.invalid` domains). Local
> mode binds **127.0.0.1 only** — private to your machine. Captures are windowed (never the whole
> request body by default) and never leave the process except by an explicit local action.

**Zero dependencies.** Pure Python standard library (`http.server` + `sqlite3`). No pip, runs offline.

## Quickstart (native)
```bash
./proofctl init          # generate synthetic canaries + ingest key (writes ./.pcrun, 0700)
./proofctl up            # start on http://127.0.0.1:8888 (loopback only)
./proofctl self-test     # drive the deterministic simulator and verify the loop → SELF-TEST PASS
./proofctl events        # see the recorded observation (no secret values)
./proofctl down
```

## Quickstart (container — hardened)
```bash
./proofctl init
docker compose -f deploy/compose.local.yaml up --build -d   # read-only rootfs, dropped caps, loopback
./proofctl self-test
docker compose -f deploy/compose.local.yaml down
```

## What Proof Collector does
- Keyed ingest `/ingest/{key}/…` with pre-decode bounds; bland `200 {"ok":true}` (never reflects).
- Bounded normalization (URL + double base64 + JSON scalars) and canary matching.
- **Observation** (minimal, no token, no raw body, no source IP) + **windowed capture**
  (local+full only): the fake canary value, decoded form, carrier, and bounded context — the
  "money shot" — plus a masked-key reproduction `curl`.
- Evidence tiered by mode: `full` locally, `minimal` externally (fail-closed; capture/debug routes
  404 outside local+full).
- In-memory, non-persistent **debug ring** for dropped requests (local+full; structural metadata only).

## Read API
`GET /api/v1/status` · `/api/v1/events` · `/api/v1/events/{id}/capture` (local+full) ·
`/api/v1/debug/dropped` (local+full) · `/api/v1/canaries` · `/healthz` · `/readyz`


## License
AGPL v3 (matches ORPHEUS). Full text to be vendored as `LICENSE` before public release.
