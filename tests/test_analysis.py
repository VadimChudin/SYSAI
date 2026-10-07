import datetime as dt
import json
import re
from types import SimpleNamespace

import pytest

from app import analyze, llm


MEETING_DATE = dt.date(2026, 10, 7)
EMPLOYEES = [
    SimpleNamespace(id=1, name="Анна Смирнова", aliases="Аня", position="Менеджер",
                    alias_list=lambda: ["Аня"]),
    SimpleNamespace(id=2, name="Борис Иванов", aliases="", position="Разработчик",
                    alias_list=lambda: []),
]


def segment(index, text, step=5):
    return {"start": index * step, "end": index * step + 4, "speaker": "Спикер 1", "text": text}


def empty_report(**kwargs):
    report = {"title": "План", "summary": "Обсудили ход работ.", "participants": [], "topics": [],
              "decisions": [], "tasks": [], "open_questions": [], "risks": [], "notes": [],
              "next_meeting": None}
    report.update(kwargs)
    return report


def task(title, quote, time, employee_id=1):
    return {"title": title, "description": title, "employee_id": employee_id,
            "assignee_name": None, "deadline": None, "deadline_quote": None, "priority": "medium",
            "time": time, "quote": quote}


class Checkpoint:
    def __init__(self):
        self.values = {}

    def get(self, stage, index, fingerprint):
        return self.values.get((stage, index, fingerprint))

    def put(self, stage, index, fingerprint, data):
        self.values[(stage, index, fingerprint)] = data


def setup_llm(monkeypatch, responder):
    monkeypatch.setattr(analyze.config, "llm_enabled", lambda: True)
    calls = []

    def chat(model, messages, **kwargs):
        calls.append(messages)
        return json.dumps(responder(messages), ensure_ascii=False)

    monkeypatch.setattr(llm, "chat", chat)
    return calls


def test_live_analysis_without_key_raises_instead_of_using_demo(monkeypatch):
    monkeypatch.setattr(analyze.config, "llm_enabled", lambda: False)

    with pytest.raises(llm.LLMError, match="(?i)openrouter"):
        analyze.analyze([segment(0, "Обычная расшифровка")], EMPLOYEES,
                        {"report_model": "test"}, MEETING_DATE)


def test_short_transcript_uses_one_request_and_verifies_task(monkeypatch):
    segments = [segment(0, "Анна подготовит план к пятнице.")]
    quote = "Анна подготовит план к пятнице."
    calls = setup_llm(monkeypatch, lambda _: empty_report(tasks=[task("Подготовить план", quote, "00:00")]))

    result = analyze.analyze(segments, EMPLOYEES, {"report_model": "test"}, MEETING_DATE)

    assert len(calls) == 1
    assert result["tasks"][0]["title"] == "Подготовить план"


