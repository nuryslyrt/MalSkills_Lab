"""SQLite store + in-memory debug ring (SPEC §10.5, §10.6).

- observations: minimal event, always.
- captures: windowed capture, local/full only (joined by event_id).
- debug ring: in-memory only, dropped/non-matching requests, local+full only.
  NEVER persisted, exported, or logged.
"""
import collections
import datetime
import json
import sqlite3
import threading


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class Store:
    def __init__(self, cfg):
        self.cfg = cfg
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = str(cfg.state_dir / "events.sqlite3")
        self._lock = threading.Lock()
        self._seq = 0
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS observations "
            "(event_id TEXT PRIMARY KEY, seq INTEGER, received_at TEXT, json TEXT)")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS captures "
            "(event_id TEXT PRIMARY KEY, received_at TEXT, full_body INTEGER, json TEXT)")
        self._conn.commit()
        row = self._conn.execute("SELECT MAX(seq) FROM observations").fetchone()
        self._seq = (row[0] or 0)
        self.debug_ring = collections.deque(maxlen=max(0, cfg.debug_ring_size))

    def insert(self, observation, capture):
        with self._lock:
            self._seq += 1
            observation["sequence"] = self._seq
            observation["received_at"] = _utcnow()
            eid = observation["event_id"]
            self._conn.execute(
                "INSERT INTO observations(event_id, seq, received_at, json) VALUES (?,?,?,?)",
                (eid, self._seq, observation["received_at"], json.dumps(observation)))
            if capture is not None:
                self._conn.execute(
                    "INSERT INTO captures(event_id, received_at, full_body, json) VALUES (?,?,?,?)",
                    (eid, observation["received_at"],
                     1 if capture.get("full_body_present") else 0, json.dumps(capture)))
            self._conn.commit()
            # enforce MAX_EVENTS ring
            self._conn.execute(
                "DELETE FROM observations WHERE seq <= ?",
                (self._seq - self.cfg.max_events,))
            self._conn.commit()
            return observation

    def list_events(self, limit=200):
        with self._lock:
            rows = self._conn.execute(
                "SELECT json FROM observations ORDER BY seq ASC LIMIT ?",
                (limit,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def get_capture(self, event_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT json FROM captures WHERE event_id=?", (event_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def add_dropped(self, method, path_template, size, snippet):
        # local+full only; caller gates. Structural-metadata-only snippet.
        if self.cfg.debug_ring_size <= 0:
            return
        self.debug_ring.append({
            "ts": _utcnow(), "method": method, "path": path_template,
            "size": size, "snippet": snippet,
        })

    def dropped(self):
        return list(self.debug_ring)

    def clear(self):
        with self._lock:
            self._conn.execute("DELETE FROM observations")
            self._conn.execute("DELETE FROM captures")
            self._conn.commit()
            self._seq = 0
            self.debug_ring.clear()

    def count(self):
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
