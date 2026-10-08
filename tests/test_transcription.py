import json
import pathlib
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from app import audio, checkpoints, config, llm, transcribe
from app.db import Meeting, ProcessingCheckpoint, SessionLocal


def make_audio(path, seconds):
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
         "sine=frequency=440:sample_rate=16000", "-t", str(seconds),
         "-c:a", "pcm_s16le", str(path)],
        check=True,
        capture_output=True,
    )
    return path


def make_long_mp3(path, seconds):
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
         "sine=frequency=440:sample_rate=16000", "-t", str(seconds),
         "-ac", "1", "-ar", "16000", "-b:a", "32k", str(path)],
        check=True,
        capture_output=True,
    )
    return path


def settings(**overrides):
    return {"transcribe_model": "test/model", **overrides}


@pytest.mark.parametrize("duration,tail", [(122.4, 2.4), (120.35, 0.35)])
def test_prepare_chunks_uses_eight_second_overlap_offsets_and_keeps_short_tail(tmp_path, duration, tail):
    source = make_audio(tmp_path / "source.wav", duration)

    total, chunks = audio.prepare_chunks(str(source), chunk_minutes=1, overlap_seconds=8)

    assert total == pytest.approx(duration, abs=0.02)
    assert [offset for offset, _ in chunks] == [0, 60, 120]
    assert audio.duration(str(chunks[0][1])) == pytest.approx(68, abs=0.25)
    assert audio.duration(str(chunks[-1][1])) == pytest.approx(tail, abs=0.2)
    shutil.rmtree(chunks[0][1].parent)


@pytest.mark.parametrize("error", [False, True])
def test_transcribe_cleans_chunk_directory_after_success_and_error(tmp_path, monkeypatch, error):
    source = make_audio(tmp_path / "source.wav", 4)
    workdirs = []
    real_mkdtemp = audio.tempfile.mkdtemp

    def tracked_mkdtemp(*args, **kwargs):
        result = real_mkdtemp(*args, **kwargs)
        workdirs.append(result)
        return result

    monkeypatch.setattr(audio.tempfile, "mkdtemp", tracked_mkdtemp)
    monkeypatch.setattr(config, "llm_enabled", lambda: True)

    def chat(*args, **kwargs):
        if error:
            raise llm.LLMError("mock failure")
        return json.dumps({"segments": [{"speaker": "A", "start": 1, "text": "hello"}]})

    monkeypatch.setattr(llm, "chat", chat)
    if error:
        with pytest.raises(llm.LLMError, match="mock failure"):
            transcribe.transcribe(str(source), settings())
    else:
        _, segments = transcribe.transcribe(str(source), settings())
        assert segments[0]["text"] == "hello"

    assert len(workdirs) == 1
    assert not pathlib.Path(workdirs[0]).exists()


def test_missing_api_key_rejects_audio_without_using_demo(tmp_path, monkeypatch):
    source = make_audio(tmp_path / "source.wav", 2)
    monkeypatch.setattr(config, "llm_enabled", lambda: False)
    monkeypatch.setattr(audio, "prepare_chunks", lambda *a, **kw: pytest.fail("audio was processed"))

    with pytest.raises(llm.LLMError, match="нужен ключ OpenRouter"):
        transcribe.transcribe(str(source), settings())


@pytest.mark.parametrize("data", [
    {},
    {"segments": {}},
    {"segments": [{"start": 1, "text": "hi"}]},
    {"segments": [{"speaker": "A", "start": float("nan"), "text": "hi"}]},
    {"segments": [{"speaker": "A", "start": -0.1, "text": "hi"}]},
    {"segments": [{"speaker": "A", "start": 10.1, "text": "hi"}]},
    {"segments": [{"speaker": "A", "start": 1, "text": 3}]},
    {"segments": [{"speaker": "", "start": 1, "text": "hi"}]},
])
def test_validation_rejects_malformed_schema_and_timestamps(data):
    with pytest.raises(llm.IncompleteResponse):
        transcribe._validate(data, offset=5, length=10)


def test_incomplete_response_splits_real_audio_and_returns_absolute_times(tmp_path, monkeypatch):
    source = make_audio(tmp_path / "source.wav", 45)
    monkeypatch.setattr(config, "llm_enabled", lambda: True)
    requests = []

    def chat(*args, **kwargs):
        requests.append(args[1])
        if len(requests) == 1:
            raise llm.IncompleteResponse("too long")
        segment = {"speaker": "A", "start": 5 if len(requests) == 2 else 3,
                   "text": "left" if len(requests) == 2 else "right"}
        return json.dumps({"segments": [segment]})

    monkeypatch.setattr(llm, "chat", chat)
    total, segments = transcribe.transcribe(str(source), settings())

    assert total == pytest.approx(45, abs=0.02)
    assert len(requests) == 3
    assert [segment["start"] for segment in segments] == [5, 25.5]
    assert [segment["text"] for segment in segments] == ["left", "right"]


