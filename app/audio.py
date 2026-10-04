"""Audio preparation with ffmpeg: mono 16 kHz MP3 (speech quality, small upload), split into chunks."""
import json
import pathlib
import subprocess
import tempfile


def duration(path: str) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path],
                         capture_output=True, text=True, check=True).stdout
    return float(json.loads(out)["format"]["duration"])


def prepare_chunks(path: str, chunk_minutes: int = 30) -> tuple[float, list[tuple[float, pathlib.Path]]]:
    """Returns total duration and [(offset_seconds, chunk_path)]."""
    total = duration(path)
    step = max(60, int(chunk_minutes * 60))
    work = pathlib.Path(tempfile.mkdtemp(prefix="sysai_"))
    chunks = []
    start = 0.0
    i = 0
    while start < total - 0.5:
        dst = work / f"chunk_{i:03d}.mp3"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-t", str(step), "-i", path,
                        "-ac", "1", "-ar", "16000", "-b:a", "32k", str(dst)], check=True)
        chunks.append((start, dst))
        start += step
        i += 1
    return total, chunks
