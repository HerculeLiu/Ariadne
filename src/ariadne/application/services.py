from __future__ import annotations

import concurrent.futures
import html
import re
import threading
from pathlib import Path
from dataclasses import asdict
from typing import Dict, List, Tuple
from uuid import uuid4

from ariadne.application.config import load_config
from ariadne.application.file_parser import FileParserService, _flush_logs
from ariadne.application.knowledge import KnowledgeDocStore, chunks_to_markdown, markdown_to_chunks, markdown_to_html
from ariadne.domain.errors import (
    FileSizeLimitError,
    NotFoundError,
    UnsupportedFileTypeError,
    ValidationError,
    VersionConflictError,
)
from ariadne.domain.models import (
    Answer,
    Asset,
    AssetStatus,
    ChatMessage,
    ChatSession,
    Chunk,
    Courseware,
    EventLog,
    ExportTask,
    GenerationJob,
    JobEvent,
    JobPhase,
    LearnerProfile,
    RetrievalSettings,
    RewriteDraft,
    Source,
    utc_now_iso,
)
from ariadne.infrastructure.repositories import (
    InMemoryAnswerRepo,
    InMemoryAssetRepo,
    InMemoryChatMessageRepo,
    InMemoryChatSessionRepo,
    InMemoryCoursewareRepo,
    InMemoryDraftRepo,
    InMemoryExportRepo,
    InMemoryJobRepo,
    InMemoryProfileRepo,
)
from ariadne.infrastructure.app_logger import get_logger, setup_logging
from ariadne.llm.agent import LLMAgent, PromptStore
from ariadne.llm.embedding_client import EmbeddingClient
from ariadne.application.rag_service import RAGService, make_fragment_id
from ariadne.infrastructure.vector_store import VectorStore

ALLOWED_FILE_TYPES = {"pdf", "md", "txt", "docx"}
VALID_UNDERSTAND = {"unknown", "understood", "not_understood"}
logger = get_logger("services")


class EventMetricStore:
    def __init__(self) -> None:
        self.events: List[EventLog] = []

    def add(self, event_type: str, payload: Dict[str, object]) -> None:
        self.events.append(EventLog(id=f"ev_{uuid4().hex[:8]}", event_type=event_type, payload=payload, created_at=utc_now_iso()))
        logger.debug("event added: %s payload=%s", event_type, payload)

    def list_events(self, event_type: str | None = None) -> List[EventLog]:
        items = self.events
        if event_type:
            items = [it for it in items if it.event_type == event_type]
        return list(reversed(items[-200:]))


class RetrievalSettingsService:
    def __init__(self) -> None:
        self._settings = RetrievalSettings()

    def get(self) -> RetrievalSettings:
        return self._settings

    def update(self, payload: Dict[str, object]) -> RetrievalSettings:
        web_enabled = bool(payload.get("web_enabled", self._settings.web_enabled))
        source_weight = payload.get("source_weight", self._settings.source_weight)
        whitelist = payload.get("domain_whitelist", self._settings.domain_whitelist)
        blacklist = payload.get("domain_blacklist", self._settings.domain_blacklist)

        if not isinstance(source_weight, dict):
            raise ValidationError("invalid source_weight", field="source_weight", reason="must be object")
        required = {"doc", "blog", "paper"}
        if set(source_weight.keys()) != required:
            raise ValidationError("invalid source_weight keys", field="source_weight", reason="must contain doc/blog/paper")
        total = float(source_weight["doc"]) + float(source_weight["blog"]) + float(source_weight["paper"])
        if total <= 0:
            raise ValidationError("invalid source_weight", field="source_weight", reason="sum must > 0")
        normalized = {k: float(v) / total for k, v in source_weight.items()}

        domain_pattern = re.compile(r"^[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")
        for domain in whitelist:
            if not domain_pattern.match(str(domain)):
                raise ValidationError("invalid domain", field="domain_whitelist", reason=f"invalid domain: {domain}")

        self._settings = RetrievalSettings(
            web_enabled=web_enabled,
            source_weight=normalized,
            domain_whitelist=[str(x) for x in whitelist],
            domain_blacklist=[str(x) for x in blacklist],
        )
        return self._settings


class ProfileService:
    def __init__(self, repo: InMemoryProfileRepo, local_only_default: bool) -> None:
        self.repo = repo
        if not self.repo.get_current():
            self.repo.set_current(
                LearnerProfile(
                    goal="",
                    background="",
                    analogy_preference="technical",
                    mastered_topics=[],
                    local_only=local_only_default,
                )
            )

    def get_current(self) -> LearnerProfile:
        profile = self.repo.get_current()
        if not profile:
            raise NotFoundError("profile not found")
        return profile

    def update_current(self, payload: Dict[str, object]) -> LearnerProfile:
        profile = self.get_current()
        updated = LearnerProfile(
            goal=str(payload.get("goal", profile.goal)),
            background=str(payload.get("background", profile.background)),
            analogy_preference=str(payload.get("analogy_preference", profile.analogy_preference)),
            mastered_topics=[str(x) for x in payload.get("mastered_topics", profile.mastered_topics)],
            local_only=bool(payload.get("local_only", profile.local_only)),
        )
        self.repo.set_current(updated)
        return updated


