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

    def generate_understanding_markdown(self, topic: str, keywords: list[str], difficulty: str, style: str, template: str) -> str:
        # New naming: 理解层提示词
        system_prompt = self.prompts.get("understand_layer.md") or self.prompts.get("generate_courseware.md")
        user_prompt = (
            f"topic={topic}\nkeywords={keywords}\ndifficulty={difficulty}\nstyle={style}\ntemplate={template}\n"
            "请输出3-5个学习chunk，每个chunk给出标题和内容。"
        )
        return self._chat([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ])

    def generate_outline_markdown(self, topic: str, keywords: list[str], difficulty: str, style: str, template: str) -> str:
        system_prompt = self.prompts.get("outline_layer.md") or self.prompts.get("understand_layer.md")
        user_prompt = (
            f"topic={topic}\nkeywords={keywords}\ndifficulty={difficulty}\nstyle={style}\ntemplate={template}\n"
            "请只输出章节和chunk标题，不要写正文段落。"
        )
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
        difficulty: str,
        style: str,
        template: str,
    ) -> str:
        system_prompt = self.prompts.get("chunk_layer.md") or self.prompts.get("understand_layer.md")
        user_prompt = (
            f"topic={topic}\nchapter_title={chapter_title}\nchapter_summary={chapter_summary}\n"
            f"chunk_title={chunk_title}\ndifficulty={difficulty}\nstyle={style}\ntemplate={template}\n"
            "请仅输出该chunk正文内容，不要重复章节或chunk标题。"
        )
        return self._chat([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ])

    def generate_courseware_text(self, topic: str, keywords: list[str], difficulty: str, style: str, template: str) -> str:
        # Backward compatibility
        return self.generate_understanding_markdown(topic, keywords, difficulty, style, template)

    def answer_chunk_question(self, context: str, question: str, mode: str) -> str:
        system_prompt = self.prompts.get("chunk_qa.md")
        user_prompt = f"mode={mode}\ncontext={context}\nquestion={question}"
        return self._chat([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ])

    def chat_reply(self, context: str, message: str) -> str:
        system_prompt = self.prompts.get("chat_general.md")
        if not system_prompt.strip():
            system_prompt = (
                "You are a helpful general assistant. "
                "Reply clearly and directly. "
                "If the user asks for unsafe content, refuse briefly and suggest a safe alternative."
            )
        user_prompt = f"context={context}\nmessage={message}"
        return self._chat([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ])

    def rewrite_chunk(self, original: str, instruction: str) -> str:
        system_prompt = self.prompts.get("rewrite_chunk.md")
        user_prompt = f"instruction={instruction}\noriginal={original}"
        return self._chat([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ])
