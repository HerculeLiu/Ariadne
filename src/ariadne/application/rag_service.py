"""RAG (Retrieval-Augmented Generation) service for Ariadne."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Tuple

from ariadne.application.config import AppConfig
from ariadne.application.query_rewrite import QueryRewriteResult, QueryRewriteService
from ariadne.application.text_splitter import TextFragment, split_fragments_from_asset_objects
from ariadne.domain.models import Asset, utc_now_iso
from ariadne.infrastructure.app_logger import get_logger
from ariadne.infrastructure.vector_store import DocumentFragment, VectorStore

if TYPE_CHECKING:
    from ariadne.llm.embedding_client import EmbeddingClient

logger = get_logger("rag")


@dataclass
class RetrievalResult:
    """Result from vector retrieval."""

    fragment_id: str
    asset_id: str
    text: str
    relevance_score: float
    order_no: int = 0
    source_start: int = 0
    source_end: int = 0
    block_type: str = "paragraph"
    section_title: str = ""
    page_no: int = 0
    heading_path: list[str] = field(default_factory=list)
    retrieval_mode: str = "vector"


class RAGService:
    """
    RAG service for document-based content generation.

    Provides:
    - Processing uploaded assets into searchable fragments
    - Vector storage management
    - Retrieval for content generation
    """

    def __init__(
        self,
        config: AppConfig,
        vector_store: VectorStore,
        embedding_client: "EmbeddingClient" = None,
        asset_repo=None,
        search_repo=None,
        query_rewriter: QueryRewriteService | None = None,
    ) -> None:
        """
        Initialize RAG service.

        Args:
            config: Application config
            vector_store: Vector store instance
            embedding_client: Embedding client for vector generation
        """
        self.config = config
        self.vector_store = vector_store
        self.embedding_client = embedding_client
        self.asset_repo = asset_repo
        self.search_repo = search_repo
        self.query_rewriter = query_rewriter or QueryRewriteService()
        if not self.embedding_client:
            logger.warning("RAG initialized without embedding client; vector retrieval rewrites will be skipped")

    def process_pre_split_fragments(
        self,
        fragments: List[Tuple[str, TextFragment]],
        asset_id: str = None,
    ) -> int:
        """
        Process pre-split text fragments into vector store.

        This is more efficient when text has already been extracted and split.

        Args:
            fragments: List of (asset_id, TextFragment) tuples
            asset_id: Optional asset ID for logging

        Returns:
            Number of fragments added
        """
        if not fragments:
            logger.info("No fragments to process")
            return 0

        # Generate embeddings for all fragments
        if not self.embedding_client:
            logger.warning("Embedding client not available, skipping vectorization")
            return 0

        # Prepare texts for batch encoding
        texts = [frag.text for _, frag in fragments]
        logger.info("Generating embeddings for %d fragments (asset=%s)", len(texts), asset_id or "unknown")

        try:
            embeddings = self.embedding_client.encode(texts)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to generate embeddings: %s", exc)
            return 0

        logger.info("Embeddings generated, now storing to vector store")

        # Store fragments in vector store
        doc_fragments = []
        for (asset_id, frag), embedding in zip(fragments, embeddings):
            doc_fragment = DocumentFragment(
                id=make_fragment_id(asset_id, frag.order_no),
                asset_id=asset_id,
                text=frag.text,
                embedding=embedding,
                order_no=frag.order_no,
                source_start=frag.source_start,
                source_end=frag.source_end,
                block_type=frag.block_type,
                section_title=frag.section_title,
                page_no=frag.page_no,
                heading_path_text=" > ".join(frag.heading_path),
            )
            doc_fragments.append(doc_fragment)

        # Batch add all fragments at once
        added_count = self.vector_store.add_fragments(doc_fragments)

        logger.info(
            "Processed %d fragments into vector store (asset=%s)",
            added_count,
            asset_id or "unknown",
        )
        return added_count

    def process_assets(self, assets: List[Asset]) -> int:
        """
        Process assets into vector store.

        1. Extract text from assets
        2. Split text into fragments
        3. Generate embeddings
        4. Store in vector database

        Args:
            assets: List of assets to process

        Returns:
            Number of fragments added
        """
        if not assets:
            logger.info("No assets to process")
            return 0

        # Filter assets that have storage_path (actual file content)
        valid_assets = [a for a in assets if a.storage_path]
        if not valid_assets:
            logger.warning("No assets with storage_path found")
            return 0

        # Split assets into text fragments
        fragments = split_fragments_from_asset_objects(valid_assets)
        if not fragments:
            logger.warning("No fragments generated from assets")
            return 0

        # Generate embeddings for all fragments
        if not self.embedding_client:
            logger.warning("Embedding client not available, skipping vectorization")
            return 0

        # Prepare texts for batch encoding
        texts = [frag.text for _, frag in fragments]
        logger.info("Generating embeddings for %d fragments", len(texts))

        try:
            embeddings = self.embedding_client.encode(texts)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to generate embeddings: %s", exc)
            return 0

        # Store fragments in vector store
        # Build DocumentFragment objects for batch add
        doc_fragments = []
        for (asset_id, frag), embedding in zip(fragments, embeddings):
            doc_fragment = DocumentFragment(
                id=make_fragment_id(asset_id, frag.order_no),
                asset_id=asset_id,
                text=frag.text,
                embedding=embedding,
                order_no=frag.order_no,
                source_start=frag.source_start,
                source_end=frag.source_end,
                block_type=frag.block_type,
                section_title=frag.section_title,
                page_no=frag.page_no,
                heading_path_text=" > ".join(frag.heading_path),
            )
            doc_fragments.append(doc_fragment)

        # Batch add all fragments at once
        added_count = self.vector_store.add_fragments(doc_fragments)

        logger.info(
            "Processed %d assets into %d fragments in vector store",
            len(valid_assets),
            added_count,
        )
        return added_count

    def retrieve(
        self,
        query: str,
        query_embedding: List[float],
        top_k: int = 3,
        asset_ids: List[str] | None = None,
        search_result_ids: List[str] | None = None,
        rewrite_context: dict | None = None,
    ) -> List[RetrievalResult]:
        """
        Retrieve relevant fragments for a query.

        Args:
            query: Query text
            query_embedding: Query vector (pre-computed)
            top_k: Number of results to return
            asset_ids: Optional filter by asset IDs

        Returns:
            List of RetrievalResult objects
        """
        rewrite_context = rewrite_context or {}
        rewrite_plan = self.query_rewriter.rewrite(query, **rewrite_context)

        vector_results = self._vector_retrieve(
            rewrite_plan=rewrite_plan,
            original_embedding=query_embedding,
            top_k=max(top_k, 3),
            asset_ids=asset_ids,
            search_result_ids=search_result_ids,
        )
        keyword_results = self._keyword_retrieve(
            rewrite_plan=rewrite_plan,
            top_k=max(top_k, 3),
            asset_ids=asset_ids,
            search_result_ids=search_result_ids,
        )
        results = self._fuse_results(vector_results, keyword_results, top_k=top_k)

        logger.info(
            "Retrieved %d fragments for query=%s rewrites=%s keywords=%s asset_ids=%s search_result_ids=%s",
            len(results),
            query[:120],
            rewrite_plan.rewrite_queries,
            rewrite_plan.keywords,
            asset_ids,
            search_result_ids,
        )
        return results

    def _vector_retrieve(
        self,
        *,
        rewrite_plan: QueryRewriteResult,
        original_embedding: List[float],
        top_k: int,
        asset_ids: List[str] | None,
        search_result_ids: List[str] | None,
    ) -> List[RetrievalResult]:
        if not original_embedding:
            return []

        query_embeddings: list[tuple[str, List[float]]] = [(rewrite_plan.original_query, original_embedding)]
        extra_queries = [q for q in rewrite_plan.rewrite_queries if q and q != rewrite_plan.original_query]
        if extra_queries and self.embedding_client:
            try:
                extra_embeddings = self.embedding_client.encode(extra_queries)
                query_embeddings.extend(zip(extra_queries, extra_embeddings))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to embed rewrite queries: %s", exc)

        ranked: dict[str, tuple[float, RetrievalResult]] = {}
        source_ids = [value for value in [*(asset_ids or []), *(search_result_ids or [])] if value]
        for query_index, (query_text, embedding) in enumerate(query_embeddings):
            fragments = self.vector_store.search(
                query_embedding=embedding,
                top_k=max(top_k * 2, 5),
                asset_ids=source_ids or None,
            )
            for rank, fragment in enumerate(fragments, start=1):
                score = 1.0 / (60 + rank + query_index)
                current = ranked.get(fragment.id)
                result = RetrievalResult(
                    fragment_id=fragment.id,
                    asset_id=fragment.asset_id,
                    text=fragment.text,
                    relevance_score=fragment.score or score,
                    order_no=fragment.order_no,
                    source_start=fragment.source_start,
                    source_end=fragment.source_end,
                    block_type=fragment.block_type,
                    section_title=fragment.section_title,
                    page_no=fragment.page_no,
                    heading_path=[x.strip() for x in fragment.heading_path_text.split(" > ") if x.strip()],
                    retrieval_mode="vector",
                )
                if current:
                    ranked[fragment.id] = (current[0] + score, current[1])
                else:
                    ranked[fragment.id] = (score, result)

        return [item[1] for item in sorted(ranked.values(), key=lambda x: x[0], reverse=True)]

    def _keyword_retrieve(
        self,
        *,
        rewrite_plan: QueryRewriteResult,
        top_k: int,
        asset_ids: List[str] | None,
        search_result_ids: List[str] | None,
    ) -> List[RetrievalResult]:
        if not self.asset_repo and not self.search_repo:
            return []

        search_terms = self._keyword_terms(rewrite_plan)
        if not search_terms:
            return []

        ranked: list[tuple[float, RetrievalResult]] = []

        def absorb(payload: dict) -> None:
            score = self._score_fragment(payload, search_terms)
            if score <= 0:
                return
            heading_path = payload.get("heading_path") or []
            if isinstance(heading_path, str):
                heading_path = [x.strip() for x in heading_path.split(" > ") if x.strip()]
            ranked.append(
                (
                    score,
                    RetrievalResult(
                        fragment_id=payload.get("fragment_id", ""),
                        asset_id=payload.get("asset_id", "") or payload.get("result_id", ""),
                        text=payload.get("text", ""),
                        relevance_score=score,
                        order_no=int(payload.get("order_no", 0)),
                        source_start=int(payload.get("source_start", 0)),
                        source_end=int(payload.get("source_end", 0)),
                        block_type=payload.get("block_type", "paragraph"),
                        section_title=payload.get("section_title", "") or payload.get("title", ""),
                        page_no=int(payload.get("page_no", 0)),
                        heading_path=list(heading_path),
                        retrieval_mode="keyword",
                    ),
                )
            )

        if self.asset_repo:
            for payload in self.asset_repo.iter_fragments(asset_ids):
                absorb(payload)
        if self.search_repo:
            for payload in self.search_repo.iter_fragments(search_result_ids):
                absorb(payload)
        ranked.sort(key=lambda x: x[0], reverse=True)
        return [item[1] for item in ranked[: max(top_k * 2, 5)]]

    def _keyword_terms(self, rewrite_plan: QueryRewriteResult) -> list[str]:
        values = list(rewrite_plan.keywords)
        if not values:
            values = self.query_rewriter.extract_keywords(" ".join(rewrite_plan.all_queries()))
        if not values:
            values = [rewrite_plan.original_query]
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            normalized = value.strip()
            if len(normalized) < 2 or normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)
        return result[:10]

    def _score_fragment(self, payload: dict, terms: list[str]) -> float:
        text = str(payload.get("text", "") or "")
        if not text:
            return 0.0
        haystack = text.lower()
        section = str(payload.get("section_title", "") or "").lower()
        heading_text = " ".join(payload.get("heading_path") or []).lower()
        score = 0.0
        for term in terms:
            lowered = term.lower()
            if not lowered:
                continue
            if lowered in haystack:
                if re.fullmatch(r"[a-z0-9._/-]+", lowered):
                    score += 4.5
                elif re.search(r"[\u4e00-\u9fff]", lowered):
                    score += 3.5
                else:
                    score += 2.5
            if lowered and section and lowered in section:
                score += 2.0
            if lowered and heading_text and lowered in heading_text:
                score += 1.5
        return score

    def _fuse_results(
        self,
        vector_results: List[RetrievalResult],
        keyword_results: List[RetrievalResult],
        *,
        top_k: int,
    ) -> List[RetrievalResult]:
        fused: dict[str, tuple[float, RetrievalResult]] = {}

        def absorb(results: List[RetrievalResult], source: str) -> None:
            for rank, result in enumerate(results, start=1):
                score = 1.0 / (60 + rank)
                current = fused.get(result.fragment_id)
                merged_mode = source if not current else f"{current[1].retrieval_mode}+{source}"
                merged = RetrievalResult(
                    fragment_id=result.fragment_id,
                    asset_id=result.asset_id,
                    text=result.text,
                    relevance_score=max(result.relevance_score, current[1].relevance_score if current else 0.0),
                    order_no=result.order_no,
                    source_start=result.source_start,
                    source_end=result.source_end,
                    block_type=result.block_type,
                    section_title=result.section_title,
                    page_no=result.page_no,
                    heading_path=list(result.heading_path),
                    retrieval_mode=merged_mode,
                )
                if current:
                    fused[result.fragment_id] = (current[0] + score, merged)
                else:
                    fused[result.fragment_id] = (score, merged)

        absorb(vector_results, "vector")
        absorb(keyword_results, "keyword")
        return [item[1] for item in sorted(fused.values(), key=lambda x: x[0], reverse=True)[:top_k]]

    def format_context_for_prompt(
        self,
        results: List[RetrievalResult],
        max_length: int = 4000,
    ) -> str:
        """
        Format retrieval results into context string for prompt injection.

        Args:
            results: Retrieval results
            max_length: Maximum total length of context

        Returns:
            Formatted context string
        """
        if not results:
            return ""

        context_parts = []
        total_length = 0

        for i, result in enumerate(results):
            part = f"参考资料{i+1}: {result.text}"
            if total_length + len(part) > max_length:
                break
            context_parts.append(part)
            total_length += len(part)

        return "\n\n".join(context_parts)

    def delete_asset_fragments(self, asset_id: str) -> int:
        """
        Delete all fragments for an asset.

        Args:
            asset_id: Asset ID

        Returns:
            Number of fragments deleted
        """
        return self.vector_store.delete_by_asset(asset_id)

    def get_available_asset_ids(self) -> List[str]:
        """
        Get all asset IDs with indexed fragments.

        Returns:
            List of asset IDs
        """
        return self.vector_store.get_asset_ids()

    def get_fragment_count(self) -> int:
        """
        Get total number of indexed fragments.

        Returns:
            Number of fragments
        """
        return self.vector_store.count()


def make_fragment_id(asset_id: str, order_no: int) -> str:
    """Generate a unique fragment ID."""
    content = f"{asset_id}_{order_no}"
    return "frag_" + hashlib.md5(content.encode()).hexdigest()[:12]
