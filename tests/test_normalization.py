"""Tests for TTS text normalization — _normalize_for_tts, _strip_think_tags, _ACTION_RE, number conversion, Devanagari transliteration, streaming number buffer."""

import pytest
from tests.conftest import (
    _normalize_for_tts, _strip_think_tags, _replace_numbers,
    _number_to_hindi, _transliterate_devanagari, _NumberBufferedNormalizer,
)


# ===================================================================
# A. Hindi number conversion
# ===================================================================
class TestHindiNumberConversion:
    def test_basic_single_digit(self):
        assert _number_to_hindi(5) == "paanch"

    def test_double_digit(self):
        assert _number_to_hindi(36) == "chhatees"

    def test_hundred(self):
        assert "sau" in _number_to_hindi(100)

    def test_thousand(self):
        assert "hazaar" in _number_to_hindi(1000)

    def test_36000(self):
        result = _number_to_hindi(36000)
        assert "chhatees" in result
        assert "hazaar" in result

    def test_25000(self):
        result = _number_to_hindi(25000)
        assert "pachchees" in result
        assert "hazaar" in result

    def test_42000(self):
        result = _number_to_hindi(42000)
        assert "bayaalees" in result
        assert "hazaar" in result

    def test_lakh(self):
        result = _number_to_hindi(100000)
        assert "lakh" in result

    def test_50000(self):
        result = _number_to_hindi(50000)
        assert "pachaas" in result
        assert "hazaar" in result

    def test_saadhe_37500(self):
        assert _number_to_hindi(37500) == "saadhe saintees hazaar"

    def test_saadhe_39500(self):
        assert _number_to_hindi(39500) == "saadhe untaalees hazaar"

    def test_dedh_hazaar(self):
        assert _number_to_hindi(1500) == "dedh hazaar"

    def test_dhaai_hazaar(self):
        assert _number_to_hindi(2500) == "dhaai hazaar"

    def test_zero(self):
        assert _number_to_hindi(0) == "zero"

    def test_99(self):
        assert _number_to_hindi(99) == "ninyanbe"

    def test_10(self):
        assert _number_to_hindi(10) == "das"

    def test_12(self):
        assert _number_to_hindi(12) == "baarah"


class TestNumberReplacementInText:
    def test_36000_in_sentence(self):
        result = _replace_numbers("36000 mein milega")
        assert "chhatees hazaar" in result
        assert "36000" not in result

    def test_42_hazaar(self):
        result = _replace_numbers("42 hazaar? Thoda zyada hai")
        assert "bayaalees" in result
        assert "42" not in result

    def test_1_point_5_becomes_dedh(self):
        result = _replace_numbers("1.5 ton ka AC")
        assert "dedh" in result

    def test_2_point_5_becomes_dhaai(self):
        result = _replace_numbers("2.5 ton")
        assert "dhaai" in result

    def test_5_star(self):
        result = _replace_numbers("5 star rating")
        assert "paanch" in result

    def test_comma_separated_number(self):
        result = _replace_numbers("36,000 ka price")
        assert "chhatees hazaar" in result

    def test_25000(self):
        result = _replace_numbers("25000 final")
        assert "pachchees hazaar" in result

    def test_range_10_to_12(self):
        result = _replace_numbers("10-12 hazaar")
        assert "das" in result
        assert "baarah" in result

    def test_no_numbers(self):
        assert _replace_numbers("koi number nahi") == "koi number nahi"

    def test_500_extra(self):
        result = _replace_numbers("500 extra")
        assert "paanch sau" in result


# ===================================================================
# B. Spacing fixes (lowercase→uppercase, digit→letter)
# ===================================================================
class TestSpacingFixes:
    def test_lowercase_to_uppercase(self, normalize):
        assert "purane AC" in normalize("puraneAC")

    def test_split_ac(self, normalize):
        assert "split AC" in normalize("splitAC")

    def test_digit_to_letter(self, normalize):
        # "5star" → "5 star" (spacing fix between digit and letter)
        # Number not replaced because \b doesn't match inside "5star"
        result = normalize("5star")
        assert "5 star" in result

    def test_letter_to_digit(self, normalize):
        result = normalize("Samsung1")
        # After number replacement: "Samsung" + "ek" with space
        assert "Samsung" in result

    def test_no_extra_spaces_clean_text(self, normalize):
        assert normalize("hello world") == "hello world"

    def test_multiple_space_collapse(self, normalize):
        result = normalize("hello   world   ji")
        assert "  " not in result

    def test_warranty_uppercase_boundary(self, normalize):
        result = normalize("warrantyKitni")
        assert "warranty Kitni" in result

    def test_installation_free(self, normalize):
        # Both lowercase — no automatic fix (streaming chunk boundary issue)
        # Normalize only fixes lowercase→uppercase transitions
        result = normalize("Installationfree")
        assert "Installation" in result


