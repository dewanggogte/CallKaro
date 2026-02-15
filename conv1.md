# Conversation 1 — Test Suite, TTS Rehaul, Voice Breaking Fix

## Session Summary

This session continued from a previous conversation that built the core agent, added per-call logging, created a 118-test suite, built a metrics dashboard, and integrated it into the browser frontend. This session focused on fixing voice quality issues, updating tests, and stabilizing the pipeline.

## Work Done (Chronological)

### 1. Test Suite Fixes After Rehaul
The previous session did a major rehaul (removed Devanagari replacements, added Hindi number conversion, rewrote prompt). Tests were broken due to stale imports.

**Fixed:**
- `tests/conftest.py` — Updated imports: removed `_TTS_REPLACEMENTS`, `_ABBREV_REPLACEMENTS`, `_WORD_REPLACEMENTS_LOWER`; added `_replace_numbers`, `_number_to_hindi`, `_HINDI_ONES`
- `tests/test_normalization.py` — Completely rewritten (~53 tests): Hindi number conversion (13), number replacement in text (10), spacing fixes (8), action markers (6), think tags (8), full pipeline (8)
- `tests/test_conversation.py` — Updated `TestSystemPromptStructure` from XML tags (`<role>`, `<output_format>`) to plain-text sections (`VOICE & TONE`, `CRITICAL OUTPUT RULES`). Updated transcript quality checks to only test 3 most recent transcripts.

**Result:** 95 passed, 6 skipped

### 2. Voice Breaking Investigation & Fix
User reported voice breaking mid-sentence.

**Root cause found in logs:** Repeated `flush audio emitter due to slow audio generation` (~15 times per call). The TTS couldn't generate audio fast enough for playback.

**Why:** `_normalize_for_tts()` and `_strip_think_tags()` both called `.strip()` on each streaming token. LLM tokens like `" Samsung"` have leading spaces for word boundaries. Stripping them caused:
1. Word concatenation: `"main" + "Samsung"` → `"mainSamsung"`
2. Sentence boundaries lost: `"hoon.Aapke"` not split by TTS SentenceTokenizer
3. Entire response sent as one long TTS request → slower than real-time → buffer underruns

**Fix:**
- Removed `.strip()` from `_normalize_for_tts()` and `_strip_think_tags()`
- Changed `if chunk:` to `if chunk.strip():` in `llm_node` (filter empty chunks without modifying them)
- Reduced browser sample rate from 24kHz to 16kHz

**Result:** Flush events dropped from ~15 to 1 per call. Word concatenation eliminated.

### 3. LLM Lost Role — Shopkeeper Instead of Caller
After fixing voice breaking, the LLM started acting as the shopkeeper instead of the caller.

**Root cause:** Greeting was sent with `add_to_chat_ctx=False`. The LLM never saw its own opening question. When user replied "Yes, tell me", the LLM had no conversation history and got confused.

**Fix:** Changed `add_to_chat_ctx=False` to `add_to_chat_ctx=True` in `agent_worker.py:516`.

**Result:** LLM correctly maintained caller role throughout the conversation.

### 4. Post-Fix Analysis
Reviewed the latest call transcript and logs. Identified remaining issues and exported to `temp.md`:

1. **Greeting spoken twice** — Sanitizer strips the greeting (first non-system must be user), LLM regenerates it
2. **Wrong store name** — LLM uses "Sharma Electronics" from examples instead of actual store name
3. **Truncated responses** — User interrupts mid-sentence, partial text saved to transcript
4. **Math errors** — LLM hallucinates arithmetic (42k + 1.5k ≠ "5 hazaar upar")
5. **`\n\n` in responses** — Multi-paragraph responses cause unnatural TTS pauses
6. **Hardcoded "Sharma Electronics"** — Examples section bleeds into behavior

### 5. Deleted v0 Folder
Removed the cloned GitHub repo (`v0/`) — safely available on GitHub if needed.

### 6. Updated All Documentation
- **README.md** — Project structure, tech stack (16kHz, enable_preprocessing), plain-text prompt, end_call as method
- **architecture.md** — Pipeline diagram, no Devanagari maps, number conversion, no `.strip()`, `add_to_chat_ctx=True`, per-call logging, `/api/metrics`
- **tests.md** — Updated test counts and descriptions for new normalization
- **MEMORY.md** — Current architecture, critical design decisions, known issues
- **.gitignore** — Added `.pytest_cache/`, `v0/`, `v1/`, `temp.md`

## Key Files Modified
| File | Change |
|------|--------|
| `agent_worker.py` | Removed `.strip()` from normalize/think functions, `chunk.strip()` guard, 16kHz browser, `add_to_chat_ctx=True` |
| `tests/conftest.py` | Updated imports for new symbols |
| `tests/test_normalization.py` | Completely rewritten (53 tests) |
| `tests/test_conversation.py` | Updated prompt structure tests, recent-only transcript checks |
| `README.md` | Full rewrite for current architecture |
| `architecture.md` | Full rewrite for current architecture |
| `tests.md` | Updated test details and counts |
| `.gitignore` | Added pytest cache, backups, temp files |
| `temp.md` | NEW — Outstanding issues to fix |

## Key Lessons Learned

1. **Never `.strip()` streaming LLM tokens** — Leading spaces are word boundaries from the tokenizer. Stripping them causes word concatenation AND prevents the TTS SentenceTokenizer from splitting sentences, causing cascading audio issues.

2. **`add_to_chat_ctx=True` for greetings** — If the agent speaks first via `session.say()`, the greeting MUST be in chat context. Otherwise the LLM has no conversation history and may adopt the wrong role.

3. **`@function_tool()` must be on Agent methods** — Module-level `@function_tool()` does NOT auto-register with the LLM. The tool must be a method on the Agent subclass.

4. **Sarvam TTS `enable_preprocessing=True`** — Handles Romanized Hindi pronunciation internally, eliminating the need for Devanagari replacement maps that caused mixed-script issues.

5. **16kHz > 24kHz for browser** — Lower sample rate generates faster, reducing TTS buffer underruns with minimal quality loss for voice.

## Current State
- 95 tests passing, 6 skipped (live)
- Agent maintains caller role correctly
- Voice breaking largely resolved (1 flush event vs ~15)
- Word concatenation fixed
- Hindi number pronunciation working
- Outstanding issues documented in `temp.md`
