"""In-memory SSE broker (M2). Best-effort live fan-out of event cards to the dashboard.

Bounded: at most MAX_SSE_CLIENTS subscribers; each has a bounded queue and a slow
client simply drops frames (never blocks ingest). Nothing is persisted here.
"""
import json
import queue
import threading


class Broker:
    def __init__(self, max_clients=8, queue_size=200):
        self._subs = []
        self._lock = threading.Lock()
        self.max_clients = max_clients
        self.queue_size = queue_size

    def subscribe(self):
        q = queue.Queue(maxsize=self.queue_size)
        with self._lock:
            if len(self._subs) >= self.max_clients:
                return None
            self._subs.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, card: dict):
        data = json.dumps(card)
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(data)
            except queue.Full:
                pass  # best-effort: drop for a slow client

    def client_count(self):
        with self._lock:
            return len(self._subs)
