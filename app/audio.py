"""Audio preparation with ffmpeg: mono 16 kHz MP3 (speech quality, small upload), split into chunks."""
import json
import math
import pathlib
import shutil
import subprocess
import tempfile


def duration(path: str) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path],
                         capture_output=True, text=True, check=True).stdout
    total = float(json.loads(out)["format"]["duration"])
    if not math.isfinite(total) or total <= 0:
        raise ValueError("Не удалось определить длительность аудио")
    return total


def prepare_chunks(path: str, chunk_minutes: float = 30, overlap_seconds: float = 0) -> tuple[float, list[tuple[float, pathlib.Path]]]:
    """Returns total duration and [(offset_seconds, chunk_path)]."""
    total = duration(path)
    step = max(15, int(chunk_minutes * 60))
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
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-t", str(seconds), "-i", str(source),
                    "-ac", "1", "-ar", "16000", "-b:a", "32k", str(destination)],
                   check=True, capture_output=True)
