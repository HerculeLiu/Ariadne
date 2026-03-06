"""RAG (Retrieval-Augmented Generation) service for Ariadne."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import List

from ariadne.application.config import AppConfig
from ariadne.application.text_splitter import TextFragment, split_fragments_from_assets
from ariadne.domain.models import Asset, utc_now_iso
from ariadne.infrastructure.app_logger import get_logger
from ariadne.infrastructure.vector_store import DocumentFragment, VectorStore

logger = get_logger("rag")


@dataclass
class RetrievalResult:
    """Result from vector retrieval."""

    fragment_id: str
    asset_id: str
    text: str
    relevance_score: float


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
    ) -> None:
        """
        Initialize RAG service.

        Args:
            config: Application config
            vector_store: Vector store instance
        """
        self.config = config
        self.vector_store = vector_store

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

        # Extract text from assets
        asset_texts = []
        for asset in assets:
            if asset.status != "ready":
                logger.warning("Asset %s not ready (status=%s), skipping", asset.id, asset.status)
                continue
            # In real implementation, we'd extract text from the asset
            # For now, skip if we don't have the actual text content
            logger.debug("Processing asset %s: %s", asset.id, asset.file_name)

        # Note: In a real implementation, we'd:
        # 1. Read the actual file content
        # 2. Parse based on file type (PDF, MD, TXT, DOCX)
        # 3. Split into fragments
        # 4. Generate embeddings
        # 5. Store in vector DB

        # For now, return 0 as we need to implement file reading
        logger.info("Asset processing not fully implemented yet")
        return 0

    def retrieve(
        self,
        query: str,
        query_embedding: List[float],
        top_k: int = 3,
        asset_ids: List[str] | None = None,
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
        if not query_embedding:
            logger.warning("Empty query embedding, returning empty results")
            return []

        fragments = self.vector_store.search(
            query_embedding=query_embedding,
            top_k=top_k,
            asset_ids=asset_ids,
        )

        results = [
            RetrievalResult(
                fragment_id=f.id,
                asset_id=f.asset_id,
                text=f.text,
                relevance_score=1.0,  # Will be replaced by actual scores if available
            )
            for f in fragments
        ]

        logger.info(
            "Retrieved %d fragments for query (top_k=%d, asset_ids=%s)",
            len(results),
            top_k,
            asset_ids,
        )

        return results

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