def test_large_transcript_keeps_tasks_deduplicates_only_exact_overlap_and_caches(monkeypatch):
    texts = [f"Обсуждение участка {i:04d}: " + ("подробности проекта " * 5) for i in range(900)]
    initial = [segment(i, text) for i, text in enumerate(texts)]
    initial_batches = analyze._transcript_batches(initial)
    shared = set(initial_batches[0].splitlines()) & set(initial_batches[1].splitlines())
    repeated_index = min(int(re.search(r"участка (\d+)", line).group(1)) for line in shared)
    repeated = "Поручение перенести проект к пятнице."
    texts[repeated_index] = repeated + " " + "x" * (len(texts[repeated_index]) - len(repeated) - 1)
    texts[500] = "Борис отдельно проверит интеграцию сервиса."
    texts[899] = "В конце Анна отправит итоговый отчет заказчику."
    segments = [segment(i, text) for i, text in enumerate(texts)]
    batches = analyze._transcript_batches(segments)
    assert len(batches) > 2
    assert all(len(batch) <= analyze.MAX_BATCH_CHARS for batch in batches)

    assert sum(repeated in batch for batch in batches) == 2
    duplicate_time = analyze.mmss(repeated_index * 5)
    middle_quote = texts[500]
    middle_time = analyze.mmss(500 * 5)
    final_quote = texts[-1]
    final_time = analyze.mmss(899 * 5)
    checkpoint = Checkpoint()

    def respond(messages):
        user = messages[-1]["content"]
        if "Результаты пакетного анализа" in user:
            return empty_report(title="Итоги", summary="Обсудили несколько направлений работы.")
        found = []
        decisions = []
        # The overlap causes this same task to be extracted from two neighboring batches.
        if repeated in user:
            found.append(task("Перенести проект", repeated, duplicate_time, 1))
            decisions.append({"text": "Сохранить текущую дату запуска", "time": duplicate_time})
        if middle_quote in user:
            found.append(task("Проверить интеграцию", middle_quote, middle_time, 2))
            decisions.append({"text": "Проверить интеграцию отдельно", "time": middle_time})
        if final_quote in user:
            found.append(task("Отправить итоговый отчет", final_quote, final_time, 1))
            decisions.append({"text": "Отправить финальный отчет", "time": final_time})
        return empty_report(tasks=found, decisions=decisions)

    calls = setup_llm(monkeypatch, respond)
    settings = {"report_model": "test", "glossary": "термины"}
    result = analyze.analyze(segments, EMPLOYEES, settings, MEETING_DATE, checkpoint=checkpoint)

    assert len(result["tasks"]) == 3
    assert [item["employee_id"] for item in result["tasks"]] == [1, 2, 1]
    assert result["tasks"][-1]["quote"] == final_quote
    assert analyze._time_seconds(result["tasks"][-1]["time"]) == 899 * 5
    assert {d["text"] for d in result["decisions"]} == {
        "Сохранить текущую дату запуска", "Проверить интеграцию отдельно", "Отправить финальный отчет"}
    first_call_count = len(calls)
    again = analyze.analyze(segments, EMPLOYEES, settings, MEETING_DATE, checkpoint=checkpoint)
    assert len(calls) == first_call_count
    assert again["tasks"] == result["tasks"]
    analyze.analyze(segments, EMPLOYEES, {**settings, "glossary": "измененный словарь"}, MEETING_DATE,
                    checkpoint=checkpoint)
    assert len(calls) > first_call_count


@pytest.mark.parametrize("bad_task", [
    {"title": "Задача", "description": "", "employee_id": None, "assignee_name": None,
     "deadline": None, "deadline_quote": None, "priority": "medium", "time": "00:01"},
    task("Задача", "не из записи", "00:01"),
    task("Задача", "Анна подготовит план.", "99:99:99"),
])
def test_invalid_task_evidence_or_time_fails(monkeypatch, bad_task):
    segments = [segment(0, "Анна подготовит план.")]
    setup_llm(monkeypatch, lambda _: empty_report(tasks=[bad_task]))

    with pytest.raises(llm.LLMError):
        analyze.analyze(segments, EMPLOYEES, {"report_model": "test"}, MEETING_DATE)


def test_task_quote_must_match_the_reported_time(monkeypatch):
    segments = [segment(0, "Анна подготовит план."), segment(30, "Борис проверит оплату.")]
    wrong_time = task("Подготовить план", "Анна подготовит план.", "00:30", 1)
    setup_llm(monkeypatch, lambda _: empty_report(tasks=[wrong_time]))

    with pytest.raises(llm.LLMError, match="время"):
        analyze.analyze(segments, EMPLOYEES, {"report_model": "test"}, MEETING_DATE)


def test_task_quote_can_span_adjacent_segments_with_normalized_whitespace():
    segments = [segment(0, "Анна подготовит"), segment(3, "  план    к пятнице.")]
    assert analyze._quote_matches_at_time("АННА подготовит план к пятнице.", 3, segments)
    assert not analyze._quote_matches_at_time("Анна подготовит план к пятнице.", 25, segments)


def test_malformed_json_types_fail(monkeypatch):
    setup_llm(monkeypatch, lambda _: {"title": "Отчет", "summary": "Есть результат", "tasks": {},
                                     "participants": [], "topics": [], "decisions": [],
                                     "open_questions": [], "risks": [], "notes": [], "next_meeting": None})

    with pytest.raises(llm.LLMError, match="tasks"):
        analyze.analyze([segment(0, "Текст")], EMPLOYEES, {"report_model": "test"}, MEETING_DATE)


