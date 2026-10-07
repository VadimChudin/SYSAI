"""Database models (SQLite by default, Postgres via DATABASE_URL)."""
import datetime as dt
import json

from sqlalchemy import (JSON, Boolean, Column, Date, DateTime, Float, ForeignKey, Integer, LargeBinary, String, Text, UniqueConstraint,
                        create_engine)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

from . import config

engine = create_engine(
    config.DATABASE_URL,
    connect_args={"check_same_thread": False} if config.DATABASE_URL.startswith("sqlite") else {},
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Setting(Base):
    __tablename__ = "settings"
    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=False)  # JSON


class TelegramChat(Base):
    """Chats the bot knows: people who pressed /start, groups and channels it was added to."""
    __tablename__ = "telegram_chats"
    chat_id = Column(String(40), primary_key=True)
    kind = Column(String(20))  # private | group | supergroup | channel
    title = Column(String(255))
    username = Column(String(100))
    active = Column(Boolean, default=True)
    updated_at = Column(DateTime(timezone=True), default=now, onupdate=now)


class Employee(Base):
    __tablename__ = "employees"
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    aliases = Column(String(500), default="")  # comma separated: "Ваня, Иван Петрович"
    position = Column(String(200), default="")
    channel = Column(String(20), default="telegram")  # only telegram in MVP
    telegram_chat_id = Column(String(40), default="")
    bitrix_user_id = Column(String(40), default="")
    active = Column(Boolean, default=True)

    def alias_list(self):
        return [a.strip() for a in (self.aliases or "").split(",") if a.strip()]


class Meeting(Base):
    __tablename__ = "meetings"
    id = Column(Integer, primary_key=True)
    title = Column(String(300), default="")
    source = Column(String(20), default="web")  # web | microphone | demo | api
    filename = Column(String(300), default="")
    audio_path = Column(String(500), default="")
    status = Column(String(30), default="queued")
    progress = Column(String(300), default="")
    error = Column(Text, default="")
    duration_sec = Column(Float, default=0)
    meeting_date = Column(Date, nullable=True)
    transcript = Column(JSON, nullable=True)  # [{speaker, start, end, text}]
    report = Column(JSON, nullable=True)
    options = Column(JSON, nullable=True)  # per-meeting overrides of settings (approval, deadlines, recipients…)
    created_at = Column(DateTime(timezone=True), default=now)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    tasks = relationship("Task", back_populates="meeting", cascade="all, delete-orphan", order_by="Task.id")
    deliveries = relationship("Delivery", cascade="all, delete-orphan", order_by="Delivery.id")
    checkpoints = relationship("ProcessingCheckpoint", cascade="all, delete-orphan")
    dialogue = relationship("ConversationMessage", cascade="all, delete-orphan", order_by="ConversationMessage.id")
    escalations = relationship("ConversationEscalation", cascade="all, delete-orphan", order_by="ConversationEscalation.id")


