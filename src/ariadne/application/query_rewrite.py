"""Heuristic query rewrite helpers for retrieval."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Iterable


_AMBIGUOUS_TERMS = (
    "这个",
    "这段",
    "这里",
    "这个项目",
    "这个经历",
    "这个部分",
    "那段",
    "那个",
    "它",
    "他",
    "她",
    "it",
    "this",
    "that",
    "they",
    "them",
    "he",
    "she",
)

_CN_STOPWORDS = {
    "什么", "怎么", "如何", "为什么", "是否", "一下", "一下子", "这个", "那个", "这里", "那边", "以及", "相关",
    "介绍", "说明", "请问", "一下子", "一下吧", "帮我", "关于", "是否有", "可以", "能否", "一下这个",
}

_CN_QUERY_SUFFIXES = (
    "讲的是什么",
    "讲了什么",
    "说的是什么",
    "讲什么",
    "说什么",
    "是什么意思",
    "是什么内容",
    "内容是什么",
    "是什么",
    "是啥",
    "吗",
    "呢",
)

_EN_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "what", "when", "where", "why",
    "how", "does", "did", "are", "was", "were", "can", "could", "would", "should", "about",
    "into", "onto", "have", "has", "had", "your", "their", "them", "they", "there", "then",
}


@dataclass
class QueryRewriteResult:
    original_query: str
    rewrite_queries: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)

    def all_queries(self) -> list[str]:
        values = [self.original_query, *self.rewrite_queries]
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            normalized = value.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)
        return result


class QueryRewriteService:
    """Generate retrieval-focused rewrites without changing user-visible text."""

    def rewrite(
        self,
        query: str,
        *,
        topic: str = "",
        chapter_title: str = "",
        chunk_title: str = "",
        selected_context: str = "",
        max_queries: int = 3,
    ) -> QueryRewriteResult:
        base_query = (query or "").strip()
        if not base_query:
            return QueryRewriteResult(original_query="")

        rewrites: list[str] = []
        keywords = self.extract_keywords(
            " ".join(x for x in [base_query, topic, chapter_title, chunk_title] if x)
        )

        if topic and topic not in base_query:
            rewrites.append(f"{topic} {base_query}".strip())

        if chapter_title or chunk_title:
            scoped = " ".join(x for x in [chapter_title, chunk_title, base_query] if x).strip()
            if scoped and scoped != base_query:
                rewrites.append(scoped)

        if selected_context and self._looks_ambiguous(base_query):
            context_hint = self._context_hint(selected_context)
            if context_hint:
                rewrites.append(f"{context_hint} {base_query}".strip())

        if len(base_query) <= 16 and keywords:
            keyword_scope = " ".join(keywords[:4]).strip()
            if keyword_scope:
                rewrites.append(f"{keyword_scope} {base_query}".strip())

        deduped: list[str] = []
        seen: set[str] = {base_query.strip()}
        for value in rewrites:
            norm = value.strip()
            if not norm or norm in seen:
                continue
            seen.add(norm)
            deduped.append(norm)
            if len(deduped) >= max_queries:
                break

        return QueryRewriteResult(
            original_query=base_query,
            rewrite_queries=deduped,
            keywords=keywords[:8],
        )

    def extract_keywords(self, text: str, max_keywords: int = 8) -> list[str]:
        values: list[str] = []

        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9._/-]{1,}", text or ""):
            normalized = token.strip()
            if len(normalized) < 2:
                continue
            lowered = normalized.lower()
            if lowered in _EN_STOPWORDS:
                continue
            values.append(normalized)

        for token in re.findall(r"[\u4e00-\u9fff]{2,16}", text or ""):
            normalized = token.strip()
            if normalized in _CN_STOPWORDS:
                continue
            normalized = self._normalize_cjk_query_token(normalized)
            if not normalized or normalized in _CN_STOPWORDS:
                continue
            if len(normalized) > 8:
                values.extend(self._split_long_cjk_token(normalized))
            elif len(normalized) > 4:
                values.append(normalized)
                values.extend(self._split_long_cjk_token(normalized))
            else:
                values.append(normalized)

        seen: set[str] = set()
        result: list[str] = []
        for token in values:
            if token in seen:
                continue
            seen.add(token)
            result.append(token)
            if len(result) >= max_keywords:
                break
        return result

    def _looks_ambiguous(self, query: str) -> bool:
        lowered = query.lower()
        return len(query.strip()) <= 16 or any(term in lowered for term in _AMBIGUOUS_TERMS)

    def _context_hint(self, selected_context: str) -> str:
        lines = [line.strip() for line in (selected_context or "").splitlines() if line.strip()]
        if not lines:
            return ""
        if lines[0].startswith("[") and lines[0].endswith("]"):
            lines = lines[1:]
        for line in lines:
            if len(line) <= 80:
                return self._trim_context_line(line)
        return self._trim_context_line(lines[0])

    def _trim_context_line(self, line: str) -> str:
        cleaned = re.sub(r"\s+", " ", line or "").strip()
        return cleaned[:80].strip()

    def _normalize_cjk_query_token(self, token: str) -> str:
        normalized = (token or "").strip()
        for suffix in _CN_QUERY_SUFFIXES:
            if normalized.endswith(suffix) and len(normalized) > len(suffix):
                normalized = normalized[: -len(suffix)]
                break
        normalized = re.sub(r"^(这个|这首|这段|这个部分|这篇|该|此)", "", normalized)
        normalized = re.sub(r"(讲|说|问)$", "", normalized)
        return normalized.strip()

    def _split_long_cjk_token(self, token: str) -> Iterable[str]:
        if len(token) <= 8:
            return [token]
        result: list[str] = []
        seen: set[str] = set()
        for size in (4, 6):
            for idx in range(0, len(token) - size + 1, max(1, size // 2)):
                piece = token[idx : idx + size]
                if piece not in _CN_STOPWORDS and piece not in seen:
                    seen.add(piece)
                    result.append(piece)
        return result[:4] or [token[:8]]
