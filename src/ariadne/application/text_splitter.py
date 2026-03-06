"""Recursive text splitter for RAG document processing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

from ariadne.infrastructure.app_logger import get_logger

logger = get_logger("text_splitter")


@dataclass
class TextFragment:
    """A text fragment after splitting."""

    text: str
    order_no: int
    source_start: int
    source_end: int


class RecursiveTextSplitter:
    """
    Recursively split text into fragments.

    Splitting priority:
    1. Paragraph boundaries (\n\n)
    2. Sentence boundaries (。！？\n)
    3. Fixed character length

    Overlap is added between fragments to maintain context continuity.
    """

    DEFAULT_MAX_LENGTH = 800
    DEFAULT_OVERLAP = 100

    # Paragraph separators
    PARAGRAPH_SEP = "\n\n"

    # Sentence separators (Chinese and English)
    SENTENCE_SEP = r"[。！？\.!?]"

    def __init__(
        self,
        max_length: int = DEFAULT_MAX_LENGTH,
        overlap: int = DEFAULT_OVERLAP,
    ) -> None:
        """
        Initialize the splitter.

        Args:
            max_length: Maximum length of each fragment in characters
            overlap: Number of overlapping characters between fragments
        """
        self.max_length = max_length
        self.overlap = overlap

    def split_text(self, text: str) -> List[TextFragment]:
        """
        Split text into fragments.

        Args:
            text: Input text to split

        Returns:
            List of TextFragment objects
        """
        if not text:
            return []

        text = text.strip()
        if len(text) <= self.max_length:
            return [TextFragment(text=text, order_no=0, source_start=0, source_end=len(text))]

        fragments: List[TextFragment] = []
        order_no = 0
        current_pos = 0

        while current_pos < len(text):
            remaining = len(text) - current_pos

            # If remaining text fits in one fragment
            if remaining <= self.max_length:
                fragment_text = text[current_pos:]
                fragments.append(
                    TextFragment(
                        text=fragment_text,
                        order_no=order_no,
                        source_start=current_pos,
                        source_end=len(text),
                    )
                )
                break

            # Try to split at paragraph boundary
            fragment_end = self._find_paragraph_boundary(
                text, current_pos, current_pos + self.max_length
            )

            if fragment_end == current_pos:  # No paragraph boundary found
                # Try sentence boundary
                fragment_end = self._find_sentence_boundary(
                    text, current_pos, current_pos + self.max_length
                )

            if fragment_end == current_pos:  # No sentence boundary found
                # Use fixed length split
                fragment_end = min(current_pos + self.max_length, len(text))

            fragment_text = text[current_pos:fragment_end]
            fragments.append(
                TextFragment(
                    text=fragment_text,
                    order_no=order_no,
                    source_start=current_pos,
                    source_end=fragment_end,
                )
            )

            # Move to next position with overlap
            current_pos = fragment_end - self.overlap
            if current_pos < 0:
                current_pos = fragment_end
            order_no += 1

        logger.info(
            "text split completed fragments=%d text_len=%d max_len=%d overlap=%d",
            len(fragments),
            len(text),
            self.max_length,
            self.overlap,
        )

        return fragments

    def _find_paragraph_boundary(self, text: str, start: int, end: int) -> int:
        """Find the nearest paragraph boundary before end position."""
        search_text = text[start:end]

        # Find all paragraph separators
        positions = []
        pos = 0
        while True:
            idx = search_text.find(self.PARAGRAPH_SEP, pos)
            if idx == -1:
                break
            positions.append(start + idx + len(self.PARAGRAPH_SEP))
            pos = idx + len(self.PARAGRAPH_SEP)

        if not positions:
            return start

        # Return the last paragraph boundary before min(end, len(text))
        for pos in reversed(positions):
            if pos <= end and pos <= len(text):
                return pos

        return start

    def _find_sentence_boundary(self, text: str, start: int, end: int) -> int:
        """Find the nearest sentence boundary before end position."""
        search_text = text[start:end]

        # Find all sentence endings
        matches = list(re.finditer(self.SENTENCE_SEP, search_text))

        if not matches:
            return start

        # Return the last sentence boundary position
        last_match = matches[-1]
        pos = start + last_match.end()

        # Make sure we don't go past the text length
        return min(pos, len(text))


def split_fragments_from_assets(asset_texts: List[tuple[str, str]]) -> List[tuple[str, TextFragment]]:
    """
    Split multiple asset texts into fragments.

    Args:
        asset_texts: List of (asset_id, text) tuples

    Returns:
        List of (asset_id, TextFragment) tuples
    """
    splitter = RecursiveTextSplitter()
    results: List[tuple[str, TextFragment]] = []

    for asset_id, text in asset_texts:
        if not text or not text.strip():
            continue
        fragments = splitter.split_text(text)
        for frag in fragments:
            results.append((asset_id, frag))

    logger.info(
        "split assets into fragments assets=%d total_fragments=%d",
        len(asset_texts),
        len(results),
    )

    return results