# ===================================================================
# C. Action marker stripping
# ===================================================================
class TestActionMarkerStripping:
    def test_asterisk_markers(self, normalize):
        assert "confused" not in normalize("*confused*")

    def test_paren_markers(self, normalize):
        assert "laughs" not in normalize("(laughs)")

    def test_bracket_markers(self, normalize):
        assert "pauses" not in normalize("[pauses]")

    def test_marker_in_sentence(self, normalize):
        result = normalize("Main *confused* hoon")
        assert "*" not in result
        assert "Main" in result
        assert "hoon" in result

    def test_multiple_markers(self, normalize):
        result = normalize("*smiles* Hello *pauses* ji")
        assert "*" not in result
        assert "Hello" in result
        assert "ji" in result

    def test_normal_text_preserved(self, normalize):
        assert "kya" in normalize("kya hai bhai")


# ===================================================================
# D. Think-tag stripping
# ===================================================================
class TestStripThinkTags:
    def test_complete_think_tag(self, strip_think):
        assert strip_think("<think>reasoning</think>Hello") == "Hello"

    def test_multiline_think_tag(self, strip_think):
        result = strip_think("<think>\nI should ask about price\n</think>Namaste")
        assert result == "Namaste"

    def test_unclosed_think_tag_streaming(self, strip_think):
        assert strip_think("<think>partial reasoning") == ""

    def test_no_think_tags(self, strip_think):
        assert strip_think("Normal text") == "Normal text"

    def test_empty_think_tag(self, strip_think):
        assert strip_think("<think></think>Achha") == "Achha"

    def test_think_tag_with_content_after(self, strip_think):
        result = strip_think("<think>Let me negotiate</think>Thoda kam karo")
        assert result == "Thoda kam karo"

    def test_multiple_think_tags(self, strip_think):
        result = strip_think("<think>first</think>Hello<think>second</think> ji")
        assert "first" not in result
        assert "second" not in result
        assert "Hello" in result

    def test_think_tag_only(self, strip_think):
        assert strip_think("<think>only thinking</think>") == ""


# ===================================================================
# E. Full pipeline / combined tests
# ===================================================================
class TestFullNormalizationPipeline:
    def test_numbers_converted_in_pipeline(self, normalize):
        result = normalize("36000 mein milega")
        assert "chhatees hazaar" in result
        assert "36000" not in result

    def test_price_with_number_words(self, normalize):
        result = normalize("42 hazaar? Online pe toh 38 mein dikha raha tha.")
        assert "bayaalees" in result
        assert "adtees" in result

    def test_1_5_ton_becomes_dedh(self, normalize):
        result = normalize("Samsung 1.5 ton ka AC")
        assert "dedh" in result

    def test_action_markers_stripped(self, normalize):
        result = normalize("*thinking* Achha theek hai")
        assert "*" not in result
        assert "Achha" in result

    def test_empty_string(self, normalize):
        assert normalize("") == ""

    def test_clean_romanized_hindi_unchanged(self, normalize):
        text = "Achha bhai sahab rate kya hai"
        assert normalize(text) == text

    def test_spacing_and_numbers_combined(self, normalize):
        result = normalize("puraneAC ke 10 hazaar kam")
        assert "purane AC" in result
        assert "das" in result

    def test_5_star_inverter(self, normalize):
        result = normalize("5 star inverter split AC")
        assert "paanch star" in result


