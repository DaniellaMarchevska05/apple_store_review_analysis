"""Source-preserving text normalization shared by collection and analysis."""

import re
import unicodedata

PREPROCESSING_VERSION = "unicode-whitespace-input-v2"


def normalize_text(text: str) -> str:
    """Normalize canonical Unicode and whitespace without changing words or punctuation."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()


def clean_text(title: str, text: str) -> str:
    """Preserve sentiment cues; lexical normalization belongs only to phrase extraction."""
    return normalize_text(f"{title}\n{text}")
