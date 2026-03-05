from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict
from uuid import uuid4

from ariadne.application.services import build_services
from ariadne.domain.errors import AriadneError, NotFoundError
from ariadne.domain.models import AssetStatus
from ariadne.infrastructure.app_logger import get_logger

logger = get_logger("api.facade")


class AriadneAPI:
    """API-like facade used by tests and by HTTP handler."""

    def __init__(self) -> None:
        services = build_services()
        self.config = services["config"]
        self.generation = services["generation"]
        self.courseware = services["courseware"]
        self.qa = services["qa"]
        self.chat = services["chat"]
        self.rewrite = services["rewrite"]
        self.assets = services["assets"]
        self.export = services["export"]
        self.retrieval_settings = services["retrieval_settings"]
        self.profile = services["profile"]
        self.monitoring = services["monitoring"]
        self.repos = services["repos"]

    def _trace_id(self) -> str:
        return f"tr_{uuid4().hex[:10]}"

    def _ok(self, data: Any) -> Dict[str, Any]:
        return {"code": 0, "message": "ok", "trace_id": self._trace_id(), "data": data}

    def _error(self, exc: AriadneError) -> Dict[str, Any]:
        logger.error("api error code=%s message=%s field=%s reason=%s", exc.code, exc.message, exc.field, exc.reason)
        payload: Dict[str, Any] = {"code": exc.code, "message": exc.message, "trace_id": self._trace_id()}
        if exc.field or exc.reason:
            payload["error"] = {"field": exc.field, "reason": exc.reason}
        return payload

    def generate_courseware(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            # MVP mode: advanced generation knobs are intentionally fixed.
            job, courseware = self.generation.generate(
                topic=payload.get("topic", ""),
                keywords=[],
                difficulty="beginner",
                style="engineering",
                template="tutorial",
            )
            return self._ok({"job_id": job.id, "courseware_id": courseware.id, "phase": job.phase.value})
        except AriadneError as exc:
            return self._error(exc)

    def get_progress(self, courseware_id: str) -> Dict[str, Any]:
        try:
            job = self.generation.progress(courseware_id)
            return self._ok(
                {
                    "courseware_id": courseware_id,
                    "phase": job.phase.value,
                    "progress": job.progress,
                    "chunk_total": job.chunk_total,
                    "chunk_done": job.chunk_done,
                    "chunk_failed": job.chunk_failed,
                    "completed_chunks": job.completed_chunks,
                    "outline": job.outline,
                    "error": job.error,
                    "events": [{"ts": event.ts, "phase": event.phase.value, "message": event.message} for event in job.events],
                }
            )
        except AriadneError as exc:
            return self._error(exc)

    def get_courseware(self, courseware_id: str) -> Dict[str, Any]:
        cw = self.repos["coursewares"].get(courseware_id)
        if not cw:
            return self._error(NotFoundError("resource not found"))
        return self._ok(
            {
                "id": cw.id,
                "topic": cw.topic,
                "status": cw.status,
                "difficulty": cw.difficulty,
                "style": cw.style,
                "template": cw.template,
                "current_version": cw.current_version,
                "knowledge_doc_path": cw.knowledge_doc_path,
                "knowledge_markdown": cw.knowledge_markdown,
                "chunks": [
                    {
                        "id": chunk.id,
                        "title": chunk.title,
                        "content": chunk.content,
                        "order_no": chunk.order_no,
                        "understand_state": getattr(chunk, "understand_state", "unknown"),
                        "is_favorite": getattr(chunk, "is_favorite", False),
                        "collapsed": getattr(chunk, "collapsed", False),
                    }
                    for chunk in cw.chunks
                ],
            }
        )

    def list_chunks(self, courseware_id: str, include_content: bool = True, only_favorite: bool = False) -> Dict[str, Any]:
        try:
            rows = self.courseware.list_chunks(courseware_id, include_content, only_favorite)
            return self._ok(rows)
        except AriadneError as exc:
            return self._error(exc)

    def update_chunk_state(self, chunk_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            row = self.courseware.update_chunk_state(chunk_id, payload)
            return self._ok(row)
        except AriadneError as exc:
            return self._error(exc)

    def ask_chunk(self, chunk_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            answer = self.qa.ask(
                chunk_id=chunk_id,
                question=payload.get("question", ""),
                page_id=payload.get("page_id", ""),
                selection=payload.get("selection"),
                mode=payload.get("mode", "deep"),
            )
            return self._ok(
                {
                    "answer_id": answer.id,
                    "answer": answer.answer,
                    "linked_chunk_id": answer.linked_chunk_id,
                    "next_suggestions": answer.next_suggestions,
                    "sources": [
                        {"title": src.title, "url": src.url, "domain": src.domain, "credibility": src.credibility}
                        for src in answer.sources
                    ],
                }
            )
        except AriadneError as exc:
            return self._error(exc)

    def append_chunk(self, chunk_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            answer = self.qa.get_answer(payload.get("answer_id", ""))
            result = self.courseware.append_answer(chunk_id, answer=answer, action=payload.get("action", "accept"))
            return self._ok(result)
        except AriadneError as exc:
            return self._error(exc)

    def upload_asset(self, file_name: str, size_bytes: int) -> Dict[str, Any]:
        try:
            asset = self.assets.upload(file_name=file_name, size_bytes=size_bytes)
            return self._ok({"asset_id": asset.id, "status": asset.status.value})
        except AriadneError as exc:
            return self._error(exc)

    def get_asset_status(self, asset_id: str) -> Dict[str, Any]:
        try:
            asset = self.assets.status(asset_id)
            return self._ok({"asset_id": asset.id, "status": asset.status.value, "progress": asset.progress, "error": asset.error})
        except AriadneError as exc:
            return self._error(exc)

    def export_courseware(self, courseware_id: str, fmt: str = "html") -> Dict[str, Any]:
        try:
            task = self.export.export_courseware(courseware_id=courseware_id, fmt=fmt)
            return self._ok({"task_id": task.id, "status": task.status, "download_url": task.download_url})
        except AriadneError as exc:
            return self._error(exc)

    def get_export(self, task_id: str) -> Dict[str, Any]:
        try:
            task = self.export.task(task_id)
            return self._ok({"task_id": task.id, "status": task.status, "download_url": task.download_url})
        except AriadneError as exc:
            return self._error(exc)

    def export_html_content(self, courseware_id: str) -> Dict[str, Any]:
        try:
            html_content = self.export.render_html(courseware_id)
            return self._ok({"html": html_content})
        except AriadneError as exc:
            return self._error(exc)

    def create_chat_session(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            session = self.chat.create_session(
                courseware_id=payload.get("courseware_id", ""),
                page_id=payload.get("page_id", ""),
                chunk_id=payload.get("chunk_id", ""),
            )
            return self._ok(asdict(session))
        except AriadneError as exc:
            return self._error(exc)

    def send_chat_message(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            result = self.chat.send_message(
                session_id=payload.get("session_id", ""),
                message=payload.get("message", ""),
                continue_from_message_id=payload.get("continue_from_message_id"),
            )
            return self._ok(result)
        except AriadneError as exc:
            return self._error(exc)

    def list_chat_sessions(self, courseware_id: str | None, page_id: str | None) -> Dict[str, Any]:
        try:
            sessions = [asdict(s) for s in self.chat.list_sessions(courseware_id=courseware_id, page_id=page_id)]
            return self._ok(sessions)
        except AriadneError as exc:
            return self._error(exc)

    def rewrite_draft(self, page_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            draft = self.rewrite.create_draft(
                page_id=page_id,
                chunk_id=payload.get("chunk_id", ""),
                instruction=payload.get("instruction", ""),
            )
            return self._ok({"draft_id": draft.id, "original": draft.original, "rewritten": draft.rewritten})
        except AriadneError as exc:
            return self._error(exc)

    def apply_draft(self, page_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            draft = self.rewrite.get_draft(payload.get("draft_id", ""))
            result = self.courseware.apply_rewrite(draft=draft, expected_version=int(payload.get("expected_version", 0)))
            return self._ok({"page_id": page_id, **result})
        except AriadneError as exc:
            return self._error(exc)

    def get_markdown(self, courseware_id: str) -> Dict[str, Any]:
        try:
            return self._ok(self.courseware.get_markdown(courseware_id))
        except AriadneError as exc:
            return self._error(exc)

    def put_markdown(self, courseware_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            result = self.courseware.update_markdown(courseware_id=courseware_id, markdown_text=payload.get("markdown", ""))
            return self._ok(result)
        except AriadneError as exc:
            return self._error(exc)

    def undo(self, page_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            result = self.courseware.undo_latest(page_id=page_id, expected_version=int(payload.get("expected_version", 0)))
            return self._ok({"page_id": page_id, **result})
        except AriadneError as exc:
            return self._error(exc)

    def get_retrieval_settings(self) -> Dict[str, Any]:
        return self._ok(asdict(self.retrieval_settings.get()))

    def put_retrieval_settings(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            settings = self.retrieval_settings.update(payload)
            return self._ok(asdict(settings))
        except AriadneError as exc:
            return self._error(exc)

    def get_profile(self) -> Dict[str, Any]:
        try:
            profile = self.profile.get_current()
            return self._ok(asdict(profile))
        except AriadneError as exc:
            return self._error(exc)

    def put_profile(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            profile = self.profile.update_current(payload)
            return self._ok(asdict(profile))
        except AriadneError as exc:
            return self._error(exc)

    def get_logs(self, event_type: str | None = None) -> Dict[str, Any]:
        return self._ok(self.monitoring.logs(event_type=event_type))

    def get_metrics(self) -> Dict[str, Any]:
        return self._ok(self.monitoring.performance())

    def health_live(self) -> Dict[str, Any]:
        return self._ok({"status": "up"})

    def health_ready(self) -> Dict[str, Any]:
        return self._ok({"status": "up", "dependencies": {"db": "up", "index": "up", "llm": "up"}})

    def is_guest_mode_available(self) -> bool:
        return self.config.guest_mode

    def is_asset_ready(self, asset_id: str) -> bool:
        asset = self.repos["assets"].get(asset_id)
        if not asset:
            return False
        return asset.status == AssetStatus.READY
