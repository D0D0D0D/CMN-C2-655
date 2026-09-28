# CMN-C2-655 — Unit Tests: public plan identifier format
"""The plan identifier crosses every text-scanning boundary this agent has,
so its format must be one that no personal-identifier pattern can match.

A version 4 UUID is not such a format: written in hexadecimal, a share of
UUIDs read as a 12-digit national ID number, a payment card number, or a
phone number, and the scanners then block the output or rewrite the value.
These tests pin the three concrete UUIDs that reproduced that failure and
prove the identifier built from the very same bits is inert.
"""

import random
import re
from uuid import UUID

import pytest

from framework.security.pii_detector import detect_pii
import src.services.plan_identifier as plan_identifier
from src.services.identifier_sanitizer import (
    find_blocking_identifiers,
    sanitize_identifiers,
)
from src.services.ingress_sanitizer import sanitize_recursive
from src.services.plan_identifier import ID_BODY_LENGTH, ID_PREFIX, new_plan_id

_ID_SHAPE = re.compile(r"plan-[a-p]{32}\Z")

# Real 128-bit values a version 4 UUID generator produces, each misread as a
# different kind of personal identifier once written in hexadecimal. Found by
# scanning 200,000 generated UUIDs; 0.44% of them were flagged.
_UUIDS_MISREAD_AS_PII = {
    # final group is 12 decimal digits -> reads as a national ID number
    "my_number_jp": "72648243-96ba-415b-b01b-695503485898",
    # groups 2-4 line up as 048-4922-8389 -> reads as a phone number
    "phone_jp": "7de6002d-a048-4922-8389-8f4e0fc01b11",
    # first 16 digits group into four -> reads as a payment card number
    "credit_card": "31692225-8694-4375-adf1-238569675337",
}


def _use_uuid(monkeypatch, value: str) -> None:
    """Force the identifier factory's random source to one known value."""
    monkeypatch.setattr(plan_identifier, "uuid4", lambda: UUID(value))


@pytest.mark.parametrize(("expected_type", "value"), sorted(_UUIDS_MISREAD_AS_PII.items()))
def test_reference_uuids_really_are_misread_as_personal_data(expected_type, value):
    """Premise of this module: these UUIDs are flagged in their raw form."""
    assert expected_type in {finding["type"] for finding in detect_pii(value)}


@pytest.mark.parametrize("value", sorted(_UUIDS_MISREAD_AS_PII.values()))
def test_identifier_built_from_a_misread_uuid_is_not_flagged(monkeypatch, value):
    _use_uuid(monkeypatch, value)
    plan_id = new_plan_id()

    assert _ID_SHAPE.fullmatch(plan_id), plan_id
    assert detect_pii(plan_id) == []
    assert find_blocking_identifiers(plan_id) == []


@pytest.mark.parametrize("value", sorted(_UUIDS_MISREAD_AS_PII.values()))
def test_identifier_survives_every_sanitizing_boundary_unchanged(monkeypatch, value):
    """A rewritten identifier is as bad as a blocked one: the feedback route
    looks the record up by this string, so ingress must leave it alone."""
    _use_uuid(monkeypatch, value)
    plan_id = new_plan_id()

    assert sanitize_identifiers(plan_id) == (plan_id, 0)
    cleaned, redactions = sanitize_recursive({"plan_id": plan_id})
    assert cleaned == {"plan_id": plan_id}
    assert redactions == 0


def test_generated_identifiers_hold_their_shape_and_stay_unflagged():
    for _ in range(2000):
        plan_id = new_plan_id()
        assert _ID_SHAPE.fullmatch(plan_id), plan_id
        assert detect_pii(plan_id) == []


def test_identifier_keeps_the_full_entropy_of_the_uuid_it_replaces(monkeypatch):
    """The letter mapping is one-to-one, so two identifiers collide exactly as
    rarely as the two UUIDs behind them would."""
    # Built from a seeded generator rather than uuid4() so the check stays
    # reproducible and stays independent of the source it is measuring.
    rng = random.Random(20260905)
    sources = [UUID(int=rng.getrandbits(128), version=4) for _ in range(500)]
    minted = []
    for source in sources:
        monkeypatch.setattr(plan_identifier, "uuid4", lambda source=source: source)
        minted.append(new_plan_id())

    assert len(set(minted)) == len(sources)
    assert all(len(plan_id) == len(ID_PREFIX) + ID_BODY_LENGTH for plan_id in minted)

    # Same bits in, same identifier out — the mapping carries no extra state.
    monkeypatch.setattr(plan_identifier, "uuid4", lambda: sources[0])
    assert new_plan_id() == minted[0]
