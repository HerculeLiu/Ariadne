"""Context compression service for chat history and RAG content."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Tuple

from ariadne.domain.models import ChatMessage, utc_now_iso
from ariadne.infrastructure.app_logger import get_logger

if TYPE_CHECKING:
    from ariadne.llm.agent import LLMAgent

logger = get_logger("compression")

# Token estimation: 1 character ≈ 2 tokens (conservative for mixed CN/EN)
TOKEN_ESTIMATE_RATIO = 2.0


@dataclass
class CompressionConfig:
    max_context_tokens: int = 16000
    compression_threshold: float = 0.8
    keep_start_ratio: float = 0.2
    keep_end_ratio: float = 0.2


@dataclass
class CompressionResult:
    was_compressed: bool
    original_tokens: int
    compressed_tokens: int
    compressed_content: str = ""
    message_ids_affected: List[str] = field(default_factory=list)
    tokens_saved: int = 0

    def __post_init__(self) -> None:
        if self.was_compressed and self.tokens_saved == 0:
            self.tokens_saved = self.original_tokens - self.compressed_tokens


class ContextCompressionService:
    """
    Service for compressing chat context when it exceeds token limits.

    Compression strategy:
    - Keep first 20% of messages (recent context foundation)
    - Keep last 20% of messages (immediate context)
    - Compress middle 60% into a summary
    """

    def __init__(self, llm: "LLMAgent", config: CompressionConfig | None = None) -> None:
        self.llm = llm
        self.config = config or CompressionConfig()

    def estimate_tokens(self, text: str) -> int:
        """Estimate token count for text (conservative: 1 char ≈ 2 tokens)."""
        if not text:
            return 0
        return int(len(text) * TOKEN_ESTIMATE_RATIO)

    def should_compress(self, content: str) -> bool:
        """Check if content exceeds compression threshold."""
        estimated = self.estimate_tokens(content)
        threshold = int(self.config.max_context_tokens * self.config.compression_threshold)
        return estimated >= threshold

    def compress_chat_messages(
        self,
        messages: List[ChatMessage],
    ) -> Tuple[List[ChatMessage], CompressionResult]:
        """
        Compress chat message list by summarizing middle messages.

        Preserves first 20% and last 20%, compresses middle 60%.
        """
        if not messages or len(messages) < 4:
            return messages, CompressionResult(was_compressed=False, original_tokens=0, compressed_tokens=0)

        # Calculate total tokens
        total_content = "\n".join([f"{m.role}: {m.content}" for m in messages])
        total_tokens = self.estimate_tokens(total_content)

        if not self.should_compress(total_content):
            return messages, CompressionResult(
                was_compressed=False,
                original_tokens=total_tokens,
                compressed_tokens=total_tokens,
            )

        n = len(messages)
        keep_start = max(1, int(n * self.config.keep_start_ratio))
        keep_end = max(1, int(n * self.config.keep_end_ratio))

        # Ensure enough messages remain
        if keep_start + keep_end >= n:
            keep_start = n // 3
            keep_end = n // 3

        # Messages to compress (middle section)
        to_compress = messages[keep_start : n - keep_end]
        keep_messages = messages[:keep_start] + messages[n - keep_end:]

        # Build compression prompt
        compress_text = self._build_compress_prompt(to_compress)
        compressed_summary = self._summarize(compress_text)

        # Create compressed message (inserted as system message)
        compressed_msg = ChatMessage(
            id=f"compressed_{uuid.uuid4().hex[:8]}",
            session_id=messages[0].session_id,
            role="system",
            content=f"[Earlier conversation summary: {compressed_summary}]",
            created_at=utc_now_iso(),
            is_compressed=True,
            original_content=compress_text,
            compression_metadata={
                "compressed_at": utc_now_iso(),
                "original_length": len(compress_text),
                "compressed_length": len(compressed_summary),
                "message_range": [keep_start, n - keep_end],
            },
        )

        # New message list: start + compressed + end
        new_messages = (
            messages[:keep_start]
            + [compressed_msg]
            + messages[n - keep_end:]
        )

        # Mark compressed messages
        compression_meta = compressed_msg.compression_metadata
        for msg in to_compress:
            msg.is_compressed = True
            msg.compression_metadata = compression_meta

        new_tokens = self.estimate_tokens("\n".join([f"{m.role}: {m.content}" for m in new_messages]))

        return new_messages, CompressionResult(
            was_compressed=True,
            original_tokens=total_tokens,
            compressed_tokens=new_tokens,
            compressed_content=compressed_summary,
            message_ids_affected=[m.id for m in to_compress],
        )

    def compress_rag_context(
        self,
        rag_context: str,
    ) -> Tuple[str, CompressionResult]:
        """
        Compress RAG context by summarizing middle sections.

        Preserves first 20% and last 20% of content blocks.
        """
        if not rag_context:
            return rag_context, CompressionResult(
                was_compressed=False,
                original_tokens=0,
                compressed_tokens=0,
            )

        original_tokens = self.estimate_tokens(rag_context)

        if not self.should_compress(rag_context):
            return rag_context, CompressionResult(
                was_compressed=False,
                original_tokens=original_tokens,
                compressed_tokens=original_tokens,
            )

        # Split into blocks
        lines = rag_context.split("\n\n")
        n = len(lines)
        keep_start = max(1, int(n * self.config.keep_start_ratio))
        keep_end = max(1, int(n * self.config.keep_end_ratio))

        if keep_start + keep_end >= n:
            return rag_context, CompressionResult(
                was_compressed=False,
                original_tokens=original_tokens,
                compressed_tokens=original_tokens,
            )

        keep_lines = lines[:keep_start] + lines[n - keep_end:]
        compress_lines = lines[keep_start : n - keep_end]
        compress_text = "\n\n".join(compress_lines)

        compressed_summary = self._summarize(
            f"Summarize the following reference materials concisely:\n\n{compress_text}"
        )

        # Insert compressed content
        compressed_context = "\n\n".join([
            *lines[:keep_start],
            f"[Summary of reference materials: {compressed_summary}]",
            *lines[n - keep_end:],
        ])

        compressed_tokens = self.estimate_tokens(compressed_context)

        return compressed_context, CompressionResult(
            was_compressed=True,
            original_tokens=original_tokens,
            compressed_tokens=compressed_tokens,
            compressed_content=compressed_summary,
        )

    def _build_compress_prompt(self, messages: List[ChatMessage]) -> str:
        """Build compression prompt from messages."""
        lines = []
        for msg in messages:
            lines.append(f"{msg.role}: {msg.content}")
        return "\n".join(lines)

    def _summarize(self, text: str) -> str:
        """Call LLM to summarize text."""
        system_prompt = (
            "You are a conversation summarizer. "
            "Summarize the given conversation or content concisely in Chinese, "
            "preserving key information and context. "
            "Output in plain text without markdown formatting. "
            "Focus on: main topics discussed, key decisions made, important questions asked."
        )
        try:
            return self.llm._chat([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Summarize this:\n\n{text}"},
            ])
        except Exception as exc:
            logger.warning("Compression summary failed: %s", exc)
            return f"[Summary unavailable: {text[:200]}...]"
