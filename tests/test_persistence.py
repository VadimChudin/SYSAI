"""Persistence/restart contracts, runnable on SQLite or SYSAI_TEST_DATABASE_URL."""
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import checkpoints, config, db, pipeline
from app.db import Delivery, Meeting, ProcessingCheckpoint, SessionLocal, Task


def meeting(**kwargs):
    with SessionLocal() as s:
        m = Meeting(source="web", filename="audio.wav", options={"approval_required": True}, **kwargs)
        s.add(m)
        s.commit()
        return m.id


def never(*args, **kwargs):
    raise AssertionError("Unexpected replay of processing or delivery")


def test_checkpoint_survives_new_process(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'restart.db'}"
    engine = create_engine(url)
    db.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(checkpoints, "SessionLocal", factory)
    with factory() as s:
        m = Meeting(filename="restart.wav")
        s.add(m)
        s.commit()
        mid = m.id
    checkpoints.Store(mid).put("transcription", 0, "a" * 64, {"segments": [{"text": "saved"}]})
    engine.dispose()
    code = """
import json, sys
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app import checkpoints
engine = create_engine(sys.argv[1])
checkpoints.SessionLocal = sessionmaker(bind=engine)
print(json.dumps(checkpoints.Store(int(sys.argv[2])).get('transcription', 0, 'a' * 64)))
"""
    result = subprocess.run([sys.executable, "-c", code, url, str(mid)],
                            capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {"segments": [{"text": "saved"}]}


def test_checkpoint_upsert_and_fingerprint_isolation():
    mid = meeting(status="transcribing")
    store = checkpoints.Store(mid)
    store.put("transcription", 0, "a" * 64, {"text": "first"})
    store.put("transcription", 0, "a" * 64, {"text": "second"})
    assert checkpoints.Store(mid).get("transcription", 0, "a" * 64) == {"text": "second"}
    assert store.get("transcription", 0, "b" * 64) is None
    with SessionLocal() as s:
        assert s.query(ProcessingCheckpoint).count() == 1
    with pytest.raises(ValueError):
        store.put("transcription", 1, "b" * 64, {"duration": math.nan})
    with SessionLocal() as s:
        assert s.query(ProcessingCheckpoint).count() == 1


def test_restart_after_report_commit_keeps_task_ids(monkeypatch):
    mid = meeting(status="analyzing", audio_path="/missing-after-restart.wav",
                  report={"title": "saved", "summary": "saved", "tasks": []})
    with SessionLocal() as s:
        task = Task(meeting_id=mid, title="Keep this task")
        s.add(task)
        s.commit()
        tid = task.id
    monkeypatch.setattr(pipeline.analyze, "analyze", never)
    monkeypatch.setattr(pipeline.transcribe, "transcribe", never)
    db.engine.dispose()  # Forget live connections, not committed state.
    pipeline.resume_jobs()
    pipeline.resume_jobs()
    with SessionLocal() as s:
        m = s.get(Meeting, mid)
        assert m.status == "awaiting_approval"
        assert m.audio_path == ""
        assert [t.id for t in m.tasks] == [tid]
        assert m.options["approval_revision"] == 1


def test_restart_uses_saved_transcript_without_audio(monkeypatch):
    saved = [{"start": 0, "end": 2, "speaker": "A", "text": "saved"}]
    mid = meeting(status="analyzing", audio_path="/missing.wav", transcript=saved, duration_sec=2)
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(pipeline.transcribe, "transcribe", never)
    def analyze(segments, *args, **kwargs):
        assert segments == saved
        return {"title": "saved", "summary": "saved", "tasks": []}
    monkeypatch.setattr(pipeline.analyze, "analyze", analyze)
    pipeline.resume_jobs()
    with SessionLocal() as s:
        assert s.get(Meeting, mid).status == "awaiting_approval"


def test_missing_ephemeral_audio_fails_honestly(monkeypatch):
    mid = meeting(status="transcribing", audio_path="/missing.wav")
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(pipeline.transcribe, "transcribe", never)
    pipeline.resume_jobs()
    with SessionLocal() as s:
        m = s.get(Meeting, mid)
        assert m.status == "error"
        assert "аудио не найдено" in m.error
        assert m.transcript is None
        assert m.report is None


@pytest.mark.parametrize("status", ["done", "ready", "awaiting_approval", "delivery_failed", "rejected"])
def test_late_processing_cannot_reopen_terminal_or_review_state(status, monkeypatch):
    mid = meeting(status=status, report={"summary": "saved"})
    monkeypatch.setattr(pipeline.analyze, "analyze", never)
    pipeline.process(mid)
    with SessionLocal() as s:
        assert s.get(Meeting, mid).status == status


@pytest.mark.parametrize("status", ["sending", "analyzing", "queued"])
def test_restart_does_not_repeat_ambiguous_or_sent_final(status, monkeypatch):
    mid = meeting(status=status, report={"summary": "saved"})
    with SessionLocal() as s:
        s.add_all([Delivery(meeting_id=mid, key="sent", phase="final", kind="summary",
                            chat_id="123", payload={"text": "sent"}, status="sent"),
                   Delivery(meeting_id=mid, key="ambiguous", phase="final", kind="summary",
                            chat_id="456", payload={"text": "maybe sent"}, status="sending")])
        s.commit()
    monkeypatch.setattr(pipeline.telegram, "call", never)
    # Existing rows must not be re-planned; recovery should only flush safe pending rows.
    monkeypatch.setattr(pipeline, "_plan_delivery", never)
    pipeline.resume_jobs()
    pipeline.resume_jobs()
    with SessionLocal() as s:
        assert [d.status for d in s.query(Delivery).order_by(Delivery.id)] == ["sent", "uncertain"]
        assert s.get(Meeting, mid).status == "delivery_failed"


def test_legacy_draft_outbox_does_not_create_new_revision(monkeypatch):
    mid = meeting(status="awaiting_approval", report={"summary": "saved"})
    with SessionLocal() as s:
        s.add(Delivery(meeting_id=mid, key="draft", phase="draft", kind="summary", chat_id="123",
                       payload={"text": "already sent"}, status="sent"))
        s.commit()
    monkeypatch.setattr(pipeline, "request_approval", never)
    pipeline.resume_jobs()
    with SessionLocal() as s:
        assert s.query(Delivery).count() == 1
        assert s.get(Meeting, mid).status == "awaiting_approval"


def test_render_blueprint_connects_database_and_persistent_audio():
    import yaml
    blueprint = yaml.safe_load((Path(__file__).resolve().parents[1] / "render-persistent.yaml").read_text())
    service = blueprint["services"][0]
    env = {item["key"]: item for item in service["envVars"]}
    assert service["plan"] != "free"
    assert service["disk"]["mountPath"] == env["DATA_DIR"]["value"]
    database = blueprint["databases"][0]
    assert database["plan"] != "free"
    assert env["DATABASE_URL"]["fromDatabase"] == {
        "name": database["name"], "property": "connectionString"}


def test_concurrent_checkpoint_writes_are_one_durable_row():
    from concurrent.futures import ThreadPoolExecutor
    mid = meeting(status="transcribing")
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda n: checkpoints.Store(mid).put(
            "transcription", 0, "a" * 64, {"text": str(n)}), range(12)))
    with SessionLocal() as s:
        assert s.query(ProcessingCheckpoint).filter_by(meeting_id=mid).count() == 1
    assert checkpoints.Store(mid).get("transcription", 0, "a" * 64)["text"] in map(str, range(12))


def test_processing_failure_keeps_original_for_resume(tmp_path, monkeypatch):
    audio = tmp_path / "original.wav"
    audio.write_bytes(b"audio")
    mid = meeting(status="queued", audio_path=str(audio))
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "test-key")
    def fail(*args, **kwargs):
        raise RuntimeError("temporary STT failure")
    monkeypatch.setattr(pipeline.transcribe, "transcribe", fail)
    pipeline.resume_jobs()
    assert audio.read_bytes() == b"audio"
    with SessionLocal() as s:
        m = s.get(Meeting, mid)
        assert m.status == "error"
        assert m.audio_path == str(audio)


def test_missing_meeting_submission_is_noop():
    pipeline.process(999999)
