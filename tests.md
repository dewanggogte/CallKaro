# Testing Guide

## Overview

The test suite covers every component of the voice AI pipeline — from text normalization and chat context handling to live Sarvam API integration and full multi-turn conversation quality. Tests are split into **unit tests** (no API keys needed) and **live integration tests** (require `.env.local`).

## Quick Start

```bash
# One-time setup
pip install pytest pytest-asyncio

# Run unit tests (no API keys needed)
pytest tests/ -v

# Run everything including live API tests
pytest tests/ --live -v

# Run a single test file
pytest tests/test_normalization.py -v

# Run scenario analysis for prompt tuning
python tests/run_scenario_analysis.py
```

## Component Test Matrix

| Component | Test File | Tests | What It Covers |
|-----------|-----------|-------|---------------|
| TTS Normalization | `test_normalization.py` | 73 | Hindi number conversion, saadhe pattern, spacing fixes, action markers, think tags, Devanagari transliteration, streaming number buffer |
| Chat Context Sanitization | `test_sanitize.py` | 25 | `SanitizedAgent._sanitize_chat_ctx` message ordering, character break detection, STT garbage filter, role reversal guard |
| LLM Provider Selection | `test_llm_provider.py` | 6 | `_create_llm` switching between Claude/Qwen |
| Transcript Saving | `test_transcript.py` | 11 | JSON schema, real file validation, save/reload roundtrip |
| Per-Call Logging | `test_logs.py` | 6 | `_setup_call_logger` file creation, handler lifecycle |
| Conversation Quality | `test_conversation.py` | 11 | Role adherence, response length, prompt structure |
| Pipeline Prompt Builder | `test_prompt_builder.py` | 22 | Casual names, greeting, research sections, model recovery, greeting NOTE |
| Constraint Checker | `test_scenario_offline.py` | 34 | All 8 constraints, scorer validation, transcript regression, edge cases |
| Live Scenarios | `test_scenario_live.py` | 20 | Multi-turn conversations against real Claude API |
| Sarvam STT (live) | `test_stt_live.py` | 2 | STT API response, language detection |
| Sarvam TTS (live) | `test_tts_live.py` | 4 | TTS audio output, normalized text, sample rates |

**Total: 188 passed** (unit tests, no API keys needed)
**With `--live`: 188 + 26 live tests**

## Conversation Quality Testing

### ConstraintChecker (`tests/conftest.py`)

Validates individual agent responses against 8 behavioral rules:

| Check | Rule | Hard/Soft |
|-------|------|-----------|
| `check_no_devanagari` | No `[\u0900-\u097F]` chars | Hard |
| `check_single_question` | Max 2 question marks | Soft |
| `check_response_length` | Under 300 chars | Hard |
| `check_no_action_markers` | No `*text*`, `(text)`, `[text]` | Hard |
| `check_no_newlines` | No `\n` in response | Hard |
| `check_no_english_translations` | No `(English text)` parentheticals | Hard |
| `check_no_end_call_text` | No `[end_call]` as literal text | Hard |
| `check_no_invented_details` | No specific old AC brands/ages/neighborhoods | Soft |

### ConversationScorer (`tests/conftest.py`)

Scores full conversations on 5 weighted dimensions:

| Dimension | Weight | Method |
|-----------|--------|--------|
| Constraint compliance | 40% | Average `check_all` score across all assistant turns |
| Topic coverage | 25% | Detect price/warranty/installation/delivery via keyword patterns |
| Price echo | 15% | Agent echoes shopkeeper's price (digit or Hindi word form) |
| Brevity | 10% | Response length scoring (< 100 chars = 1.0, < 200 = 0.7, < 300 = 0.4) |
| No repetition | 10% | Word overlap between consecutive assistant turns |

### Shopkeeper Scenarios (`tests/shopkeeper_scenarios.py`)

11 scenarios derived from real call transcripts:

| Scenario | Tests What |
|----------|-----------|
| `cooperative_direct` | Smooth info gathering |
| `cooperative_price_bundle` | Price + conditions in one message |
| `defensive_price_firm` | Agent handles "price fix hai" |
| `defensive_warranty` | Agent handles warranty deflection |
| `wrong_brand` | Agent pivots when Samsung unavailable |
| `evasive_nonsensical` | Agent redirects off-topic responses |
| `question_reversal` | Shopkeeper asks personal questions back |
| `hold_wait` | Agent responds patiently to "ek minute ruko" |
| `exchange_refusal` | Agent moves on after "exchange nahi karte" |
| `frequent_interruptions` | Agent doesn't repeat truncated text |
| `shopkeeper_rushes` | Agent persists when shopkeeper rushes |