class GenerationService:
    def __init__(
        self,
        coursewares: InMemoryCoursewareRepo,
        jobs: InMemoryJobRepo,
        assets: InMemoryAssetRepo,
        llm: LLMAgent,
        event_store: EventMetricStore,
        knowledge_store: KnowledgeDocStore,
        config: AppConfig = None,
        embedding_client: EmbeddingClient = None,
        rag_service: RAGService = None,
    ) -> None:
        self.coursewares = coursewares
        self.jobs = jobs
        self.assets = assets
        self.llm = llm
        self.event_store = event_store
        self.knowledge_store = knowledge_store
        self.config = config or load_config()
        self.embedding_client = embedding_client or EmbeddingClient(self.config)
        self.rag_service = rag_service
        self._job_lock = threading.Lock()

    def generate(
        self,
        topic: str,
        keywords: List[str],
        asset_ids: List[str] = None,
    ) -> Tuple[GenerationJob, Courseware]:
        logger.info("generate start topic=%s asset_ids=%s", topic, asset_ids)
        self._validate(topic, keywords)

        courseware_id = f"cw_{uuid4().hex[:8]}"
        job_id = f"job_{uuid4().hex[:8]}"
        now = utc_now_iso()

        courseware = Courseware(
            id=courseware_id,
            topic=topic,
            created_at=now,
            status="processing",
            chunks=[],
            knowledge_markdown="",
            knowledge_doc_path="",
            source_asset_ids=asset_ids or [],
        )
        job = GenerationJob(
            id=job_id,
            courseware_id=courseware_id,
            phase=JobPhase.QUEUED,
            progress=1,
            events=[JobEvent(ts=now, phase=JobPhase.QUEUED, message="job queued")],
        )
        self.coursewares.save(courseware)
        self.jobs.save(job)
        threading.Thread(
            target=self._run_generation_job,
            args=(courseware_id, topic, keywords, asset_ids or []),
            daemon=True,
        ).start()
        self.event_store.add("generate", {"courseware_id": courseware_id, "topic": topic})
        logger.info("generate queued courseware_id=%s job_id=%s", courseware_id, job_id)
        return job, courseware

    def progress(self, courseware_id: str) -> GenerationJob:
        job = self.jobs.get_by_courseware(courseware_id)
        if not job:
            raise NotFoundError("generation job not found")
        return job

    def _update_job(
        self,
        courseware_id: str,
        *,
        phase: JobPhase | None = None,
        progress: int | None = None,
        message: str | None = None,
        chunk_total: int | None = None,
        chunk_done: int | None = None,
        chunk_failed: int | None = None,
        outline: List[dict] | None = None,
        completed_chunk: str | None = None,
        error: str | None = None,
    ) -> None:
        job = self.jobs.get_by_courseware(courseware_id)
        if not job:
            return
        with self._job_lock:
            if phase is not None:
                job.phase = phase
            if progress is not None:
                job.progress = max(0, min(100, int(progress)))
            if chunk_total is not None:
                job.chunk_total = max(0, int(chunk_total))
            if chunk_done is not None:
                job.chunk_done = max(0, int(chunk_done))
            if chunk_failed is not None:
                job.chunk_failed = max(0, int(chunk_failed))
            if outline is not None:
                job.outline = outline
            if completed_chunk:
                job.completed_chunks.append(completed_chunk)
                job.completed_chunks = job.completed_chunks[-20:]
            if error is not None:
                job.error = error
            if message:
                job.events.append(JobEvent(ts=utc_now_iso(), phase=job.phase, message=message))
                job.events = job.events[-100:]

    def _run_generation_job(
        self,
        courseware_id: str,
        topic: str,
        keywords: List[str],
        asset_ids: List[str],
    ) -> None:
        courseware = self.coursewares.get(courseware_id)
        if not courseware:
            return
        try:
            self._update_job(courseware_id, phase=JobPhase.RETRIEVING, progress=5, message="materials collected")
            material_lines = self._collect_material_lines(asset_ids)  # Only show assets used for this generation
            topic_norm = (topic or "").strip().lower()
            example_mode = topic_norm == "test"

            if example_mode:
                page_title = "Test Playground"
                self._update_job(courseware_id, phase=JobPhase.OUTLINE, progress=15, message="building test outline")
                outline = self._build_test_outline()
                outline_meta = [
                    {"chapter_no": ch["chapter_no"], "chapter_title": ch["title"], "chunk_titles": [c["title"] for c in ch["chunks"]]}
                    for ch in outline
                ]
                tasks: List[dict] = []
                for ch in outline:
                    for ck in ch["chunks"]:
                        tasks.append(
                            {
                                "chapter_no": ch["chapter_no"],
                                "chapter_title": ch["title"],
                                "chunk_no": ck["chunk_no"],
                                "chunk_title": ck["title"],
                                "order_no": ck["order_no"],
                            }
                        )
                total = len(tasks)
                self._update_job(
                    courseware_id,
                    phase=JobPhase.CHUNK_GENERATING,
                    progress=20,
                    message=f"test chunk generation started ({total})",
                    chunk_total=total,
                    chunk_done=0,
                    chunk_failed=0,
                    outline=outline_meta,
                )
                result_map: Dict[int, Chunk] = {}
                for task in tasks:
                    body = self._build_test_chunk_content(task["chapter_no"], task["chunk_no"])
                    chunk = Chunk(
                        id=f"ck_{uuid4().hex[:8]}",
                        title=self._sanitize_heading(task["chunk_title"]),
                        content=self._sanitize_chunk_body(body),
                        order_no=task["order_no"],
                    )
                    result_map[task["order_no"]] = chunk
                    done = len(result_map)
                    progress = 20 + int((done / max(1, total)) * 70)
                    self._update_job(
                        courseware_id,
                        progress=progress,
                        chunk_done=done,
                        completed_chunk=f"Chapter {task['chapter_no']} · Chunk {task['chunk_no']}",
                        message=f"test chunk done {task['chapter_no']}.{task['chunk_no']}",
                    )

                self._update_job(courseware_id, phase=JobPhase.ASSEMBLING, progress=94, message="assembling test markdown")
                chunks_sorted = [result_map[k] for k in sorted(result_map)]
                knowledge_md = self._build_markdown_from_outline(page_title, outline, chunks_sorted, material_lines)
                courseware.chunks = chunks_sorted
                courseware.knowledge_markdown = knowledge_md
                courseware.knowledge_doc_path = self.knowledge_store.save(courseware_id=courseware_id, markdown_text=knowledge_md, source_asset_ids=courseware.source_asset_ids)
                courseware.status = "ready"
                self._update_job(
                    courseware_id,
                    phase=JobPhase.DONE,
                    progress=100,
                    message="test page ready",
                    chunk_total=len(chunks_sorted),
                    chunk_done=len(chunks_sorted),
                )
                return

            self._update_job(courseware_id, phase=JobPhase.OUTLINE, progress=15, message="generating outline")

            # RAG retrieval for outline generation - use uploaded files to inform structure
            outline_rag_context = ""
            if asset_ids and self.rag_service and self.embedding_client:
                try:
                    query_embedding = self.embedding_client.encode_single(topic)
                    results = self.rag_service.retrieve(
                        query=topic,
                        query_embedding=query_embedding,
                        top_k=5,
                        asset_ids=asset_ids,
                    )
                    if results:
                        outline_rag_context = self.rag_service.format_context_for_prompt(results, max_length=6000)
                        logger.info("RAG context for outline: %d chars, %d results", len(outline_rag_context), len(results))
                except Exception as exc:
                    logger.warning("RAG retrieval failed for outline: %s", exc)

            outline_md = self.llm.generate_outline_markdown(topic, keywords, rag_context=outline_rag_context).strip()
            outline = self._parse_outline_markdown(outline_md)
            if not outline:
                # fallback for unstable outline responses
                fallback = self.llm.generate_understanding_markdown(topic, keywords).strip()
                outline = self._parse_outline_markdown(fallback)
            if not outline:
                outline = self._fallback_outline(topic)

            outline_meta = [
                {"chapter_no": ch["chapter_no"], "chapter_title": ch["title"], "chunk_titles": [c["title"] for c in ch["chunks"]]}
                for ch in outline
            ]
            tasks: List[dict] = []
            for ch in outline:
                for ck in ch["chunks"]:
                    tasks.append(
                        {
                            "chapter_no": ch["chapter_no"],
                            "chapter_title": ch["title"],
                            "chapter_summary": ch.get("summary") or ch["title"],
                            "chunk_no": ck["chunk_no"],
                            "chunk_title": ck["title"],
                            "order_no": ck["order_no"],
                        }
                    )
            total = len(tasks)
            self._update_job(
                courseware_id,
                phase=JobPhase.CHUNK_GENERATING,
                progress=20,
                message=f"chunk generation started ({total})",
                chunk_total=total,
                chunk_done=0,
                chunk_failed=0,
                outline=outline_meta,
            )
            if total == 0:
                raise ValidationError("outline has no chunk", field="outline", reason="chunk list is empty")

            result_map: Dict[int, Chunk] = {}
            failed = 0
            max_workers = max(1, load_config().chunk_max_concurrency)

            def _run_one(task: dict) -> Tuple[int, str, str]:
                # Content Layer: retrieve chunk-specific references before generating explanation text.
                query = f"{task['chapter_title']} {task['chunk_title']}"
                rag_context = ""

                if asset_ids and self.rag_service:
                    try:
                        query_embedding = self.embedding_client.encode_single(query)
                        results = self.rag_service.retrieve(
                            query=query,
                            query_embedding=query_embedding,
                            top_k=3,
                            asset_ids=asset_ids,
                        )
                        if results:
                            rag_context = self.rag_service.format_context_for_prompt(results)
                            logger.debug("RAG context for chunk %s: %d chars", task['chunk_title'], len(rag_context))
                    except Exception as exc:
                        logger.warning("RAG retrieval failed for chunk %s: %s", task['chunk_title'], exc)

                body = self.llm.generate_chunk_content(
                    topic=topic,
                    chapter_title=task["chapter_title"],
                    chapter_summary=task["chapter_summary"],
                    chunk_title=task["chunk_title"],
                    rag_context=rag_context,
                ).strip()
                return task["order_no"], task["chunk_title"], body

            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
                future_map = {pool.submit(_run_one, t): t for t in tasks}
                for fut in concurrent.futures.as_completed(future_map):
                    task = future_map[fut]
                    try:
                        order_no, chunk_title, body = fut.result()
                        normalized_title = self._sanitize_heading(chunk_title)
                        normalized_body = self._sanitize_chunk_body(body)
                        chunk = Chunk(id=f"ck_{uuid4().hex[:8]}", title=normalized_title, content=normalized_body, order_no=order_no)
                        result_map[order_no] = chunk
                        courseware.chunks = [result_map[k] for k in sorted(result_map)]
                        done = len(result_map)
                        progress = 20 + int((done / total) * 70)
                        self._update_job(
                            courseware_id,
                            progress=progress,
                            chunk_done=done,
                            completed_chunk=f"Chapter {task['chapter_no']} · Chunk {task['chunk_no']}",
                            message=f"chunk done {task['chapter_no']}.{task['chunk_no']}",
                        )
                    except Exception as exc:  # noqa: BLE001
                        failed += 1
                        self._update_job(
                            courseware_id,
                            chunk_failed=failed,
                            message=f"chunk failed {task['chapter_no']}.{task['chunk_no']}: {exc}",
                        )

            if not result_map:
                raise ValidationError("all chunks failed", field="generation", reason="no chunk generated")

            self._update_job(courseware_id, phase=JobPhase.ASSEMBLING, progress=94, message="assembling markdown")
            chunks_sorted = [result_map[k] for k in sorted(result_map)]
            knowledge_md = self._build_markdown_from_outline(topic, outline, chunks_sorted, material_lines)
            courseware.chunks = chunks_sorted
            courseware.knowledge_markdown = knowledge_md
            courseware.knowledge_doc_path = self.knowledge_store.save(courseware_id=courseware_id, markdown_text=knowledge_md, source_asset_ids=courseware.source_asset_ids)
            courseware.status = "ready"
            self._update_job(courseware_id, phase=JobPhase.DONE, progress=100, message="courseware ready")
            logger.info("generate done courseware_id=%s chunks=%s failed=%s", courseware_id, len(chunks_sorted), failed)
        except Exception as exc:  # noqa: BLE001
            courseware.status = "failed"
            self._update_job(
                courseware_id,
                phase=JobPhase.FAILED,
                progress=100,
                message=f"generation failed: {exc}",
                error=str(exc),
            )
            logger.exception("generation failed courseware_id=%s", courseware_id)

    def _collect_material_lines(self, asset_ids: List[str] | None) -> List[str]:
        """Collect material reference lines for the specified assets only."""
        lines: List[str] = []
        seen: set[str] = set()

        for asset_id in asset_ids or []:
            asset = self.assets.get(asset_id)
            if not asset or asset.status != AssetStatus.READY:
                continue
            label = f"- {asset.file_name} ({asset.file_type})"
            if label in seen:
                continue  # Deduplicate by filename + type
            seen.add(label)
            lines.append(label)

        return lines

    def _validate(self, topic: str, keywords: List[str]) -> None:
        topic = (topic or "").strip()
        if len(topic) < 2 or len(topic) > 120:
            raise ValidationError("invalid topic length", field="topic", reason="length must be between 2 and 120")
        if len(keywords) > 10:
            raise ValidationError("too many keywords", field="keywords", reason="max size is 10")
        for kw in keywords:
            if len(kw) < 1 or len(kw) > 64:
                raise ValidationError(
                    "invalid keyword length",
                    field="keywords",
                    reason="single keyword length must be between 1 and 64",
                )

    def _make_chunks(self, topic: str, keywords: List[str], llm_text: str) -> List[Chunk]:
        key_text = ", ".join(keywords[:3]) if keywords else "核心概念"
        base = llm_text[:180].replace("\n", " ").strip()
        return [
            Chunk(id=f"ck_{uuid4().hex[:8]}", title=f"{topic} 是什么", content=f"{topic} 的核心是 {key_text}。{base}", order_no=1),
            Chunk(id=f"ck_{uuid4().hex[:8]}", title=f"{topic} 的关键机制", content="拆解关键机制与常见误区。", order_no=2),
            Chunk(id=f"ck_{uuid4().hex[:8]}", title=f"{topic} 的实践建议", content="给出实践路径和下一步建议。", order_no=3),
        ]

    def _fallback_outline(self, topic: str) -> List[dict]:
        return [
            {
                "chapter_no": 1,
                "title": f"{topic} 核心讲解",
                "summary": f"{topic} 核心讲解",
                "chunks": [
                    {"chunk_no": 1, "title": f"{topic} 是什么", "order_no": 1},
                    {"chunk_no": 2, "title": f"{topic} 的关键机制", "order_no": 2},
                    {"chunk_no": 3, "title": f"{topic} 的实践建议", "order_no": 3},
                ],
            }
        ]

    def _parse_outline_markdown(self, markdown_text: str) -> List[dict]:
        chapters: List[dict] = []
        current: dict | None = None
        chapter_no = 0
        global_order = 1
        for raw in markdown_text.splitlines():
            line = raw.strip()
            if line.startswith("## "):
                chapter_no += 1
                current = {
                    "chapter_no": chapter_no,
                    "title": self._sanitize_heading(line[3:].strip()) or f"章节{chapter_no}",
                    "summary": self._sanitize_heading(line[3:].strip()) or f"章节{chapter_no}",
                    "chunks": [],
                }
                chapters.append(current)
                continue
            if line.startswith("### "):
                if current is None:
                    chapter_no += 1
                    current = {
                        "chapter_no": chapter_no,
                        "title": f"章节{chapter_no}",
                        "summary": f"章节{chapter_no}",
                        "chunks": [],
                    }
                    chapters.append(current)
                title = self._sanitize_heading(line[4:].strip())
                if title:
                    current["chunks"].append({"chunk_no": len(current["chunks"]) + 1, "title": title, "order_no": global_order})
                    global_order += 1
        return [ch for ch in chapters if ch["chunks"]]

    def _sanitize_heading(self, text: str) -> str:
        t = (text or "").strip()
        t = re.sub(r"^#+\s*", "", t, flags=re.IGNORECASE)
        t = re.sub(r"^章节\s*[Xx\d]+(?:\s*[.．]\s*[Yy\d]+)?\s*[:：]\s*", "", t, flags=re.IGNORECASE)
        t = re.sub(r"^chunk\s*[Xx\d]+(?:\s*[.．]\s*[Yy\d]+)?\s*[:：]\s*", "", t, flags=re.IGNORECASE)
        return t.strip()

    def _sanitize_chunk_body(self, body: str) -> str:
        lines = []
        for raw in (body or "").splitlines():
            line = raw.rstrip()
            if re.match(r"^#+\s*(章节|chunk)\s*", line, flags=re.IGNORECASE):
                continue
            lines.append(line)
        cleaned = "\n".join(lines).strip()
        return cleaned or "（该 chunk 内容生成为空，建议重试）"

    def _build_markdown_from_outline(self, topic: str, outline: List[dict], chunks: List[Chunk], material_lines: List[str]) -> str:
        lines: List[str] = [f"# {topic}", ""]
        chunk_by_order = {c.order_no: c for c in chunks}
        for ch in outline:
            chapter_title = self._sanitize_heading(ch["title"]) or f"章节{ch['chapter_no']}"
            lines.extend([f"## 章节{ch['chapter_no']}：{chapter_title}", ""])
            for ck in ch["chunks"]:
                chunk = chunk_by_order.get(ck["order_no"])
                if not chunk:
                    continue
                chunk_title = self._sanitize_heading(chunk.title) or f"Chunk {ch['chapter_no']}.{ck['chunk_no']}"
                lines.extend([f"### Chunk {ch['chapter_no']}.{ck['chunk_no']}：{chunk_title}", chunk.content.strip(), ""])
        return "\n".join(lines).strip() + "\n"

    def _build_knowledge_markdown(self, topic: str, llm_text: str, material_lines: List[str]) -> str:
        # Prefer direct LLM markdown output when available.
        if llm_text.startswith("#") or "## " in llm_text:
            return llm_text.rstrip() + "\n"

        # Fallback: wrap plain text into markdown.
        body = llm_text if llm_text else "暂无模型内容，已使用默认结构。"
        return f"# {topic}\n\n## 知识讲解\n{body}\n"

    def _build_example_markdown(self, topic: str) -> str:
        page_title = "Test Playground" if (topic or "").strip().lower() == "test" else topic
        return (
            f"# {page_title}\n\n"
            "```render-config\n"
            "show_hero: false\n"
            "back_home_path: /\n"
            "layout_mode: continuous\n"
            "nav_collapsible: true\n"
            "```\n\n"
            "## 章节1：Theme Preview\n\n"
            "### Chunk 1.1：占位内容（无 LLM）\n\n"
            "你当前进入的是示例模式：输入 `test` 时，系统跳过理解层与生成层模型调用，直接输出这份占位课件。\n\n"
            "这个页面用于验证最新主题是否生效：米色纸感背景、橄榄主色按钮、左右侧栏统一视觉。\n\n"
            "```interaction\n"
            "title: 主题状态检查\n"
            "input: test mode\n"
            "output: 示例模式运行成功：当前为新版主题（home 与 generated 页面已统一）。\n"
            "button: 检查主题\n"
            "```\n\n"
            "## 章节2：UI Checklist\n\n"
            "### Chunk 2.1：检查项\n\n"
            "- 左侧导航可收起/展开\n"
            "- 中间内容区连续滚动\n"
            "- 右侧聊天栏可收起/展开\n"
            "- 元素定位按钮可复制定位信息\n\n"
            "### Chunk 2.2：选中条目堆叠预览\n\n"
            "点击每个 Chunk 右上角 `+`，将内容加入右侧聊天选择区，用于验证条目堆叠样式。\n\n"
            "### Chunk 2.3：密集布局检查\n\n"
            "预期效果：第一条较高，第二条开始半高；条目之间无视觉缝隙，点击删除按钮后即时回收占位。\n\n"
            "```interaction\n"
            "title: 交互状态回显\n"
            "input: nav/chat/inspector/theme\n"
            "output: 占位输出：结构与交互可继续验证，此流程不依赖 LLM 可用性。\n"
            "button: 显示结果\n"
            "```\n"
        )

    def _build_test_outline(self) -> List[dict]:
        return [
            {
                "chapter_no": 1,
                "title": "Theme Preview",
                "summary": "Theme Preview",
                "chunks": [
                    {"chunk_no": 1, "title": "占位内容（无 LLM）", "order_no": 1},
                    {"chunk_no": 2, "title": "主题状态检查", "order_no": 2},
                ],
            },
            {
                "chapter_no": 2,
                "title": "UI Checklist",
                "summary": "UI Checklist",
                "chunks": [
                    {"chunk_no": 1, "title": "检查项", "order_no": 3},
                    {"chunk_no": 2, "title": "选中条目堆叠预览", "order_no": 4},
                    {"chunk_no": 3, "title": "密集布局检查", "order_no": 5},
                ],
            },
        ]

    def _build_test_chunk_content(self, chapter_no: int, chunk_no: int) -> str:
        data = {
            (1, 1): (
                "你当前进入的是示例模式：输入 `test` 时，系统跳过真实模型调用，"
                "但保留新系统的生成流程（大纲 -> chunk -> 拼接）。"
            ),
            (1, 2): (
                "这个页面用于验证固定壳层是否正确：米色纸感背景、左右侧栏常驻、"
                "中间内容区独立渲染。"
            ),
            (2, 1): (
                "检查项：\n"
                "- 左侧导航可收起/展开\n"
                "- 中间内容区连续滚动\n"
                "- 右侧聊天栏可收起/展开\n"
                "- 元素定位按钮可复制定位信息"
            ),
            (2, 2): "点击每个 Chunk 右上角 `+`，将内容加入右侧聊天选择区，验证堆叠卡片效果。",
            (2, 3): (
                "预期效果：第一张卡完整展示，后续卡片按蜘蛛纸牌方式层叠，"
                "仅露出一部分。删除后应即时回收占位。"
            ),
        }
        return data.get((chapter_no, chunk_no), "测试内容占位。")


