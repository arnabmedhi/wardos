"""Many small record writes at once, used to copy a whole demo ward quickly."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

BATCH = 400


def _firestore(store):
    return store if hasattr(store, "client") and hasattr(store, "_col") else None


def bulk_put(store, pairs: list[tuple[str, dict]]) -> None:
    fire = _firestore(store)
    if fire is None:
        def write(tx):
            for collection, doc in pairs:
                tx.put(collection, doc)
        store.transact(write)
        return

    def send(part):
        batch = fire.client.batch()
        for collection, doc in part:
            batch.set(fire._col(collection).document(doc["id"]), doc)
        batch.commit()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(send, [pairs[i:i + BATCH] for i in range(0, len(pairs), BATCH)]))


def bulk_delete(store, pairs: list[tuple[str, str]]) -> None:
    fire = _firestore(store)
    if fire is None:
        conn = store._connect()
        try:
            conn.executemany("DELETE FROM docs WHERE coll = ? AND id = ?", pairs)
        finally:
            conn.close()
        return

    def send(part):
        batch = fire.client.batch()
        for collection, doc_id in part:
            batch.delete(fire._col(collection).document(doc_id))
        batch.commit()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(send, [pairs[i:i + BATCH] for i in range(0, len(pairs), BATCH)]))