# ===================================================================
# F. Devanagari transliteration safety net
# ===================================================================
class TestDevanagariTransliteration:
    """Tests for _transliterate_devanagari — converts leaked Devanagari to Romanized Hindi."""

    def test_no_devanagari_passthrough(self):
        text = "Achha bhai sahab rate kya hai"
        assert _transliterate_devanagari(text) == text

    def test_ka_matra(self):
        # The actual bug: "usका" → "uskaa"
        result = _transliterate_devanagari("usका")
        assert result == "uskaa"
        assert "का" not in result

    def test_full_devanagari_word(self):
        result = _transliterate_devanagari("कैसे")
        assert all(c.isascii() for c in result)

    def test_mixed_script(self):
        result = _transliterate_devanagari("Toh usका price kya hai?")
        assert "का" not in result
        assert "Toh us" in result
        assert "price kya hai?" in result

    def test_devanagari_digits(self):
        result = _transliterate_devanagari("₹४०,०००")
        assert "40,000" in result

    def test_empty_string(self):
        assert _transliterate_devanagari("") == ""

    def test_halant_suppresses_vowel(self):
        # क् (ka + halant) → "k" (halant suppresses the inherent 'a')
        result = _transliterate_devanagari("क्")
        assert result == "k"

    def test_normalize_pipeline_strips_devanagari(self):
        """Ensure _normalize_for_tts catches Devanagari via transliteration."""
        result = _normalize_for_tts("Achha. Toh usका price kya hai?")
        assert "का" not in result
        assert all(c.isascii() or c in '₹' for c in result)


# ===================================================================
# G. Streaming number buffer (_NumberBufferedNormalizer)
# ===================================================================
class TestNumberBufferedNormalizer:
    """Tests for the streaming digit buffer that prevents number splitting."""

    def test_split_28000_across_chunks(self):
        """'28' + '000' should produce 'attaaees hazaar', not 'attaaees' + 'zero'."""
        buf = _NumberBufferedNormalizer()
        out1 = buf.process("Achha, 28")
        out2 = buf.process("000.")
        out3 = buf.flush()
        combined = out1 + out2 + out3
        assert "attaaees hazaar" in combined
        assert "zero" not in combined

    def test_split_16000_across_chunks(self):
        """'16' + '000' should produce 'solah hazaar'."""
        buf = _NumberBufferedNormalizer()
        out1 = buf.process("Price 16")
        out2 = buf.process("000 hai.")
        out3 = buf.flush()
        combined = out1 + out2 + out3
        assert "solah hazaar" in combined
        assert "zero" not in combined

    def test_no_split_needed(self):
        """Complete number in a single chunk should work normally."""
        buf = _NumberBufferedNormalizer()
        out1 = buf.process("Achha, ")
        out2 = buf.process("38000")
        out3 = buf.process(".")
        out4 = buf.flush()
        combined = out1 + out2 + out3 + out4
        assert "adtees hazaar" in combined

    def test_number_at_end_of_stream(self):
        """Number at the end with no following chunk should be flushed."""
        buf = _NumberBufferedNormalizer()
        out1 = buf.process("price 500")
        out2 = buf.flush()
        combined = out1 + out2
        assert "paanch sau" in combined

    def test_no_numbers(self):
        """Plain text without numbers should pass through unchanged."""
        buf = _NumberBufferedNormalizer()
        out1 = buf.process("Achha ji ")
        out2 = buf.process("theek hai")
        out3 = buf.flush()
        combined = out1 + out2 + out3
        assert "Achha ji" in combined
        assert "theek hai" in combined

    def test_multiple_numbers_in_stream(self):
        """Multiple numbers across chunks should all be converted correctly."""
        buf = _NumberBufferedNormalizer()
        out1 = buf.process("Price 38")
        out2 = buf.process("000, installation 15")
        out3 = buf.process("00 extra.")
        out4 = buf.flush()
        combined = out1 + out2 + out3 + out4
        assert "adtees hazaar" in combined
        assert "dedh hazaar" in combined

    def test_single_digit_not_buffered_unnecessarily(self):
        """Single digit followed by non-digit should be processed immediately."""
        buf = _NumberBufferedNormalizer()
        out1 = buf.process("5 star AC")
        out2 = buf.flush()
        combined = out1 + out2
        assert "paanch star AC" in combined

    def test_empty_chunks(self):
        """Empty chunks should not cause errors."""
        buf = _NumberBufferedNormalizer()
        assert buf.process("") == ""
        assert buf.process("hello") == "hello"
        assert buf.flush() == ""
