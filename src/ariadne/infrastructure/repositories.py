from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

from ariadne.domain.models import (
    Answer,
    Asset,
    AssetStatus,
    ChatMessage,
    ChatSession,
    Courseware,
    ExportTask,
    GenerationJob,
    LearnerProfile,
    RewriteDraft,
)


class InMemoryCoursewareRepo:
    def __init__(self) -> None:
        self._items: Dict[str, Courseware] = {}

    def save(self, courseware: Courseware) -> None:
        self._items[courseware.id] = courseware

    def get(self, courseware_id: str) -> Courseware | None:
        return self._items.get(courseware_id)


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
        """Load assets from JSON file."""
        if self._storage_path.exists():
            try:
                data = json.loads(self._storage_path.read_text(encoding="utf-8"))
                for asset_id, asset_data in data.items():
                    # Convert status string back to enum
                    if "status" in asset_data and isinstance(asset_data["status"], str):
                        asset_data["status"] = AssetStatus(asset_data["status"])
                    # Ensure all required fields have values
                    asset_data.setdefault("progress", 0)
                    asset_data.setdefault("error", None)
                    asset_data.setdefault("storage_path", None)
                    asset_data.setdefault("content_preview", None)
                    asset_data.setdefault("chunk_count", 0)
                    self._items[asset_id] = Asset(**asset_data)
            except Exception as exc:
                # Start fresh if load fails
                pass

    def _save(self) -> None:
        """Save assets to JSON file."""
        data = {}
        for asset_id, asset in self._items.items():
            asset_dict = {
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
            data[asset_id] = asset_dict
        self._storage_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def save(self, asset: Asset) -> None:
        self._items[asset.id] = asset
        self._save()

    def get(self, asset_id: str) -> Asset | None:
        return self._items.get(asset_id)

    def list_all(self) -> List[Asset]:
        """List all assets."""
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


class InMemoryChatMessageRepo:
    def __init__(self) -> None:
        self._items: Dict[str, ChatMessage] = {}

    def save(self, message: ChatMessage) -> None:
        self._items[message.id] = message

    def list_by_session(self, session_id: str) -> List[ChatMessage]:
        return sorted([m for m in self._items.values() if m.session_id == session_id], key=lambda x: x.created_at)


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
