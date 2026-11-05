"""The short read cache in front of Firestore: fast repeat reads, never stale after this process writes."""
from __future__ import annotations

from wardos.chain.docstore import CachedDocStore, SQLiteDocStore


class Counting:
    def __init__(self, inner):
        self.inner, self.reads = inner, 0

    def get(self, c, i):
        self.reads += 1
        return self.inner.get(c, i)

    def query(self, c, **eq):
        self.reads += 1
        return self.inner.query(c, **eq)

    def put(self, c, d):
        return self.inner.put(c, d)

    def transact(self, fn):
        return self.inner.transact(fn)


def test_repeat_reads_are_served_from_memory_and_writes_are_seen_at_once(tmp_path):
    raw = Counting(SQLiteDocStore(tmp_path / "r.sqlite3"))
    store = CachedDocStore(raw, ttl=60)
    store.put("facts", {"id": "f1", "admission_id": "a", "v": 1})
    assert store.query("facts", admission_id="a")[0]["v"] == 1
    assert store.query("facts", admission_id="a")[0]["v"] == 1
    assert raw.reads == 1
    store.query("facts", admission_id="a")[0]["v"] = 99
    assert store.query("facts", admission_id="a")[0]["v"] == 1
    store.transact(lambda tx: tx.put("facts", {"id": "f1", "admission_id": "a", "v": 2}))
    assert store.query("facts", admission_id="a")[0]["v"] == 2
    assert store.get("facts", "f1")["v"] == 2


def test_reads_expire_so_other_processes_writes_show_up(tmp_path):
    inner = SQLiteDocStore(tmp_path / "r.sqlite3")
    store = CachedDocStore(inner, ttl=0.0, stale=0.0)
    inner.put("facts", {"id": "f1", "admission_id": "a", "v": 1})
    assert store.get("facts", "f1")["v"] == 1
    inner.put("facts", {"id": "f1", "admission_id": "a", "v": 3})
    assert store.get("facts", "f1")["v"] == 3


def test_an_older_copy_is_answered_at_once_and_refreshed_behind(tmp_path):
    import time
    inner = SQLiteDocStore(tmp_path / "r.sqlite3")
    store = CachedDocStore(inner, ttl=0.05, stale=60)
    inner.put("facts", {"id": "f1", "admission_id": "a", "v": 1})
    assert store.get("facts", "f1")["v"] == 1
    inner.put("facts", {"id": "f1", "admission_id": "a", "v": 4})
    time.sleep(0.1)
    assert store.get("facts", "f1")["v"] == 1
    for _ in range(50):
        if store.get("facts", "f1")["v"] == 4:
            break
        time.sleep(0.02)
    assert store.get("facts", "f1")["v"] == 4
