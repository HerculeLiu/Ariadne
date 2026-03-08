from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Dict, Iterable, List

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
    JobEvent,
    JobPhase,
    LearnerProfile,
    Page,
    RewriteDraft,
    SearchResult,
    SearchRun,
    utc_now_iso,
)


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def _atomic_write_json(path: Path, payload: object) -> None:
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _remove_path(path: Path) -> None:
    if path.is_dir():
        for child in path.iterdir():
            _remove_path(child)
        path.rmdir()
    elif path.exists():
        path.unlink()


def _ensure_index(index_path: Path) -> None:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    if not index_path.exists():
        _atomic_write_json(index_path, [])


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
    def __init__(
        self,
        base_dir: str = "storage/coursewares",
        index_path: str = "storage/indexes/coursewares.json",
    ) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = Path(index_path)
        _ensure_index(self.index_path)
        self._items: Dict[str, Courseware] = {}

    def _courseware_dir(self, courseware_id: str) -> Path:
        return self.base_dir / courseware_id

    def _meta_path(self, courseware_id: str) -> Path:
        return self._courseware_dir(courseware_id) / "meta.json"

    def _outline_path(self, courseware_id: str) -> Path:
        return self._courseware_dir(courseware_id) / "outline.json"

    def _markdown_path(self, courseware_id: str) -> Path:
        return self._courseware_dir(courseware_id) / "markdown.md"

    def _html_path(self, courseware_id: str) -> Path:
        return self._courseware_dir(courseware_id) / "html.html"

    def _chunks_dir(self, courseware_id: str) -> Path:
        return self._courseware_dir(courseware_id) / "chunks"

    def _pages_dir(self, courseware_id: str) -> Path:
        return self._courseware_dir(courseware_id) / "pages"

    def _page_dir(self, courseware_id: str, page_id: str) -> Path:
        return self._pages_dir(courseware_id) / page_id

    def _page_meta_path(self, courseware_id: str, page_id: str) -> Path:
        return self._page_dir(courseware_id, page_id) / "meta.json"

    def _page_html_path(self, courseware_id: str, page_id: str) -> Path:
        return self._page_dir(courseware_id, page_id) / "html.html"

    def _snapshots_dir(self, courseware_id: str) -> Path:
        return self._courseware_dir(courseware_id) / "snapshots"

    def _load_index(self) -> List[dict]:
        return _read_json(self.index_path, [])

    def _write_index(self, items: List[dict]) -> None:
        _atomic_write_json(self.index_path, items)

    def _update_index(self, courseware: Courseware) -> None:
        rows = [row for row in self._load_index() if row.get("id") != courseware.id]
        rows.append(
            {
                "id": courseware.id,
                "topic": courseware.topic,
                "status": courseware.status,
                "updated_at": courseware.updated_at if hasattr(courseware, "updated_at") else courseware.created_at,
                "path": str(self._meta_path(courseware.id)),
            }
        )
        rows.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
        self._write_index(rows)

    def _chunk_to_payload(self, courseware: Courseware, chunk: Chunk) -> dict:
        return {
            "id": chunk.id,
            "courseware_id": courseware.id,
            "page_id": chunk.page_id or courseware.default_page_id,
            "chapter_no": chunk.chapter_no,
            "chunk_no": chunk.chunk_no,
            "order_no": chunk.order_no,
            "title": chunk.title,
            "content": chunk.content,
            "understand_state": chunk.understand_state,
            "is_favorite": chunk.is_favorite,
            "collapsed": chunk.collapsed,
            "created_at": chunk.created_at or courseware.created_at,
            "updated_at": chunk.updated_at or courseware.created_at,
        }

    def _payload_to_chunk(self, payload: dict) -> Chunk:
        return Chunk(
            id=payload["id"],
            title=payload.get("title", ""),
            content=payload.get("content", ""),
            order_no=int(payload.get("order_no", 0)),
            chapter_no=int(payload.get("chapter_no", 0)),
            chunk_no=int(payload.get("chunk_no", 0)),
            page_id=payload.get("page_id", ""),
            understand_state=payload.get("understand_state", "unknown"),
            is_favorite=bool(payload.get("is_favorite", False)),
            collapsed=bool(payload.get("collapsed", False)),
            created_at=payload.get("created_at", ""),
            updated_at=payload.get("updated_at", ""),
        )

    def _write_page(self, page: Page, html_text: str) -> None:
        _atomic_write_json(self._page_meta_path(page.courseware_id, page.id), asdict(page))
        _atomic_write_text(self._page_html_path(page.courseware_id, page.id), html_text)

    def save(self, courseware: Courseware) -> None:
        cw_dir = self._courseware_dir(courseware.id)
        cw_dir.mkdir(parents=True, exist_ok=True)
        self._chunks_dir(courseware.id).mkdir(parents=True, exist_ok=True)
        self._pages_dir(courseware.id).mkdir(parents=True, exist_ok=True)
        self._snapshots_dir(courseware.id).mkdir(parents=True, exist_ok=True)

        markdown_text = courseware.knowledge_markdown or chunks_to_markdown(topic=courseware.topic, chunks=courseware.chunks)
        courseware.knowledge_markdown = markdown_text
        courseware.knowledge_doc_path = str(self._markdown_path(courseware.id))

        # 生成 HTML，失败时明确标记不可用
        html_text = ""
        html_available = False
        if courseware.chunks:
            try:
                html_text = markdown_to_html(markdown_text)
                html_available = bool(html_text.strip())
            except Exception as e:
                from ariadne.infrastructure.app_logger import get_logger
                logger = get_logger("repo.courseware")
                logger.warning("Failed to generate HTML for courseware %s: %s", courseware.id, e)
                html_available = False

        # 只有 HTML 真正生成时才记录路径
        if html_available:
            courseware.knowledge_html_path = str(self._html_path(courseware.id))
        else:
            courseware.knowledge_html_path = None

        meta = {
            "id": courseware.id,
            "topic": courseware.topic,
            "created_at": courseware.created_at,
            "status": courseware.status,
            "current_version": courseware.current_version,
            "source_asset_ids": list(courseware.source_asset_ids),
            "source_search_run_id": courseware.source_search_run_id,
            "source_search_result_ids": list(courseware.source_search_result_ids),
            "default_page_id": courseware.default_page_id,
            "knowledge_doc_path": courseware.knowledge_doc_path,
            "knowledge_html_path": courseware.knowledge_html_path,
            "html_available": html_available,  # 新增字段，明确标记 HTML 是否可用
        }
        _atomic_write_json(self._meta_path(courseware.id), meta)
        _atomic_write_json(self._outline_path(courseware.id), courseware.outline)
        _atomic_write_text(self._markdown_path(courseware.id), markdown_text)
        if html_available:
            _atomic_write_text(self._html_path(courseware.id), html_text)

        chunks_dir = self._chunks_dir(courseware.id)
        existing = {p.name for p in chunks_dir.glob("*.json")}
        current = set()
        for chunk in sorted(courseware.chunks, key=lambda x: x.order_no):
            if not chunk.page_id:
                chunk.page_id = courseware.default_page_id
            payload = self._chunk_to_payload(courseware, chunk)
            chunk_path = chunks_dir / f"{chunk.id}.json"
            _atomic_write_json(chunk_path, payload)
            current.add(chunk_path.name)
        for stale in existing - current:
            (chunks_dir / stale).unlink(missing_ok=True)

        if html_text:
            for page in self.default_pages(courseware, html_text):
                self._write_page(page, html_text)

        self._update_index(courseware)
        self._items[courseware.id] = courseware

    def create_snapshot(self, courseware: Courseware, reason: str) -> str:
        snap_id = f"v{int(courseware.current_version):03d}"
        snap_dir = self._snapshots_dir(courseware.id) / snap_id
        snap_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(
            snap_dir / "meta.json",
            {
                "id": snap_id,
                "courseware_id": courseware.id,
                "version": courseware.current_version,
                "reason": reason,
                "created_at": courseware.created_at,
            },
        )
        _atomic_write_text(snap_dir / "markdown.md", courseware.knowledge_markdown or "")
        try:
            snapshot_html = markdown_to_html(courseware.knowledge_markdown or "")
        except Exception:
            snapshot_html = ""
        if snapshot_html:
            _atomic_write_text(snap_dir / "html.html", snapshot_html)
        snap_chunks_dir = snap_dir / "chunks"
        snap_chunks_dir.mkdir(parents=True, exist_ok=True)
        for chunk in sorted(courseware.chunks, key=lambda x: x.order_no):
            _atomic_write_json(snap_chunks_dir / f"{chunk.id}.json", self._chunk_to_payload(courseware, chunk))
        return snap_id

    def restore_snapshot(self, courseware_id: str, version: int) -> Courseware | None:
        snap_dir = self._snapshots_dir(courseware_id) / f"v{int(version):03d}"
        meta = _read_json(snap_dir / "meta.json", None)
        if not meta:
            return None
        courseware = self.get(courseware_id)
        if not courseware:
            return None
        courseware.current_version = int(meta.get("version", version))
        courseware.knowledge_markdown = (snap_dir / "markdown.md").read_text(encoding="utf-8")
        courseware.chunks = []
        for path in sorted((snap_dir / "chunks").glob("*.json")):
            payload = _read_json(path, None)
            if payload:
                courseware.chunks.append(self._payload_to_chunk(payload))
        courseware.chunks.sort(key=lambda x: x.order_no)
        self.save(courseware)
        return courseware

    def default_pages(self, courseware: Courseware, html_text: str | None = None) -> List[Page]:
        html_path = str(self._html_path(courseware.id))
        now = courseware.created_at
        return [
            Page(
                id="pg_generated",
                courseware_id=courseware.id,
                page_type="generated_shell",
                title="默认课件页面",
                html_path=html_path,
                render_config={},
                created_at=now,
                updated_at=now,
            ),
            Page(
                id="pg_knowledge_shell",
                courseware_id=courseware.id,
                page_type="knowledge_shell",
                title="知识页",
                html_path=html_path,
                render_config={},
                created_at=now,
                updated_at=now,
            ),
        ]

    def get(self, courseware_id: str) -> Courseware | None:
        cached = self._items.get(courseware_id)
        if cached:
            return cached
        meta = _read_json(self._meta_path(courseware_id), None)
        if not meta:
            return None
        courseware = Courseware(
            id=meta["id"],
            topic=meta.get("topic", courseware_id),
            created_at=meta.get("created_at", ""),
            status=meta.get("status", "ready"),
            current_version=int(meta.get("current_version", 1)),
            knowledge_markdown=self._markdown_path(courseware_id).read_text(encoding="utf-8") if self._markdown_path(courseware_id).exists() else "",
            knowledge_doc_path=meta.get("knowledge_doc_path", str(self._markdown_path(courseware_id))),
            chunks=[],
            source_asset_ids=list(meta.get("source_asset_ids", [])),
            source_search_run_id=meta.get("source_search_run_id", ""),
            source_search_result_ids=list(meta.get("source_search_result_ids", [])),
            outline=_read_json(self._outline_path(courseware_id), []),
            default_page_id=meta.get("default_page_id", "pg_generated"),
            knowledge_html_path=meta.get("knowledge_html_path") or "",
        )
        for path in sorted(self._chunks_dir(courseware_id).glob("*.json")):
            payload = _read_json(path, None)
            if payload:
                courseware.chunks.append(self._payload_to_chunk(payload))
        courseware.chunks.sort(key=lambda x: x.order_no)
        self._items[courseware_id] = courseware
        return courseware

    def list_all(self) -> List[Courseware]:
        rows = self._load_index()
        if not rows:
            ids = [p.name for p in self.base_dir.iterdir() if p.is_dir()]
            return [cw for cw in (self.get(courseware_id) for courseware_id in ids) if cw]
        return [cw for cw in (self.get(row.get("id", "")) for row in rows) if cw]

    def get_page(self, courseware_id: str, page_id: str) -> Page | None:
        payload = _read_json(self._page_meta_path(courseware_id, page_id), None)
        if not payload:
            return None
        return Page(**payload)


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