class CoursewareService:
    def __init__(self, repo: InMemoryCoursewareRepo, event_store: EventMetricStore, knowledge_store: KnowledgeDocStore) -> None:
        self.repo = repo
        self.event_store = event_store
        self.knowledge_store = knowledge_store

    def _ensure_metadata(self, cw: Courseware) -> Courseware:
        """Restore source_asset_ids from persistent storage if not already set."""
        if not cw.source_asset_ids:
            # Try to load from metadata file
            asset_ids = self.knowledge_store.get_source_asset_ids(cw.id)
            if asset_ids:
                cw.source_asset_ids = asset_ids
                self.repo.save(cw)  # Update in-memory courseware
        return cw

    def get(self, courseware_id: str) -> Courseware | None:
        """Get courseware with restored metadata. Reconstructs from disk if not in memory."""
        cw = self.repo.get(courseware_id)
        if cw:
            return self._ensure_metadata(cw)

        # Repo miss - try to reconstruct from disk
        markdown = self.knowledge_store.load(courseware_id)
        if not markdown:
            return None

        # Parse markdown to get chunks
        chunks = markdown_to_chunks(markdown)

        # Extract topic from markdown (first # heading)
        topic = courseware_id
        for line in markdown.splitlines():
            if line.strip().startswith("# "):
                topic = line.strip()[2:].strip()
                break

        # Load source_asset_ids from metadata
        source_asset_ids = self.knowledge_store.get_source_asset_ids(courseware_id)

        # Reconstruct courseware
        from ariadne.domain.models import utc_now_iso
        cw = Courseware(
            id=courseware_id,
            topic=topic,
            created_at=utc_now_iso(),  # Fallback timestamp
            status="ready",
            knowledge_markdown=markdown,
            knowledge_doc_path=str(self.knowledge_store.base_dir / f"{courseware_id}.md"),
            chunks=chunks,
            source_asset_ids=source_asset_ids,
        )
        self.repo.save(cw)
        logger.info("Reconstructed courseware from disk id=%s chunks=%d source_assets=%d",
                    courseware_id, len(chunks), len(source_asset_ids))
        return cw

    def list_chunks(self, courseware_id: str, include_content: bool = True, only_favorite: bool = False) -> List[dict]:
        cw = self.get(courseware_id)  # Use get() to handle disk reconstruction
        if not cw:
            raise NotFoundError("courseware not found")
        rows = []
        for chunk in cw.chunks:
            if only_favorite and not getattr(chunk, "is_favorite", False):
                continue
            row = {
                "id": chunk.id,
                "title": chunk.title,
                "order_no": chunk.order_no,
                "understand_state": getattr(chunk, "understand_state", "unknown"),
                "is_favorite": getattr(chunk, "is_favorite", False),
                "collapsed": getattr(chunk, "collapsed", False),
            }
            if include_content:
                row["content"] = chunk.content
            rows.append(row)
        return rows

    def update_chunk_state(self, chunk_id: str, payload: Dict[str, object]) -> dict:
        cw, chunk = self._find_chunk(chunk_id)
        understand_state = payload.get("understand_state", getattr(chunk, "understand_state", "unknown"))
        if understand_state not in VALID_UNDERSTAND:
            raise ValidationError("invalid understand_state", field="understand_state", reason="invalid enum")

        chunk.understand_state = understand_state
        chunk.is_favorite = bool(payload.get("is_favorite", getattr(chunk, "is_favorite", False)))
        chunk.collapsed = bool(payload.get("collapsed", getattr(chunk, "collapsed", False)))
        self.repo.save(cw)
        self.event_store.add("chunk_state", {"chunk_id": chunk.id, "understand_state": chunk.understand_state})
        return {
            "id": chunk.id,
            "understand_state": chunk.understand_state,
            "is_favorite": chunk.is_favorite,
            "collapsed": chunk.collapsed,
        }

    def append_answer(self, chunk_id: str, answer: Answer, action: str) -> dict:
        cw, chunk = self._find_chunk(chunk_id)
        if action == "reject":
            self.event_store.add("append_reject", {"chunk_id": chunk_id, "answer_id": answer.id})
            logger.info("append rejected chunk=%s answer=%s", chunk_id, answer.id)
            return {"applied": False, "version": cw.current_version}
        if action != "accept":
            raise ValidationError("invalid action", field="action", reason="must be accept or reject")

        chunk.content = f"{chunk.content}\n\n补充：{answer.answer}"
        cw.current_version += 1
        self._sync_markdown(cw)
        self.repo.save(cw)
        self.event_store.add("append_accept", {"chunk_id": chunk_id, "answer_id": answer.id, "version": cw.current_version})
        logger.info("append accepted chunk=%s version=%s", chunk_id, cw.current_version)
        return {"applied": True, "version": cw.current_version}

    def apply_rewrite(self, draft: RewriteDraft, expected_version: int) -> dict:
        cw, chunk = self._find_chunk(draft.chunk_id)
        if expected_version != cw.current_version:
            raise VersionConflictError("version mismatch", field="expected_version", reason="current version changed")
        chunk.content = draft.rewritten
        cw.current_version += 1
        self._sync_markdown(cw)
        self.repo.save(cw)
        self.event_store.add("rewrite_apply", {"draft_id": draft.id, "version": cw.current_version})
        logger.info("rewrite applied draft=%s version=%s", draft.id, cw.current_version)
        return {"version": cw.current_version}

    def undo_latest(self, page_id: str, expected_version: int) -> dict:
        for cw in self.repo._items.values():  # noqa: SLF001
            if cw.current_version != expected_version:
                continue
            cw.current_version -= 1
            self.repo.save(cw)
            self.event_store.add("undo", {"page_id": page_id, "version": cw.current_version})
            return {"version": cw.current_version}
        raise VersionConflictError("version mismatch", field="expected_version", reason="no matching page version")

    def get_markdown(self, courseware_id: str) -> dict:
        cw = self.get(courseware_id)
        if not cw:
            raise NotFoundError("courseware not found")
        text = self.knowledge_store.load(courseware_id)
        if text:
            cw.knowledge_markdown = text
        return {"courseware_id": cw.id, "markdown": cw.knowledge_markdown, "path": cw.knowledge_doc_path}

    def update_markdown(self, courseware_id: str, markdown_text: str) -> dict:
        cw = self.get(courseware_id)
        if not cw:
            raise NotFoundError("courseware not found")
        if not markdown_text.strip():
            raise ValidationError("markdown is empty", field="markdown", reason="required")

        cw.knowledge_markdown = markdown_text
        cw.chunks = markdown_to_chunks(markdown_text)
        cw.current_version += 1
        cw.knowledge_doc_path = self.knowledge_store.save(courseware_id, markdown_text, source_asset_ids=cw.source_asset_ids)
        self.repo.save(cw)
        self.event_store.add("markdown_update", {"courseware_id": courseware_id, "version": cw.current_version})
        logger.info("markdown updated courseware=%s version=%s path=%s", courseware_id, cw.current_version, cw.knowledge_doc_path)
        return {"courseware_id": cw.id, "version": cw.current_version, "path": cw.knowledge_doc_path}

    def _sync_markdown(self, cw: Courseware) -> None:
        cw.knowledge_markdown = chunks_to_markdown(topic=cw.topic, chunks=cw.chunks)
        cw.knowledge_doc_path = self.knowledge_store.save(cw.id, cw.knowledge_markdown, source_asset_ids=cw.source_asset_ids)

    def _find_chunk(self, chunk_id: str) -> Tuple[Courseware, Chunk]:
        for cw in self.repo._items.values():  # noqa: SLF001
            for ck in cw.chunks:
                if ck.id == chunk_id:
                    return cw, ck
        raise NotFoundError("chunk not found")


