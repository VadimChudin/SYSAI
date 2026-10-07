"""Immutable PDF and data snapshots captured in the approval transaction."""
import copy

from . import render
from .db import Delivery, ReportVersion

APPROVED_STATUSES = ("delivery_queued", "sending", "delivery_retry", "delivery_failed", "done", "ready")


def latest(s, meeting_id):
    return s.query(ReportVersion).filter_by(meeting_id=meeting_id).order_by(ReportVersion.version.desc()).first()


def ensure(s, meeting, settings, origin="approval", draft_revision=None):
    existing = latest(s, meeting.id)
    if existing:
        return existing
    tasks = [{"id": t.id, "title": t.title, "description": t.description,
              "assignee_id": t.assignee_id, "assignee_name": t.assignee.name if t.assignee else t.assignee_name,
              "deadline": t.deadline.isoformat() if t.deadline else None,
              "deadline_source": t.deadline_source, "priority": t.priority,
              "quote": t.source_quote, "time": t.source_time} for t in meeting.tasks]
    stored = s.query(Delivery).filter(Delivery.meeting_id == meeting.id, Delivery.phase == "final",
                                     Delivery.kind == "pdf", Delivery.document.is_not(None)).order_by(Delivery.id).first()
    draft = None
    if not stored and draft_revision is not None:
        draft = s.query(Delivery).filter(Delivery.meeting_id == meeting.id, Delivery.phase == "draft",
                                        Delivery.kind == "pdf", Delivery.document.is_not(None),
                                        Delivery.payload["approval_revision"].as_integer() == draft_revision).order_by(Delivery.id).first()
    source = stored or draft
    pdf = source.document if source else render.report_pdf(meeting, settings)
    formatting = (meeting.options or {}).get("approval_formatting", {}) if draft else settings
    snapshot = {"report": copy.deepcopy(meeting.report or {}), "tasks": tasks,
                "transcript": copy.deepcopy(meeting.transcript),
                "meeting_date": meeting.meeting_date.isoformat() if meeting.meeting_date else None,
                "duration_sec": meeting.duration_sec,
                "formatting": {key: formatting.get(key) for key in ("company_name", "accent_color", "include_transcript_in_pdf")},
                "data_origin": "current_at_recovery" if stored else "approval"}
    version = ReportVersion(meeting_id=meeting.id, version=1, title=meeting.title or (meeting.report or {}).get("title", "Совещание"),
                            filename=source.payload.get("filename", render.pdf_name(meeting)) if source else render.pdf_name(meeting), pdf=pdf, snapshot=snapshot,
                            origin="delivery_recovery" if stored else origin)
    s.add(version)
    s.flush()
    return version