class FileJobRepo:
    def __init__(self, base_dir: str = "storage/jobs", index_path: str = "storage/indexes/jobs.json") -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = Path(index_path)
        _ensure_index(self.index_path)
        self._items: Dict[str, GenerationJob] = {}

    def _job_path(self, job_id: str) -> Path:
        return self.base_dir / f"{job_id}.json"

    def save(self, job: GenerationJob) -> None:
        payload = {
            "id": job.id,
            "courseware_id": job.courseware_id,
            "phase": job.phase.value,
            "progress": job.progress,
            "retry_count": job.retry_count,
            "chunk_total": job.chunk_total,
            "chunk_done": job.chunk_done,
            "chunk_failed": job.chunk_failed,
            "outline": job.outline,
            "completed_chunks": job.completed_chunks,
            "error": job.error,
            "events": [{"ts": e.ts, "phase": e.phase.value, "message": e.message} for e in job.events],
        }
        _atomic_write_json(self._job_path(job.id), payload)
        rows = [row for row in _read_json(self.index_path, []) if row.get("id") != job.id]
        rows.append({"id": job.id, "courseware_id": job.courseware_id, "path": str(self._job_path(job.id))})
        _atomic_write_json(self.index_path, rows)
        self._items[job.id] = job

    def _payload_to_job(self, payload: dict) -> GenerationJob:
        return GenerationJob(
            id=payload["id"],
            courseware_id=payload["courseware_id"],
            phase=JobPhase(payload.get("phase", "queued")),
            progress=int(payload.get("progress", 0)),
            retry_count=int(payload.get("retry_count", 0)),
            chunk_total=int(payload.get("chunk_total", 0)),
            chunk_done=int(payload.get("chunk_done", 0)),
            chunk_failed=int(payload.get("chunk_failed", 0)),
            outline=list(payload.get("outline", [])),
            completed_chunks=list(payload.get("completed_chunks", [])),
            error=payload.get("error", ""),
            events=[
                JobEvent(ts=row.get("ts", ""), phase=JobPhase(row.get("phase", "queued")), message=row.get("message", ""))
                for row in payload.get("events", [])
            ],
        )

    def get(self, job_id: str) -> GenerationJob | None:
        cached = self._items.get(job_id)
        if cached:
            return cached
        payload = _read_json(self._job_path(job_id), None)
        if not payload:
            return None
        job = self._payload_to_job(payload)
        self._items[job.id] = job
        return job

    def get_by_courseware(self, courseware_id: str) -> GenerationJob | None:
        rows = _read_json(self.index_path, [])
        for row in reversed(rows):
            if row.get("courseware_id") == courseware_id:
                return self.get(row.get("id", ""))
        return None