class QAService:
    def __init__(
        self,
        coursewares: InMemoryCoursewareRepo,
        answers: InMemoryAnswerRepo,
        llm: LLMAgent,
        event_store: EventMetricStore,
        rag_service: "RAGService" = None,
        embedding_client: "EmbeddingClient" = None,
        courseware_service: "CoursewareService" = None,
    ) -> None:
        self.repo = coursewares  # Keep for _find_chunk
        self.answers = answers
        self.llm = llm
        self.event_store = event_store
        self.rag_service = rag_service
        self.embedding_client = embedding_client
        self.courseware_service = courseware_service  # For metadata restoration

    def ask(self, chunk_id: str, question: str, page_id: str, selection: Dict[str, object] | None, mode: str) -> Answer:
        courseware, chunk = self._find_chunk(chunk_id)

        # Restore metadata from persistent storage if available
        if self.courseware_service:
            courseware = self.courseware_service._ensure_metadata(courseware)

        if not question.strip():
            raise ValidationError("question is empty", field="question", reason="question is required")
        if mode not in {"brief", "deep"}:
            raise ValidationError("invalid mode", field="mode", reason="mode should be brief or deep")

        selection_text = str((selection or {}).get("text", "")).strip()

        # Build context with chunk content and RAG
        context_parts = [
            f"topic={courseware.topic}",
            f"chunk_title={chunk.title}",
            f"chunk_content={chunk.content}",  # Include full chunk content
        ]
        if selection_text:
            context_parts.append(f"selection={selection_text}")

        # Add RAG context if courseware has source assets
        rag_context = ""
        if courseware.source_asset_ids and self.rag_service and self.embedding_client:
            try:
                query_embedding = self.embedding_client.encode_single(question)
                results = self.rag_service.retrieve(
                    query=question,
                    query_embedding=query_embedding,
                    top_k=3,
                    asset_ids=courseware.source_asset_ids,
                )
                if results:
                    rag_context = self.rag_service.format_context_for_prompt(results, max_length=4000)
                    logger.debug("RAG context for QA: %d chars, %d results", len(rag_context), len(results))
            except Exception as exc:
                logger.warning("RAG retrieval failed for QA: %s", exc)

        context = ";".join(context_parts)
        llm_text = self.llm.answer_chunk_question(context=context, question=question, mode=mode, rag_context=rag_context if rag_context else None)
        answer_text = f"[{mode}] chunk({chunk.id}) {llm_text}"

        answer = Answer(
            id=f"ans_{uuid4().hex[:8]}",
            chunk_id=chunk.id,
            linked_chunk_id=chunk.id,
            answer=answer_text,
            next_suggestions=["继续展开这一点"],
            sources=[Source(title=f"{courseware.topic} 来源", url="https://example.org/reference", domain="example.org")],
        )
        self.answers.save(answer)
        self.event_store.add("ask", {"chunk_id": chunk.id, "page_id": page_id, "with_rag": bool(rag_context)})
        logger.info("qa answered chunk=%s mode=%s with_rag=%s", chunk.id, mode, bool(rag_context))
        return answer

    def get_answer(self, answer_id: str) -> Answer:
        answer = self.answers.get(answer_id)
        if not answer:
            raise NotFoundError("answer not found")
        return answer

    def _find_chunk(self, chunk_id: str) -> Tuple[Courseware, Chunk]:
        for cw in self.repo._items.values():  # noqa: SLF001
            for ck in cw.chunks:
                if ck.id == chunk_id:
                    return cw, ck
        raise NotFoundError("chunk not found")


