import datetime as dt

from app import checkpoints, config, llm, pipeline, settings_store
from app.db import Meeting, ProcessingCheckpoint, SessionLocal, Task


def test_real_audio_without_key_never_becomes_demo(tmp_path, client):
    path = tmp_path / "real.wav"
    path.write_bytes(b"actual upload")
    mid = pipeline.create_meeting("real.wav", str(path))
    pipeline.process(mid)
    with SessionLocal() as s:
        m = s.get(Meeting, mid)
        assert m.status == "error" and "ключ OpenRouter" in m.error
        assert m.transcript is None and m.report is None and not m.tasks
        assert path.exists()
    page = client.get(f"/meetings/{mid}")
    assert "Продолжить обработку" in page.text
    assert client.post(f"/meetings/{mid}/retry-processing").status_code == 200
    with SessionLocal() as s:
        assert s.get(Meeting, mid).status == "error"


def test_demo_with_key_does_not_call_external_models(monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "fake")
    monkeypatch.setattr(pipeline.analyze, "analyze", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("live call")))
    settings_store.set_many({"approver_chat_ids": []})
    mid = pipeline.create_meeting("demo", source="demo")
    pipeline.process(mid)
    with SessionLocal() as s:
        m = s.get(Meeting, mid)
        assert m.status == "awaiting_approval" and len(m.tasks) == 3


def test_microphone_simulation_remains_explicit_demo(client):
    client.post("/demo", data={"source": "microphone", "approval_required": "on"})
    with SessionLocal() as s:
        m = s.query(Meeting).one()
        assert m.source == "demo" and m.status == "awaiting_approval"
        assert "имитация" in m.title and len(m.tasks) == 3


def test_resume_analysis_reuses_transcript_when_source_file_was_deleted(monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "fake")
    called = []
    segments = [{"speaker": "Иван", "start": 0, "end": 61, "text": "Подготовить отчёт"}]
    with SessionLocal() as s:
        m = Meeting(source="web", filename="real.wav", audio_path="/missing.wav", status="error",
                    transcript=segments, duration_sec=61, meeting_date=dt.date(2026, 10, 5))
        s.add(m)
        s.commit()
        mid = m.id

    def analyze(saved, employees, settings, date, progress, checkpoint):
        assert saved == segments and date == dt.date(2026, 10, 5)
        assert isinstance(checkpoint, checkpoints.Store)
        called.append(saved)
        if len(called) == 1:
            checkpoint.put("analysis", 0, "a" * 64, {"summary": "saved"})
            raise llm.LLMError("Модель временно недоступна")
        assert checkpoint.get("analysis", 0, "a" * 64) == {"summary": "saved"}
        return {"title": "Отчёт", "summary": "Итог", "tasks": []}

    monkeypatch.setattr(pipeline.analyze, "analyze", analyze)
    monkeypatch.setattr(pipeline.transcribe, "transcribe", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("STT rerun")))
    assert pipeline.retry_processing(mid)
    with SessionLocal() as s:
        assert s.get(Meeting, mid).status == "error"
        assert s.query(ProcessingCheckpoint).filter_by(meeting_id=mid).count() == 1
        assert s.query(Task).filter_by(meeting_id=mid).count() == 0
    assert pipeline.retry_processing(mid)
    with SessionLocal() as s:
        assert s.get(Meeting, mid).status == "awaiting_approval"
    assert len(called) == 2
    assert pipeline.retry_processing(mid) is False


def test_checkpoint_store_persists_and_is_bound_to_meeting_and_parameters():
    first = pipeline.create_meeting("a", source="demo")
    second = pipeline.create_meeting("b", source="demo")
    store = checkpoints.Store(first)
    key = checkpoints.fingerprint({"model": "A", "audio": "hash"})
    store.put("transcribe", "0L", key, [{"text": "validated"}])
    assert checkpoints.Store(first).get("transcribe", "0L", key) == [{"text": "validated"}]
    assert checkpoints.Store(second).get("transcribe", "0L", key) is None
    assert store.get("transcribe", "0L", checkpoints.fingerprint({"model": "B", "audio": "hash"})) is None
    store.put("transcribe", "0L", key, [{"text": "updated"}])
    with SessionLocal() as s:
        assert s.query(ProcessingCheckpoint).count() == 1
