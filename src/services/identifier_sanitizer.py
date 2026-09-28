"""AgentCore Platform v1.0 — CMN-C2-655 identifier sanitizer (C-01 defense-in-depth).

Shared by hitl_refine_node.py and feedback_update_node.py — both process
organizer-authored free text (HITL resume response / complaint text) OUTSIDE
PreProcessNode's S-2 extension gate (resume does not re-run pre_process; see
hitl_refine_node.py module docstring), so both must independently sanitize
before the text touches State/checkpoint/DB/KB.

KNOWN GAP in `shared.security.pii_detector.detect_pii` (found during MR-B
integration testing, this session — see final report "要ユーザー判断"):
  1. No Japanese personal-name detection — the `name` pattern is a
     Latin-script Title-Case heuristic and never matches e.g. "田中太郎".
  2. Every pattern that ends in a trailing `\\b` (`email`, `phone_jp`,
     `phone_us`, ...) silently fails to match when the identifier is
     immediately followed by Japanese text with no delimiter — e.g.
     "080-1234-5678です" or "yamada@example.comにも共有". Python's
     Unicode-aware `\\b` treats the last ASCII "word" character of the match
     and the following Japanese character (itself a Unicode "word"
     character) as adjacent, so no boundary exists there and the whole match
     fails (not just the trailing edge). Confirmed for `email` and
     `phone_jp` in this session's integration testing; `phone_us` shares the
     same trailing-`\\b` shape and is presumed to share the bug but was not
     separately exercised for domestic JP organizer text.
`shared.security.pii_detector` is a vendored `shared/` module and MUST NOT be
edited here (it belongs to the framework distribution) — this module adds a
supplementary regex pass
instead, run AFTER detect_pii(), narrowly scoped to avoid over-redacting
legitimate plan text (destination names, generic nouns).
"""

from __future__ import annotations

import re

from shared.security.pii_detector import detect_pii

_REDACTION_MARK = "[識別子除去済み]"

_SUPPLEMENTARY_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    # Email address without requiring a trailing word boundary (fixes the
    # \b-after-Japanese-text gap) — same shape as pii_detector's email
    # pattern, minus the failing trailing \b.
    ("email_suppl", re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")),
    # JP phone number without requiring a trailing word boundary (fixes the
    # \b-after-Japanese-text gap) — same digit-grouping shape as pii_detector's
    # phone_jp, minus the failing trailing \b.
    ("phone_jp_suppl", re.compile(r"(?<!\d)(?:\+81[-\s]?\d{1,4}[-\s]\d{1,4}[-\s]\d{4}|0\d{1,4}[-\s]\d{1,4}[-\s]\d{4})")),
    # Japanese personal name with an honorific suffix (high precision — the
    # honorific is what makes this identifiably a person's name, not a place
    # or generic noun): e.g. "田中太郎さん", "山田様".
    ("name_jp_honorific", re.compile(r"[一-鿿぀-ゟ゠-ヿ]{2,10}(?:さん|様|氏|くん|ちゃん)")),
]


# detect_pii types precise enough to block on. `name` is excluded: it is a
# Latin Title-Case heuristic that fires on ordinary plan text ("Kyoto Station",
# "Ryokan Hotel"), so blocking on it would reject legitimate itineraries.
_BLOCKING_PII_TYPES = ("email", "phone_jp", "phone_us", "ssn_us", "my_number_jp")


def find_blocking_identifiers(text: str) -> list[str]:
    """Detection-only counterpart of sanitize_identifiers(), for S-3 gating.

    S-3's correct action on residual identifiers is to BLOCK the output, not
    to silently redact it, so the gate needs the labels rather than a rewritten
    string. Covers the same supplementary patterns — without them the S-3 walk
    would miss a Japanese personal name ("田中太郎さん") entirely and would miss
    an email/phone followed directly by Japanese text (the trailing-\\b gap
    documented in this module's docstring).
    """
    if not text:
        return []
    labels = [f["type"] for f in detect_pii(text) if f["type"] in _BLOCKING_PII_TYPES]
    labels.extend(label for label, pattern in _SUPPLEMENTARY_PATTERNS if pattern.search(text))
    return labels


def sanitize_identifiers(text: str) -> tuple[str, int]:
    """Strips direct identifiers from organizer-authored free text.

    Returns (sanitized_text, redaction_count). Never returns or logs the
    removed spans/content — only the count (S-4 event payload contract).

    Runs shared.security.pii_detector.detect_pii() first, THEN the
    supplementary pass above to close the Japanese-name / JP-phone-boundary
    gaps documented in this module's docstring.
    """
    if not text:
        return "", 0

    spans: list[tuple[int, int]] = [(f["start"], f["end"]) for f in detect_pii(text)]
    for _label, pattern in _SUPPLEMENTARY_PATTERNS:
        for m in pattern.finditer(text):
            spans.append((m.start(), m.end()))

    if not spans:
        return text, 0

    # Merge overlapping/adjacent spans so a supplementary match that overlaps
    # a detect_pii() match doesn't double-redact or corrupt offsets when
    # applied in reverse.
    spans.sort()
    merged: list[list[int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    sanitized = text
    for start, end in reversed(merged):  # reverse to preserve offsets
        sanitized = sanitized[:start] + _REDACTION_MARK + sanitized[end:]
    return sanitized, len(merged)
