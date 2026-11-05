"""Where the record's small documents live: a SQLite file locally, Firestore in the cloud (same behaviour)."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Protocol


class Tx(Protocol):
    def get(self, collection: str, doc_id: str) -> dict | None: ...
    def query(self, collection: str, **equals: Any) -> list[dict]: ...
    def put(self, collection: str, doc: dict) -> None: ...


class DocStore(Protocol):
    def get(self, collection: str, doc_id: str) -> dict | None: ...
    def query(self, collection: str, **equals: Any) -> list[dict]: ...
    def put(self, collection: str, doc: dict) -> None: ...
    def transact(self, fn: Callable[[Tx], Any]) -> Any: ...


class _SQLiteTx:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def get(self, collection, doc_id):
        row = self._conn.execute("SELECT data FROM docs WHERE coll = ? AND id = ?", (collection, doc_id)).fetchone()
        return json.loads(row[0]) if row else None

    def query(self, collection, **equals):
        sql, args = "SELECT data FROM docs WHERE coll = ?", [collection]
        for field, value in equals.items():
            if value is None:
                sql += f" AND json_extract(data, '$.{field}') IS NULL"
            else:
                sql += f" AND json_extract(data, '$.{field}') = ?"
                args.append(int(value) if isinstance(value, bool) else value)
        sql += " ORDER BY rowid"
        return [json.loads(r[0]) for r in self._conn.execute(sql, args).fetchall()]

    def put(self, collection, doc):
        if not doc.get("id"):
            raise ValueError("document has no id")
        self._conn.execute("INSERT INTO docs (coll, id, data) VALUES (?, ?, ?) "
                           "ON CONFLICT(coll, id) DO UPDATE SET data = excluded.data",
                           (collection, doc["id"], json.dumps(doc, ensure_ascii=False, sort_keys=True)))


class SQLiteDocStore:

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS docs (coll TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, "
                         "PRIMARY KEY (coll, id))")
            conn.execute("CREATE INDEX IF NOT EXISTS docs_admission ON docs "
                         "(coll, json_extract(data, '$.admission_id'))")
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=60, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=60000")
        return conn

    def get(self, collection, doc_id):
        conn = self._connect()
        try:
            return _SQLiteTx(conn).get(collection, doc_id)
        finally:
            conn.close()

    def query(self, collection, **equals):
        conn = self._connect()
        try:
            return _SQLiteTx(conn).query(collection, **equals)
        finally:
            conn.close()

    def put(self, collection, doc):
        self.transact(lambda tx: tx.put(collection, doc))

    def transact(self, fn):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = fn(_SQLiteTx(conn))
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            return result
        finally:
            conn.close()


class _FirestoreTx:

    def __init__(self, store: "FirestoreDocStore", transaction):
        self._store, self._t = store, transaction
        self.pending: dict[tuple[str, str], dict] = {}

    def get(self, collection, doc_id):
        if (collection, doc_id) in self.pending:
            return json.loads(json.dumps(self.pending[(collection, doc_id)]))
        snap = self._store._col(collection).document(doc_id).get(transaction=self._t)
        return snap.to_dict() if snap.exists else None

    def query(self, collection, **equals):
        found = {d["id"]: d for d in self._store._query(collection, equals, transaction=self._t)}
        for (coll, doc_id), doc in self.pending.items():
            if coll == collection:
                if all(doc.get(k) == v for k, v in equals.items()):
                    found[doc_id] = doc
                else:
                    found.pop(doc_id, None)
        return sorted(found.values(), key=_order)

    def put(self, collection, doc):
        if not doc.get("id"):
            raise ValueError("document has no id")
        self.pending[(collection, doc["id"])] = json.loads(json.dumps(doc, ensure_ascii=False))


def _order(doc: dict):
    return (str(doc.get("created_at") or doc.get("received_at") or ""), str(doc.get("id")))


class FirestoreDocStore:

    def __init__(self, *, project: str, prefix: str = "wardos_", database: str = "(default)", credentials=None):
        from google.cloud import firestore
        self._firestore = firestore
        self.client = firestore.Client(project=project, database=database, credentials=credentials)
        self.prefix = prefix

    def _col(self, collection: str):
        return self.client.collection(self.prefix + collection)

    def _query(self, collection, equals, transaction=None):
        from google.cloud.firestore_v1.base_query import FieldFilter
        q = self._col(collection)
        for field, value in equals.items():
            q = q.where(filter=FieldFilter(field, "==", value))
        return [s.to_dict() for s in (q.get(transaction=transaction) if transaction else q.stream())]

    def get(self, collection, doc_id):
        snap = self._col(collection).document(doc_id).get()
        return snap.to_dict() if snap.exists else None

    def query(self, collection, **equals):
        return sorted(self._query(collection, equals), key=_order)

    def put(self, collection, doc):
        self.transact(lambda tx: tx.put(collection, doc))

    def transact(self, fn):
        @self._firestore.transactional
        def run(transaction):
            tx = _FirestoreTx(self, transaction)
            result = fn(tx)
            for (collection, doc_id), doc in tx.pending.items():
                transaction.set(self._col(collection).document(doc_id), doc)
            return result
        return run(self.client.transaction())

    def delete_all(self, collections) -> int:
        n = 0
        for collection in collections:
            for snap in self._col(collection).stream():
                snap.reference.delete()
                n += 1
        return n


class CachedDocStore:

    def __init__(self, inner, ttl: float = 10.0, stale: float = 300.0):
        import threading
        self.inner, self.ttl, self.stale = inner, ttl, stale
        self._data: dict[tuple, tuple[float, Any]] = {}
        self._loading: set[tuple] = set()
        self._gen = 0
        self._lock = threading.Lock()

    def _store(self, key: tuple, gen: int, at: float, value: Any) -> None:
        with self._lock:
            if gen == self._gen:
                self._data[key] = (at, value)

    def _refresh(self, key: tuple, load: Callable[[], Any], gen: int) -> None:
        import time
        try:
            at = time.monotonic()
            self._store(key, gen, at, load())
        except Exception:
            pass
        finally:
            with self._lock:
                self._loading.discard(key)

    def _read(self, key: tuple, load: Callable[[], Any]) -> Any:
        import copy
        import threading
        import time
        now = time.monotonic()
        with self._lock:
            hit, gen = self._data.get(key), self._gen
            age = now - hit[0] if hit else None
            background = hit is not None and self.ttl <= age < self.stale and key not in self._loading
            if background:
                self._loading.add(key)
        if hit and age < self.ttl:
            return copy.deepcopy(hit[1])
        if background:
            threading.Thread(target=self._refresh, args=(key, load, gen), daemon=True).start()
            return copy.deepcopy(hit[1])
        if hit and key in self._loading and age < self.stale:
            return copy.deepcopy(hit[1])
        value = load()
        self._store(key, gen, now, value)
        return copy.deepcopy(value)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._gen += 1

    def get(self, collection, doc_id):
        return self._read(("get", collection, doc_id), lambda: self.inner.get(collection, doc_id))

    def query(self, collection, **equals):
        key = ("query", collection, tuple(sorted((k, json.dumps(v, sort_keys=True)) for k, v in equals.items())))
        return self._read(key, lambda: self.inner.query(collection, **equals))

    def put(self, collection, doc):
        self._invalidate({collection})
        try:
            return self.inner.put(collection, doc)
        finally:
            self._invalidate({collection})

    def transact(self, fn):
        touched = set()
        def track(tx):
            class Writes:
                get = tx.get
                query = tx.query

                def put(self, collection, doc):
                    touched.add(collection)
                    tx.put(collection, doc)
            result = fn(Writes())
            self._invalidate(touched)
            return result
        try:
            return self.inner.transact(track)
        finally:
            self._invalidate(touched)

    def _invalidate(self, collections):
        with self._lock:
            self._data = {k: v for k, v in self._data.items() if k[1] not in collections}
            self._gen += 1

    def __getattr__(self, name):
        attr = getattr(self.inner, name)
        if not callable(attr):
            return attr

        def call(*args, **kwargs):
            try:
                return attr(*args, **kwargs)
            finally:
                self.clear()
        return call
