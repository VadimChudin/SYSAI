"""Durable, ordered chunk storage for browser microphone recordings."""
import datetime as dt
import hashlib
import json
import logging
import math
import os
import pathlib
import shutil
import subprocess
import threading
import uuid

from fastapi import HTTPException
from sqlalchemy import delete

from . import config, pipeline
from .db import Meeting, Recording, RecordingChunk, SessionLocal, now

log = logging.getLogger(__name__)

_MIME_EXTENSIONS = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "m4a"}
_MAX_CHUNK_BYTES = 8 * 1024 * 1024
_locks_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}


def _lock(recording_id: str) -> threading.RLock:
    with _locks_guard:
        return _locks.setdefault(recording_id, threading.RLock())


def _http(status: int, detail: str):
    raise HTTPException(status_code=status, detail=detail)


def _get_owned(session, owner: str, recording_id: str) -> Recording:
    recording = session.get(Recording, recording_id)
    if recording is None or recording.owner != owner:
        _http(404, "Recording not found")
    return recording


def _date(value) -> dt.date | None:
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        _http(400, "Invalid meeting date")


def _chunks_dir(recording_id: str) -> pathlib.Path:
    return config.DATA_DIR / "recordings" / recording_id


def _part_path(recording_id: str, sequence: int) -> pathlib.Path:
    return _chunks_dir(recording_id) / f"{sequence:010d}.part"


def _atomic_write(path: pathlib.Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _recording_dict(recording: Recording) -> dict:
    return {"id": recording.id, "next_sequence": recording.next_sequence,
            "size_bytes": recording.size_bytes, "status": recording.status,
            "mime_type": recording.mime_type, "title": recording.title}


def start(owner: str, mime_type: str, title: str = "", meeting_date=None, options=None) -> dict:
    if not isinstance(owner, str) or not owner or len(owner) > 128:
        _http(400, "Invalid recording owner")
    if not isinstance(mime_type, str):
        _http(415, "Unsupported recording audio type")
    mime = mime_type.split(";", 1)[0].strip().lower()
    if mime not in _MIME_EXTENSIONS:
        _http(415, "Unsupported recording audio type")
    if not isinstance(title, str) or len(title) > 300:
        _http(400, "Invalid recording title")
    if options is None:
        options = {}
    if not isinstance(options, dict):
        _http(400, "Invalid recording options")
    try:
        json.dumps(options)
    except (TypeError, ValueError):
        _http(400, "Invalid recording options")

    recording = Recording(id=uuid.uuid4().hex, owner=owner, mime_type=mime, title=title,
                          meeting_date=_date(meeting_date), options=options.copy(), status="recording",
                          next_sequence=0, size_bytes=0)
    with SessionLocal() as session:
        session.add(recording)
        session.commit()
        session.refresh(recording)
        return _recording_dict(recording)


def append(owner: str, recording_id: str, sequence: int, chunk: bytes) -> dict:
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        _http(409, "Invalid chunk sequence")
    if not isinstance(chunk, bytes) or not chunk:
        _http(409, "Audio chunk must not be empty")
    if len(chunk) > _MAX_CHUNK_BYTES:
        _http(413, "Audio chunk exceeds 8 MB")
    digest = hashlib.sha256(chunk).hexdigest()

    with _lock(recording_id):
        with SessionLocal() as session:
            recording = _get_owned(session, owner, recording_id)
            prior = session.get(RecordingChunk, (recording_id, sequence))
            if prior:
                if prior.digest != digest or prior.size_bytes != len(chunk):
                    _http(409, "Chunk sequence was already used with different audio")
                if recording.status not in ("recording", "finishing", "error", "finished"):
                    _http(409, "Recording is not accepting chunks")
                return {"next_sequence": recording.next_sequence, "size_bytes": recording.size_bytes}
            if recording.status != "recording":
                _http(409, "Recording is not accepting chunks")
            if sequence != recording.next_sequence:
                _http(409, "Unexpected chunk sequence")
            max_bytes = max(0, config.MAX_UPLOAD_MB) * 1024 * 1024
            if recording.size_bytes + len(chunk) > max_bytes:
                _http(413, "Recording exceeds upload limit")

            _atomic_write(_part_path(recording_id, sequence), chunk)
            session.add(RecordingChunk(recording_id=recording_id, sequence=sequence,
                                       digest=digest, size_bytes=len(chunk)))
            recording.next_sequence += 1
            recording.size_bytes += len(chunk)
            recording.updated_at = now()
            session.commit()
            return {"next_sequence": recording.next_sequence, "size_bytes": recording.size_bytes}


def _assemble_file(session, recording: Recording, destination: pathlib.Path):
    temp = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    size = 0
    sequence = 0
    try:
        with temp.open("wb") as output:
            chunks = session.query(RecordingChunk).filter_by(recording_id=recording.id).order_by(
                RecordingChunk.sequence).yield_per(100)
            for chunk in chunks:
                if chunk.sequence != sequence:
                    _http(409, "Recording is missing audio chunks")
                digest = hashlib.sha256()
                chunk_size = 0
                try:
                    with _part_path(recording.id, sequence).open("rb") as source:
                        while data := source.read(1024 * 1024):
                            output.write(data)
                            digest.update(data)
                            chunk_size += len(data)
                except OSError:
                    _http(409, "Recording audio chunk is unavailable")
                if chunk_size != chunk.size_bytes or digest.hexdigest() != chunk.digest:
                    _http(409, "Recording audio chunk failed integrity check")
                size += chunk_size
                sequence += 1
            if sequence != recording.next_sequence:
                _http(409, "Recording is missing audio chunks")
            if size != recording.size_bytes:
                _http(409, "Recording size does not match stored chunks")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, destination)
    finally:
        temp.unlink(missing_ok=True)


