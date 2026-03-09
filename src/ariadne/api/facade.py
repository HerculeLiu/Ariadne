from __future__ import annotations

import json
import re
from dataclasses import asdict
from typing import Any, Dict
from uuid import uuid4

from ariadne.application.knowledge import blocks_to_markdown, blocks_to_plain_text, normalize_structured_blocks
from ariadne.application.query_rewrite import QueryRewriteService
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
        self.search = services["search"]
        self.history = services["history"]
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
            job, courseware = self.generation.generate(
                topic=payload.get("topic", ""),
                keywords=[],
                asset_ids=payload.get("asset_ids", []),
                search_run_id=payload.get("search_run_id", ""),
                selected_search_result_ids=payload.get("selected_search_result_ids", []),
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
        cw = self.courseware.get(courseware_id)  # Use courseware.get() for disk reconstruction
        if not cw:
            return self._error(NotFoundError("resource not found"))
        return self._ok(
            {
                "id": cw.id,
                "topic": cw.topic,
                "status": cw.status,
                "current_version": cw.current_version,
                "knowledge_doc_path": cw.knowledge_doc_path,
                "knowledge_markdown": cw.knowledge_markdown,
                "source_asset_ids": cw.source_asset_ids,  # Include source_asset_ids
                "source_search_run_id": cw.source_search_run_id,
                "source_search_result_ids": cw.source_search_result_ids,
                "chunks": [
                    {
                        "id": chunk.id,
                        "title": chunk.title,
                        "content": chunk.content,
                        "blocks": chunk.blocks,
                        "order_no": chunk.order_no,
                        "chapter_no": chunk.chapter_no,
                        "chunk_no": chunk.chunk_no,
                        "page_id": chunk.page_id,
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

    def upload_asset_with_content(self, file_name: str, file_content: bytes, size_bytes: int) -> Dict[str, Any]:
        """Upload asset with actual file content for processing."""
        try:
            asset = self.assets.upload_with_content(
                file_name=file_name,
                file_content=file_content,
                size_bytes=size_bytes,
            )
            return self._ok({
                "asset_id": asset.id,
                "status": asset.status.value,
                "progress": asset.progress,
            })
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
            existing_session_id = payload.get("session_id", "")
            if existing_session_id:
                session = self.chat.get_session(existing_session_id)
                messages = [asdict(m) for m in self.chat.list_messages(existing_session_id)]
                return self._ok({"session": asdict(session), "messages": messages})
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
                asset_ids=payload.get("asset_ids"),
                selected_context=payload.get("selected_context"),
                selected_chunk_ids=payload.get("selected_chunk_ids"),
            )
            return self._ok(result)
        except AriadneError as exc:
            return self._error(exc)

    def list_chat_sessions(self, courseware_id: str | None, page_id: str | None) -> Dict[str, Any]:
        try:
            sessions = [asdict(s) for s in self.chat.list_sessions(courseware_id=courseware_id, page_id=page_id)]
            return self._ok({"sessions": sessions})
        except AriadneError as exc:
            return self._error(exc)

    def delete_chat_session(self, session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            courseware_id = payload.get("courseware_id", "")
            result = self.chat.delete_session(session_id, courseware_id=courseware_id or None)
            return self._ok({"deleted": result, "session_id": session_id})
        except AriadneError as exc:
            return self._error(exc)

    def list_history_coursewares(self, limit: int = 80) -> Dict[str, Any]:
        try:
            rows = self.history.list_coursewares(limit=limit)
            return self._ok({"coursewares": rows})
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

    def undo(self, courseware_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """撤销最后一次修改

        Args:
            courseware_id: 课件 ID
            payload: {"page_id": str, "expected_version": int}
        """
        try:
            page_id = payload.get("page_id", "")
            result = self.courseware.undo_latest(
                courseware_id=courseware_id,
                page_id=page_id,
                expected_version=int(payload.get("expected_version", 0))
            )
            return self._ok({"courseware_id": courseware_id, "page_id": page_id, **result})
        except AriadneError as exc:
            return self._error(exc)

    def get_retrieval_settings(self) -> Dict[str, Any]:
        return self._ok(asdict(self.retrieval_settings.get()))

    def search_materials(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            run = self.search.search(
                query=payload.get("query", ""),
                top_k=int(payload.get("top_k", 8) or 8),
            )
            return self._ok(
                {
                    "search_run_id": run.id,
                    "query": run.query,
                    "results": [asdict(result) for result in run.results],
                }
            )
        except AriadneError as exc:
            return self._error(exc)

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

    def analyze_intent(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """意图识别：判断是否需要修改 chunks，返回修改建议"""
        try:
            message = payload.get("message", "")
            chunks = payload.get("chunks", [])
            courseware_id = str(payload.get("courseware_id", "")).strip()
            explicit_mode = payload.get("explicit_mode", False)
            wants_modification = self._looks_like_modification_request(message)
            logger.info(
                "analyze_intent start explicit_mode=%s chunk_count=%s wants_modification=%s courseware_id=%s message=%s",
                explicit_mode,
                len(chunks or []),
                wants_modification,
                courseware_id,
                (message or "")[:120],
            )

            if not chunks and courseware_id and (explicit_mode or wants_modification):
                chunks = self._resolve_target_chunks(courseware_id, message, limit=3)
                logger.info(
                    "analyze_intent auto_resolve chunk_count=%s courseware_id=%s",
                    len(chunks or []),
                    courseware_id,
                )
                if self._should_require_target_resolution(message, chunks):
                    logger.info(
                        "analyze_intent target_resolution_required courseware_id=%s candidates=%s message=%s",
                        courseware_id,
                        len(chunks or []),
                        (message or "")[:120],
                    )
                    return self._ok({
                        "is_modification": True,
                        "needs_target_resolution": True,
                        "candidate_chunks": chunks,
                        "chat_reply": "定位到多个可能的卡片，请先选择要修改的目标卡片。",
                        "chunks": [],
                    })

            if not chunks:
                return self._ok({
                    "is_modification": False,
                    "chat_reply": "没有定位到要修改的卡片，请先选中卡片，或在问题里说清第几章/哪一张卡片。"
                })

            if not message:
                return self._ok({
                    "is_modification": False,
                    "chat_reply": "请描述你的修改意图"
                })

            llm = self.generation.llm
            if explicit_mode or wants_modification:
                result = self._direct_explicit_rewrite(
                    message=message,
                    chunks=chunks,
                    llm=llm,
                )
                logger.info(
                    "analyze_intent finish path=%s modified=%s chunk_count=%s",
                    "explicit_direct_rewrite" if explicit_mode else "auto_direct_rewrite",
                    result.get("is_modification"),
                    len(result.get("chunks", []) or []),
                )
                return self._ok(result)

            system_prompt = llm.prompts.get("intent_edit_implicit.md")
            if not system_prompt:
                return self._ok({
                    "is_modification": False,
                    "chat_reply": "意图识别服务暂不可用"
                })

            # 构建用户 prompt
            chunks_text = "\n\n".join([
                f"**Chunk {i+1}**\nKey: {c.get('key', '')}\nLabel: {c.get('label', '')}\nTitle: {c.get('title', '')}\n内容: {c.get('content', '')[:500]}..."
                for i, c in enumerate(chunks)
            ])

            user_prompt = f"""用户消息: {message}

已选 Chunks:
{chunks_text}"""

            # 调用 LLM
            response = llm._chat([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ])

            # 解析 JSON 响应
            result = self._parse_json_response(response)
            result = self._normalize_intent_result(
                result=result,
                message=message,
                chunks=chunks,
                llm=llm,
            )
            logger.info(
                "analyze_intent finish path=classifier modified=%s chunk_count=%s",
                result.get("is_modification"),
                len(result.get("chunks", []) or []),
            )
            return self._ok(result)
        except AriadneError as exc:
            return self._error(exc)
        except Exception as exc:
            logger.exception("analyze_intent error")
            return self._ok({
                "is_modification": False,
                "chat_reply": f"意图识别失败: {str(exc)}"
            })

    def _parse_json_response(self, response: str) -> Dict[str, Any]:
        """解析 LLM 返回的 JSON 响应"""
        # 尝试提取 JSON 代码块
        json_match = re.search(r'```json\s*(.*?)\s*```', response, re.DOTALL)
        if json_match:
            response = json_match.group(1)
        else:
            # 尝试提取纯 JSON
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if json_match:
                response = json_match.group(0)

        try:
            return json.loads(response)
        except json.JSONDecodeError:
            # 解析失败，返回默认响应
            return {
                "is_modification": False,
                "chat_reply": response,
                "chunks": []
            }

    def _looks_like_modification_request(self, message: str) -> bool:
        text = (message or "").strip().lower()
        if not text:
            return False
        direct_terms = (
            "修改",
            "改一下",
            "改成",
            "重写",
            "优化",
            "润色",
            "删除",
            "删掉",
            "做成",
            "做一个",
            "加一个",
            "加一张",
            "加个",
            "添加",
            "换成",
            "改为",
        )
        if any(term in text for term in direct_terms):
            return True
        patterns = (
            r"(?:这个|那个|这张|那张|这个卡片|那个卡片|这一段|那一段).{0,10}(?:修改|改一下|改成|重写|做成)",
            r"(?:第\s*\d+\s*章.{0,8}(?:第\s*\d+\s*(?:个)?\s*(?:chunk|块|小节|部分)?|chunk\s*\d+|\d+\.\d+)).{0,12}(?:修改|改一下|改成|做成|删除)",
        )
        return any(re.search(pattern, text, re.I) for pattern in patterns)

    def _extract_chunk_reference(self, query: str) -> tuple[int | None, int | None]:
        text = (query or "").strip()
        if not text:
            return None, None
        match = re.search(r"\b(\d+)\.(\d+)\b", text)
        if match:
            return int(match.group(1)), int(match.group(2))
        chapter_match = re.search(r"第\s*(\d+)\s*章", text)
        chunk_match = re.search(r"第\s*(\d+)\s*(?:个)?\s*(?:chunk|块|小节|部分|卡片)", text, flags=re.I)
        if not chunk_match:
            chunk_match = re.search(r"(?:chunk|块|小节|部分|卡片)\s*(\d+)", text, flags=re.I)
        chapter_no = int(chapter_match.group(1)) if chapter_match else None
        chunk_no = int(chunk_match.group(1)) if chunk_match else None
        return chapter_no, chunk_no

    def _normalize_chunk_title_for_match(self, title: str) -> str:
        text = str(title or "").strip()
        text = re.sub(r"^\s*(?:chapter\s*)?\d+(?:[.\s]\d+)*\s*", "", text, flags=re.I)
        text = re.sub(r"^[\s:：\-—.、]+", "", text)
        text = re.sub(r"[，,。！？?!.；;：:（）()“”\"'、\[\]{}<>/\\\\|`~@#$%^&*_+=\-\s]+", "", text)
        return text.lower()

    def _extract_target_phrase(self, message: str) -> str:
        text = str(message or "").strip()
        if not text:
            return ""
        patterns = (
            r"把\s*(.+?)\s*(?:这张|那个|这个)?\s*(?:卡片|chunk|块|小节|部分)?\s*(?:改|修改|改成|重写|做成|换成|加上|添加)",
            r"将\s*(.+?)\s*(?:这张|那个|这个)?\s*(?:卡片|chunk|块|小节|部分)?\s*(?:改|修改|改成|重写|做成|换成|加上|添加)",
            r"修改\s*(.+?)\s*(?:这张|那个|这个)?\s*(?:卡片|chunk|块|小节|部分)",
            r"把\s*(.+?)\s*(?:做成|改成)\s*(?:一个|一张|可交互|交互|模拟|demo|interactive|视频|图片)",
        )
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.I)
            if match:
                candidate = match.group(1).strip()
                if candidate:
                    return self._cleanup_target_phrase(candidate)
        return self._cleanup_target_phrase(text)

    def _cleanup_target_phrase(self, text: str) -> str:
        value = str(text or "").strip()
        if not value:
            return ""
        value = re.sub(
            r"(?:请|帮我|帮忙|给我|你来|一下|一个|一张|这个|那个|这张|那张|卡片|卡|chunk|块|小节|部分|内容|课件)",
            " ",
            value,
            flags=re.I,
        )
        value = re.sub(
            r"(?:修改|改一下|改成|重写|优化|润色|删除|删掉|做成|做一个|加一个|加一张|加个|添加|换成|改为|可交互的|交互式的|可运行的|模拟的|demo|interactive|widget|视频版|图片版|图片讲解|视频讲解|演示)",
            " ",
            value,
            flags=re.I,
        )
        value = re.sub(r"\s+", " ", value).strip()
        return value

    def _build_chunk_payload(self, courseware, chunk) -> Dict[str, Any]:
        chapter_idx = max(0, int(chunk.chapter_no or 1) - 1)
        chunk_idx = max(0, int(chunk.chunk_no or chunk.order_no or 1) - 1)
        label = f"Chapter {chapter_idx + 1} · Chunk {chunk_idx + 1}"
        return {
            "key": f"{chapter_idx}-{chunk_idx}",
            "label": label,
            "title": chunk.title,
            "content": chunk.content,
            "chunk_id": chunk.id,
            "courseware_id": courseware.id,
        }

    def _should_require_target_resolution(self, message: str, resolved_chunks: list[Dict[str, Any]]) -> bool:
        if len(resolved_chunks or []) <= 1:
            return False
        chapter_no, chunk_no = self._extract_chunk_reference(message)
        if chapter_no or chunk_no:
            return False
        target_phrase = self._extract_target_phrase(message)
        target_norm = self._normalize_chunk_title_for_match(target_phrase)
        if not target_norm:
            return True
        first = resolved_chunks[0]
        second = resolved_chunks[1]
        first_title = self._normalize_chunk_title_for_match(str(first.get("title", "")))
        second_title = self._normalize_chunk_title_for_match(str(second.get("title", "")))
        if target_norm == first_title and target_norm != second_title:
            return False
        first_score = float(first.get("match_score", 0.0) or 0.0)
        second_score = float(second.get("match_score", 0.0) or 0.0)
        return abs(first_score - second_score) < 6.0

    def _resolve_target_chunks(self, courseware_id: str, message: str, limit: int = 3) -> list[Dict[str, Any]]:
        if not courseware_id:
            return []
        courseware = self.courseware.get(courseware_id)
        if not courseware or not courseware.chunks:
            return []

        query = (message or "").strip()
        if not query:
            return []

        ref_chapter_no, ref_chunk_no = self._extract_chunk_reference(query)
        target_phrase = self._extract_target_phrase(query)
        target_phrase_norm = self._normalize_chunk_title_for_match(target_phrase)
        rewriter = QueryRewriteService()
        rewrite = rewriter.rewrite(query, topic=courseware.topic, max_queries=3)
        all_queries = rewrite.all_queries()
        terms = rewriter.extract_keywords(" ".join(x for x in [query, target_phrase] if x))
        ranked: list[tuple[Any, float]] = []
        exact_title_matches: list[Any] = []

        if target_phrase_norm:
            for chunk in courseware.chunks:
                title_norm = self._normalize_chunk_title_for_match(getattr(chunk, "title", ""))
                if title_norm and title_norm == target_phrase_norm:
                    exact_title_matches.append(chunk)
            if exact_title_matches:
                rows: list[Dict[str, Any]] = []
                for chunk in exact_title_matches[:limit]:
                    row = self._build_chunk_payload(courseware, chunk)
                    row["match_score"] = 100.0
                    rows.append(row)
                return rows

        for chunk in courseware.chunks:
            title = (chunk.title or "").strip()
            content = (chunk.content or "").strip()
            if not title and not content:
                continue
            title_lower = title.lower()
            content_lower = content.lower()
            title_norm = self._normalize_chunk_title_for_match(title)
            score = 0.0

            if ref_chapter_no and int(chunk.chapter_no or 0) == ref_chapter_no:
                score += 4.0
                if ref_chunk_no and int(chunk.chunk_no or chunk.order_no or 0) == ref_chunk_no:
                    score += 8.0

            if target_phrase_norm:
                if target_phrase_norm == title_norm:
                    score += 20.0
                elif target_phrase_norm and target_phrase_norm in title_norm:
                    score += 12.0
                elif target_phrase_norm and target_phrase_norm in re.sub(r"\s+", "", content_lower):
                    score += 3.5

            for full_query in all_queries:
                normalized = full_query.strip().lower()
                if len(normalized) < 2:
                    continue
                if normalized == title_lower:
                    score += 8.0
                elif normalized in title_lower:
                    score += 4.0
                elif normalized in content_lower:
                    score += 1.5

            for term in terms:
                lowered = term.lower()
                if len(lowered) < 2:
                    continue
                if lowered == title_lower:
                    score += 6.0
                elif lowered in title_lower:
                    score += 2.2
                elif lowered in content_lower:
                    score += 0.8

            if score > 0:
                ranked.append((chunk, score))

        ranked.sort(key=lambda item: item[1], reverse=True)
        rows: list[Dict[str, Any]] = []
        for chunk, score in ranked[:limit]:
            row = self._build_chunk_payload(courseware, chunk)
            row["match_score"] = round(float(score), 3)
            rows.append(row)
        return rows

    def _direct_explicit_rewrite(self, message: str, chunks: list, llm) -> Dict[str, Any]:
        rewritten_rows = []
        for chunk in chunks or []:
            if not isinstance(chunk, dict):
                continue
            key = str(chunk.get("key", "")).strip()
            label = str(chunk.get("label", "")).strip()
            source_title = str(chunk.get("title", "")).strip()
            source_content = str(chunk.get("content", "")).strip()
            logger.info(
                "explicit_rewrite chunk_start key=%s label=%s title=%s content_len=%s",
                key,
                label,
                source_title[:80],
                len(source_content),
            )
            rewritten_blocks = []
            rewrite_path = "structured_empty"
            try:
                rewritten_blocks = normalize_structured_blocks(llm.rewrite_chunk_structured(
                    original=source_content,
                    instruction=message,
                ))
                if rewritten_blocks:
                    rewrite_path = "structured_success"
            except Exception as exc:  # noqa: BLE001
                logger.warning("explicit_rewrite structured_failed key=%s reason=%s", key, exc)
                rewritten_blocks = []
                rewrite_path = "structured_error"

            row = {
                "key": key,
                "label": label,
                "should_modify": True,
                "reason": "explicit_ai_edit_mode",
                "new_title": source_title,
                "rewritten_content": blocks_to_plain_text(rewritten_blocks),
                "rewritten_blocks": rewritten_blocks,
            }
            before_fallback = list(row.get("rewritten_blocks", []) or [])
            row = self._enforce_structured_result(message, row, chunk)
            after_fallback = list(row.get("rewritten_blocks", []) or [])
            if after_fallback != before_fallback:
                rewrite_path = "fallback_forced"
            logger.info(
                "explicit_rewrite chunk_finish key=%s path=%s block_count=%s meta_artifact=%s content_preview=%s",
                key,
                rewrite_path,
                len(after_fallback),
                self._looks_like_meta_artifact(str(row.get("rewritten_content", ""))),
                str(row.get("rewritten_content", ""))[:160],
            )
            rewritten_rows.append(row)
        return {
            "is_modification": True,
            "chat_reply": "",
            "chunks": rewritten_rows,
        }

    def _needs_structured_card(self, message: str) -> bool:
        text = (message or "").strip().lower()
        if not text:
            return False
        direct_terms = (
            "可交互",
            "模拟例子",
            "模拟器",
            "可运行",
            "运行例子",
            "拖动演示",
            "demo",
            "interactive",
            "widget",
        )
        if any(token in text for token in direct_terms):
            return True
        patterns = (
            r"做(?:一个|一张|成)?[^。！？\n]{0,12}(?:演示|模拟|例子|demo)",
            r"(?:添加|插入|放一段|加(?:一个|一张|一段)?)[^。！？\n]{0,8}(?:视频|图片|图像|插图)",
            r"做成[^。！？\n]{0,12}(?:交互|可运行|可交互)",
            r"(?:做|加|插入)[^。！？\n]{0,10}(?:interactive|widget)",
        )
        return any(re.search(pattern, text, re.I) for pattern in patterns)

    def _looks_like_meta_artifact(self, text: str) -> bool:
        lowered = (text or "").strip().lower()
        if not lowered:
            return False
        has_html_structure = (
            "<!doctype html" in lowered
            or ("<html" in lowered and "<body" in lowered)
            or ("<script" in lowered and "<style" in lowered and "function" in lowered)
        )
        if has_html_structure:
            return True
        meta_phrases = (
            "复制下面代码到",
            "保存为 .html 文件",
            "保存为html文件",
            "双击 index.html",
            "双击文件，浏览器中打开",
            "请在编辑器中使用",
            "ai卡片生成工具",
            "演示概念",
            "交互设计",
            "下一步操作",
            "需要我帮你",
            "或者直接告诉我",
            "我来帮你设计",
            "核心理念",
            "预期交互",
            "如何创建",
        )
        return any(phrase in lowered for phrase in meta_phrases)

    def _contains_supported_display_blocks(self, text: str) -> bool:
        value = self._sanitize_structured_markdown(text)
        return bool(re.search(r"```(?:example|demo|image|video|interactive)\b", value, re.I))

    def _sanitize_structured_markdown(self, text: str) -> str:
        value = str(text or "")
        if not value:
            return value
        # Turn escaped newlines back into real newlines for model outputs that flattened markdown.
        value = value.replace("\\n", "\n")
        # Be tolerant to malformed double-backtick fences produced by the model or intermediate layers.
        value = re.sub(
            r"(?m)^(\s*)``(?=\s*(?:example|demo|image|video|interactive)\b)",
            r"\1```",
            value,
        )
        value = re.sub(r"(?m)^(\s*)``\s*$", r"\1```", value)
        return value

    def _build_structured_card_fallback(self, message: str, source_chunk: dict) -> list[dict]:
        text = (message or "").strip().lower()
        title = str(source_chunk.get("title", "")).strip() or "交互演示"
        content = str(source_chunk.get("content", "")).strip()

        first_paragraph = ""
        for piece in re.split(r"\n\s*\n+", content):
            cleaned = piece.strip()
            if cleaned:
                first_paragraph = re.sub(r"\s+", " ", cleaned)
                break
        if len(first_paragraph) > 120:
            first_paragraph = first_paragraph[:117].rstrip() + "..."

        intro = f"下面给出一个最小可运行的卡片示例，帮助理解“{title}”。"
        if first_paragraph:
            intro += f"\n\n核心提示：{first_paragraph}"

        if any(token in text for token in ("拖动", "拖拽")):
            demo_title = title if len(title) <= 24 else (title[:24].rstrip() + "…")
            blocks = [
                {"type": "paragraph", "text": intro},
                {
                    "type": "interactive",
                    "title": f"{demo_title} · 结构演示",
                    "description": "拖动点 P，观察同一条边上的位置变化如何对应表达层次的推进。",
                    "widget": "triangle-point-slider",
                    "edge": "AB",
                    "initial_t": 0.42,
                    "triangle": [[0.08, 0.86], [0.92, 0.86], [0.46, 0.14]],
                },
            ]
            return normalize_structured_blocks(blocks)

        if any(token in text for token in ("可交互", "模拟", "demo", "演示", "可运行")):
            demo_title = title if len(title) <= 24 else (title[:24].rstrip() + "…")
            blocks = [
                {"type": "paragraph", "text": intro},
                {
                    "type": "demo",
                    "title": f"{demo_title} · 模拟练习",
                    "description": "点击运行，观察更好的表达方式会如何组织信息。",
                    "mode": "typing",
                    "input": "先铺陈细节，再让听众自己猜结论",
                    "output": "先给结论，再给两到三点支撑理由，最后补充关键细节。",
                    "button_label": "运行看看",
                },
            ]
            return normalize_structured_blocks(blocks)

        return []

    def _enforce_structured_result(self, message: str, row: dict, source_chunk: dict) -> dict:
        if not row.get("should_modify") or not self._needs_structured_card(message):
            return row
        current_blocks = normalize_structured_blocks(row.get("rewritten_blocks", []))
        row["rewritten_blocks"] = current_blocks
        row["rewritten_content"] = blocks_to_plain_text(current_blocks)
        if current_blocks and not self._looks_like_meta_artifact(str(row.get("rewritten_content", ""))):
            return row
        fallback = self._build_structured_card_fallback(message, source_chunk)
        if fallback:
            row["rewritten_blocks"] = fallback
            row["rewritten_content"] = blocks_to_plain_text(fallback)
        return row

    def _normalize_intent_result(
        self,
        result: Dict[str, Any],
        message: str,
        chunks: list,
        llm,
    ) -> Dict[str, Any]:
        if not isinstance(result, dict):
            return {"is_modification": False, "chat_reply": "", "chunks": []}
        if not result.get("is_modification"):
            return result
        by_key = {
            str(chunk.get("key", "")): chunk
            for chunk in (chunks or [])
            if isinstance(chunk, dict) and chunk.get("key")
        }
        needs_structured = self._needs_structured_card(message)
        structured_rewrites = 0
        structured_rewrite_limit = 1
        normalized_chunks = []
        for item in result.get("chunks", []) or []:
            if not isinstance(item, dict):
                continue
            row = dict(item)
            rewritten_blocks = normalize_structured_blocks(row.get("rewritten_blocks", []))
            if rewritten_blocks:
                row["rewritten_blocks"] = rewritten_blocks
                row["rewritten_content"] = blocks_to_plain_text(rewritten_blocks)
            source_chunk = by_key.get(str(row.get("key", "")), {})
            current_content = str(row.get("rewritten_content", "")).strip()
            row["rewritten_content"] = current_content
            if row.get("should_modify") and needs_structured and (
                not current_content or self._looks_like_meta_artifact(current_content)
                or not rewritten_blocks
            ) and structured_rewrites < structured_rewrite_limit:
                structured = normalize_structured_blocks(llm.rewrite_chunk_structured(
                    original=str(source_chunk.get("content", "")),
                    instruction=message,
                ))
                if structured:
                    row["rewritten_blocks"] = structured
                    row["rewritten_content"] = blocks_to_plain_text(structured)
                    structured_rewrites += 1
                    rewritten_blocks = structured
                    current_content = str(row.get("rewritten_content", "")).strip()
            current_content = str(row.get("rewritten_content", "")).strip()
            row["rewritten_content"] = current_content
            if row.get("should_modify") and needs_structured and (
                not current_content or self._looks_like_meta_artifact(current_content)
                or not rewritten_blocks
            ):
                fallback = self._build_structured_card_fallback(message, source_chunk)
                if fallback:
                    row["rewritten_blocks"] = fallback
                    row["rewritten_content"] = blocks_to_plain_text(fallback)
            row = self._enforce_structured_result(message, row, source_chunk)
            normalized_chunks.append(row)
        result["chunks"] = normalized_chunks
        return result

    def apply_chunk_modification(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """应用 chunk 修改"""
        try:
            chunk_id = payload.get("chunk_id", "")
            new_blocks = normalize_structured_blocks(payload.get("new_blocks", []))
            new_title = payload.get("new_title")
            expected_version = payload.get("expected_version", 0)

            if not chunk_id:
                return self._error(NotFoundError("chunk_id is required"))
            if not new_blocks:
                return self._error(NotFoundError("new_blocks is required"))

            # 找到 chunk
            cw, chunk = self._find_chunk_by_key(chunk_id)
            if expected_version != cw.current_version:
                from ariadne.domain.errors import VersionConflictError
                raise VersionConflictError("version mismatch", field="expected_version", reason="current version changed")

            # 更新内容和标题
            chunk.blocks = new_blocks
            chunk.content = blocks_to_plain_text(new_blocks)
            if new_title:
                chunk.title = new_title

            cw.current_version += 1

            # 同步到 markdown
            from ariadne.application.knowledge import chunks_to_markdown
            cw.knowledge_markdown = chunks_to_markdown(topic=cw.topic, chunks=cw.chunks)
            cw.knowledge_doc_path = self.courseware.knowledge_store.save(
                cw.id, cw.knowledge_markdown, source_asset_ids=cw.source_asset_ids
            )

            # 保存到 repo
            self.courseware.repo.save(cw)
            self.courseware.event_store.add("chunk_modification_apply", {
                "chunk_key": chunk_id,
                "version": cw.current_version,
                "title_updated": new_title is not None
            })
            logger.info("chunk modification applied key=%s version=%s", chunk_id, cw.current_version)

            return self._ok({
                "version": cw.current_version,
                "outline_updated": new_title is not None
            })
        except AriadneError as exc:
            return self._error(exc)
        except Exception as exc:
            logger.exception("apply_chunk_modification error")
            return self._ok({
                "error": str(exc)
            })

    def _find_chunk_by_key(self, chunk_key: str) -> tuple:
        """根据 chunk_key (格式: "章节索引-chunk索引") 找到 chunk"""
        # chunk_key 格式是 "章节索引-chunk索引"
        parts = chunk_key.split("-")
        if len(parts) != 2:
            raise NotFoundError(f"invalid chunk_key format: {chunk_key}")

        try:
            chapter_idx = int(parts[0])
            chunk_idx = int(parts[1])
        except ValueError:
            raise NotFoundError(f"invalid chunk_key format: {chunk_key}")

        target_chapter_no = chapter_idx + 1
        target_chunk_no = chunk_idx + 1

        # 遍历所有 courseware 找到对应的 chunk
        for cw in self.courseware.repo.list_all():
            for chunk in cw.chunks:
                if chunk.chapter_no == target_chapter_no and chunk.chunk_no == target_chunk_no:
                    return cw, chunk

        raise NotFoundError(f"chunk not found: {chunk_key}")

    def delete_chunk(self, chunk_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            courseware_id = payload.get("courseware_id", "")
            if not courseware_id:
                return self._error(NotFoundError("courseware_id is required"))
            result = self.courseware.delete_chunk(
                courseware_id=courseware_id,
                chunk_id=chunk_id,
                expected_version=int(payload.get("expected_version", 0)),
            )
            return self._ok({"chunk_id": chunk_id, "courseware_id": courseware_id, **result})
        except AriadneError as exc:
            return self._error(exc)
