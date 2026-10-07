"""Validated processing results, stored independently of the running worker."""
import hashlib
import json

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
            return row.data if row else None

    def put(self, stage, index, fingerprint, data):
        with SessionLocal() as s:
            row = s.query(ProcessingCheckpoint).filter_by(
                meeting_id=self.meeting_id, stage=stage, part=str(index), fingerprint=fingerprint,
            ).first()
            if row:
                row.data = data
            else:
                s.add(ProcessingCheckpoint(meeting_id=self.meeting_id, stage=stage, part=str(index),
                                           fingerprint=fingerprint, data=data))
            s.commit()