class InMemoryAnswerRepo:
    def __init__(self) -> None:
        self._items: Dict[str, Answer] = {}

    def save(self, answer: Answer) -> None:
        self._items[answer.id] = answer

    def get(self, answer_id: str) -> Answer | None:
        return self._items.get(answer_id)


class InMemoryAssetRepo:
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
                    self._items[asset_id] = Asset(**asset_data)
            except Exception:
                pass

    def _save(self) -> None:
        data = {asset_id: {**asdict(asset), "status": asset.status.value} for asset_id, asset in self._items.items()}
        _atomic_write_json(self._storage_path, data)

    def save(self, asset: Asset) -> None:
        self._items[asset.id] = asset
        self._save()

    def get(self, asset_id: str) -> Asset | None:
        return self._items.get(asset_id)

    def list_all(self) -> List[Asset]:
        return list(self._items.values())


class FileAssetRepo:
    def __init__(
        self,
        base_dir: str = "storage/assets",
        index_path: str = "storage/indexes/assets.json",
        legacy_storage_path: str = "storage/assets/assets.json",
    ) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = Path(index_path)
        _ensure_index(self.index_path)
        self.legacy_storage_path = Path(legacy_storage_path)
        self._items: Dict[str, Asset] = {}
        self._migrate_legacy_if_needed()

    def _asset_dir(self, asset_id: str) -> Path:
        return self.base_dir / asset_id

    def _meta_path(self, asset_id: str) -> Path:
        return self._asset_dir(asset_id) / "meta.json"

    def _fragments_path(self, asset_id: str) -> Path:
        return self._asset_dir(asset_id) / "fragments.json"

    def _migrate_legacy_if_needed(self) -> None:
        rows = _read_json(self.index_path, [])
        if rows or not self.legacy_storage_path.exists():
            return
        data = _read_json(self.legacy_storage_path, {})
        for asset_data in data.values():
            status = asset_data.get("status", "queued")
            asset = Asset(
                id=asset_data["id"],
                file_name=asset_data.get("file_name", ""),
                file_type=asset_data.get("file_type", ""),
                size_bytes=int(asset_data.get("size_bytes", 0)),
                status=AssetStatus(status) if not isinstance(status, AssetStatus) else status,
                progress=int(asset_data.get("progress", 0)),
                error=asset_data.get("error"),
                storage_path=asset_data.get("storage_path"),
                content_preview=asset_data.get("content_preview"),
                chunk_count=int(asset_data.get("chunk_count", 0)),
                created_at=asset_data.get("created_at", ""),
                updated_at=asset_data.get("updated_at", ""),
            )
            self.save(asset)

    def save(self, asset: Asset) -> None:
        asset_dir = self._asset_dir(asset.id)
        asset_dir.mkdir(parents=True, exist_ok=True)
        now = utc_now_iso()
        if not asset.created_at:
            asset.created_at = now
        asset.updated_at = now
        payload = {**asdict(asset), "status": asset.status.value}
        _atomic_write_json(self._meta_path(asset.id), payload)
        rows = [row for row in _read_json(self.index_path, []) if row.get("id") != asset.id]
        rows.append(
            {
                "id": asset.id,
                "file_name": asset.file_name,
                "status": asset.status.value,
                "updated_at": asset.updated_at,
                "path": str(self._meta_path(asset.id)),
            }
        )
        rows.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
        _atomic_write_json(self.index_path, rows)
        self._items[asset.id] = asset

    def save_fragments(self, asset_id: str, fragments: List[dict]) -> None:
        asset_dir = self._asset_dir(asset_id)
        asset_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(self._fragments_path(asset_id), fragments)

    def get_fragments(self, asset_id: str) -> List[dict]:
        return _read_json(self._fragments_path(asset_id), [])

    def iter_fragments(self, asset_ids: List[str] | None = None) -> Iterable[dict]:
        target_ids = asset_ids or [row.get("id", "") for row in _read_json(self.index_path, [])]
        for asset_id in target_ids:
            if not asset_id:
                continue
            for fragment in self.get_fragments(asset_id):
                if isinstance(fragment, dict):
                    yield fragment

    def get(self, asset_id: str) -> Asset | None:
        cached = self._items.get(asset_id)
        if cached:
            return cached
        payload = _read_json(self._meta_path(asset_id), None)
        if not payload:
            return None
        status = payload.get("status", "queued")
        payload["status"] = AssetStatus(status) if not isinstance(status, AssetStatus) else status
        asset = Asset(**payload)
        self._items[asset.id] = asset
        return asset

    def list_all(self) -> List[Asset]:
        return [asset for asset in (self.get(row.get("id", "")) for row in _read_json(self.index_path, [])) if asset]


