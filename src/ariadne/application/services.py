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

ALLOWED_FILE_TYPES = {"pdf", "md", "txt"}
VALID_DIFFICULTY = {"beginner", "intermediate", "advanced"}
VALID_STYLE = {"intuitive", "rigorous", "engineering"}
VALID_TEMPLATE = {"tutorial", "qa", "project"}
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
    ) -> None:
        self.coursewares = coursewares
        self.jobs = jobs
        self.assets = assets
        self.llm = llm
        self.event_store = event_store
        self.knowledge_store = knowledge_store
        self._job_lock = threading.Lock()

    def generate(
        self,
        topic: str,
        keywords: List[str],
        difficulty: str,
        style: str,
        template: str,
    ) -> Tuple[GenerationJob, Courseware]:
        logger.info("generate start topic=%s difficulty=%s style=%s template=%s", topic, difficulty, style, template)
        self._validate(topic, keywords, difficulty, style, template)

        courseware_id = f"cw_{uuid4().hex[:8]}"
        job_id = f"job_{uuid4().hex[:8]}"
        now = utc_now_iso()

        courseware = Courseware(
            id=courseware_id,
            topic=topic,
            difficulty=difficulty,
            style=style,
            template=template,
            created_at=now,
            status="processing",
            chunks=[],
            knowledge_markdown="",
            knowledge_doc_path="",
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
            args=(courseware_id, topic, keywords, difficulty, style, template),
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
        difficulty: str,
        style: str,
        template: str,
    ) -> None:
        courseware = self.coursewares.get(courseware_id)
        if not courseware:
            return
        try:
            self._update_job(courseware_id, phase=JobPhase.RETRIEVING, progress=5, message="materials collected")
            material_lines = self._collect_material_lines()
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
                courseware.knowledge_doc_path = self.knowledge_store.save(courseware_id=courseware_id, markdown_text=knowledge_md)
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
            outline_md = self.llm.generate_outline_markdown(topic, keywords, difficulty, style, template).strip()
            outline = self._parse_outline_markdown(outline_md)
            if not outline:
                # fallback for unstable outline responses
                fallback = self.llm.generate_understanding_markdown(topic, keywords, difficulty, style, template).strip()
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
                body = self.llm.generate_chunk_content(
                    topic=topic,
                    chapter_title=task["chapter_title"],
                    chapter_summary=task["chapter_summary"],
                    chunk_title=task["chunk_title"],
                    difficulty=difficulty,
                    style=style,
                    template=template,
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
            courseware.knowledge_doc_path = self.knowledge_store.save(courseware_id=courseware_id, markdown_text=knowledge_md)
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

    def _collect_material_lines(self) -> List[str]:
        lines: List[str] = []
        for asset in self.assets._items.values():  # noqa: SLF001
            if asset.status == AssetStatus.READY:
                lines.append(f"- {asset.file_name} ({asset.file_type})")
        return lines

    def _validate(self, topic: str, keywords: List[str], difficulty: str, style: str, template: str) -> None:
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
        if difficulty not in VALID_DIFFICULTY:
            raise ValidationError("invalid difficulty", field="difficulty", reason="invalid enum value")
        if style not in VALID_STYLE:
            raise ValidationError("invalid style", field="style", reason="invalid enum value")
        if template not in VALID_TEMPLATE:
            raise ValidationError("invalid template", field="template", reason="invalid enum value")

    def _make_chunks(self, topic: str, keywords: List[str], difficulty: str, style: str, llm_text: str) -> List[Chunk]:
        key_text = ", ".join(keywords[:3]) if keywords else "核心概念"
        depth = {"beginner": "直观理解", "intermediate": "机制理解", "advanced": "工程细节"}[difficulty]
        base = llm_text[:180].replace("\n", " ").strip()
        return [
            Chunk(id=f"ck_{uuid4().hex[:8]}", title=f"{topic} 是什么", content=f"[{style}] {depth}：{topic} 的核心是 {key_text}。{base}", order_no=1),
            Chunk(id=f"ck_{uuid4().hex[:8]}", title=f"{topic} 的关键机制", content=f"[{style}] 拆解关键机制与常见误区。", order_no=2),
            Chunk(id=f"ck_{uuid4().hex[:8]}", title=f"{topic} 的实践建议", content=f"[{style}] 给出实践路径和下一步建议。", order_no=3),
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
        if material_lines:
            lines.extend(["## 参考资料", *material_lines, ""])
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
            if material_lines:
                material_block = "\n".join(["## 参考资料", *material_lines, ""])
                if llm_text.startswith("# "):
                    parts = llm_text.splitlines()
                    head = parts[0]
                    tail = "\n".join(parts[1:]).strip()
                    return f"{head}\n\n{material_block}\n{tail}\n".strip() + "\n"
            return llm_text.rstrip() + "\n"

        # Fallback: wrap plain text into markdown.
        body = llm_text if llm_text else "暂无模型内容，已使用默认结构。"
        return f"# {topic}\n\n## 参考资料\n" + ("\n".join(material_lines) if material_lines else "- 无") + f"\n\n## 知识讲解\n{body}\n"

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

    def list_chunks(self, courseware_id: str, include_content: bool = True, only_favorite: bool = False) -> List[dict]:
        cw = self.repo.get(courseware_id)
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
        cw = self.repo.get(courseware_id)
        if not cw:
            raise NotFoundError("courseware not found")
        text = self.knowledge_store.load(courseware_id)
        if text:
            cw.knowledge_markdown = text
        return {"courseware_id": cw.id, "markdown": cw.knowledge_markdown, "path": cw.knowledge_doc_path}

    def update_markdown(self, courseware_id: str, markdown_text: str) -> dict:
        cw = self.repo.get(courseware_id)
        if not cw:
            raise NotFoundError("courseware not found")
        if not markdown_text.strip():
            raise ValidationError("markdown is empty", field="markdown", reason="required")

        cw.knowledge_markdown = markdown_text
        cw.chunks = markdown_to_chunks(markdown_text)
        cw.current_version += 1
        cw.knowledge_doc_path = self.knowledge_store.save(courseware_id, markdown_text)
        self.repo.save(cw)
        self.event_store.add("markdown_update", {"courseware_id": courseware_id, "version": cw.current_version})
        logger.info("markdown updated courseware=%s version=%s path=%s", courseware_id, cw.current_version, cw.knowledge_doc_path)
        return {"courseware_id": cw.id, "version": cw.current_version, "path": cw.knowledge_doc_path}

    def _sync_markdown(self, cw: Courseware) -> None:
        material_lines = []
        if "## 参考资料" in cw.knowledge_markdown:
            section = cw.knowledge_markdown.split("## 参考资料", 1)[1]
            for line in section.splitlines():
                if line.startswith("- "):
                    material_lines.append(line)
                if line.startswith("## ") and not line.startswith("## 参考资料"):
                    break
        cw.knowledge_markdown = chunks_to_markdown(topic=cw.topic, chunks=cw.chunks, material_lines=material_lines)
        cw.knowledge_doc_path = self.knowledge_store.save(cw.id, cw.knowledge_markdown)

    def _find_chunk(self, chunk_id: str) -> Tuple[Courseware, Chunk]:
        for cw in self.repo._items.values():  # noqa: SLF001
            for ck in cw.chunks:
                if ck.id == chunk_id:
                    return cw, ck
        raise NotFoundError("chunk not found")


class QAService:
    def __init__(self, coursewares: InMemoryCoursewareRepo, answers: InMemoryAnswerRepo, llm: LLMAgent, event_store: EventMetricStore) -> None:
        self.coursewares = coursewares
        self.answers = answers
        self.llm = llm
        self.event_store = event_store

    def ask(self, chunk_id: str, question: str, page_id: str, selection: Dict[str, object] | None, mode: str) -> Answer:
        courseware, chunk = self._find_chunk(chunk_id)
        if not question.strip():
            raise ValidationError("question is empty", field="question", reason="question is required")
        if mode not in {"brief", "deep"}:
            raise ValidationError("invalid mode", field="mode", reason="mode should be brief or deep")

        selection_text = str((selection or {}).get("text", "")).strip()
        context = f"topic={courseware.topic};chunk={chunk.title};selection={selection_text}"
        llm_text = self.llm.answer_chunk_question(context=context, question=question, mode=mode)
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
        self.event_store.add("ask", {"chunk_id": chunk.id, "page_id": page_id})
        logger.info("qa answered chunk=%s mode=%s", chunk.id, mode)
        return answer

    def get_answer(self, answer_id: str) -> Answer:
        answer = self.answers.get(answer_id)
        if not answer:
            raise NotFoundError("answer not found")
        return answer

    def _find_chunk(self, chunk_id: str) -> Tuple[Courseware, Chunk]:
        for cw in self.coursewares._items.values():  # noqa: SLF001
            for ck in cw.chunks:
                if ck.id == chunk_id:
                    return cw, ck
        raise NotFoundError("chunk not found")


class ChatService:
    def __init__(self, sessions: InMemoryChatSessionRepo, messages: InMemoryChatMessageRepo, llm: LLMAgent, event_store: EventMetricStore) -> None:
        self.sessions = sessions
        self.messages = messages
        self.llm = llm
        self.event_store = event_store

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

    def send_message(self, session_id: str, message: str, continue_from_message_id: str | None = None) -> Dict[str, object]:
        session = self.sessions.get(session_id)
        if not session:
            raise NotFoundError("chat session not found")
        if not message.strip():
            raise ValidationError("message is empty", field="message", reason="required")

        user_msg = ChatMessage(id=f"msg_{uuid4().hex[:8]}", session_id=session_id, role="user", content=message, created_at=utc_now_iso())
        self.messages.save(user_msg)
        llm_reply = self.llm.chat_reply(
            context=f"session={session_id};courseware={session.courseware_id};page={session.page_id};continue_from={continue_from_message_id or ''}",
            message=message,
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
        self.event_store.add("chat_message", {"session_id": session_id})
        logger.info("chat message processed session=%s", session_id)
        return {"reply": assistant_msg.content, "message_id": assistant_msg.id}

    def list_sessions(self, courseware_id: str | None, page_id: str | None) -> List[ChatSession]:
        return self.sessions.list(courseware_id=courseware_id, page_id=page_id)


class RewriteService:
    def __init__(self, coursewares: InMemoryCoursewareRepo, drafts: InMemoryDraftRepo, llm: LLMAgent, event_store: EventMetricStore) -> None:
        self.coursewares = coursewares
        self.drafts = drafts
        self.llm = llm
        self.event_store = event_store

    def create_draft(self, page_id: str, chunk_id: str, instruction: str) -> RewriteDraft:
        cw, chunk = self._find_chunk(chunk_id)
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
    def __init__(self, assets: InMemoryAssetRepo, max_file_size_bytes: int, event_store: EventMetricStore) -> None:
        self.assets = assets
        self.max_file_size_bytes = max_file_size_bytes
        self.event_store = event_store

    def upload(self, file_name: str, size_bytes: int) -> Asset:
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

    return {
        "config": config,
        "generation": GenerationService(coursewares, jobs, assets, llm, event_store, knowledge_store),
        "courseware": CoursewareService(coursewares, event_store, knowledge_store),
        "qa": QAService(coursewares, answers, llm, event_store),
        "chat": ChatService(sessions, messages, llm, event_store),
        "rewrite": RewriteService(coursewares, drafts, llm, event_store),
        "assets": AssetService(assets, config.max_file_size_mb * 1024 * 1024, event_store),
        "export": ExportService(coursewares, exports, event_store, knowledge_store),
        "retrieval_settings": retrieval_settings,
        "profile": profile_service,
        "monitoring": MonitoringService(event_store),
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
