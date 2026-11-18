"""Bounded snapshots for history queries, invalidated by database/WAL writes."""
from collections import OrderedDict
from copy import deepcopy
from functools import wraps
from pathlib import Path
import threading
import time


class HistoryQueryCache:
    def __init__(self, path: Path, *, limit: int = 16, ttl: float = 30) -> None:
        self.path, self.limit, self.ttl = path, limit, ttl
        self._entries = OrderedDict()
        self._lock = threading.RLock()

    def _revision(self):
        values = []
        for path in (self.path, self.path.with_name(self.path.name + "-wal")):
            try:
                stat = path.stat()
                values.append((stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
            except FileNotFoundError:
                values.append(None)
        return tuple(values)

    def read(self, key, query):
        with self._lock:
            revision = self._revision()
            entry = self._entries.pop(key, None)
            if entry and entry[0] == revision and time.monotonic() - entry[1] < self.ttl:
                self._entries[key] = entry
                return deepcopy(entry[2])
            result = query()
            # Never cache a snapshot straddling another connection's commit.
            if self._revision() == revision:
                self._entries[key] = (revision, time.monotonic(), deepcopy(result))
                while len(self._entries) > self.limit:
                    self._entries.popitem(last=False)
            return result


def cached_history_read(function):
    @wraps(function)
    def read(self, *args, **kwargs):
        self.sync()
        key = (function.__name__, args, tuple(sorted(kwargs.items())))
        return self._query_cache.read(key, lambda: function(self, *args, **kwargs))
    return read
