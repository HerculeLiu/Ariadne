"""GLM Embedding-3 API Client for text vectorization."""

from __future__ import annotations

import json
import time
from typing import List
from urllib import request

from ariadne.application.config import AppConfig
from ariadne.domain.errors import LLMServiceError
from ariadne.infrastructure.app_logger import get_logger

logger = get_logger("embedding")


class EmbeddingClient:
    """GLM Embedding-3 API client."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.api_key = config.llm_api_key
        self.api_base = config.llm_api_base
        self.timeout_sec = config.llm_timeout_sec
        self.max_retries = config.llm_max_retries
        self.retry_delay_sec = config.llm_retry_delay_sec

    def encode(self, texts: List[str], model: str = "embedding-3") -> List[List[float]]:
        """
        Encode texts to embedding vectors.

        Args:
            texts: List of text strings to encode
            model: Model name, default "embedding-3"

        Returns:
            List of embedding vectors (list of floats)

        Raises:
            LLMServiceError: If API call fails
        """
        if not texts:
            return []

        if self.config.model_provider == "mock":
            logger.debug("mock embedding response used")
            # Return mock vectors with fixed dimension (1024)
            dim = 1024
            return [[0.0] * dim for _ in texts]

        if not self.api_key:
            raise LLMServiceError(
                "API key is missing for embedding",
                field="api_key",
                reason="Configure GLM_API_KEY",
            )

        url = f"{self.api_base.rstrip('/')}/embeddings"
        logger.info("embedding request model=%s texts=%d url=%s", model, len(texts), url)

        payload = {
            "model": model,
            "input": texts,
            "encoding_format": "float",
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
                        f"Embedding API error: {data['error']}",
                        reason=data.get("error", {}).get("message", "Unknown error"),
                    )

                embeddings = []
                for item in data.get("data", []):
                    embedding = item.get("embedding", [])
                    if not embedding:
                        raise LLMServiceError("Empty embedding in response", reason="embedding field is empty")
                    embeddings.append(embedding)

                logger.info(
                    "embedding success model=%s embeddings=%d dim=%d attempt=%s",
                    model,
                    len(embeddings),
                    len(embeddings[0]) if embeddings else 0,
                    attempt + 1,
                )
                return embeddings

            except LLMServiceError as exc:
                last_exc = exc
                logger.exception("embedding service error")
                break

            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "embedding request failed attempt=%s/%s reason=%s",
                    attempt + 1,
                    self.max_retries + 1,
                    exc,
                )
                if attempt < self.max_retries:
                    delay = self.retry_delay_sec * (2**attempt)
                    time.sleep(delay)
                else:
                    logger.exception("embedding request exhausted retries")

        raise LLMServiceError(
            "Embedding request failed",
            reason=f"url={url}, error={last_exc}",
        )

    def encode_single(self, text: str, model: str = "embedding-3") -> List[float]:
        """
        Encode a single text to embedding vector.

        Args:
            text: Text string to encode
            model: Model name, default "embedding-3"

        Returns:
            Embedding vector (list of floats)
        """
        result = self.encode([text], model=model)
        return result[0] if result else []
