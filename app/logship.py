"""Ships this process's log lines to the dashboard's log viewer from a background thread.

Logging never waits for the network: lines queue in memory (the oldest are dropped past a cap)
and a daemon thread posts them in batches. A batch keeps its batch_id until the dashboard has
accepted it, so a retry can never store the same lines twice.
"""

import collections
import logging
import threading
import uuid
from typing import Optional

from .dashboard import Client

MAX_LINE_CHARS = 2000
# The dashboard client logs about its own failures; shipping those would feed back on itself.
_IGNORED_PREFIXES = ("app.dashboard", "app.logship")


class LogShipper(logging.Handler):
    def __init__(self, client: Client, flush_interval_s: float = 5.0, max_buffer: int = 2000,
                 batch_size: int = 500):
        super().__init__()
        self._client = client
        self._interval = flush_interval_s
        self._max_buffer = max_buffer
        self._batch_size = batch_size
        self._pending: dict = {}
        self._inflight: dict = {}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stopped = False
        self._formatter = logging.Formatter("%(message)s")
        self._thread = threading.Thread(target=self._run, name="log-shipper", daemon=True)
        self._thread.start()

    @staticmethod
    def stream_for(record: logging.LogRecord) -> str:
        return "tick" if record.name.startswith("app.tick") else "backup"

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(_IGNORED_PREFIXES):
            return
        try:
            line = {"message": self._formatter.format(record)[:MAX_LINE_CHARS], "level": record.levelname,
                    "ts": record.created}
        except Exception:  # pylint: disable=broad-except  # a logging handler must never raise into the backup
            return
        with self._lock:
            queue = self._pending.setdefault(self.stream_for(record), collections.deque(maxlen=self._max_buffer))
            queue.append(line)

    def _flush_stream(self, stream: str) -> bool:
        """Send one batch of a stream; False when it must be retried later (or there was nothing to send)."""
        with self._lock:
            inflight = self._inflight.get(stream)
            if inflight is None:
                queue = self._pending.get(stream)
                if not queue:
                    return False
                lines = [queue.popleft() for _ in range(min(self._batch_size, len(queue)))]
                inflight = self._inflight[stream] = (uuid.uuid4().hex, lines)
        result = self._client.post_logs(stream, inflight[1], inflight[0])
        if result == "retry":
            return False
        with self._lock:
            self._inflight.pop(stream, None)
        return True

    def flush(self) -> None:
        """Send everything queued, stopping at the first batch the dashboard cannot take right now."""
        with self._lock:
            streams = list(set(self._pending) | set(self._inflight))
        for stream in streams:
            while self._flush_stream(stream):
                pass

    def _run(self) -> None:
        while not self._stopped:
            self._wake.wait(self._interval)
            self._wake.clear()
            self.flush()

    def close(self) -> None:
        self._stopped = True
        self._wake.set()
        self._thread.join(timeout=2)
        self.flush()
        super().close()

    def pending_count(self, stream: Optional[str] = None) -> int:
        with self._lock:
            queues = [self._pending.get(stream, ())] if stream else list(self._pending.values())
            inflight = [v[1] for k, v in self._inflight.items() if not stream or k == stream]
            return sum(len(q) for q in queues) + sum(len(lines) for lines in inflight)