def test_relative_deadline_uses_meeting_date_base():
    result = analyze.normalize({"tasks": [{"title": "Подготовить план", "deadline": "через неделю"}]},
                              EMPLOYEES, MEETING_DATE)
    assert result["tasks"][0]["deadline"] == "2026-10-14"


@pytest.mark.parametrize(("deadline", "deadline_quote"), [
    ("2026-10-16", "к пятнице"),
    ("2026-10-09", ""),
    ("2026-10-09", "к понедельнику"),
])
def test_live_deadline_requires_transcript_quote_and_matches_recognized_date(monkeypatch, deadline,
                                                                            deadline_quote):
    quote = "Анна отправит отчет к пятнице."
    task_data = task("Отправить отчет", quote, "00:00")
    task_data.update(deadline=deadline, deadline_quote=deadline_quote)
    setup_llm(monkeypatch, lambda _: empty_report(tasks=[task_data]))

    with pytest.raises(llm.LLMError):
        analyze.analyze([segment(0, quote)], EMPLOYEES, {"report_model": "test"}, MEETING_DATE)


def test_live_deadline_quote_is_verified_and_relative_date_uses_meeting_date(monkeypatch):
    quote = "Анна отправит отчет через неделю."
    task_data = task("Отправить отчет", quote, "00:00")
    task_data.update(deadline="2026-10-14", deadline_quote="через неделю")
    setup_llm(monkeypatch, lambda _: empty_report(tasks=[task_data]))

    result = analyze.analyze([segment(0, quote)], EMPLOYEES, {"report_model": "test"}, MEETING_DATE)

    assert result["tasks"][0]["deadline"] == "2026-10-14"


def test_dedup_preserves_distinct_unknown_assignees_and_conflicting_task_details():
    first = task("Отправить отчет", "Подготовить и отправить отчет.", "00:00", None)
    first.update(assignee_name="Ольга", description="Отправить клиенту", deadline="2026-10-09")
    other_assignee = {**first, "assignee_name": "Павел"}
    other_deadline = {**first, "deadline": "2026-10-16"}

    result = analyze._deduplicate_tasks([first, {**first}, other_assignee, other_deadline], EMPLOYEES)

    assert len(result) == 3
    assert [item["assignee_name"] for item in result] == ["Ольга", "Павел", "Ольга"]


def test_batch_overlap_is_prior_replies_and_time_parser_supports_hours():
    segments = [segment(i, f"Реплика {i} " + ("слово " * 8), step=60) for i in range(500)]
    batches = analyze._transcript_batches(segments)
    assert len(batches) > 1
    first_lines = set(batches[0].splitlines())
    second_lines = set(batches[1].splitlines())
    assert len(first_lines & second_lines) == analyze.OVERLAP_REPLIES
    assert analyze._time_seconds("01:06:40") == 4000
    assert analyze.mmss(4000) == "1:06:40"


def test_huge_utterance_after_overlap_stays_in_bounded_batches():
    segments = [segment(0, "До большой реплики " + "контекст " * 1500),
                segment(1, "Еще предыдущая реплика " + "обсуждение " * 1500),
                segment(2, "Начало большой реплики " + "слово " * 5000),
                segment(3, "Реплика после длинной " + "итог " * 1500)]

    batches = analyze._transcript_batches(segments)

    assert len(batches) >= 3
    assert all(len(batch) <= analyze.MAX_BATCH_CHARS for batch in batches)
    assert "До большой реплики" in batches[0]
    assert any("Начало большой реплики" in batch for batch in batches)
    assert any("итог" in batch for batch in batches)


def test_normalize_nulls_unknown_participant_employee_id():
    result = analyze.normalize({"participants": [{"speaker": "Спикер 1", "name": "Неизвестный",
                                                   "employee_id": 999, "role": "участник"}]}, EMPLOYEES)
    assert result["participants"][0]["employee_id"] is None