class ChatService:
    def __init__(
        self,
        sessions: InMemoryChatSessionRepo,
        messages: InMemoryChatMessageRepo,
        llm: LLMAgent,
        event_store: EventMetricStore,
        rag_service: "RAGService" = None,
        embedding_client: "EmbeddingClient" = None,
        courseware_service: "CoursewareService" = None,
    ) -> None:
        self.sessions = sessions
        self.messages = messages
        self.llm = llm
        self.event_store = event_store
        self.rag_service = rag_service
        self.embedding_client = embedding_client
        self.courseware_service = courseware_service

    def create_session(self, courseware_id: str, page_id: str, chunk_id: str) -> ChatSession:
        now = utc_now_iso()
        session = ChatSession(
            id=f"cs_{uuid4().hex[:8]}",
            courseware_id=courseware_id,
            page_id=page_id,
            chunk_id=chunk_id,
            created_at=now,
            last_active_at=now,
        )
        self.sessions.save(session)
        self.event_store.add("chat_session", {"session_id": session.id})
        logger.info("chat session created id=%s", session.id)
        return session

    def send_message(
        self,
        session_id: str,
        message: str,
        continue_from_message_id: str | None = None,
        asset_ids: List[str] | None = None,
        selected_context: str | None = None,
    ) -> Dict[str, object]:
        """
        Send a chat message.

        Args:
            session_id: Chat session ID
            message: User's question (without chunk content - used for RAG retrieval)
            continue_from_message_id: Optional message to continue from
            asset_ids: Asset IDs for RAG retrieval
            selected_context: Optional selected chunk content (only sent to LLM, not used for RAG)
        """
        session = self.sessions.get(session_id)
        if not session:
            raise NotFoundError("chat session not found")
        if not message.strip():
            raise ValidationError("message is empty", field="message", reason="required")

        # Build the full message for storage (include selected context if provided)
        full_message = f"{selected_context}\n\n{message}" if selected_context else message
        user_msg = ChatMessage(id=f"msg_{uuid4().hex[:8]}", session_id=session_id, role="user", content=full_message, created_at=utc_now_iso())
        self.messages.save(user_msg)

        # Determine which asset_ids to use for RAG
        effective_asset_ids = asset_ids
        if not effective_asset_ids and self.courseware_service:
            # Fall back to courseware's source assets if available
            courseware = self.courseware_service.get(session.courseware_id)
            if courseware and courseware.source_asset_ids:
                effective_asset_ids = courseware.source_asset_ids
                logger.debug("Using courseware source_asset_ids: %s", effective_asset_ids)

        # Build RAG context using ONLY the user's question (not selected_context)
        rag_context = ""
        if effective_asset_ids and self.rag_service and self.embedding_client:
            try:
                # Use message directly for RAG - no extraction needed since selected_context is separate
                query_embedding = self.embedding_client.encode_single(message)
                results = self.rag_service.retrieve(
                    query=message,
                    query_embedding=query_embedding,
                    top_k=5,
                    asset_ids=effective_asset_ids,
                )
                if results:
                    rag_context = self.rag_service.format_context_for_prompt(results, max_length=8000)
                    logger.debug("RAG context for chat: query='%s', %d chars, %d results", message[:100], len(rag_context), len(results))
            except Exception as exc:
                logger.warning("RAG retrieval failed for chat: %s", exc)

        # Build context for LLM: combine selected_context + rag_context + message
        context_parts = [
            f"session={session_id}",
            f"courseware={session.courseware_id}",
            f"page={session.page_id}",
            f"continue_from={continue_from_message_id or ''}",
        ]
        if rag_context:
            context_parts.append(f"rag_context={rag_context[:500]}...")  # Log preview

        # Build the full message for LLM: selected_context + rag_context + user question
        llm_message = message
        if selected_context:
            llm_message = f"【选中的内容】\n{selected_context}\n\n【我的问题】\n{message}"
        if rag_context:
            llm_message = f"{llm_message}\n\n【参考资料】\n{rag_context}"

        llm_reply = self.llm.chat_reply(
            context=";".join(context_parts),
            message=llm_message,
            rag_context=None,  # Already embedded in llm_message
        )
        assistant_msg = ChatMessage(
            id=f"msg_{uuid4().hex[:8]}",
            session_id=session_id,
            role="assistant",
            content=llm_reply,
            created_at=utc_now_iso(),
        )
        self.messages.save(assistant_msg)
        session.last_active_at = utc_now_iso()
        self.sessions.save(session)
        self.event_store.add("chat_message", {"session_id": session_id, "with_rag": bool(rag_context)})
        logger.info("chat message processed session=%s with_rag=%s", session_id, bool(rag_context))
        return {"reply": assistant_msg.content, "message_id": assistant_msg.id}

    def list_sessions(self, courseware_id: str | None, page_id: str | None) -> List[ChatSession]:
        return self.sessions.list(courseware_id=courseware_id, page_id=page_id)


