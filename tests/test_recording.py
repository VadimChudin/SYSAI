import datetime as dt
import json
import pathlib
import shutil
import subprocess

import pytest
from fastapi import HTTPException

from app import config, recording
from app.db import Meeting, Recording, RecordingChunk, SessionLocal


@pytest.fixture(scope="module")
def webm_audio(tmp_path_factory):
    path = tmp_path_factory.mktemp("recording-audio") / "sample.webm"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-c:a", "libopus", "-y", str(path)], check=True)
    return path.read_bytes()


@pytest.fixture(scope="module")
def live_webm_audio(tmp_path_factory):
    path = tmp_path_factory.mktemp("live-recording-audio") / "live.webm"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=7",
                    "-c:a", "libopus", "-f", "webm", "-live", "1", "-y", str(path)], check=True)
    result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json",
                             str(path)], capture_output=True, text=True, check=True)
    assert "duration" not in json.loads(result.stdout)["format"]
    return path.read_bytes()


@pytest.fixture(autouse=True)
def recording_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)


def start(owner="session-1", **kwargs):
    return recording.start(owner, "audio/webm;codecs=opus", "Meeting", dt.date(2026, 10, 7), {"flag": True})


def test_chunks_stitch_real_audio_and_finish_idempotently(webm_audio, monkeypatch):
    submitted = []
    monkeypatch.setattr(recording.pipeline, "submit", submitted.append)
    rec = start()
    split = len(webm_audio) // 3
    chunks = [webm_audio[:split], webm_audio[split:2 * split], webm_audio[2 * split:]]
    for sequence, chunk in enumerate(chunks):
        result = recording.append("session-1", rec["id"], sequence, chunk)
        assert result["next_sequence"] == sequence + 1
    assert recording.append("session-1", rec["id"], 0, chunks[0]) == {
        "next_sequence": 3, "size_bytes": len(webm_audio)}

    result = recording.finish("session-1", rec["id"])
    assert result["url"] == f"/meetings/{result['meeting_id']}"
    assert recording.finish("session-1", rec["id"]) == result
    assert submitted == [result["meeting_id"]]
    with SessionLocal() as session:
        saved = session.get(Recording, rec["id"])
        meeting = session.get(Meeting, result["meeting_id"])
        assert saved.status == "finished" and saved.path
        assert meeting.audio_path == saved.path and meeting.source == "microphone"
        assert meeting.title == "Meeting" and meeting.meeting_date == dt.date(2026, 10, 7)
        assert meeting.options == {"flag": True}
    audio_path, mime = recording.download("session-1", rec["id"])
    assert mime == "audio/webm"
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json",
                            str(audio_path)], capture_output=True, text=True, check=True)
    assert float(json.loads(probe.stdout)["format"]["duration"]) > 0.9
    assert not (config.DATA_DIR / "recordings" / rec["id"]).exists()


def test_finish_remuxes_live_webm_with_missing_duration(live_webm_audio, monkeypatch):
    submitted = []
    monkeypatch.setattr(recording.pipeline, "submit", submitted.append)
    rec = start()
    chunks = [live_webm_audio[index:index + 4096] for index in range(0, len(live_webm_audio), 4096)]
    for sequence, chunk in enumerate(chunks):
        recording.append("session-1", rec["id"], sequence, chunk)

    result = recording.finish("session-1", rec["id"])
    assert submitted == [result["meeting_id"]]
    with SessionLocal() as session:
        meeting = session.get(Meeting, result["meeting_id"])
        assert meeting.audio_path and pathlib.Path(meeting.audio_path).is_file()
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json",
                            meeting.audio_path], capture_output=True, text=True, check=True)
    assert float(json.loads(probe.stdout)["format"]["duration"]) > 6
    assert not (config.DATA_DIR / "recordings" / rec["id"]).exists()


def test_owner_isolation_and_order_and_changed_duplicate_conflict(webm_audio):
    rec = start()
    with pytest.raises(HTTPException) as exc:
        recording.append("other-session", rec["id"], 0, webm_audio)
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        recording.append("session-1", rec["id"], 1, webm_audio)
    assert exc.value.status_code == 409
    recording.append("session-1", rec["id"], 0, webm_audio)
    with pytest.raises(HTTPException) as exc:
        recording.append("session-1", rec["id"], 0, webm_audio + b"changed")
    assert exc.value.status_code == 409


def test_chunk_and_total_upload_limits(webm_audio, monkeypatch):
    rec = start()
    with pytest.raises(HTTPException) as exc:
        recording.append("session-1", rec["id"], 0, b"x" * (8 * 1024 * 1024 + 1))
    assert exc.value.status_code == 413

    monkeypatch.setattr(config, "MAX_UPLOAD_MB", 1)
    rec = start()
    chunk = b"x" * (1024 * 1024)
    recording.append("session-1", rec["id"], 0, chunk)
    with pytest.raises(HTTPException) as exc:
        recording.append("session-1", rec["id"], 1, b"x")
    assert exc.value.status_code == 413


def test_cancel_removes_parts_and_rejects_later_chunks(webm_audio):
    rec = start()
    recording.append("session-1", rec["id"], 0, webm_audio)
    part_dir = config.DATA_DIR / "recordings" / rec["id"]
    assert part_dir.exists()
    assert recording.cancel("session-1", rec["id"]) == {"id": rec["id"], "status": "cancelled"}
    assert not part_dir.exists()
    with pytest.raises(HTTPException) as exc:
        recording.append("session-1", rec["id"], 1, webm_audio)
    assert exc.value.status_code == 409


def test_finished_recording_audio_is_hidden_after_meeting_deletion(webm_audio, monkeypatch):
    monkeypatch.setattr(recording.pipeline, "submit", lambda _: None)
    rec = start()
    recording.append("session-1", rec["id"], 0, webm_audio)
    result = recording.finish("session-1", rec["id"])
    with SessionLocal() as session:
        session.delete(session.get(Meeting, result["meeting_id"]))
        session.commit()

    with pytest.raises(HTTPException) as exc:
        recording.download("session-1", rec["id"])
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        recording.finish("session-1", rec["id"])
    assert exc.value.status_code == 404


def test_invalid_media_remains_recoverable_for_retry(webm_audio, monkeypatch):
    rec = start()
    recording.append("session-1", rec["id"], 0, b"not audio")
    with pytest.raises(HTTPException) as exc:
        recording.finish("session-1", rec["id"])
    assert exc.value.status_code == 409
    with SessionLocal() as session:
        saved = session.get(Recording, rec["id"])
        assert saved.status == "error" and saved.meeting_id is None
        assert session.query(RecordingChunk).filter_by(recording_id=rec["id"]).count() == 1
    audio_path, _ = recording.download("session-1", rec["id"])
    assert audio_path.read_bytes() == b"not audio"

    monkeypatch.setattr(recording, "_probe_audio", lambda _: None)
    monkeypatch.setattr(recording, "_remux_audio", lambda source, destination: shutil.copyfile(source, destination))
    monkeypatch.setattr(recording.pipeline, "submit", lambda _: None)
    assert recording.finish("session-1", rec["id"])["meeting_id"]


def test_audio_is_assembled_from_chunks_after_database_reread(webm_audio):
    rec = start()
    recording.append("session-1", rec["id"], 0, webm_audio[:100])
    recording.append("session-1", rec["id"], 1, webm_audio[100:])
    with SessionLocal() as session:
        reread = session.get(Recording, rec["id"])
        assert reread.next_sequence == 2 and reread.size_bytes == len(webm_audio)
    path, _ = recording.download("session-1", rec["id"])
    assert path.read_bytes() == webm_audio
