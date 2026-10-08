"""Audio preparation with ffmpeg: mono 16 kHz MP3 (speech quality, small upload), split into chunks."""
import json
import math
import pathlib
import shutil
import subprocess
import tempfile


def duration(path: str) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    value = json.loads(out).get("format", {}).get("duration")
    if value in (None, "N/A"):
        # Browser/live WebM often has no container duration. Decode only in
        # that case; ffmpeg's final audio clock measures the actual recording.
        decoded = subprocess.run(
            ["ffmpeg", "-v", "error", "-nostats", "-i", str(path), "-map", "0:a:0",
             "-progress", "pipe:1", "-f", "null", "-"],
            capture_output=True, text=True, check=True,
        ).stdout
        times = [float(line.partition("=")[2]) / 1_000_000
                 for line in decoded.splitlines() if line.startswith("out_time_us=")
                 and line.partition("=")[2] != "N/A"]
        total = max(times, default=0)
    else:
        total = float(value)
    if not math.isfinite(total) or total <= 0:
        raise ValueError("Не удалось определить длительность аудио")
    return total


def chunk_seconds(total: float, maximum_minutes: float | None = None) -> float:
    """Bound model workload, with smaller parts for lengthy recordings.

    Short recordings remain a single part; no padding, silence trimming or
    volume threshold is applied (quiet speech must not be discarded).
    """
    if not math.isfinite(total) or total <= 0:
        raise ValueError("Некорректная длительность аудио")
    step = 45 if total > 3600 else 60 if total > 600 else min(90, total)
    if maximum_minutes is not None:
        if not math.isfinite(maximum_minutes) or maximum_minutes <= 0:
            raise ValueError("Некорректная длина части аудио")
        # Legacy settings may request smaller parts, never larger than the
        # adaptive safety ceiling. Preserve the extraction minimum of 15 s.
        step = min(step, max(15, maximum_minutes * 60))
    return step


def prepare_chunks(path: str, chunk_minutes: float | None = 30, overlap_seconds: float = 0,
                   max_chunk_minutes: float | None = None) -> tuple[float, list[tuple[float, pathlib.Path]]]:
    """Return measured duration and chunks; None selects adaptive part lengths."""
    total = duration(path)
    if not math.isfinite(overlap_seconds) or overlap_seconds < 0:
        raise ValueError("Некорректное перекрытие частей аудио")
    if chunk_minutes is None:
        step = chunk_seconds(total, max_chunk_minutes)
    else:
        if not math.isfinite(chunk_minutes) or chunk_minutes <= 0:
            raise ValueError("Некорректная длина части аудио")
        step = max(15, chunk_minutes * 60)
    work = pathlib.Path(tempfile.mkdtemp(prefix="sysai_"))
    chunks = []
    start = 0.0
    i = 0
    try:
        while start < total:
            dst = work / f"chunk_{i:03d}.mp3"
            extract(path, dst, start, min(step + overlap_seconds, total - start))
            chunks.append((start, dst))
            start += step
            i += 1
    except Exception:
        shutil.rmtree(work)
        raise
    return total, chunks


def extract(source, destination, start, seconds):
    if not math.isfinite(start) or start < 0 or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("Некорректные границы части аудио")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-t", str(seconds), "-i", str(source),
                    "-ac", "1", "-ar", "16000", "-b:a", "32k", str(destination)],
                   check=True, capture_output=True)
    if not pathlib.Path(destination).is_file() or pathlib.Path(destination).stat().st_size == 0:
        raise ValueError("Пустая часть аудио после преобразования")