def _remux_audio(source: pathlib.Path, destination: pathlib.Path):
    try:
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(source), "-map", "0:a:0", "-c:a", "copy",
                        "-y", str(destination)], capture_output=True, text=True, check=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        raise HTTPException(status_code=409, detail="Recording is not valid audio") from exc


def _probe_audio(path: pathlib.Path):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type:format=duration",
             "-of", "json", str(path)], capture_output=True, text=True, check=True, timeout=60)
        metadata = json.loads(result.stdout)
        duration = float(metadata["format"]["duration"])
        has_audio = any(stream.get("codec_type") == "audio" for stream in metadata.get("streams", []))
        if not has_audio or not math.isfinite(duration) or duration <= 0:
            raise ValueError("No valid audio stream")
    except (OSError, subprocess.SubprocessError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=409, detail="Recording is not valid audio") from exc


def finish(owner: str, recording_id: str) -> dict:
    with _lock(recording_id):
        with SessionLocal() as session:
            recording = _get_owned(session, owner, recording_id)
            if recording.status == "finished":
                if recording.meeting_id is None or session.get(Meeting, recording.meeting_id) is None:
                    _http(404, "Recording meeting not found")
                return {"meeting_id": recording.meeting_id, "url": f"/meetings/{recording.meeting_id}"}
            if recording.status not in ("recording", "finishing", "error"):
                _http(409, "Recording cannot be finished")
            if recording.size_bytes <= 0:
                _http(409, "Recording has no audio")
            recording.status = "finishing"
            recording.updated_at = now()
            session.commit()

        with SessionLocal() as session:
            recording = _get_owned(session, owner, recording_id)
            extension = _MIME_EXTENSIONS[recording.mime_type]
            source_temp = _chunks_dir(recording_id) / f"assembled.{extension}"
            normalized_temp = _chunks_dir(recording_id) / f"normalized.{uuid.uuid4().hex}.{extension}"
            try:
                _assemble_file(session, recording, source_temp)
                _remux_audio(source_temp, normalized_temp)
                _probe_audio(normalized_temp)
            except HTTPException:
                source_temp.unlink(missing_ok=True)
                normalized_temp.unlink(missing_ok=True)
                recording.status = "error"
                recording.updated_at = now()
                session.commit()
                raise
            try:
                final_path = config.DATA_DIR / "uploads" / f"{recording_id}.{extension}"
                final_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(normalized_temp, final_path)
            finally:
                source_temp.unlink(missing_ok=True)
                normalized_temp.unlink(missing_ok=True)
            meeting = Meeting(title=recording.title, source="microphone", filename=final_path.name,
                              audio_path=str(final_path), status="queued", progress="В очереди",
                              meeting_date=recording.meeting_date, options=recording.options)
            session.add(meeting)
            session.flush()
            recording.meeting_id = meeting.id
            recording.path = str(final_path)
            recording.status = "finished"
            recording.updated_at = now()
            session.commit()
            meeting_id = meeting.id

        shutil.rmtree(_chunks_dir(recording_id), ignore_errors=True)
        try:
            pipeline.submit(meeting_id)
        except Exception:
            log.exception("Could not enqueue microphone recording meeting %s", meeting_id)
        return {"meeting_id": meeting_id, "url": f"/meetings/{meeting_id}"}


def cancel(owner: str, recording_id: str) -> dict:
    with _lock(recording_id):
        with SessionLocal() as session:
            recording = _get_owned(session, owner, recording_id)
            if recording.status in ("finished", "cancelled"):
                if recording.status == "cancelled":
                    return {"id": recording_id, "status": "cancelled"}
                _http(409, "Finished recording cannot be cancelled")
            recording.status = "cancelled"
            recording.updated_at = now()
            session.execute(delete(RecordingChunk).where(RecordingChunk.recording_id == recording_id))
            session.commit()
        directory = _chunks_dir(recording_id)
        if directory.exists():
            import shutil
            shutil.rmtree(directory)
        return {"id": recording_id, "status": "cancelled"}


def download(owner: str, recording_id: str) -> tuple[pathlib.Path, str]:
    """Return a safely rebuilt audio file and its MIME type for authorized recovery."""
    with _lock(recording_id):
        with SessionLocal() as session:
            recording = _get_owned(session, owner, recording_id)
            if recording.status == "cancelled":
                _http(409, "Cancelled recording has no audio")
            if recording.status == "finished":
                if recording.meeting_id is None or session.get(Meeting, recording.meeting_id) is None:
                    _http(404, "Recording audio not found")
                stored = pathlib.Path(recording.path)
                if stored.is_file():
                    return stored, recording.mime_type
                _http(404, "Recording audio is no longer available")
            extension = _MIME_EXTENSIONS[recording.mime_type]
            recovery = _chunks_dir(recording_id) / f"recovery.{extension}"
            _assemble_file(session, recording, recovery)
            return recovery, recording.mime_type


audio_file = download
