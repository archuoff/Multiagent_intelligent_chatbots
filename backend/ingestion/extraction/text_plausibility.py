"""High-precision plausibility check for newly-recovered PDF text.

This is deliberately not a language-quality or dictionary-word-ratio check.
JLR technical documents are full of legitimate content that would fail a
naive "looks like real words" test: part numbers (`LR-084-7721`), material
codes (`PA6-GF30`), units and exponent notation (`10^12 Ω·cm`), tolerance
notation (`±0.05`), and dense units-only table rows. A ratio-based filter
would reject a large fraction of genuinely correct engineering text.

Instead, this looks for known, specific corruption patterns -- the actual
ways PDF text extraction breaks (an embedded subset font leaking raw glyph
IDs, Unicode replacement characters, garbled letter-spacing from a bad
reading-order guess) -- which stays high precision even on dense,
symbol-heavy text. Signals are split into "hard" (any single match is
already unambiguous corruption) and "soft" (need at least two together,
since any one alone is often just unusual-but-legitimate formatting).

Never used to delete anything -- flagged text is retained verbatim and
routed to review (see merger.py / document.py), never discarded.
"""

from __future__ import annotations

import re

_CID_PATTERN = re.compile(r"\(cid:\d+\)", re.IGNORECASE)
_CONTROL_CHAR_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_REPEATED_CHAR_PATTERN = re.compile(r"(\S)\1{4,}")
_VOWELLESS_RUN_PATTERN = re.compile(r"\b[b-df-hj-np-tv-z]{8,}\b", re.IGNORECASE)
_TOKEN_PATTERN = re.compile(r"\S+")
_CJK_OR_CYRILLIC_PATTERN = re.compile(r"[぀-ヿ㐀-鿿Ѐ-ӿ]")
_LATIN_LETTER_PATTERN = re.compile(r"[A-Za-z]")

# Common technical punctuation that must never count against a piece of
# text -- engineering specs are dense with this and it's never a corruption
# signal on its own.
_TECHNICAL_PUNCTUATION = set(".,:;/\\-–—_^+±×·°%()[]{}<>='\"~&@#*|µΩ")

_SOFT_SIGNAL_THRESHOLD = 2
_NON_TECHNICAL_CHAR_RATIO_LIMIT = 0.25
_SINGLE_CHAR_TOKEN_RATIO_LIMIT = 0.6
_SHORT_MEAN_TOKEN_LENGTH_LIMIT = 1.8


def noise_signals(text: str) -> list[str]:
    """Returns codes for every corruption pattern matched; empty means clean."""
    normalized = text or ""
    if not normalized.strip():
        return []
    hard = _hard_signals(normalized)
    soft = _soft_signals(normalized)
    signals = list(hard)
    if len(soft) >= _SOFT_SIGNAL_THRESHOLD:
        signals.extend(soft)
    return signals


def is_probably_noise(text: str) -> bool:
    """True when noise_signals finds anything -- a hard signal alone, or
    enough soft signals together."""
    return bool(noise_signals(text))


def _hard_signals(text: str) -> list[str]:
    """Corruption patterns unambiguous enough that a single match is sufficient."""
    signals: list[str] = []
    if _CID_PATTERN.search(text):
        signals.append("cid_artifact")
    if "�" in text or _CONTROL_CHAR_PATTERN.search(text):
        signals.append("replacement_or_control_char")
    for match in _REPEATED_CHAR_PATTERN.finditer(text):
        if match.group(1) in (".", "-", "_", "="):
            # Dot leaders, underline/divider runs, and "====" separators are
            # ubiquitous and legitimate in real documents -- never corruption.
            continue
        signals.append("repeated_character_run")
        break
    for match in _VOWELLESS_RUN_PATTERN.finditer(text):
        if match.group(0).isupper():
            continue  # a legitimate all-caps acronym/code, not garbled text
        signals.append("vowelless_run")
        break
    return signals


def _soft_signals(text: str) -> list[str]:
    """Weaker patterns that only indicate corruption when several co-occur."""
    signals: list[str] = []
    tokens = _TOKEN_PATTERN.findall(text)
    if len(tokens) >= 8:
        single_char = sum(1 for token in tokens if len(token) == 1)
        if single_char / len(tokens) > _SINGLE_CHAR_TOKEN_RATIO_LIMIT:
            signals.append("excessive_single_character_tokens")
    if len(tokens) >= 10:
        mean_length = sum(len(token) for token in tokens) / len(tokens)
        if mean_length < _SHORT_MEAN_TOKEN_LENGTH_LIMIT:
            signals.append("short_mean_token_length")
    if text:
        allowed = sum(1 for character in text if character.isalnum() or character.isspace() or character in _TECHNICAL_PUNCTUATION)
        if (1 - allowed / len(text)) > _NON_TECHNICAL_CHAR_RATIO_LIMIT:
            signals.append("excessive_non_technical_punctuation")
    for token in tokens:
        if _LATIN_LETTER_PATTERN.search(token) and _CJK_OR_CYRILLIC_PATTERN.search(token):
            signals.append("mixed_script_token")
            break
    return signals