class FileSearchRunRepo:
    def __init__(
        self,
        base_dir: str = "storage/search_runs",
        index_path: str = "storage/indexes/search_runs.json",
    ) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = Path(index_path)
        _ensure_index(self.index_path)
        self._items: Dict[str, SearchRun] = {}

    def _run_dir(self, search_run_id: str) -> Path:
        return self.base_dir / search_run_id

    def _meta_path(self, search_run_id: str) -> Path:
        return self._run_dir(search_run_id) / "meta.json"

    def _results_path(self, search_run_id: str) -> Path:
        return self._run_dir(search_run_id) / "results.json"

    def _fragments_path(self, search_run_id: str) -> Path:
        return self._run_dir(search_run_id) / "fragments.json"

    def save(self, run: SearchRun) -> None:
        run_dir = self._run_dir(run.id)
        run_dir.mkdir(parents=True, exist_ok=True)
        now = utc_now_iso()
        if not run.created_at:
            run.created_at = now
        payload = {
            "id": run.id,
            "query": run.query,
            "created_at": run.created_at,
            "web_enabled": run.web_enabled,
            "status": run.status,
            "result_count": len(run.results),
            "selected_result_ids": list(run.selected_result_ids),
        }
        _atomic_write_json(self._meta_path(run.id), payload)
        _atomic_write_json(self._results_path(run.id), [asdict(result) for result in run.results])

        rows = [row for row in _read_json(self.index_path, []) if row.get("id") != run.id]
        rows.append(
            {
                "id": run.id,
                "query": run.query,
                "created_at": run.created_at,
                "status": run.status,
                "result_count": len(run.results),
                "path": str(self._meta_path(run.id)),
            }
        )
        rows.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        _atomic_write_json(self.index_path, rows)
        self._items[run.id] = run

    def get(self, search_run_id: str) -> SearchRun | None:
        cached = self._items.get(search_run_id)
        if cached:
            return cached
        meta = _read_json(self._meta_path(search_run_id), None)
        if not meta:
            return None
        results = [
            SearchResult(**row)
            for row in _read_json(self._results_path(search_run_id), [])
            if isinstance(row, dict)
        ]
        run = SearchRun(
            id=meta["id"],
            query=meta.get("query", ""),
            created_at=meta.get("created_at", ""),
            web_enabled=bool(meta.get("web_enabled", True)),
            status=meta.get("status", "ready"),
            result_count=int(meta.get("result_count", len(results))),
            selected_result_ids=list(meta.get("selected_result_ids", [])),
            results=results,
        )
        self._items[run.id] = run
        return run

    def update_result(self, search_run_id: str, result: SearchResult) -> SearchRun | None:
        run = self.get(search_run_id)
        if not run:
            return None
        updated: List[SearchResult] = []
        replaced = False
        for current in run.results:
            if current.id == result.id:
                updated.append(result)
                replaced = True
            else:
                updated.append(current)
        if not replaced:
            updated.append(result)
        run.results = updated
        run.result_count = len(updated)
        self.save(run)
        return run

    def save_fragments(self, search_run_id: str, result_id: str, fragments: List[dict]) -> None:
        rows = [row for row in _read_json(self._fragments_path(search_run_id), []) if isinstance(row, dict)]
        rows = [row for row in rows if row.get("result_id") != result_id]
        rows.extend(fragments)
        _atomic_write_json(self._fragments_path(search_run_id), rows)

    def get_fragments(self, search_run_id: str, result_ids: List[str] | None = None) -> List[dict]:
        rows = [row for row in _read_json(self._fragments_path(search_run_id), []) if isinstance(row, dict)]
        if not result_ids:
            return rows
        selected = set(result_ids)
        return [row for row in rows if row.get("result_id") in selected]

    def iter_fragments(self, search_result_ids: List[str] | None = None) -> Iterable[dict]:
        target_ids = set(search_result_ids or [])
        for row in _read_json(self.index_path, []):
            run_id = row.get("id", "")
            if not run_id:
                continue
            for fragment in self.get_fragments(run_id):
                if target_ids and fragment.get("result_id") not in target_ids:
                    continue
                yield fragment


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

    def delete(self, session_id: str) -> bool:
        if session_id in self._items:
            del self._items[session_id]
            return True
        return False


