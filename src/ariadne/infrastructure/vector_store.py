"""Vector store for RAG using ChromaDB."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from ariadne.application.config import AppConfig
from ariadne.application.text_splitter import TextFragment
from ariadne.domain.models import utc_now_iso
from ariadne.infrastructure.app_logger import get_logger

logger = get_logger("vector_store")

try:
    import chromadb
    from chromadb.config import Settings
    CHROMADB_AVAILABLE = True
except ImportError:
    CHROMADB_AVAILABLE = False
    logger.warning("chromadb not installed, vector store will be disabled")


@dataclass
class DocumentFragment:
    """A document fragment with vector."""

    id: str
    asset_id: str
    text: str
    embedding: List[float]
    order_no: int
    created_at: str = field(default_factory=utc_now_iso)


class VectorStore:
    """
    Vector store for RAG using ChromaDB.

    Provides:
    - Adding document fragments with embeddings
    - Searching by query text
    - Managing per-asset fragments
    """

    COLLECTION_NAME = "ariadne_documents"

    def __init__(self, config: AppConfig) -> None:
        """
        Initialize vector store.

        Args:
            config: Application config
        """
        self.config = config
        self.enabled = CHROMADB_AVAILABLE

        if not self.enabled:
            logger.warning("Vector store is disabled (chromadb not installed)")
            self.client = None
            self.collection = None
            return

        # Set up persistent storage path
        storage_path = Path(config.knowledge_doc_dir).parent / "vectors"
        storage_path.mkdir(parents=True, exist_ok=True)

        try:
            self.client = chromadb.PersistentClient(
                path=str(storage_path),
                settings=Settings(
                    anonymized_telemetry=False,
                    allow_reset=True,
                ),
            )
            self.collection = self.client.get_or_create_collection(
                name=self.COLLECTION_NAME,
                metadata={"description": "Ariadne RAG document fragments"},
            )
            logger.info(
                "Vector store initialized path=%s collection=%s",
                storage_path,
                self.COLLECTION_NAME,
            )
        except Exception as exc:
            logger.exception("Failed to initialize ChromaDB: %s", exc)
            self.enabled = False
            self.client = None
            self.collection = None

    def add_fragments(self, fragments: List[DocumentFragment]) -> int:
        """
        Add document fragments to the store.

        Args:
            fragments: List of DocumentFragment objects

        Returns:
            Number of fragments added
        """
        if not self.enabled or not fragments:
            return 0

        try:
            ids = [f.id for f in fragments]
            embeddings = [f.embedding for f in fragments]
            documents = [f.text for f in fragments]
            metadatas = [
                {
                    "asset_id": f.asset_id,
                    "order_no": f.order_no,
                    "created_at": f.created_at,
                }
                for f in fragments
            ]

            # Check if any IDs already exist and update instead
            existing_ids = set(self.collection.get(ids=ids).get("ids", []))
            if existing_ids:
                # Remove existing fragments for update
                self.collection.delete(ids=list(existing_ids))

            self.collection.add(
                ids=ids,
                embeddings=embeddings,
                documents=documents,
                metadatas=metadatas,
            )

            logger.info("Added %d fragments to vector store", len(fragments))
            return len(fragments)

        except Exception as exc:
            logger.exception("Failed to add fragments to vector store: %s", exc)
            return 0

    def search(
        self,
        query_embedding: List[float],
        top_k: int = 3,
        asset_ids: List[str] | None = None,
    ) -> List[DocumentFragment]:
        """
        Search for similar fragments by query embedding.

        Args:
            query_embedding: Query vector
            top_k: Number of results to return
            asset_ids: Optional filter by asset IDs

        Returns:
            List of DocumentFragment objects sorted by similarity
        """
        if not self.enabled or not query_embedding:
            return []

        try:
            where = None
            if asset_ids:
                where = {"asset_id": {"$in": asset_ids}}

            results = self.collection.query(
                query_embeddings=[query_embedding],
                n_results=top_k,
                where=where,
                include=["embeddings", "documents", "metadatas", "distances"],
            )

            fragments = []
            if results and results.get("ids"):
                for i, doc_id in enumerate(results["ids"][0]):
                    fragments.append(
                        DocumentFragment(
                            id=doc_id,
                            asset_id=results["metadatas"][0][i].get("asset_id", ""),
                            text=results["documents"][0][i],
                            embedding=results["embeddings"][0][i],
                            order_no=results["metadatas"][0][i].get("order_no", 0),
                        )
                    )

            logger.info("Vector search returned %d fragments (top_k=%d)", len(fragments), top_k)
            return fragments

        except Exception as exc:
            logger.exception("Vector search failed: %s", exc)
            return []

    def delete_by_asset(self, asset_id: str) -> int:
        """
        Delete all fragments for a given asset ID.

        Args:
            asset_id: Asset ID to delete

        Returns:
            Number of fragments deleted
        """
        if not self.enabled:
            return 0

        try:
            # Get all fragments with this asset_id
            results = self.collection.get(
                where={"asset_id": asset_id},
            )

            ids = results.get("ids", [])
            if ids:
                self.collection.delete(ids=ids)
                logger.info("Deleted %d fragments for asset_id=%s", len(ids), asset_id)
                return len(ids)

            return 0

        except Exception as exc:
            logger.exception("Failed to delete fragments for asset_id=%s: %s", asset_id, exc)
            return 0

    def get_asset_ids(self) -> List[str]:
        """
        Get all unique asset IDs in the store.

        Returns:
            List of asset IDs
        """
        if not self.enabled:
            return []

        try:
            results = self.collection.get()
            asset_ids = set()
            for metadata in results.get("metadatas", []):
                asset_id = metadata.get("asset_id")
                if asset_id:
                    asset_ids.add(asset_id)
            return sorted(asset_ids)

        except Exception as exc:
            logger.exception("Failed to get asset IDs: %s", exc)
            return []

    def clear(self) -> bool:
        """
        Clear all fragments from the store.

        Returns:
            True if successful
        """
        if not self.enabled:
            return False

        try:
            # Delete and recreate collection
            self.client.delete_collection(name=self.COLLECTION_NAME)
            self.collection = self.client.get_or_create_collection(
                name=self.COLLECTION_NAME,
                metadata={"description": "Ariadne RAG document fragments"},
            )
            logger.info("Vector store cleared")
            return True

        except Exception as exc:
            logger.exception("Failed to clear vector store: %s", exc)
            return False

    def count(self) -> int:
        """
        Get total number of fragments in the store.

        Returns:
            Number of fragments
        """
        if not self.enabled:
            return 0

        try:
            return self.collection.count()
        except Exception as exc:
            logger.exception("Failed to get fragment count: %s", exc)
            return 0
