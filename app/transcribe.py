"""Speech to text with speaker separation.

Providers:
  openrouter - an audio-capable model on OpenRouter (default google/gemini-2.5-flash), chunked
  mock       - returns the bundled demo transcript (no API key needed)
The interface is small so ElevenLabs / Deepgram / WhisperX can be added later.
"""
import base64
import json
import pathlib

from . import audio, config, llm

DEMO = pathlib.Path(__file__).resolve().parent.parent / "samples" / "demo_transcript.json"

PROMPT = """Ты — профессиональный стенографист совещаний. Расшифруй аудиофрагмент ДОСЛОВНО.
Правила:
- Язык: {language}. Английские слова и термины пиши латиницей, как произнесены. Числа, даты и суммы — цифрами.
- Раздели речь по говорящим. Метки: "Спикер 1", "Спикер 2"... Если говорящего называют по имени или он
  представляется — используй имя (например "Иван"). Сохраняй одни и те же метки для одного голоса.
- Для каждой реплики укажи время начала от начала ЭТОГО фрагмента в секундах.
- Не сокращай, не пересказывай, не исправляй смысл. Неразборчивое помечай [неразборчиво].
- Термины компании (пиши именно так): {glossary}
{context}
Верни ТОЛЬКО JSON: {{"segments": [{{"speaker": "Спикер 1", "start": 0.0, "text": "..."}}]}}"""


def _context(prev_segments: list) -> str:
    if not prev_segments:
        return ""
    tail = prev_segments[-8:]
    lines = "\n".join(f"{s['speaker']}: {s['text'][:200]}" for s in tail)
    speakers = sorted({s["speaker"] for s in prev_segments})
    return ("Это продолжение записи. Уже известные говорящие: " + ", ".join(speakers) +
            ". Предыдущий фрагмент закончился так:\n" + lines + "\nПродолжай те же метки для тех же людей.")


def transcribe(path: str, settings: dict, progress=lambda msg: None) -> tuple[float, list]:
    if not config.llm_enabled():
        return mock_transcribe()
    total, chunks = audio.prepare_chunks(path, settings.get("chunk_minutes", 30))
    segments: list = []
    for i, (offset, chunk) in enumerate(chunks):
        progress(f"Расшифровка фрагмента {i + 1} из {len(chunks)}")
        b64 = base64.b64encode(chunk.read_bytes()).decode()
        prompt = PROMPT.format(language="русский" if settings.get("language", "ru") == "ru" else settings["language"],
                               glossary=settings.get("glossary", ""), context=_context(segments))
        text = llm.chat(settings["transcribe_model"], [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "input_audio", "input_audio": {"data": b64, "format": "mp3"}},
        ]}], json_mode=True, temperature=0, max_tokens=60000)
        data = llm.parse_json(text)
        segs = data.get("segments", data) if isinstance(data, dict) else data
        for s in segs:
            try:
                start = float(s.get("start", 0)) + offset
            except (TypeError, ValueError):
                start = offset
            segments.append({"speaker": str(s.get("speaker", "Спикер")).strip() or "Спикер",
                             "start": round(start, 1), "text": str(s.get("text", "")).strip()})
        chunk.unlink(missing_ok=True)
    segments = [s for s in segments if s["text"]]
    for a, b in zip(segments, segments[1:]):
        a["end"] = b["start"]
    if segments:
        segments[-1]["end"] = round(total, 1)
    return total, segments


def mock_transcribe() -> tuple[float, list]:
    data = json.loads(DEMO.read_text(encoding="utf-8"))
    return data["duration"], data["segments"]