class FileChatSessionRepo:
    def __init__(
        self,
        base_dir: str = "storage/coursewares",
        index_path: str = "storage/indexes/chat_sessions.json",
    ) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = Path(index_path)
        _ensure_index(self.index_path)
        self._items: Dict[str, ChatSession] = {}

    def _session_path(self, courseware_id: str, session_id: str) -> Path:
        return self.base_dir / courseware_id / "chats" / f"{session_id}.json"

    def save(self, session: ChatSession) -> None:
        payload = _read_json(self._session_path(session.courseware_id, session.id), {"messages": []})
        payload.update(
            {
                "id": session.id,
                "courseware_id": session.courseware_id,
                "page_id": session.page_id,
                "chunk_id": session.chunk_id,
                "created_at": session.created_at,
                "last_active_at": session.last_active_at,
                "title": session.title,
            }
        )
        payload.setdefault("messages", [])
        _atomic_write_json(self._session_path(session.courseware_id, session.id), payload)
        rows = [row for row in _read_json(self.index_path, []) if row.get("id") != session.id]
        rows.append(
            {
                "id": session.id,
                "courseware_id": session.courseware_id,
                "page_id": session.page_id,
                "chunk_id": session.chunk_id,
                "created_at": session.created_at,
                "last_active_at": session.last_active_at,
                "title": session.title,
                "path": str(self._session_path(session.courseware_id, session.id)),
            }
        )
        rows.sort(key=lambda x: x.get("last_active_at", ""), reverse=True)
        _atomic_write_json(self.index_path, rows)
        self._items[session.id] = session

    def get(self, session_id: str) -> ChatSession | None:
        cached = self._items.get(session_id)
        if cached:
            return cached
        row = next((row for row in _read_json(self.index_path, []) if row.get("id") == session_id), None)
        if not row:
            return None
        payload = _read_json(Path(row["path"]), None)
        if not payload:
            return None
        session = ChatSession(
            id=payload["id"],
            courseware_id=payload.get("courseware_id", ""),
            page_id=payload.get("page_id", ""),
            chunk_id=payload.get("chunk_id", ""),
            created_at=payload.get("created_at", ""),
            last_active_at=payload.get("last_active_at", ""),
            title=payload.get("title", ""),
        )
        self._items[session.id] = session
        return session

    def list(self, courseware_id: str | None = None, page_id: str | None = None) -> List[ChatSession]:
        sessions: List[ChatSession] = []
        for row in _read_json(self.index_path, []):
            if courseware_id and row.get("courseware_id") != courseware_id:
                continue
            if page_id and row.get("page_id") != page_id:
                continue
            session = self.get(row.get("id", ""))
            if session:
                sessions.append(session)
        return sorted(sessions, key=lambda x: x.last_active_at, reverse=True)

    def append_message(self, session_id: str, message: ChatMessage) -> None:
        session = self.get(session_id)
        if not session:
            raise KeyError(session_id)
        path = self._session_path(session.courseware_id, session.id)
        payload = _read_json(path, None)
        if not payload:
            raise KeyError(session_id)
        payload.setdefault("messages", []).append(asdict(message))
        _atomic_write_json(path, payload)

    def list_messages(self, session_id: str) -> List[ChatMessage]:
        session = self.get(session_id)
        if not session:
            return []
        payload = _read_json(self._session_path(session.courseware_id, session.id), {"messages": []})
        messages = []
        for row in payload.get("messages", []):
            messages.append(
                ChatMessage(
                    id=row["id"],
                    session_id=row.get("session_id", session.id),
                    role=row.get("role", "user"),
                    content=row.get("content", ""),
                    created_at=row.get("created_at", ""),
                    selected_context=row.get("selected_context", ""),
                    selected_chunk_ids=list(row.get("selected_chunk_ids", [])),
                    asset_ids=list(row.get("asset_ids", [])),
                    sources=list(row.get("sources", [])),
                    is_compressed=row.get("is_compressed", False),
                    original_content=row.get("original_content", ""),
                    compression_metadata=dict(row.get("compression_metadata", {})),
                )
            )
        return sorted(messages, key=lambda x: x.created_at)

    def delete(self, session_id: str) -> bool:
        row = next((row for row in _read_json(self.index_path, []) if row.get("id") == session_id), None)
        if not row:
            return False

        session_path = Path(row.get("path", ""))
        if session_path.exists():
            session_path.unlink(missing_ok=True)

        rows = [r for r in _read_json(self.index_path, []) if r.get("id") != session_id]
        _atomic_write_json(self.index_path, rows)

        if session_id in self._items:
            del self._items[session_id]

        return True


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


