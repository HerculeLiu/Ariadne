from __future__ import annotations

from typing import Dict, List

from ariadne.domain.models import (
    Answer,
    Asset,
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
    def __init__(self) -> None:
        self._items: Dict[str, Asset] = {}

    def save(self, asset: Asset) -> None:
        self._items[asset.id] = asset

    def get(self, asset_id: str) -> Asset | None:
        return self._items.get(asset_id)


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