class RewriteService:
    def __init__(
        self,
        coursewares: InMemoryCoursewareRepo,
        drafts: InMemoryDraftRepo,
        llm: LLMAgent,
        event_store: EventMetricStore,
        courseware_service: "CoursewareService" = None,
    ) -> None:
        self.coursewares = coursewares
        self.drafts = drafts
        self.llm = llm
        self.event_store = event_store
        self.courseware_service = courseware_service

    def create_draft(self, page_id: str, chunk_id: str, instruction: str) -> RewriteDraft:
        cw, chunk = self._find_chunk(chunk_id)

        # Restore metadata from persistent storage if available
        if self.courseware_service:
            cw = self.courseware_service._ensure_metadata(cw)

        rewritten = self.llm.rewrite_chunk(original=chunk.content, instruction=instruction)
        draft = RewriteDraft(
            id=f"dr_{uuid4().hex[:8]}",
            page_id=page_id,
            chunk_id=chunk_id,
            original=chunk.content,
            rewritten=rewritten,
            status="drafted",
            created_at=utc_now_iso(),
        )
        self.drafts.save(draft)
        self.event_store.add("rewrite_draft", {"draft_id": draft.id, "courseware_id": cw.id})
        logger.info("rewrite draft created id=%s chunk=%s", draft.id, chunk_id)
        return draft

    def get_draft(self, draft_id: str) -> RewriteDraft:
        draft = self.drafts.get(draft_id)
        if not draft:
            raise NotFoundError("draft not found")
        return draft

    def _find_chunk(self, chunk_id: str) -> Tuple[Courseware, Chunk]:
        for cw in self.coursewares._items.values():  # noqa: SLF001
            for ck in cw.chunks:
                if ck.id == chunk_id:
                    return cw, ck
        raise NotFoundError("chunk not found")


