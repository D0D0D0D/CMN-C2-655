"""AgentCore Platform v1.0 — CMN-C2-655 public plan identifier generation.

A plan identifier is handed back to the organizer and is the key of the
feedback route, so it travels through every text-scanning boundary this
agent has: the graph ingress sanitizer
(``src/services/ingress_sanitizer.py``), the framework's input gate on the
inner-graph JSON envelope, and the output gate in
``src/nodes/post_process_node.py``.

Those scanners look for digit-shaped personal identifiers — a 12-digit
national ID number, a 16-digit payment card number, a hyphen-grouped phone
number. A version 4 UUID is written in hexadecimal, so a small share of the
UUIDs a generator produces happen to read as one of those shapes: the final
12-character group can come out as 12 decimal digits, or three groups can
line up as ``0nnn-nnnn-nnnn``. Measured over 200,000 UUIDs, 0.44% were
flagged. The consequences were real on both directions of travel: the
output gate blocked an otherwise valid plan (the run ended in ``error``),
and the ingress sanitizer rewrote a caller-supplied identifier on the
feedback route so the record could no longer be found.

The identifier below carries the same 128 random bits as a version 4 UUID
but spells them with letters only: each 4-bit nibble maps to one of ``a``
through ``p``. The mapping is one-to-one with hexadecimal, so no entropy is
lost and two identifiers collide exactly as rarely as two UUIDs would. What
changes is that the result contains no decimal digit and no whitespace at
all, so none of the digit-shaped patterns — nor the Latin-name heuristic,
which needs capitalized words separated by spaces — can match it, whatever
text surrounds it.

Keeping the identifier itself unmatchable is deliberate: the alternative,
exempting the identifier's field from the scan, would create a field whose
contents are never checked, and it would not help the ingress direction,
where the value arrives from the caller under a key the sanitizer must not
trust.
"""

from __future__ import annotations

from uuid import uuid4

# Marks the value as an identifier issued by this agent when it shows up in
# an operator log or a support request. Letters and a hyphen only.
ID_PREFIX = "plan-"

# One letter per nibble value, 0x0 -> "a" ... 0xf -> "p". This is a
# one-to-one re-encoding of the hexadecimal digit, not a lossy substitution:
# distinct UUIDs always produce distinct identifiers.
_NIBBLE_LETTERS = "abcdefghijklmnop"

# 16 random bytes, two letters each.
ID_BODY_LENGTH = 32


def new_plan_id() -> str:
    """Return a fresh plan identifier: ``plan-`` plus 32 letters from a-p.

    Carries the full 128 bits of a version 4 UUID. Contains no decimal
    digit, so no personal-identifier pattern can match it.
    """
    return ID_PREFIX + "".join(
        _NIBBLE_LETTERS[byte >> 4] + _NIBBLE_LETTERS[byte & 0x0F]
        for byte in uuid4().bytes
    )