### Scenario Analysis (`tests/run_scenario_analysis.py`)

Diagnostic script that runs all scenarios and prints:
- Per-turn responses with constraint check results
- Per-scenario conversation scores
- Aggregate constraint failure rates
- Longest responses and most questions per response

Use this after prompt changes to verify improvements.

## Test Details

### TTS Normalization (`test_normalization.py`)

Tests the full text normalization pipeline that converts LLM output to TTS-friendly text:

- **Hindi number conversion** (17 tests) — `_number_to_hindi()` for single digits, double digits (1-99 unique words), hundreds, thousands (hazaar), lakhs. Saadhe pattern: 37500 → "saadhe saintees hazaar", 1500 → "dedh hazaar", 2500 → "dhaai hazaar". Edge cases: 0, 99.
- **Number replacement in text** (10 tests) — `_replace_numbers()` for in-sentence numbers, comma-separated (36,000), ranges (10-12), special cases (1.5→dedh, 2.5→dhaai).
- **Spacing fixes** (8 tests) — lowercase→uppercase transitions (`puraneAC` → `purane AC`), digit↔letter transitions (`5star` → `5 star`), multiple space collapse.
- **Action marker stripping** (6 tests) — Removes `*confused*`, `(laughs)`, `[pauses]`, markers mid-sentence, multiple markers.
- **Think-tag stripping** (8 tests) — Complete tags, multiline, unclosed (streaming), empty, multiple.
- **Full pipeline** (8 tests) — Combined normalization with real LLM output patterns.
- **Devanagari transliteration** (8 tests) — Passthrough for Latin text, consonant+matra handling (`का` → `kaa`), mixed script, Devanagari digits, halant, full pipeline integration.

### Chat Context Sanitization (`test_sanitize.py`)

Tests `SanitizedAgent._sanitize_chat_ctx()` which ensures the first non-system message is from the user (required by vLLM/Qwen). Uses real `ChatContext` Pydantic objects — no mocking needed.

### LLM Provider Selection (`test_llm_provider.py`)

Tests `_create_llm()` returns the correct provider based on `LLM_PROVIDER` env var. Mocks `anthropic.LLM` and `openai.LLM` constructors to avoid actual API connections.

### Conversation Quality (`test_conversation.py`)

Rule-based checks on real transcript data (no judge LLM needed):
- Agent never uses shopkeeper phrases
- Responses stay under 300 characters
- No action markers or think tags leak through (checks 3 most recent transcripts)
- System prompt has required sections (VOICE & TONE, CONVERSATION FLOW, OUTPUT RULES, EXAMPLES)
- Prompt specifies caller role and end_call tool

## Mocking Strategy

- **ChatContext** — Real Pydantic models from LiveKit SDK. Instantiated with `ChatContext()` + `add_message(role=..., content=...)`. No network needed.
- **LLM constructors** — Mocked with `unittest.mock.patch` to avoid API initialization.
- **Log handlers** — Tested directly since `_setup_call_logger` only creates files. Each test cleans up handlers in `try/finally`.
- **Live APIs** — Only called when `--live` flag is passed.

## Live Test Requirements

To run `pytest tests/ --live`, you need `.env.local` with:
```
SARVAM_API_KEY=<your_key>
ANTHROPIC_API_KEY=<your_key>
```

Live tests call Claude Haiku 4.5 for conversation scenarios and Sarvam for STT/TTS. They consume API credits.

## Dashboard

```bash
python dashboard.py
# Opens http://localhost:9090
```

Also accessible via the browser test server at `GET /api/metrics` (returns JSON).

Shows: test results, latency metrics (TTFT, turn latency), token usage, conversation analytics, normalization report.

## Adding New Tests

When modifying normalization in `agent_worker.py`:
1. Add tests for the specific function (`_number_to_hindi`, `_replace_numbers`, `_normalize_for_tts`, `_transliterate_devanagari`)
2. Test edge cases (empty input, no matches, special characters)
3. Add a full-pipeline test if the change affects multiple normalization stages
4. Run `pytest tests/ -v` to verify all 188 tests still pass

When modifying the conversation prompt:
1. Run `python tests/run_scenario_analysis.py` to check constraint compliance
2. Run `pytest tests/test_scenario_live.py --live -v` to verify live tests pass
3. Check for regressions in specific constraints (question stacking, response length, etc.)
