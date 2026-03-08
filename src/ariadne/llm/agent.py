from __future__ import annotations

import json
import time
from pathlib import Path
from typing import List
from urllib import request

from ariadne.application.config import AppConfig
from ariadne.domain.errors import LLMServiceError
from ariadne.infrastructure.app_logger import get_logger

logger = get_logger("llm")


class PromptStore:
    def __init__(self, prompt_dir: str, hot_reload: bool = True) -> None:
        self.prompt_dir = Path(prompt_dir)
        self.hot_reload = hot_reload
        self._cache: dict[str, str] = {}

    def get(self, name: str) -> str:
        if not self.hot_reload and name in self._cache:
            return self._cache[name]
        path = self.prompt_dir / name
        if not path.exists():
            return ""
        content = path.read_text(encoding="utf-8")
        if not self.hot_reload:
            self._cache[name] = content
        return content


class LLMAgent:
    def __init__(self, config: AppConfig, prompts: PromptStore) -> None:
        self.config = config
        self.prompts = prompts

    def _chat(self, messages: List[dict]) -> str:
        if self.config.model_provider == "mock":
            logger.debug("mock llm response used")
            return "[mock-llm] " + (messages[-1].get("content", "")[:280] if messages else "")
        if not self.config.llm_api_key:
            raise LLMServiceError("llm api key is missing", field="api_key", reason="configure GLM_API_KEY/DEEPSEEK_API_KEY")

        url = f"{self.config.llm_api_base.rstrip('/')}{self.config.llm_chat_path}"
        logger.info("llm request provider=%s model=%s url=%s", self.config.model_provider, self.config.llm_model, url)
        payload = {
            "model": self.config.llm_model,
            "messages": messages,
            "temperature": 0.3,
        }

        req = request.Request(
            url,
            method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.llm_api_key}",
            },
        )
        last_exc: Exception | None = None
        for attempt in range(self.config.llm_max_retries + 1):
            try:
                with request.urlopen(req, timeout=self.config.llm_timeout_sec) as resp:
                    raw = resp.read().decode("utf-8")
                    data = json.loads(raw)
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                if not content:
                    raise LLMServiceError("empty llm response", reason="choices[0].message.content is empty")
                logger.info(
                    "llm request success model=%s content_len=%s attempt=%s",
                    self.config.llm_model,
                    len(content),
                    attempt + 1,
                )
                return content
            except LLMServiceError as exc:
                last_exc = exc
                logger.exception("llm service error")
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning(
                    "llm request failed attempt=%s/%s timeout=%ss reason=%s",
                    attempt + 1,
                    self.config.llm_max_retries + 1,
                    self.config.llm_timeout_sec,
                    exc,
                )
                if attempt < self.config.llm_max_retries:
                    delay = self.config.llm_retry_delay_sec * (2**attempt)
                    time.sleep(delay)
                else:
                    logger.exception("llm request exception exhausted retries")

        reason = (
            f"provider={self.config.model_provider}, model={self.config.llm_model}, "
            f"url={url}, timeout={self.config.llm_timeout_sec}s, error={last_exc}"
        )
        raise LLMServiceError("llm request failed", reason=reason)

    def generate_understanding_markdown(self, topic: str, keywords: list[str]) -> str:
        # New naming: 理解层提示词
        system_prompt = self.prompts.get("understand_layer.md") or self.prompts.get("generate_courseware.md")
        user_prompt = (
            f"topic={topic}\nkeywords={keywords}\n"
            "请输出3-5个学习chunk，每个chunk给出标题和内容。"
        )
        return self._chat([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ])

    def generate_outline_markdown(self, topic: str, keywords: list[str], rag_context: str = "") -> str:
        system_prompt = self.prompts.get("outline_layer.md") or self.prompts.get("understand_layer.md")
        user_prompt_parts = [
            f"topic={topic}",
            f"keywords={keywords}",
            "请只输出章节和chunk标题，不要写正文段落。",
        ]
        if rag_context:
            user_prompt_parts.insert(2, f"reference_materials={rag_context}")
            user_prompt_parts.append("请基于以上参考资料来设计章节结构。")

        user_prompt = "\n".join(user_prompt_parts)
        return self._chat([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ])

    def generate_chunk_content(
        self,
        topic: str,
        chapter_title: str,
        chapter_summary: str,
        chunk_title: str,
        rag_context: str = "",
        writing_style: str = "",
        audience_level: str = "",
    ) -> str:
        system_prompt = self.prompts.get("explain_layer.md")
        if not system_prompt.strip():
            raise LLMServiceError(
                "missing explain layer prompt",
                field="prompt",
                reason="src/prompt/explain_layer.md is required for content generation",
            )
        user_prompt_parts = [
            f"topic={topic}",
            f"chapter_title={chapter_title}",
            f"chapter_summary={chapter_summary}",
            f"chunk_title={chunk_title}",
        ]
        if writing_style:
            user_prompt_parts.append(f"writing_style={writing_style}")
        if audience_level:
            user_prompt_parts.append(f"audience_level={audience_level}")
        if rag_context:
            user_prompt_parts.append(f"reference_materials={rag_context}")
            user_prompt_parts.append("请基于以上参考资料生成该 chunk 的讲解内容；如有补充，请与参考资料保持一致且明确克制。")
        else:
            user_prompt_parts.append("请输出该 chunk 的讲解内容，不要重复章节或 chunk 标题。")
        user_prompt = "\n".join(user_prompt_parts)
        return self._chat([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ])

    def generate_courseware_text(self, topic: str, keywords: list[str]) -> str:
        # Backward compatibility
        return self.generate_understanding_markdown(topic, keywords)

    def answer_chunk_question(self, context: str, question: str, mode: str, rag_context: str | None = None) -> str:
        system_prompt = self.prompts.get("chunk_qa.md")

        # Build user prompt with optional RAG context
        user_prompt_parts = [f"mode={mode}", f"context={context}", f"question={question}"]
        if rag_context:
            user_prompt_parts.insert(2, f"reference_materials={rag_context}")

        user_prompt = "\n".join(user_prompt_parts)
        return self._chat([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ])

    def chat_reply(
        self,
        context: str,
        message: str,
        rag_context: str | None = None,
        chat_history: List[Dict[str, str]] | None = None,
    ) -> str:
        """
        Generate chat reply with optional history and RAG context.

        Args:
            context: Session context string
            message: Current user message
            rag_context: Optional RAG retrieval results
            chat_history: Optional conversation history [{"role": "user/assistant", "content": "..."}]
        """
        system_prompt = self.prompts.get("chat_general.md")
        if not system_prompt.strip():
            system_prompt = (
                "You are a helpful general assistant. "
                "Reply clearly and directly. "
                "If the user asks for unsafe content, refuse briefly and suggest a safe alternative."
            )

        # Build messages list
        messages = [{"role": "system", "content": system_prompt}]

        # Add chat history if provided
        if chat_history:
            messages.extend(chat_history)

        # Build user prompt with optional RAG context
        user_prompt_parts = [f"context={context}", f"message={message}"]
        if rag_context:
            user_prompt_parts.insert(1, f"reference_materials={rag_context}")

        user_prompt = "\n".join(user_prompt_parts)
        messages.append({"role": "user", "content": user_prompt})

        return self._chat(messages)

    def rewrite_chunk(self, original: str, instruction: str) -> str:
        system_prompt = self.prompts.get("rewrite_chunk.md")
        user_prompt = f"instruction={instruction}\noriginal={original}"
        return self._chat([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ])