class Recording(Base):
    __tablename__ = "recordings"

    id = Column(String(64), primary_key=True)
    owner = Column(String(128), nullable=False, index=True)
    mime_type = Column(String(80), nullable=False)
    title = Column(String(300), nullable=False, default="")
    meeting_date = Column(Date, nullable=True)
    options = Column(JSON, nullable=False, default=dict)
    status = Column(String(20), nullable=False, default="recording", index=True)
    next_sequence = Column(Integer, nullable=False, default=0)
    size_bytes = Column(Integer, nullable=False, default=0)
    path = Column(String(500), nullable=False, default="")
    meeting_id = Column(Integer, ForeignKey("meetings.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now, onupdate=now)


class RecordingChunk(Base):
    __tablename__ = "recording_chunks"
    __table_args__ = (UniqueConstraint("recording_id", "sequence"),)

    recording_id = Column(String(64), ForeignKey("recordings.id", ondelete="CASCADE"), primary_key=True)
    sequence = Column(Integer, primary_key=True)
    digest = Column(String(64), nullable=False)
    size_bytes = Column(Integer, nullable=False)


class Task(Base):
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True)
    meeting_id = Column(Integer, ForeignKey("meetings.id"))
    title = Column(String(500), nullable=False)
    description = Column(Text, default="")
    assignee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    assignee_name = Column(String(200), default="")  # as heard in the meeting
    deadline = Column(Date, nullable=True)
    deadline_source = Column(String(20), default="none")  # stated | default | asked | manual | none
    deadline_quote = Column(String(500), default="")
    priority = Column(String(10), default="medium")
    source_quote = Column(Text, default="")
    source_time = Column(String(20), default="")
    status = Column(String(30), default="draft")  # draft | sent | awaiting_deadline | no_recipient
    bitrix_task_id = Column(String(40), default="")
    meeting = relationship("Meeting", back_populates="tasks")
    assignee = relationship("Employee")


class DeadlineRequest(Base):
    __tablename__ = "deadline_requests"
    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("tasks.id"))
    chat_id = Column(String(40))
    message_id = Column(Integer, nullable=True)
    asked_at = Column(DateTime(timezone=True), default=now)
    resolved = Column(Boolean, default=False)
    task = relationship("Task")


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"
    __table_args__ = (UniqueConstraint("chat_id", "message_id"),)
    id = Column(Integer, primary_key=True)
    chat_id = Column(String(40), nullable=False, index=True)
    message_id = Column(Integer, nullable=True)
    role = Column(String(20), nullable=False)
    status = Column(String(20), nullable=False, default="pending", index=True)
    text = Column(Text, nullable=False)
    meeting_id = Column(Integer, ForeignKey("meetings.id"), nullable=True)
    reply_to = Column(Integer, nullable=True)
    result = Column(JSON, nullable=True)
    action_applied = Column(Boolean, nullable=False, default=False)
    error = Column(Text, default="")
    created_at = Column(DateTime(timezone=True), default=now)


class ConversationEscalation(Base):
    __tablename__ = "conversation_escalations"
    id = Column(Integer, primary_key=True)
    meeting_id = Column(Integer, ForeignKey("meetings.id"), nullable=False, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    chat_id = Column(String(40), nullable=False)
    reason = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default="open")
    created_at = Column(DateTime(timezone=True), default=now)


class ProcessingCheckpoint(Base):
    __tablename__ = "processing_checkpoints"
    __table_args__ = (UniqueConstraint("meeting_id", "stage", "part", "fingerprint"),)
    id = Column(Integer, primary_key=True)
    meeting_id = Column(Integer, ForeignKey("meetings.id"), nullable=False, index=True)
    stage = Column(String(30), nullable=False)
    part = Column(String(100), nullable=False)
    fingerprint = Column(String(64), nullable=False)
    data = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), default=now)


class Delivery(Base):
    __tablename__ = "deliveries"
    __table_args__ = (UniqueConstraint("meeting_id", "key"),)
    id = Column(Integer, primary_key=True)
    meeting_id = Column(Integer, ForeignKey("meetings.id"), nullable=False, index=True)
    key = Column(String(160), nullable=False)
    phase = Column(String(10), nullable=False)
    kind = Column(String(30), nullable=False)
    chat_id = Column(String(40), nullable=False)
    payload = Column(JSON, nullable=False)
    document = Column(LargeBinary, nullable=True)
    task_ids = Column(JSON, nullable=False, default=list)
    depends_on = Column(Integer, ForeignKey("deliveries.id", ondelete="SET NULL"), nullable=True)
    status = Column(String(20), nullable=False, default="pending", index=True)
    attempts = Column(Integer, nullable=False, default=0)
    cycle_attempts = Column(Integer, nullable=False, default=0)
    message_id = Column(Integer, nullable=True)
    error = Column(Text, default="")
    next_attempt_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=now)
    sent_at = Column(DateTime(timezone=True), nullable=True)


def init_db():
    Base.metadata.create_all(engine)
    # lightweight migration for databases created by earlier versions
    from sqlalchemy import inspect, text
    cols = {c["name"] for c in inspect(engine).get_columns("meetings")}
    if "options" not in cols:
        with engine.begin() as con:
            con.execute(text("ALTER TABLE meetings ADD COLUMN options JSON"))
    delivery_cols = {c["name"] for c in inspect(engine).get_columns("deliveries")}
    if "cycle_attempts" not in delivery_cols:
        with engine.begin() as con:
            con.execute(text("ALTER TABLE deliveries ADD COLUMN cycle_attempts INTEGER NOT NULL DEFAULT 0"))


def dumps(v) -> str:
    return json.dumps(v, ensure_ascii=False)
