from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

from ariadne.application.knowledge import chunks_to_markdown, markdown_to_html
from ariadne.domain.models import (
    Answer,
    Asset,
    AssetStatus,
    ChatMessage,
    ChatSession,
    Chunk,
    Courseware,
    ExportTask,
    GenerationJob,
    LearnerProfile,
    RewriteDraft,
)


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def _atomic_write_json(path: Path, payload: object) -> None:
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def _read_json(path: Path, default: object):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


class InMemoryCoursewareRepo:
    def __init__(self) -> None:
        self._items: Dict[str, Courseware] = {}

    def save(self, courseware: Courseware) -> None:
        self._items[courseware.id] = courseware

    def get(self, courseware_id: str) -> Courseware | None:
        return self._items.get(courseware_id)

    def list_all(self) -> List[Courseware]:
        return list(self._items.values())


class FileCoursewareRepo:
    def __init__(self, base_dir: str = "storage/coursewares", index_path: str = "storage/indexes/coursewares.json") -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = Path(index_path)
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self._items: Dict[str, Courseware] = {}

    def _cw_dir(self, courseware_id: str) -> Path:
        return self.base_dir / courseware_id

    def _page_meta_path(self, courseware_id: str, page_id: str) -> Path:
        return self._cw_dir(courseware_id) / "pages" / page_id / "meta.json"

    def _page_html_path(self, courseware_id: str, page_id: str) -> Path:
        return self._cw_dir(courseware_id) / "pages" / page_id / "html.html"

    def save(self, courseware: Courseware) -> None:
        cw_dir = self._cw_dir(courseware.id)
        cw_dir.mkdir(parents=True, exist_ok=True)
        chunks_dir = cw_dir / "chunks"
        chunks_dir.mkdir(parents=True, exist_ok=True)

        markdown_text = courseware.knowledge_markdown or chunks_to_markdown(topic=courseware.topic, chunks=courseware.chunks)
        html_text = markdown_to_html(markdown_text)

        meta = {
            "id": courseware.id,
            "topic": courseware.topic,
            "status": courseware.status,
            "current_version": courseware.current_version,
            "source_asset_ids": list(courseware.source_asset_ids or []),
            "default_page_id": "pg_generated",
            "markdown_path": str(cw_dir / "markdown.md"),
            "html_path": str(cw_dir / "html.html"),
            "created_at": courseware.created_at,
            "updated_at": courseware.created_at,
        }
        _atomic_write_json(cw_dir / "meta.json", meta)
        _atomic_write_json(cw_dir / "outline.json", getattr(courseware, "outline", []))
        _atomic_write_text(cw_dir / "markdown.md", markdown_text)
        _atomic_write_text(cw_dir / "html.html", html_text)

        existing_chunk_files = {p.name for p in chunks_dir.glob("*.json")}
        current_chunk_files = set()
        for chunk in sorted(courseware.chunks, key=lambda x: x.order_no):
            payload = {
                "id": chunk.id,
                "courseware_id": courseware.id,
                "page_id": "pg_generated",
                "chapter_no": self._extract_chapter_no(chunk.order_no),
                "chunk_no": self._extract_chunk_no(chunk.order_no),
                "order_no": chunk.order_no,
                "title": chunk.title,
                "content": chunk.content,
                "understand_state": getattr(chunk, "understand_state", "unknown"),
                "is_favorite": getattr(chunk, "is_favorite", False),
                "collapsed": getattr(chunk, "collapsed", False),
                "created_at": courseware.created_at,
                "updated_at": courseware.created_at,
            }
            chunk_file = f"{chunk.id}.json"
            current_chunk_files.add(chunk_file)
            _atomic_write_json(chunks_dir / chunk_file, payload)
        for orphan in existing_chunk_files - current_chunk_files:
            (chunks_dir / orphan).unlink(missing_ok=True)

        self._write_page(courseware.id, "pg_generated", "generated_shell", "默认课件页面", html_text)
        self._write_page(courseware.id, "pg_knowledge_shell", "knowledge_shell", "知识页", html_text)
        self._update_index(courseware, str(cw_dir / "meta.json"))
        courseware.knowledge_markdown = markdown_text
        courseware.knowledge_doc_path = str(cw_dir / "markdown.md")
        self._items[courseware.id] = courseware

    def _extract_chapter_no(self, order_no: int) -> int:
        return max(1, (order_no // 1000) or 1)

    def _extract_chunk_no(self, order_no: int) -> int:
        return order_no

    def _write_page(self, courseware_id: str, page_id: str, page_type: str, title: str, html_text: str) -> None:
        meta_path = self._page_meta_path(courseware_id, page_id)
        html_path = self._page_html_path(courseware_id, page_id)
        _atomic_write_json(meta_path, {
            "id": page_id,
            "courseware_id": courseware_id,
            "page_type": page_type,
            "title": title,
            "html_path": str(html_path),
            "render_config": {},
        })
        _atomic_write_text(html_path, html_text)

    def _update_index(self, courseware: Courseware, meta_path: str) -> None:
        index = _read_json(self.index_path, [])
        index = [row for row in index if row.get("id") != courseware.id]
        index.append({
            "id": courseware.id,
            "topic": courseware.topic,
            "status": courseware.status,
            "updated_at": courseware.created_at,
            "path": meta_path,
        })
        index.sort(key=lambda x: x.get("id", ""))
        _atomic_write_json(self.index_path, index)

    def get(self, courseware_id: str) -> Courseware | None:
        cached = self._items.get(courseware_id)
        if cached:
            return cached
        cw_dir = self._cw_dir(courseware_id)
        meta = _read_json(cw_dir / "meta.json", None)
        if not meta:
            return None
        markdown_text = (cw_dir / "markdown.md").read_text(encoding="utf-8") if (cw_dir / "markdown.md").exists() else ""
        chunks = []
        for path in sorted((cw_dir / "chunks").glob("*.json")):
            data = _read_json(path, None)
            if not data:
                continue
            chunk = Chunk(
                id=data["id"],
                title=data.get("title", ""),
                content=data.get("content", ""),
                order_no=int(data.get("order_no", 0)),
            )
            chunk.understand_state = data.get("understand_state", "unknown")
            chunk.is_favorite = bool(data.get("is_favorite", False))
            chunk.collapsed = bool(data.get("collapsed", False))
            chunks.append(chunk)
        cw = Courseware(
            id=meta["id"],
            topic=meta.get("topic", courseware_id),
            created_at=meta.get("created_at", ""),
            status=meta.get("status", "ready"),
            current_version=int(meta.get("current_version", 1)),
            knowledge_markdown=markdown_text,
            knowledge_doc_path=meta.get("markdown_path", str(cw_dir / "markdown.md")),
            chunks=sorted(chunks, key=lambda x: x.order_no),
            source_asset_ids=list(meta.get("source_asset_ids", [])),
        )
        cw.outline = _read_json(cw_dir / "outline.json", [])
        self._items[courseware_id] = cw
        return cw

    def list_all(self) -> List[Courseware]:
        coursewares = []
        ids = [row.get("id", "") for row in _read_json(self.index_path, [])]
        if not ids:
            ids = [p.name for p in self.base_dir.iterdir() if p.is_dir()]
        for courseware_id in ids:
            cw = self.get(courseware_id)
            if cw:
                coursewares.append(cw)
        return coursewares


class InMemoryJobRepo:
    def __init__(self) -> None:
        self._items: Dict[str, GenerationJob] = {}

    def save(self, job: GenerationJob) -> None:
        self._items[job.id] = job

    def get(self, job_id: str) -> GenerationJob | None:
        return self._items.get(job_id)

    def get_by_courseware(self, courseware_id: str) -> GenerationJob | None:
        for job in reversed(list(self._items.values())):
            if job.courseware_id == courseware_id:
                return job
        return None


class InMemoryAnswerRepo:
    def __init__(self) -> None:
        self._items: Dict[str, Answer] = {}

    def save(self, answer: Answer) -> None:
        self._items[answer.id] = answer

    def get(self, answer_id: str) -> Answer | None:
        return self._items.get(answer_id)


class InMemoryAssetRepo:
    """Persistent asset repository that saves to JSON file."""

    def __init__(self, storage_path: str = "storage/assets/assets.json") -> None:
        self._storage_path = Path(storage_path)
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._items: Dict[str, Asset] = {}
        self._load()

    def _load(self) -> None:
        if self._storage_path.exists():
            try:
                data = json.loads(self._storage_path.read_text(encoding="utf-8"))
                for asset_id, asset_data in data.items():
                    if "status" in asset_data and isinstance(asset_data["status"], str):
                        asset_data["status"] = AssetStatus(asset_data["status"])
                    asset_data.setdefault("progress", 0)
                    asset_data.setdefault("error", None)
                    asset_data.setdefault("storage_path", None)
                    asset_data.setdefault("content_preview", None)
                    asset_data.setdefault("chunk_count", 0)
                    self._items[asset_id] = Asset(**asset_data)
            except Exception:
                pass

    def _save(self) -> None:
        data = {}
        for asset_id, asset in self._items.items():
            data[asset_id] = {
                "id": asset.id,
                "file_name": asset.file_name,
                "file_type": asset.file_type,
                "size_bytes": asset.size_bytes,
                "status": asset.status.value,
                "progress": asset.progress,
                "error": asset.error,
                "storage_path": asset.storage_path,
                "content_preview": asset.content_preview,
                "chunk_count": asset.chunk_count,
            }
        _atomic_write_json(self._storage_path, data)

    def save(self, asset: Asset) -> None:
        self._items[asset.id] = asset
        self._save()

    def get(self, asset_id: str) -> Asset | None:
        return self._items.get(asset_id)

    def list_all(self) -> List[Asset]:
        return list(self._items.values())


class InMemoryExportRepo:
    def __init__(self) -> None:
        self._items: Dict[str, ExportTask] = {}

    def save(self, task: ExportTask) -> None:
        self._items[task.id] = task

    def get(self, task_id: str) -> ExportTask | None:
        return self._items.get(task_id)


class InMemoryChatSessionRepo:
    def __init__(self) -> None:
        self._items: Dict[str, ChatSession] = {}

    def save(self, session: ChatSession) -> None:
        self._items[session.id] = session

    def get(self, session_id: str) -> ChatSession | None:
        return self._items.get(session_id)

    def list(self, courseware_id: str | None = None, page_id: str | None = None) -> List[ChatSession]:
        items = list(self._items.values())
        if courseware_id:
            items = [it for it in items if it.courseware_id == courseware_id]
        if page_id:
            items = [it for it in items if it.page_id == page_id]
        return sorted(items, key=lambda x: x.last_active_at, reverse=True)


class FileChatSessionRepo:
    def __init__(self, base_dir: str = "storage/coursewares", index_path: str = "storage/indexes/chat_sessions.json") -> None:
        self.base_dir = Path(base_dir)
        self.index_path = Path(index_path)
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self._items: Dict[str, ChatSession] = {}

    def _session_path(self, courseware_id: str, session_id: str) -> Path:
        return self.base_dir / courseware_id / "chats" / f"{session_id}.json"

    def save(self, session: ChatSession) -> None:
        session_file = self._session_path(session.courseware_id, session.id)
        payload = _read_json(session_file, None) or {"messages": []}
        payload.update({
            "id": session.id,
            "courseware_id": session.courseware_id,
            "page_id": session.page_id,
            "chunk_id": session.chunk_id,
            "created_at": session.created_at,
            "last_active_at": session.last_active_at,
        })
        payload.setdefault("messages", [])
        _atomic_write_json(session_file, payload)
        self._items[session.id] = session
        self._update_index(session)

    def _update_index(self, session: ChatSession) -> None:
        index = _read_json(self.index_path, [])
        index = [row for row in index if row.get("id") != session.id]
        index.append({
            "id": session.id,
            "courseware_id": session.courseware_id,
            "page_id": session.page_id,
            "chunk_id": session.chunk_id,
            "created_at": session.created_at,
            "last_active_at": session.last_active_at,
            "path": str(self._session_path(session.courseware_id, session.id)),
        })
        index.sort(key=lambda x: x.get("last_active_at", ""), reverse=True)
        _atomic_write_json(self.index_path, index)

    def get(self, session_id: str) -> ChatSession | None:
        cached = self._items.get(session_id)
        if cached:
            return cached
        row = next((row for row in _read_json(self.index_path, []) if row.get("id") == session_id), None)
        if not row:
            return None
        path = Path(row["path"])
        data = _read_json(path, None)
        if not data:
            return None
        session = ChatSession(
            id=data["id"],
            courseware_id=data.get("courseware_id", ""),
            page_id=data.get("page_id", ""),
            chunk_id=data.get("chunk_id", ""),
            created_at=data.get("created_at", ""),
            last_active_at=data.get("last_active_at", ""),
        )
        self._items[session.id] = session
        return session

    def list(self, courseware_id: str | None = None, page_id: str | None = None) -> List[ChatSession]:
        items = []
        for row in _read_json(self.index_path, []):
            if courseware_id and row.get("courseware_id") != courseware_id:
                continue
            if page_id and row.get("page_id") != page_id:
                continue
            session = self.get(row.get("id", ""))
            if session:
                items.append(session)
        return sorted(items, key=lambda x: x.last_active_at, reverse=True)

    def append_message(self, session_id: str, message: ChatMessage) -> None:
        session = self.get(session_id)
        if not session:
            raise KeyError(session_id)
        path = self._session_path(session.courseware_id, session_id)
        data = _read_json(path, None)
        if not data:
            raise KeyError(session_id)
        messages = data.setdefault("messages", [])
        messages.append({
            "id": message.id,
            "session_id": message.session_id,
            "role": message.role,
            "content": message.content,
            "created_at": message.created_at,
        })
        _atomic_write_json(path, data)

    def list_messages(self, session_id: str) -> List[ChatMessage]:
        session = self.get(session_id)
        if not session:
            return []
        data = _read_json(self._session_path(session.courseware_id, session_id), None) or {}
        items = []
        for row in data.get("messages", []):
            items.append(ChatMessage(
                id=row["id"],
                session_id=row.get("session_id", session_id),
                role=row.get("role", "user"),
                content=row.get("content", ""),
                created_at=row.get("created_at", ""),
            ))
        return sorted(items, key=lambda x: x.created_at)


class InMemoryChatMessageRepo:
    def __init__(self) -> None:
        self._items: Dict[str, ChatMessage] = {}

    def save(self, message: ChatMessage) -> None:
        self._items[message.id] = message

    def list_by_session(self, session_id: str) -> List[ChatMessage]:
        return sorted([m for m in self._items.values() if m.session_id == session_id], key=lambda x: x.created_at)


class FileChatMessageRepo:
    def __init__(self, sessions: FileChatSessionRepo) -> None:
        self.sessions = sessions
        self._items: Dict[str, ChatMessage] = {}

    def save(self, message: ChatMessage) -> None:
        self.sessions.append_message(message.session_id, message)
        self._items[message.id] = message

    def list_by_session(self, session_id: str) -> List[ChatMessage]:
        return self.sessions.list_messages(session_id)


class InMemoryDraftRepo:
    def __init__(self) -> None:
        self._items: Dict[str, RewriteDraft] = {}

    def save(self, draft: RewriteDraft) -> None:
        self._items[draft.id] = draft

    def get(self, draft_id: str) -> RewriteDraft | None:
        return self._items.get(draft_id)


class InMemoryProfileRepo:
    def __init__(self) -> None:
        self._current: LearnerProfile | None = None

    def set_current(self, profile: LearnerProfile) -> None:
        self._current = profile

    def get_current(self) -> LearnerProfile | None:
        return self._current