def test_31_second_audio_splits_below_30_and_recovers_on_grandchildren(tmp_path, monkeypatch):
    source = make_audio(tmp_path / "source.wav", 50)
    monkeypatch.setattr(config, "llm_enabled", lambda: True)
    requests = []
    split_durations = []
    real_extract = audio.extract

    def tracked_extract(source_path, destination, start, seconds):
        split_durations.append(seconds)
        return real_extract(source_path, destination, start, seconds)

    monkeypatch.setattr(audio, "extract", tracked_extract)

    def chat(*args, **kwargs):
        call = len(requests)
        requests.append(args[1])
        if call < 2:
            raise llm.IncompleteResponse("split this part")
        return json.dumps({"segments": [{"speaker": "A", "start": 1,
                                         "text": f"recovered {call}"}]})

    monkeypatch.setattr(llm, "chat", chat)
    total, segments = transcribe.transcribe(str(source), settings())

    assert len(requests) == 5
    assert split_durations[1:5] == pytest.approx([29.0, 25.0, 18.5, 14.5])
    assert len(segments) == 3 and all(segment["end"] <= total for segment in segments)
    assert all(segment["end"] <= total for segment in segments)


def test_truncated_flag_splits_and_keeps_both_halves(tmp_path, monkeypatch):
    source = make_audio(tmp_path / "source.wav", 50)
    monkeypatch.setattr(config, "llm_enabled", lambda: True)
    calls = []

    def chat(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return json.dumps({"segments": [{"speaker": "A", "start": 1, "text": "cut"}], "truncated": True})
        text = "left" if len(calls) == 2 else "right"
        return json.dumps({"segments": [{"speaker": "A", "start": 1, "text": text}]})

    monkeypatch.setattr(llm, "chat", chat)
    _, segments = transcribe.transcribe(str(source), settings())
    assert [item["text"] for item in segments] == ["left", "right"]


def test_long_real_audio_transcription_offsets_tail_checkpoint_and_cleanup(tmp_path, monkeypatch):
    source = make_long_mp3(tmp_path / "clip.mp3", 200)
    with SessionLocal() as session:
        meeting = Meeting(title="hour-long transcription")
        session.add(meeting)
        session.commit()
        meeting_id = meeting.id
    checkpoint = checkpoints.Store(meeting_id)
    monkeypatch.setattr(config, "llm_enabled", lambda: True)
    workdirs = []
    real_mkdtemp = audio.tempfile.mkdtemp

    def tracked_mkdtemp(*args, **kwargs):
        result = real_mkdtemp(*args, **kwargs)
        workdirs.append(result)
        return result

    monkeypatch.setattr(audio.tempfile, "mkdtemp", tracked_mkdtemp)
    calls = []

    def chat(*args, **kwargs):
        call_index = len(calls)
        calls.append(args[1])
        local_start = 0.5 if call_index == 2 else 5
        return json.dumps({"segments": [{"speaker": "A", "start": local_start,
                                         "text": f"part {call_index + 1}"}]})

    monkeypatch.setattr(llm, "chat", chat)
    total, segments = transcribe.transcribe(
        str(source), settings(), checkpoint=checkpoint,
    )

    assert len(calls) == 3
    assert [segment["start"] for segment in segments] == pytest.approx([5, 95, 180.5])
    assert len(workdirs) == 1
    assert not pathlib.Path(workdirs[0]).exists()
    with SessionLocal() as session:
        assert session.query(ProcessingCheckpoint).filter_by(
            meeting_id=meeting_id, stage="transcribe",
        ).count() == 3
    assert all(segment["end"] <= total for segment in segments), (
        f"segment ends {[segment['end'] for segment in segments]} exceed duration {total}"
    )
    assert segments[-1]["end"] == min(round(total, 1), total)


def test_merge_suppresses_exact_overlap_duplicate():
    existing = [{"speaker": "A", "start": 61, "text": "Same words!"}]
    transcribe._merge(existing, [{"speaker": "A", "start": 63, "text": "same words"}], boundary=60)

    assert existing == [{"speaker": "A", "start": 61, "text": "Same words!"}]


def test_merge_keeps_repeated_same_speaker_words_in_new_chunk():
    segments = []
    transcribe._merge(segments, [
        {"speaker": "A", "start": 0, "text": "yes"},
        {"speaker": "A", "start": 3, "text": "yes"},
    ], boundary=0)

    assert [segment["start"] for segment in segments] == [0, 3]


def test_merge_keeps_repeated_single_word_in_neighboring_overlap():
    segments = [{"speaker": "A", "start": 61, "text": "да"}]
    transcribe._merge(segments, [{"speaker": "A", "start": 62, "text": "да"}], boundary=60)

    assert [segment["start"] for segment in segments] == [61, 62]


def test_merge_suppresses_two_word_duplicate_within_two_seconds():
    segments = [{"speaker": "A", "start": 61, "text": "Да, точно"}]
    transcribe._merge(segments, [{"speaker": "A", "start": 63, "text": "да точно"}], boundary=60)

    assert segments == [{"speaker": "A", "start": 61, "text": "Да, точно"}]


def test_merge_keeps_identical_words_from_different_speakers():
    existing = [{"speaker": "A", "start": 61, "text": "Yes, exactly."}]
    transcribe._merge(existing, [{"speaker": "B", "start": 62, "text": "Yes exactly"}], boundary=60)

    assert len(existing) == 2
    assert {segment["speaker"] for segment in existing} == {"A", "B"}


def test_merge_keeps_repeated_words_outside_overlap():
    existing = [{"speaker": "A", "start": 20, "text": "Repeat this"}]
    transcribe._merge(existing, [{"speaker": "A", "start": 90, "text": "Repeat this"}], boundary=90)

    assert [segment["start"] for segment in existing] == [20, 90]


def test_checkpoint_retry_reuses_successful_part_and_invalidates_on_settings_change(tmp_path, monkeypatch):
    source = make_audio(tmp_path / "source.wav", 100)
    with SessionLocal() as session:
        meeting = Meeting(title="checkpoint test")
        session.add(meeting)
        session.commit()
        meeting_id = meeting.id
    checkpoint = checkpoints.Store(meeting_id)
    monkeypatch.setattr(config, "llm_enabled", lambda: True)
    calls = []

    def chat(*args, **kwargs):
        messages = args[1]
        calls.append(messages)
        if len(calls) == 2:
            raise llm.LLMError("second part failed")
        return json.dumps({"segments": [{"speaker": "A", "start": 1, "text": f"part {len(calls)}"}]})

    monkeypatch.setattr(llm, "chat", chat)
    original = settings(glossary="alpha", chunk_minutes=1)
    with pytest.raises(llm.LLMError, match="second part failed"):
        transcribe.transcribe(str(source), original, checkpoint=checkpoint)
    assert len(calls) == 2

    _, retried = transcribe.transcribe(str(source), original, checkpoint=checkpoint)
    assert len(calls) == 3
    assert [item["text"] for item in retried] == ["part 1", "part 3"]

    changed_model = settings(glossary="alpha", transcribe_model="test/other", chunk_minutes=1)
    transcribe.transcribe(str(source), changed_model, checkpoint=checkpoint)
    assert len(calls) == 5

    changed_glossary = settings(glossary="beta", transcribe_model="test/other", chunk_minutes=1)
    transcribe.transcribe(str(source), changed_glossary, checkpoint=checkpoint)
    assert len(calls) == 7


@pytest.mark.parametrize("payload,error", [
    ({"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]}, llm.IncompleteResponse),
    ({"choices": [{"finish_reason": "content_filter", "message": {"content": "{}"}}]}, llm.LLMError),
    ({"choices": [{"finish_reason": "stop", "message": {"content": "  "}}]}, llm.LLMError),
    ({"choices": []}, llm.LLMError),
    ({"choices": [{"message": {}}]}, llm.LLMError),
    ({"choices": [{"message": {"content": "{}"}}]}, llm.LLMError),
])
def test_chat_rejects_truncated_filtered_empty_and_malformed_responses(monkeypatch, payload, error):
    monkeypatch.setattr(llm.config, "openrouter_key", lambda: "local-test-key")
    monkeypatch.setattr(llm.httpx, "post", lambda *a, **kw: SimpleNamespace(
        status_code=200, json=lambda: payload, text=json.dumps(payload)))

    with pytest.raises(error):
        llm.chat("test/model", [], json_mode=True)


def test_chat_returns_normal_json_content(monkeypatch):
    payload = {"choices": [{"finish_reason": "stop", "message": {"content": '{"ok": true}'}}]}
    monkeypatch.setattr(llm.config, "openrouter_key", lambda: "local-test-key")
    monkeypatch.setattr(llm.httpx, "post", lambda *a, **kw: SimpleNamespace(
        status_code=200, json=lambda: payload, text=json.dumps(payload)))

    assert llm.chat("test/model", [], json_mode=True) == '{"ok": true}'
    assert llm.parse_json(llm.chat("test/model", [], json_mode=True)) == {"ok": True}


def test_chat_retries_http_408_then_returns_success(monkeypatch):
    payload = {"choices": [{"finish_reason": "stop", "message": {"content": '{"ok": true}'}}]}
    responses = iter([
        SimpleNamespace(status_code=408, json=lambda: {}, text="request timeout"),
        SimpleNamespace(status_code=200, json=lambda: payload, text=json.dumps(payload)),
    ])
    calls = []
    sleeps = []
    monkeypatch.setattr(llm.config, "openrouter_key", lambda: "local-test-key")
    monkeypatch.setattr(llm.httpx, "post", lambda *a, **kw: calls.append((a, kw)) or next(responses))
    monkeypatch.setattr(llm.time, "sleep", sleeps.append)

    assert llm.chat("test/model", [], json_mode=True) == '{"ok": true}'
    assert len(calls) == 2
    assert sleeps == [5]
