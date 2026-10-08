"""Validated processing results, stored independently of the running worker."""
import hashlib
import json

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .db import ProcessingCheckpoint, SessionLocal


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False).encode()).hexdigest()


class Store:
    def __init__(self, meeting_id):
        self.meeting_id = meeting_id

    def get(self, stage, index, fingerprint):
        with SessionLocal() as s:
            row = s.query(ProcessingCheckpoint).filter_by(
                meeting_id=self.meeting_id, stage=stage, part=str(index), fingerprint=fingerprint,
            ).first()
            # Detached, independent JSON; callers cannot mutate persisted state by accident.
            return json.loads(json.dumps(row.data, allow_nan=False)) if row else None

    def put(self, stage, index, fingerprint, data):
        # Reject non-JSON/NaN before starting a transaction, consistently on both DBs.
        data = json.loads(json.dumps(data, ensure_ascii=False, allow_nan=False))
        values = dict(meeting_id=self.meeting_id, stage=stage, part=str(index),
                      fingerprint=fingerprint, data=data)
        with SessionLocal() as s:
            dialect = s.get_bind().dialect.name
            if dialect in ("postgresql", "sqlite"):
                insert = pg_insert if dialect == "postgresql" else sqlite_insert
                stmt = insert(ProcessingCheckpoint).values(**values)
                # Concurrent retries for the same key must not abort the worker.
                stmt = stmt.on_conflict_do_update(
                    index_elements=["meeting_id", "stage", "part", "fingerprint"],
                    set_={"data": stmt.excluded.data},
                )
                s.execute(stmt)
            else:
                row = s.query(ProcessingCheckpoint).filter_by(
                    **{k: v for k, v in values.items() if k != "data"}).first()
                if row:
                    row.data = data
                else:
                    s.add(ProcessingCheckpoint(**values))
            s.commit()
