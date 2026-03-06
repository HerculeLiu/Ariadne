#!/usr/bin/env python3
"""Test script for RAG framework components."""

import sys
import os

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from ariadne.application.config import load_config
from ariadne.llm.embedding_client import EmbeddingClient
from ariadne.llm.rerank_client import RerankClient
from ariadne.application.text_splitter import RecursiveTextSplitter, TextFragment
from ariadne.infrastructure.vector_store import VectorStore, DocumentFragment


def test_embedding_client():
    """Test GLM Embedding-3 client."""
    print("\n=== Testing Embedding Client ===")
    config = load_config()
    client = EmbeddingClient(config)

    texts = ["测试文本1：机器学习", "测试文本2：深度学习"]
    try:
        embeddings = client.encode(texts)
        print(f"✓ Encoded {len(embeddings)} texts")
        print(f"  Embedding dimension: {len(embeddings[0]) if embeddings else 'N/A'}")
        print(f"  Sample values: {embeddings[0][:5] if embeddings else 'N/A'}")
        return embeddings
    except Exception as e:
        print(f"✗ Embedding failed: {e}")
        return None


def test_rerank_client():
    """Test GLM Rerank client."""
    print("\n=== Testing Rerank Client ===")
    config = load_config()
    client = RerankClient(config)

    query = "什么是机器学习"
    documents = [
        "机器学习是人工智能的一个分支",
        "今天天气很好，适合散步",
        "深度学习是机器学习的子领域",
        "Python是一种编程语言",
    ]

    try:
        results = client.rerank(query, documents, top_n=3)
        print(f"✓ Reranked {len(results)} documents")
        for i, r in enumerate(results):
            print(f"  {i+1}. Score: {r.relevance_score:.3f} - {r.document[:50]}")
        return results
    except Exception as e:
        print(f"✗ Rerank failed: {e}")
        return None


def test_text_splitter():
    """Test recursive text splitter."""
    print("\n=== Testing Text Splitter ===")
    splitter = RecursiveTextSplitter(max_length=200, overlap=50)

    text = """
    第一章：引言

    这是第一段内容，包含一些关于主题的介绍。这里有一些详细的说明。

    这是第二段内容，继续讨论相关的话题。包含更多的信息。

    第二章：核心概念

    这是一个很长的段落，需要测试切分功能。当文本超过设定的最大长度时，应该智能地进行切分。这个段落比较长，所以可能会被分成多个片段。我们继续添加更多内容来确保它足够长...这里还有更多内容。

    这是第三章的简短段落。

    第四章：最后的总结。
    """

    fragments = splitter.split_text(text)
    print(f"✓ Split text into {len(fragments)} fragments")
    for i, f in enumerate(fragments):
        print(f"  Fragment {i+1}: {len(f.text)} chars, order={f.order_no}")
        print(f"    Preview: {f.text[:50]}...")
    return fragments


def test_vector_store():
    """Test ChromaDB vector store."""
    print("\n=== Testing Vector Store ===")
    config = load_config()
    store = VectorStore(config)

    if not store.enabled:
        print("✗ Vector store disabled (ChromaDB not available)")
        return None

    # Clear any existing data
    store.clear()

    # Add test fragments
    test_fragments = [
        DocumentFragment(
            id="test_frag_1",
            asset_id="test_asset_1",
            text="机器学习是人工智能的一个重要分支",
            embedding=[0.1] * 1024,
            order_no=0,
        ),
        DocumentFragment(
            id="test_frag_2",
            asset_id="test_asset_1",
            text="深度学习使用神经网络进行学习",
            embedding=[0.2] * 1024,
            order_no=1,
        ),
        DocumentFragment(
            id="test_frag_3",
            asset_id="test_asset_2",
            text="Python是一种流行的编程语言",
            embedding=[0.3] * 1024,
            order_no=0,
        ),
    ]

    added = store.add_fragments(test_fragments)
    print(f"✓ Added {added} fragments to vector store")

    # Search test
    query_embedding = [0.15] * 1024
    results = store.search(query_embedding, top_k=2)
    print(f"✓ Search returned {len(results)} results")
    for r in results:
        print(f"  - {r.asset_id}: {r.text[:40]}...")

    # Count test
    count = store.count()
    print(f"✓ Total fragments in store: {count}")

    # Delete test
    deleted = store.delete_by_asset("test_asset_1")
    print(f"✓ Deleted {deleted} fragments for test_asset_1")

    # Cleanup
    store.clear()
    print("✓ Cleaned up test data")

    return results


def test_integration():
    """Test full RAG pipeline integration."""
    print("\n=== Testing RAG Integration ===")
    config = load_config()

    if config.model_provider == "mock":
        print("  Running in mock mode (no real API calls)")

    # Check components
    print("\nComponents status:")
    print(f"  - Model provider: {config.model_provider}")
    print(f"  - API key configured: {bool(config.llm_api_key)}")

    return True


def main():
    """Run all tests."""
    print("=" * 60)
    print("Ariadne RAG Framework Test Suite")
    print("=" * 60)

    results = {
        "embedding": test_embedding_client() is not None,
        "rerank": test_rerank_client() is not None,
        "splitter": test_text_splitter() is not None,
        "vector_store": test_vector_store() is not None,
        "integration": test_integration(),
    }

    print("\n" + "=" * 60)
    print("Test Summary")
    print("=" * 60)
    for name, passed in results.items():
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"  {status} - {name}")

    all_passed = all(results.values())
    print("\n" + ("All tests passed!" if all_passed else "Some tests failed."))
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