class AssetService:
    def __init__(
        self,
        assets: InMemoryAssetRepo,
        max_file_size_bytes: int,
        event_store: EventMetricStore,
        storage_dir: str = None,
        rag_service: "RAGService" = None,
        config: "AppConfig" = None,
    ) -> None:
        self.assets = assets
        self.max_file_size_bytes = max_file_size_bytes
        self.event_store = event_store
        self.storage_dir = Path(storage_dir or "storage/assets")
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.rag_service = rag_service
        self.config = config
        self.parser = FileParserService()
        self._processing_lock = threading.Lock()

    def upload(self, file_name: str, size_bytes: int) -> Asset:
        """Legacy upload method - creates asset without actual file content."""
        ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
        if ext not in ALLOWED_FILE_TYPES:
            raise UnsupportedFileTypeError("unsupported file type", field="file", reason=f"extension {ext} not allowed")
        if size_bytes <= 0:
            raise ValidationError("file cannot be empty", field="file", reason="size must be > 0")
        if size_bytes > self.max_file_size_bytes:
            raise FileSizeLimitError("file too large", field="file", reason=f"size exceeds {self.max_file_size_bytes} bytes")

        asset = Asset(
            id=f"as_{uuid4().hex[:8]}",
            file_name=file_name,
            file_type=ext,
            size_bytes=size_bytes,
            status=AssetStatus.READY,
            progress=100,
        )
        self.assets.save(asset)
        self.event_store.add("asset_upload", {"asset_id": asset.id, "file_name": file_name})
        logger.info("asset uploaded id=%s name=%s size=%s", asset.id, file_name, size_bytes)
        return asset

    def upload_with_content(self, file_name: str, file_content: bytes, size_bytes: int) -> Asset:
        """
        Upload file with actual content and process it asynchronously.

        Args:
            file_name: Name of the uploaded file
            file_content: Raw file content as bytes
            size_bytes: Size of the file in bytes

        Returns:
            Created Asset (status will be PROCESSING initially)
        """
        ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
        if ext not in ALLOWED_FILE_TYPES:
            raise UnsupportedFileTypeError("unsupported file type", field="file", reason=f"extension {ext} not allowed")
        if not file_content:
            raise ValidationError("file content is empty", field="file", reason="content is required")
        actual_size = len(file_content)
        if actual_size != size_bytes:
            size_bytes = actual_size
        if size_bytes > self.max_file_size_bytes:
            raise FileSizeLimitError("file too large", field="file", reason=f"size exceeds {self.max_file_size_bytes} bytes")

        asset_id = f"as_{uuid4().hex[:8]}"

        # Store file to disk
        storage_path = self._store_file(asset_id, file_content, ext)

        # Create asset with PROCESSING status
        asset = Asset(
            id=asset_id,
            file_name=file_name,
            file_type=ext,
            size_bytes=size_bytes,
            status=AssetStatus.PROCESSING,
            progress=0,
            storage_path=storage_path,
        )
        self.assets.save(asset)
        self.event_store.add("asset_upload", {"asset_id": asset.id, "file_name": file_name, "with_content": True})
        logger.info("asset uploaded with content id=%s name=%s size=%s path=%s", asset.id, file_name, size_bytes, storage_path)

        # Start background processing
        threading.Thread(
            target=self._process_file_async,
            args=(asset,),
            daemon=True,
        ).start()

        return asset

    def _store_file(self, asset_id: str, file_content: bytes, file_type: str) -> str:
        """
        Store file to local filesystem.

        Args:
            asset_id: Asset ID
            file_content: File content as bytes
            file_type: File extension

        Returns:
            Path where file was stored
        """
        # Create subdirectory by date for better organization
        from datetime import datetime
        date_dir = datetime.utcnow().strftime("%Y%m")
        target_dir = self.storage_dir / date_dir
        target_dir.mkdir(parents=True, exist_ok=True)

        # Store file with asset_id as prefix
        file_path = target_dir / f"{asset_id}.{file_type}"
        file_path.write_bytes(file_content)
        logger.debug("File stored: %s", file_path)
        return str(file_path)

    def _process_file_async(self, asset: Asset) -> None:
        """
        Process uploaded file asynchronously: parse -> split -> vectorize -> store.

        Args:
            asset: Asset to process
        """
        try:
            with self._processing_lock:
                # Update progress: parsing
                asset.progress = 10
                self.assets.save(asset)

            # Parse file to extract text
            logger.info("Processing asset %s: parsing file", asset.id)
            text = self.parser.parse(asset.storage_path, asset.file_type)
            if not text or not text.strip():
                raise ValueError("Failed to extract text from file")

            # Generate content preview
            preview = self.parser.get_preview(text, max_length=500)
            asset.content_preview = preview

            with self._processing_lock:
                asset.progress = 30
                self.assets.save(asset)

            # Split text into fragments (use pre-extracted text to avoid re-parsing)
            import sys
            print(f"[DEBUG-1] About to import split_fragments_from_pre_extracted_text")
            sys.stdout.flush()

            from ariadne.application.text_splitter import split_fragments_from_pre_extracted_text
            print(f"[DEBUG-2] Import complete, about to call split function")
            sys.stdout.flush()

            logger.info("Processing asset %s: splitting text (chars=%d)", asset.id, len(text))
            _flush_logs()

            print(f"[DEBUG-3] About to call split_fragments_from_pre_extracted_text with asset_id={asset.id}")
            sys.stdout.flush()

            fragments = split_fragments_from_pre_extracted_text(asset.id, text)

            print(f"[DEBUG-4] split_fragments_from_pre_extracted_text returned {len(fragments)} fragments")
            sys.stdout.flush()
            _flush_logs()
            logger.debug("Asset %s: split_fragments_from_pre_extracted_text returned", asset.id)

            if not fragments:
                logger.warning("Asset %s: no fragments generated from text", asset.id)

            asset.chunk_count = len(fragments)
            logger.info("Processing asset %s: split into %d fragments", asset.id, len(fragments))
            _flush_logs()

            with self._processing_lock:
                asset.progress = 50
                self.assets.save(asset)

            # Vectorize and store to ChromaDB
            if self.rag_service:
                logger.info("Processing asset %s: vectorizing %d fragments", asset.id, len(fragments))
                count = self.rag_service.process_pre_split_fragments(fragments, asset.id)
                logger.info("Processing asset %s: vectorized %d fragments", asset.id, count)
            else:
                logger.warning("RAG service not available, skipping vectorization for asset %s", asset.id)

            # Mark as ready
            with self._processing_lock:
                asset.status = AssetStatus.READY
                asset.progress = 100
                self.assets.save(asset)

            self.event_store.add("asset_processed", {
                "asset_id": asset.id,
                "file_name": asset.file_name,
                "chunk_count": asset.chunk_count,
            })
            logger.info("Asset processing complete: id=%s chunks=%d", asset.id, asset.chunk_count)

        except Exception as exc:  # noqa: BLE001
            logger.exception("Asset processing failed: id=%s error=%s", asset.id, exc)
            with self._processing_lock:
                asset.status = AssetStatus.FAILED
                asset.error = str(exc)
                asset.progress = 0
                self.assets.save(asset)
            self.event_store.add("asset_failed", {
                "asset_id": asset.id,
                "file_name": asset.file_name,
                "error": str(exc),
            })

    def status(self, asset_id: str) -> Asset:
        asset = self.assets.get(asset_id)
        if not asset:
            raise NotFoundError("asset not found")
        return asset


