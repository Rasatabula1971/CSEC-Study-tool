# PHASE: build
"""
backend/ingest_v2/normalize.py
==============================
Shared text normalisation used by every adapter, so chunked text is consistent
regardless of source family.

  * Mojibake repair: the Caribbean AI markdown (and some MoE exports) were saved
    with UTF-8 read back as cp1252, leaving telltale "a-euro" sequences. We map the
    common ones back to the intended punctuation. The mojibake keys are GENERATED
    (encode the real char as UTF-8, decode those bytes as cp1252) rather than typed
    as literal garbage, so this file has no fragile non-ASCII source.
  * Custom-tag stripping: <wiki>/<callout> keep their inner text; the tags
    themselves go. <mermaid> diagram blocks are dropped entirely except a caption
    (diagrams are useless as retrieval text).
  * Whitespace tidy: collapse 3+ blank lines, trim trailing spaces.
"""

import re


def _moji(ch: str) -> str:
    """The cp1252-misread-as-UTF8 mojibake form of a single character.

    e.g. _moji("’")  ->  "a-circumflex euro trademark"  (the classic
    apostrophe mojibake). Built by encoding the char as UTF-8 then decoding those
    bytes as cp1252 -- exactly the corruption we are reversing."""
    return ch.encode("utf-8").decode("cp1252")


# --- mojibake repair --------------------------------------------------------
# Specific 3-byte sequences first (they all share the same 2-byte prefix), then the
# bare prefix as a last resort. U+201D's UTF-8 third byte (0x9D) is undefined in
# cp1252, so its mojibake collapses to the bare prefix -- handled by the final rule.
_MOJIBAKE = [
    (_moji("’"), "’"),       # right single quote / apostrophe
    (_moji("‘"), "‘"),       # left single quote
    (_moji("“"), "“"),       # left double quote
    (_moji("–"), "–"),       # en dash
    (_moji("—"), "—"),       # em dash
    (_moji("…"), "…"),       # ellipsis
    ("â€", "”"),        # bare "a-euro" remnant -> right double quote
]

# <mermaid ...>...</mermaid> -- drop the block; keep a caption= attribute if present.
# Capture the opening-tag attributes, then pull caption= from them separately so the
# caption is reliably extracted regardless of attribute order.
_MERMAID_RE = re.compile(r"<mermaid([^>]*)>.*?</mermaid>", re.IGNORECASE | re.DOTALL)
_CAPTION_RE = re.compile(r"caption=(['\"])(.*?)\1", re.IGNORECASE)
# A self-closing or unmatched mermaid tag -- drop it.
_MERMAID_LONE_RE = re.compile(r"</?mermaid[^>]*>", re.IGNORECASE)
# <wiki>...</wiki> and <callout>...</callout> -- keep inner text, drop the tags.
_KEEP_INNER_RE = re.compile(r"</?(?:wiki|callout)[^>]*>", re.IGNORECASE)
# Excess blank lines / trailing whitespace.
_MULTI_BLANK_RE = re.compile(r"\n[ \t]*\n[ \t]*(?:\n[ \t]*)+")
_TRAIL_WS_RE = re.compile(r"[ \t]+\n")


def fix_mojibake(text: str) -> str:
    for bad, good in _MOJIBAKE:
        text = text.replace(bad, good)
    return text


def strip_custom_tags(text: str) -> str:
    """Drop <mermaid> blocks (keep caption text only), and unwrap <wiki>/<callout>
    (keep inner text, drop the tags)."""
    def _mermaid_sub(m: "re.Match") -> str:
        cm = _CAPTION_RE.search(m.group(1))
        return f"\n{cm.group(2).strip()}\n" if cm else "\n"
    text = _MERMAID_RE.sub(_mermaid_sub, text)
    text = _MERMAID_LONE_RE.sub("", text)
    text = _KEEP_INNER_RE.sub("", text)
    return text


def normalize_text(text: str) -> str:
    """Full normalisation pass used by all adapters before chunking."""
    if not text:
        return ""
    text = fix_mojibake(text)
    text = strip_custom_tags(text)
    text = _TRAIL_WS_RE.sub("\n", text)
    text = _MULTI_BLANK_RE.sub("\n\n", text)
    return text.strip()
