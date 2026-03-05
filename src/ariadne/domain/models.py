from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional


class JobPhase(str, Enum):
    QUEUED = "queued"
    RETRIEVING = "retrieving"
    OUTLINE = "outline"
    CHUNK_GENERATING = "chunk_generating"
    GENERATING = "generating"
    ASSEMBLING = "assembling"
    DONE = "done"
    FAILED = "failed"


class AssetStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


@dataclass
class JobEvent:
    ts: str
    phase: JobPhase
    message: str


@dataclass
class Chunk:
    id: str
    title: str
    content: str
    order_no: int


@dataclass
class Courseware:
    id: str
    topic: str
    difficulty: str
    style: str
    template: str
    created_at: str
    status: str = "ready"
    current_version: int = 1
    knowledge_markdown: str = ""
    knowledge_doc_path: str = ""
    chunks: List[Chunk] = field(default_factory=list)


@dataclass
class GenerationJob:
    id: str
    courseware_id: str
    phase: JobPhase
    progress: int = 0
    retry_count: int = 0
    chunk_total: int = 0
    chunk_done: int = 0
    chunk_failed: int = 0
    outline: List[Dict[str, object]] = field(default_factory=list)
    completed_chunks: List[str] = field(default_factory=list)
    error: str = ""
    events: List[JobEvent] = field(default_factory=list)


@dataclass
class Source:
    title: str
    url: str
    domain: str
    credibility: str = "medium"


@dataclass
class Answer:
    id: str
    chunk_id: str
    answer: str
    linked_chunk_id: str
    next_suggestions: List[str] = field(default_factory=list)
    sources: List[Source] = field(default_factory=list)


@dataclass
class Asset:
    id: str
    file_name: str
    file_type: str
    size_bytes: int
    status: AssetStatus
    progress: int = 0
    error: Optional[str] = None


@dataclass
class ExportTask:
    id: str
    courseware_id: str
    format: str
    status: str
    download_url: Optional[str] = None


@dataclass
class ChatSession:
    id: str
    courseware_id: str
    page_id: str
    chunk_id: str
    created_at: str
    last_active_at: str


@dataclass
class ChatMessage:
    id: str
    session_id: str
    role: str
    content: str
    created_at: str


@dataclass
class RewriteDraft:
    id: str
    page_id: str
    chunk_id: str
    original: str
    rewritten: str
    status: str
    created_at: str


@dataclass
class LearnerProfile:
    goal: str
    background: str
    analogy_preference: str
    mastered_topics: List[str] = field(default_factory=list)
    local_only: bool = True


@dataclass
class RetrievalSettings:
    web_enabled: bool = True
    source_weight: Dict[str, float] = field(default_factory=lambda: {"doc": 0.4, "blog": 0.2, "paper": 0.4})
    domain_whitelist: List[str] = field(default_factory=list)
    domain_blacklist: List[str] = field(default_factory=list)


@dataclass
class EventLog:
    id: str
    event_type: str
    payload: Dict[str, object]
    created_at: str


def utc_now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
