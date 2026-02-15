"""Shared fixtures and configuration for the test suite."""

import os
import re
import sys
import pytest

# Set dummy env vars so agent_worker.py can be imported without .env.local
os.environ.setdefault("SARVAM_API_KEY", "test-dummy-key")
os.environ.setdefault("LIVEKIT_URL", "ws://localhost:7880")
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-dummy-key")

# Ensure project root is on sys.path
sys.path.insert(0, str(os.path.dirname(os.path.dirname(__file__))))

from agent_worker import (
    _normalize_for_tts,
    _strip_think_tags,
    _ACTION_RE,
    _replace_numbers,
    _number_to_hindi,
    _transliterate_devanagari,
    _HINDI_ONES,
    SanitizedAgent,
    _create_llm,
    _setup_call_logger,
    DEFAULT_INSTRUCTIONS,
    CLAUDE_MODEL,
)
from call_analysis import ConstraintChecker, ConversationScorer
from livekit.agents.llm import ChatContext


# ---------------------------------------------------------------------------
# CLI option: --live
# ---------------------------------------------------------------------------
def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", default=False, help="Run live API tests")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--live"):
        skip_live = pytest.mark.skip(reason="need --live option to run")
        for item in items:
            if "live" in item.keywords:
                item.add_marker(skip_live)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def normalize():
    """Return the _normalize_for_tts function."""
    return _normalize_for_tts


@pytest.fixture
def strip_think():
    """Return the _strip_think_tags function."""
    return _strip_think_tags


@pytest.fixture
def action_re():
    """Return the _ACTION_RE compiled regex."""
    return _ACTION_RE


@pytest.fixture
def make_chat_ctx():
    """Factory to create ChatContext with a specified role sequence."""
    def _make(roles_and_texts: list[tuple[str, str]]) -> ChatContext:
        ctx = ChatContext()
        for role, text in roles_and_texts:
            ctx.add_message(role=role, content=text)
        return ctx
    return _make


@pytest.fixture
def sample_transcript_data():
    """Return a dict matching the transcript JSON schema."""
    return {
        "store_name": "Test Store",
        "ac_model": "Samsung 1.5 Ton 5 Star Inverter Split AC",
        "room": "test-room-abc123",
        "phone": "+919876543210",
        "timestamp": "2026-02-11T17:30:23.924321",
        "messages": [
            {"role": "user", "text": "Hello", "time": "2026-02-11T17:27:41.875130"},
            {"role": "assistant", "text": "Namaste", "time": "2026-02-11T17:27:52.494117"},
        ],
    }


@pytest.fixture
def constraint_checker():
    return ConstraintChecker()


@pytest.fixture
def conversation_scorer(constraint_checker):
    return ConversationScorer(constraint_checker)


@pytest.fixture
def build_chat_context():
    """Build a ChatContext with DEFAULT_INSTRUCTIONS as system + conversation turns."""
    def _build(turns: list[tuple[str, str]], store_name="Gupta Electronics",
               ac_model="Samsung 1.5 Ton 5 Star Inverter Split AC"):
        ctx = ChatContext()
        greeting = f"Hello, yeh {store_name} hai? Aap log AC dealer ho?"
        instructions = DEFAULT_INSTRUCTIONS + (
            f"\nPRODUCT: {ac_model}\nSTORE: {store_name}\n"
            f'\nNOTE: You have already greeted the shopkeeper with: "{greeting}"\n'
            "Do NOT repeat the greeting. Continue the conversation from the shopkeeper's response.\n"
        )
        ctx.add_message(role="system", content=instructions)
        for role, text in turns:
            ctx.add_message(role=role, content=text)
        return ctx
    return _build