class ExportService:
    def __init__(
        self,
        coursewares: InMemoryCoursewareRepo,
        exports: InMemoryExportRepo,
        event_store: EventMetricStore,
        knowledge_store: KnowledgeDocStore,
    ) -> None:
        self.coursewares = coursewares
        self.exports = exports
        self.event_store = event_store
        self.knowledge_store = knowledge_store

    def export_courseware(self, courseware_id: str, fmt: str) -> ExportTask:
        if fmt not in {"html", "zip", "readonly_zip"}:
            raise ValidationError("invalid export format", field="format", reason="unsupported format")
        cw = self.coursewares.get(courseware_id)
        if not cw:
            raise NotFoundError("courseware not found")

        task = ExportTask(
            id=f"ex_{uuid4().hex[:8]}",
            courseware_id=courseware_id,
            format=fmt,
            status="done",
            download_url=f"/downloads/{courseware_id}.{'html' if fmt == 'html' else 'zip'}",
        )
        self.exports.save(task)
        self.event_store.add("export", {"courseware_id": courseware_id, "format": fmt})
        logger.info("export done task=%s courseware=%s format=%s", task.id, courseware_id, fmt)
        return task

    def task(self, task_id: str) -> ExportTask:
        task = self.exports.get(task_id)
        if not task:
            raise NotFoundError("export task not found")
        return task

    def render_html(self, courseware_id: str) -> str:
        cw = self.coursewares.get(courseware_id)
        if cw:
            markdown_text = cw.knowledge_markdown or chunks_to_markdown(topic=cw.topic, chunks=cw.chunks)
        else:
            # Fallback to persisted markdown so /downloads/<id>.html remains available after process restart.
            markdown_text = self.knowledge_store.load(courseware_id).strip()
            if not markdown_text:
                raise NotFoundError("courseware not found")
        return markdown_to_html(markdown_text)


class MonitoringService:
    def __init__(self, event_store: EventMetricStore) -> None:
        self.event_store = event_store

    def logs(self, event_type: str | None = None) -> List[dict]:
        return [asdict(x) for x in self.event_store.list_events(event_type)]

    def performance(self) -> Dict[str, object]:
        counts: Dict[str, int] = {}
        for ev in self.event_store.events:
            counts[ev.event_type] = counts.get(ev.event_type, 0) + 1
        generate_count = counts.get("generate", 0)
        ask_count = counts.get("ask", 0) + counts.get("chat_message", 0)
        export_count = counts.get("export", 0)
        return {
            "generation": {"p50_ms": 9000, "p95_ms": 18000, "success_rate": 0.98 if generate_count else 1.0},
            "qa": {"p50_ms": 1600, "p95_ms": 4500 if ask_count else 1000},
            "export": {"success_rate": 0.995 if export_count else 1.0},
            "counters": counts,
        }


def build_services() -> Dict[str, object]:
    config = load_config()
    setup_logging(config.log_file_path, config.log_level)
    logger.info("services bootstrap provider=%s model=%s log=%s", config.model_provider, config.llm_model, Path(config.log_file_path))
    prompts = PromptStore(config.prompt_dir, hot_reload=config.prompt_hot_reload)
    llm = LLMAgent(config=config, prompts=prompts)
    event_store = EventMetricStore()
    knowledge_store = KnowledgeDocStore(config.knowledge_doc_dir)

    # RAG components
    embedding_client = EmbeddingClient(config)
    vector_store = VectorStore(config)
    rag_service = RAGService(config, vector_store, embedding_client)

    coursewares = InMemoryCoursewareRepo()
    jobs = InMemoryJobRepo()
    answers = InMemoryAnswerRepo()
    assets = InMemoryAssetRepo()
    exports = InMemoryExportRepo()
    sessions = InMemoryChatSessionRepo()
    messages = InMemoryChatMessageRepo()
    drafts = InMemoryDraftRepo()
    profiles = InMemoryProfileRepo()

    retrieval_settings = RetrievalSettingsService()
    profile_service = ProfileService(profiles, local_only_default=config.local_only)

    # Create CoursewareService first (needed by ChatService and QAService)
    courseware_service = CoursewareService(coursewares, event_store, knowledge_store)

    return {
        "config": config,
        "generation": GenerationService(
            coursewares, jobs, assets, llm, event_store, knowledge_store,
            config=config, embedding_client=embedding_client, rag_service=rag_service,
        ),
        "courseware": courseware_service,
        "qa": QAService(coursewares, answers, llm, event_store, rag_service=rag_service, embedding_client=embedding_client, courseware_service=courseware_service),
        "chat": ChatService(sessions, messages, llm, event_store, rag_service=rag_service, embedding_client=embedding_client, courseware_service=courseware_service),
        "rewrite": RewriteService(coursewares, drafts, llm, event_store, courseware_service=courseware_service),
        "assets": AssetService(
            assets,
            config.max_file_size_mb * 1024 * 1024,
            event_store,
            storage_dir=config.asset_storage_dir if hasattr(config, 'asset_storage_dir') else None,
            rag_service=rag_service,
            config=config,
        ),
        "export": ExportService(coursewares, exports, event_store, knowledge_store),
        "retrieval_settings": retrieval_settings,
        "profile": profile_service,
        "monitoring": MonitoringService(event_store),
        "rag": rag_service,
        "vector_store": vector_store,
        "embedding": embedding_client,
        "repos": {
            "coursewares": coursewares,
            "jobs": jobs,
            "answers": answers,
            "assets": assets,
            "exports": exports,
            "sessions": sessions,
            "messages": messages,
            "drafts": drafts,
            "profiles": profiles,
        },
    }
