"""Checkpoint-safe identifier removal at every graph ingress.

``BaseGraph`` persists its initial State before node-level S-2 gates run.
Sanitize all values that can become that State before calling ``invoke()``,
``resume()``, or ``stream()``. Only JSON-compatible values are accepted so
an unknown object's string representation cannot reintroduce an identifier
while the framework serializes it.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import secrets
from typing import Any

from src.services.identifier_sanitizer import sanitize_identifiers

# Safe internal hand-off from the graph boundary to PreProcessNode. It retains
# the existing care-notes S-4 signal after ingress has already redacted text.
CARE_NOTES_REDACTION_COUNT_KEY = "_care_notes_identifier_redaction_count"
INGRESS_REDACTION_COUNT_KEY = "_ingress_identifier_redaction_count"
_REDACTION_COUNT_PROVENANCE_KEY = "_redaction_count_provenance"
_INTERNAL_REDACTION_COUNT_KEYS = frozenset(
    (
        CARE_NOTES_REDACTION_COUNT_KEY,
        INGRESS_REDACTION_COUNT_KEY,
        _REDACTION_COUNT_PROVENANCE_KEY,
    )
)
_REDACTION_COUNT_PROVENANCE_SECRET = secrets.token_bytes(32)


def _unsupported_ingress_type(value: Any, path: str) -> TypeError:
    """Return a fail-closed error without rendering a caller-provided value."""
    return TypeError(f"Unsupported ingress value type {type(value).__name__!r} at {path}")


def _has_exact_type(value: Any, expected_type: type[Any]) -> bool:
    """Do not accept user-defined subclasses of JSON primitive types."""
    return type(value) is expected_type


def _redaction_count_provenance(count_key: str, count: int) -> str:
    """Return an unforgeable, process-local provenance tag for an internal count."""
    message = f"{count_key}:{count}".encode()
    return hmac.new(
        _REDACTION_COUNT_PROVENANCE_SECRET, message, hashlib.sha256
    ).hexdigest()


def trusted_redaction_count(value: Any, count_key: str) -> int:
    """Return a count created at this process's ingress boundary, else zero.

    Nested graphs sanitize their own public ingress, so a count measured by an
    outer graph needs a provenance check before it may be reattached.  The tag
    is deliberately dropped from every client input by ``sanitize_recursive``.
    """
    if not _has_exact_type(value, dict):
        return 0
    count = value.get(count_key)
    provenance = value.get(_REDACTION_COUNT_PROVENANCE_KEY)
    if (
        not isinstance(count, int)
        or isinstance(count, bool)
        or count <= 0
        or not isinstance(provenance, str)
    ):
        return 0
    expected = _redaction_count_provenance(count_key, count)
    return count if hmac.compare_digest(provenance, expected) else 0


def sanitize_recursive(value: Any, path: str = "ingress") -> tuple[Any, int]:
    """Sanitize string leaves and dict keys in a JSON-compatible value.

    ``set``, ``bytes``, dataclasses, Pydantic models, and arbitrary objects
    are rejected. Error messages identify only the type and location, never
    the supplied value.
    """
    value_type = type(value)
    if value_type is str:
        return sanitize_identifiers(value)
    if value is None or value_type in (bool, int):
        return value, 0
    if value_type is float:
        if not math.isfinite(value):
            raise TypeError(f"Unsupported non-finite float at {path}")
        return value, 0
    if value_type is dict:
        total = 0
        cleaned: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if not _has_exact_type(key, str):
                raise _unsupported_ingress_type(key, f"{path}[key#{index}]")
            # These values are audit-only internal hand-offs.  Drop them
            # before traversing their values so no client-provided count can
            # survive an ingress boundary (including inside nested feedback).
            if key in _INTERNAL_REDACTION_COUNT_KEYS:
                continue
            clean_key, key_count = sanitize_recursive(key, f"{path}[key#{index}]")
            clean_item, item_count = sanitize_recursive(item, f"{path}[{index}]")
            total += key_count + item_count
            if clean_key in cleaned:
                suffix = 2
                candidate = f"{clean_key}_{suffix}"
                while candidate in cleaned:
                    suffix += 1
                    candidate = f"{clean_key}_{suffix}"
                clean_key = candidate
            cleaned[clean_key] = clean_item
        return cleaned, total
    if value_type is list:
        total = 0
        cleaned_list: list[Any] = []
        for index, item in enumerate(value):
            clean_item, count = sanitize_recursive(item, f"{path}[{index}]")
            cleaned_list.append(clean_item)
            total += count
        return cleaned_list, total
    if value_type is tuple:
        total = 0
        cleaned_items: list[Any] = []
        for index, item in enumerate(value):
            clean_item, count = sanitize_recursive(item, f"{path}[{index}]")
            cleaned_items.append(clean_item)
            total += count
        return tuple(cleaned_items), total
    raise _unsupported_ingress_type(value, path)


def sanitize_graph_ingress(user_input: str, input_context: dict | None) -> tuple[str, dict]:
    """Prepare invoke/stream inputs before framework State construction."""
    if not _has_exact_type(user_input, str):
        raise _unsupported_ingress_type(user_input, "user_input")
    clean_user_input, _ = sanitize_recursive(user_input, "user_input")

    if input_context is None:
        raw_context: dict[str, Any] = {}
    elif _has_exact_type(input_context, dict):
        raw_context = input_context
    else:
        raise _unsupported_ingress_type(input_context, "input_context")

    clean_context, _ = sanitize_recursive(raw_context, "input_context")
    # Keep this explicit at the graph boundary as a defence in depth guard if
    # the recursive sanitizer's dict handling changes in the future.
    clean_context.pop(CARE_NOTES_REDACTION_COUNT_KEY, None)
    clean_context.pop(INGRESS_REDACTION_COUNT_KEY, None)
    clean_context.pop(_REDACTION_COUNT_PROVENANCE_KEY, None)
    raw_care_notes = raw_context.get("care_notes")
    if _has_exact_type(raw_care_notes, str):
        _, care_redaction_count = sanitize_recursive(
            raw_care_notes, "input_context.care_notes"
        )
        # This key is internal and is set only from the ingress measurement.
        clean_context[CARE_NOTES_REDACTION_COUNT_KEY] = care_redaction_count
        if care_redaction_count:
            clean_context[_REDACTION_COUNT_PROVENANCE_KEY] = _redaction_count_provenance(
                CARE_NOTES_REDACTION_COUNT_KEY, care_redaction_count
            )
    return clean_user_input, clean_context


def attach_redaction_count_to_feedback(feedback: Any, redaction_count: int) -> Any:
    """Carry an already-safe removal count to the HITL node's S-4 event.

    The count is checkpoint-safe and does not contain removed text. Wrapping
    a scalar preserves the string shape consumed by ``HitlRefineNode`` while
    a dict keeps its existing fields intact.
    """
    if _has_exact_type(feedback, dict):
        # ``attach_redaction_count_to_feedback`` is also a defensive boundary
        # for callers that did not first use ``sanitize_recursive``.
        with_count = {
            key: value
            for key, value in feedback.items()
            if key not in _INTERNAL_REDACTION_COUNT_KEYS
        }
        if redaction_count:
            with_count[INGRESS_REDACTION_COUNT_KEY] = redaction_count
            with_count[_REDACTION_COUNT_PROVENANCE_KEY] = _redaction_count_provenance(
                INGRESS_REDACTION_COUNT_KEY, redaction_count
            )
        return with_count
    if not redaction_count:
        return feedback
    return {
        "text": feedback,
        INGRESS_REDACTION_COUNT_KEY: redaction_count,
        _REDACTION_COUNT_PROVENANCE_KEY: _redaction_count_provenance(
            INGRESS_REDACTION_COUNT_KEY, redaction_count
        ),
    }


def sanitize_feedback_ingress(feedback: Any) -> Any:
    """Sanitize feedback and retain only an authenticated upstream count."""
    upstream_count = trusted_redaction_count(feedback, INGRESS_REDACTION_COUNT_KEY)
    clean_feedback, measured_count = sanitize_recursive(feedback, "feedback")
    return attach_redaction_count_to_feedback(
        clean_feedback, measured_count or upstream_count
    )
