"""PII detectors -- especially that they do not cry wolf."""

from __future__ import annotations

import pytest

from ytshort.stages.detectors import detect, luhn_valid, mask, verhoeff_valid


class TestChecksums:
    @pytest.mark.parametrize(
        "number", ["4111111111111111", "4111 1111 1111 1111", "5500005555555559"]
    )
    def test_luhn_accepts_valid_cards(self, number: str) -> None:
        assert luhn_valid(number)

    @pytest.mark.parametrize("number", ["4111111111111112", "1234567812345678"])
    def test_luhn_rejects_invalid(self, number: str) -> None:
        assert not luhn_valid(number)

    def test_luhn_rejects_short_runs(self) -> None:
        assert not luhn_valid("12345678")

    def test_verhoeff_round_trip(self) -> None:
        # 234123412346 is the canonical valid Verhoeff example.
        assert verhoeff_valid("234123412346")
        assert not verhoeff_valid("234123412347")

    def test_verhoeff_requires_twelve_digits(self) -> None:
        assert not verhoeff_valid("12341234")


class TestMasking:
    def test_numbers_keep_only_the_last_two_digits(self) -> None:
        assert mask("4111111111111111").endswith("11")
        assert "4111" not in mask("4111111111111111")

    def test_emails_keep_the_domain_for_context(self) -> None:
        masked = mask("alice@example.com")
        assert masked.endswith("@example.com")
        assert "alice" not in masked


class TestDetection:
    def test_finds_an_email(self) -> None:
        found = detect("write to alice@example.com please")
        assert [d.kind for d in found] == ["email"]
        assert "alice" not in found[0].redacted

    def test_finds_a_valid_card_and_not_a_random_16_digit_run(self) -> None:
        assert any(d.kind == "payment_card" for d in detect("card 4111 1111 1111 1111"))
        assert not any(d.kind == "payment_card" for d in detect("ref 1234567812345678"))

    def test_a_card_is_not_also_reported_as_a_phone(self) -> None:
        kinds = [d.kind for d in detect("4111111111111111")]
        assert kinds.count("payment_card") == 1
        assert "phone" not in kinds

    def test_finds_an_indian_pan(self) -> None:
        assert any(d.kind == "pan" for d in detect("PAN ABCDE1234F on file"))

    def test_aadhaar_requires_a_valid_checksum(self) -> None:
        assert any(d.kind == "aadhaar" for d in detect("uid 2341 2341 2346"))
        # One digit off a valid number -- the checksum is what rejects it.
        assert not any(d.kind == "aadhaar" for d in detect("uid 1111 2222 3334"))
        assert not any(d.kind == "aadhaar" for d in detect("uid 1234 5678 9012"))

    def test_finds_a_phone_number(self) -> None:
        assert any(d.kind == "phone" for d in detect("call +44 7700 900123 today"))

    def test_address_hints_are_flagged(self) -> None:
        assert any(d.kind == "postal_address_hint" for d in detect("12 Baker Street, London"))

    def test_confidence_separates_checksummed_from_pattern_only(self) -> None:
        card = next(d for d in detect("4111111111111111") if d.kind == "payment_card")
        phone = next(d for d in detect("call 07700900123 now") if d.kind == "phone")
        assert card.confidence == "high"
        assert phone.confidence == "medium"

    def test_duplicates_are_collapsed(self) -> None:
        found = detect("alice@example.com and again alice@example.com")
        assert len([d for d in found if d.kind == "email"]) == 1

    @pytest.mark.parametrize("text", ["", "   ", "nothing sensitive here at all"])
    def test_clean_text_yields_nothing(self, text: str) -> None:
        assert detect(text) == []

    def test_a_plain_date_is_not_a_phone_number(self) -> None:
        assert not any(d.kind == "phone" for d in detect("published 2026-09-01"))
