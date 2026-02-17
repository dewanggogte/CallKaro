"""Tests for SanitizedAgent._sanitize_chat_ctx, character break detection, STT garbage filter."""

from tests.conftest import SanitizedAgent, _is_character_break, _is_likely_garbage, DEFAULT_INSTRUCTIONS


class TestSanitizeChatCtx:
    def test_already_valid_user_first(self, make_chat_ctx):
        """No changes needed when user message is first after system."""
        ctx = make_chat_ctx([("system", "You are..."), ("user", "Hello"), ("assistant", "Hi")])
        result = SanitizedAgent._sanitize_chat_ctx(ctx)
        roles = [i.role for i in result.items if hasattr(i, "role")]
        assert roles == ["system", "user", "assistant"]

    def test_assistant_before_user_gets_synthetic_user_injected(self, make_chat_ctx):
        """Leading assistant message is preserved with a synthetic user message injected before it."""
        ctx = make_chat_ctx([("system", "You are..."), ("assistant", "greeting"), ("user", "Hello")])
        result = SanitizedAgent._sanitize_chat_ctx(ctx)
        non_system = [i for i in result.items if hasattr(i, "role") and i.role != "system"]
        # Synthetic user message is first
        assert non_system[0].role == "user"
        assert non_system[0].text_content == "[call connected]"
        # Original assistant message (greeting) is preserved
        assert non_system[1].role == "assistant"
        assert non_system[1].text_content == "greeting"
        # Original user message follows
        assert non_system[2].role == "user"
        assert non_system[2].text_content == "Hello"

    def test_system_only(self, make_chat_ctx):
        """No crash when only system messages exist."""
        ctx = make_chat_ctx([("system", "You are...")])
        result = SanitizedAgent._sanitize_chat_ctx(ctx)
        assert len(result.items) == 1

    def test_empty_context(self, make_chat_ctx):
        ctx = make_chat_ctx([])
        result = SanitizedAgent._sanitize_chat_ctx(ctx)
        assert len(result.items) == 0

    def test_multiple_system_then_assistant(self, make_chat_ctx):
        ctx = make_chat_ctx([("system", "A"), ("system", "B"), ("assistant", "stale"), ("user", "Hi")])
        result = SanitizedAgent._sanitize_chat_ctx(ctx)
        non_system = [i for i in result.items if hasattr(i, "role") and i.role != "system"]
        assert non_system[0].role == "user"
        assert non_system[0].text_content == "[call connected]"
        assert non_system[1].role == "assistant"

    def test_returns_copy_not_original(self, make_chat_ctx):
        ctx = make_chat_ctx([("system", "A"), ("assistant", "stale"), ("user", "Hi")])
        result = SanitizedAgent._sanitize_chat_ctx(ctx)
        assert result is not ctx

    def test_user_first_no_system(self, make_chat_ctx):
        ctx = make_chat_ctx([("user", "Hello"), ("assistant", "Hi")])
        result = SanitizedAgent._sanitize_chat_ctx(ctx)
        assert len(result.items) == 2

    def test_assistant_first_no_system(self, make_chat_ctx):
        ctx = make_chat_ctx([("assistant", "stale"), ("user", "Hi")])
        result = SanitizedAgent._sanitize_chat_ctx(ctx)
        non_system = [i for i in result.items if hasattr(i, "role")]
        assert non_system[0].role == "user"
        assert non_system[0].text_content == "[call connected]"
        assert non_system[1].role == "assistant"


class TestCharacterBreakDetection:
    """Tests for _is_character_break — detects English responses from the LLM."""

    def test_pure_english_is_break(self):
        assert _is_character_break("Okay, do you have Samsung models available?") is True

    def test_romanized_hindi_not_break(self):
        assert _is_character_break("Achha ji, toh Samsung kitne ka hai?") is False

    def test_short_text_not_break(self):
        """Short texts (<=20 chars) are never flagged as character breaks."""
        assert _is_character_break("Yes, hello there") is False

    def test_mixed_hindi_english_not_break(self):
        """Text with Hindi markers mixed in should not be a break."""
        assert _is_character_break("Achha, Samsung model available hai kya?") is False

    def test_english_with_ai_explanation(self):
        assert _is_character_break("I only speak Hindi. Let me try again in the correct language.") is True

    def test_hindi_markers_present(self):
        assert _is_character_break("Haan ji, price bata dijiye bhai sahab") is False


class TestSTTGarbageFilter:
    """Tests for _is_likely_garbage — detects STT noise artifacts."""

    def test_table_is_garbage(self):
        assert _is_likely_garbage("Table.") is True

    def test_the_is_garbage(self):
        assert _is_likely_garbage("The.") is True

    def test_and_is_garbage(self):
        assert _is_likely_garbage("And") is True

    def test_it_is_garbage(self):
        assert _is_likely_garbage("It.") is True

    def test_yes_not_garbage(self):
        """'Yes' is not in the garbage patterns — it could be valid."""
        assert _is_likely_garbage("Yes.") is False

    def test_valid_sentence_not_garbage(self):
        assert _is_likely_garbage("Tell me the price.") is False

    def test_valid_short_phrase(self):
        assert _is_likely_garbage("Haan ji") is False

    def test_empty_string_is_garbage(self):
        assert _is_likely_garbage("") is True

    def test_single_word_valid(self):
        assert _is_likely_garbage("Namaste") is False


class TestRoleReversalGuard:
    """Tests that the role reversal prevention text is present in prompts."""

    def test_default_instructions_has_role_reversal_guard(self):
        assert "main toh customer hoon" in DEFAULT_INSTRUCTIONS

    def test_default_instructions_has_redirect(self):
        assert "DO NOT answer as the shopkeeper" in DEFAULT_INSTRUCTIONS
