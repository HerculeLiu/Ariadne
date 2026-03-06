"""GLM Rerank API Client for document re-ranking."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import List
from urllib import request

from ariadne.application.config import AppConfig
from ariadne.domain.errors import LLMServiceError
from ariadne.infrastructure.app_logger import get_logger

logger = get_logger("rerank")


@dataclass
class RerankResult:
    """Result from rerank API."""

    index: int
    relevance_score: float
    document: str


class RerankClient:
    """GLM Rerank API client."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.api_key = config.llm_api_key
        self.api_base = config.llm_api_base
        self.timeout_sec = config.llm_timeout_sec
        self.max_retries = config.llm_max_retries
        self.retry_delay_sec = config.llm_retry_delay_sec

    def rerank(
        self,
        query: str,
        documents: List[str],
        top_n: int | None = None,
        model: str = "rerank",
    ) -> List[RerankResult]:
        """
        Rerank documents by relevance to query.

        Args:
            query: Query text
            documents: List of candidate documents
            top_n: Return top N results (None for all)
            model: Model name, default "rerank"

        Returns:
            List of RerankResult sorted by relevance score

        Raises:
            LLMServiceError: If API call fails
        """
        if not documents:
            return []

        if self.config.model_provider == "mock":
            logger.debug("mock rerank response used")
            # Return mock results in original order
            return [
                RerankResult(
                    index=i,
                    relevance_score=1.0 - (i * 0.1),  # Decreasing scores
                    document=doc,
                )
                for i, doc in enumerate(documents[: top_n or len(documents)])
            ]

        if not self.api_key:
            raise LLMServiceError(
                "API key is missing for rerank",
                field="api_key",
                reason="Configure GLM_API_KEY",
            )

        url = f"{self.api_base.rstrip('/')}/rerank"
        logger.info("rerank request model=%s documents=%d url=%s", model, len(documents), url)

        payload = {
            "model": model,
            "query": query,
            "documents": documents,
            "top_n": top_n if top_n is not None else len(documents),
            "return_documents": True,
        }

        req = request.Request(
            url,
            method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with request.urlopen(req, timeout=self.timeout_sec) as resp:
                    raw = resp.read().decode("utf-8")
                    data = json.loads(raw)

                if "error" in data:
                    raise LLMServiceError(
                        f"Rerank API error: {data['error']}",
                        reason=data.get("error", {}).get("message", "Unknown error"),
                    )

                results = []
                for item in data.get("results", []):
                    results.append(
                        RerankResult(
                            index=item.get("index", -1),
                            relevance_score=item.get("relevance_score", 0.0),
                            document=item.get("document", ""),
                        )
                    )

                logger.info(
                    "rerank success model=%s results=%d attempt=%s",
                    model,
                    len(results),
                    attempt + 1,
                )
                return results

            except LLMServiceError as exc:
                last_exc = exc
                logger.exception("rerank service error")
                break

            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "rerank request failed attempt=%s/%s reason=%s",
                    attempt + 1,
                    self.max_retries + 1,
                    exc,
                )
                if attempt < self.max_retries:
                    delay = self.retry_delay_sec * (2**attempt)
                    time.sleep(delay)
                else:
                    logger.exception("rerank request exhausted retries")

        raise LLMServiceError(
            "Rerank request failed",
            reason=f"url={url}, error={last_exc}",
        )