class FileDraftRepo:
    def __init__(self, base_dir: str = "storage/drafts", index_path: str = "storage/indexes/drafts.json") -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = Path(index_path)
        _ensure_index(self.index_path)
        self._items: Dict[str, RewriteDraft] = {}

    def _draft_path(self, draft_id: str) -> Path:
        return self.base_dir / f"{draft_id}.json"

    def save(self, draft: RewriteDraft) -> None:
        _atomic_write_json(self._draft_path(draft.id), asdict(draft))
        rows = [row for row in _read_json(self.index_path, []) if row.get("id") != draft.id]
        rows.append(
            {
                "id": draft.id,
                "courseware_id": draft.courseware_id,
                "page_id": draft.page_id,
                "chunk_id": draft.chunk_id,
                "status": draft.status,
                "created_at": draft.created_at,
                "path": str(self._draft_path(draft.id)),
            }
        )
        rows.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        _atomic_write_json(self.index_path, rows)
        self._items[draft.id] = draft

    def get(self, draft_id: str) -> RewriteDraft | None:
        cached = self._items.get(draft_id)
        if cached:
            return cached
        payload = _read_json(self._draft_path(draft_id), None)
        if not payload:
            return None
        draft = RewriteDraft(**payload)
        self._items[draft.id] = draft
        return draft


class InMemoryProfileRepo:
    def __init__(self) -> None:
        self._current: LearnerProfile | None = None

    def set_current(self, profile: LearnerProfile) -> None:
        self._current = profile

    def get_current(self) -> LearnerProfile | None:
        return self._current
