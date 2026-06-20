# PHASE: build
"""
backend/ingest_v2/chunking.py
=============================
Paragraph-aware chunking for the prose adapters (Caribbean AI, MoE SLMS).

Targets ~500-char chunks with ~100-char overlap, but tries to keep paragraph
boundaries intact: paragraphs are packed into a chunk until the next would overflow,
then a tail of the previous chunk is carried forward as overlap. A single paragraph
longer than the target is hard-split into overlapping char windows (the v1 scheme).

The GenericPDFAdapter does NOT use this -- it uses v1.chunk_page verbatim to stay
byte-equivalent to v1.
"""

import re

CHUNK_SIZE = 500
CHUNK_OVERLAP = 100

_PARA_SPLIT_RE = re.compile(r"\n\s*\n")


def _hard_split(text: str, size: int, overlap: int) -> list[str]:
    """Overlapping fixed-width windows over a single long paragraph (v1 scheme)."""
    step = max(size - overlap, 1)
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        out.append(text[i:i + size])
        if i + size >= n:
            break
        i += step
    return out


def chunk_paragraphs(text: str, size: int = CHUNK_SIZE,
                     overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split text into ~`size`-char chunks with ~`overlap`-char overlap, preserving
    paragraph boundaries where possible."""
    text = (text or "").strip()
    if not text:
        return []
    paras = [p.strip() for p in _PARA_SPLIT_RE.split(text) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for p in paras:
        if len(p) > size:
            # Flush what we have, then hard-split the oversized paragraph.
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.extend(_hard_split(p, size, overlap))
            continue
        if buf and len(buf) + 2 + len(p) > size:
            chunks.append(buf)
            tail = buf[-overlap:] if overlap else ""
            buf = f"{tail}\n\n{p}" if tail else p
        else:
            buf = f"{buf}\n\n{p}" if buf else p
    if buf:
        chunks.append(buf)
    return chunks
