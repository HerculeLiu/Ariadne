"""RAG (Retrieval-Augmented Generation) service for Ariadne."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Tuple

from ariadne.application.config import AppConfig
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
