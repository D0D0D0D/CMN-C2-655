# CMN-C2-655 — Unit Tests: identifier_sanitizer
# Regression coverage for the MR-B finding: shared.security.pii_detector.detect_pii
# misses (1) Japanese personal names entirely and (2) a JP phone number
# immediately followed by Japanese text with no delimiter (trailing \b bug).
# See src/services/identifier_sanitizer.py module docstring.

from shared.security.pii_detector import detect_pii
from src.services.identifier_sanitizer import sanitize_identifiers


class TestDetectPiiKnownGaps:
    """Documents the upstream gap this module works around (not a claim on
    our own code — these assertions pin the framework's current behavior so
    a future SDK bump that fixes it is visible as a test change, not silence)."""

    def test_detect_pii_misses_japanese_name(self):
        findings = detect_pii("田中太郎さんによろしく")
        assert not any(f["type"] == "name" for f in findings)

    def test_detect_pii_misses_phone_before_japanese_text(self):
        findings = detect_pii("電話080-1234-5678です")
        assert not any(f["type"] in ("phone_jp",) for f in findings)


class TestSanitizeIdentifiers:
    def test_redacts_japanese_name_with_honorific(self):
        sanitized, count = sanitize_identifiers("田中太郎さんによろしく")
        assert "田中太郎" not in sanitized
        assert count >= 1

    def test_redacts_jp_phone_before_japanese_text(self):
        sanitized, count = sanitize_identifiers("電話080-1234-5678です")
        assert "080-1234-5678" not in sanitized
        assert count >= 1

    def test_redacts_email_via_detect_pii(self):
        sanitized, count = sanitize_identifiers("連絡先はtanaka@example.comです")
        assert "tanaka@example.com" not in sanitized
        assert count >= 1

    def test_combined_identifiers_all_redacted(self):
        text = "田中太郎さんはベジタリアンなので和食にしてください。連絡先は tanaka@example.com、電話080-1234-5678です。"
        sanitized, count = sanitize_identifiers(text)
        assert "田中太郎" not in sanitized
        assert "tanaka@example.com" not in sanitized
        assert "080-1234-5678" not in sanitized
        assert count == 3

    def test_no_identifiers_returns_unchanged(self):
        sanitized, count = sanitize_identifiers("予算をもう少し下げてください")
        assert sanitized == "予算をもう少し下げてください"
        assert count == 0

    def test_empty_text(self):
        sanitized, count = sanitize_identifiers("")
        assert sanitized == ""
        assert count == 0

    def test_non_person_place_name_not_over_redacted(self):
        # Guard against over-broad honorific matching swallowing legitimate
        # plan text (destination names etc.) — "様" alone without a preceding
        # name-shaped run should not nuke unrelated content beyond the match.
        sanitized, _count = sanitize_identifiers("箱根で温泉を楽しみたいです")
        assert "箱根" in sanitized
