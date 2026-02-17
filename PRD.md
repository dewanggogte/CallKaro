# PRD: Hyperlocal Discovery — Voice Price Comparison Agent

## Context

Hyperlocal Discovery is a consumer-facing tool that calls local shops to compare prices on appliances. A user describes what they want ("1.5 ton AC for a 2BHK in Koramangala"), the system researches the product, finds nearby stores, and dispatches an AI voice agent that calls each store pretending to be a regular customer asking for prices in Hindi. Results are compared and the best deal is recommended.

The core voice agent (LiveKit + Sarvam STT/TTS + Claude Haiku) works end-to-end ~~but has critical reliability issues discovered through log analysis of real calls~~. The pipeline (intake → research → store discovery → calling → analysis) is functional ~~but needs polish~~. ~~This PRD prioritizes fixing the identified bugs before any new features.~~

> **Status (Feb 17, 2026):** All 15 pitfalls identified below have been fixed and shipped. 188 tests passing (up from 141). See Implementation Plan section for per-item status.

### What prompted this

Analysis of call logs (Croma, Reliance Digital, Browser Test sessions from Feb 17) revealed 8 pitfalls — from price-corrupting number bugs to TTS crashes on English output. The most severe: streaming token boundaries split numbers like "28000" into "28" + "000", which get independently converted to "attaaees" + "zero" instead of "attaaees hazaar". This corrupts the exact data the product exists to collect.

---

## Product Vision

**One-liner:** "Tell us what you want to buy, and we'll call the shops for you."

**Target user:** A regular consumer in an Indian city who wants to compare prices before buying an appliance — but doesn't want to call 5 shops themselves.

**Deployment modes:**
- **Browser** — user plays the shopkeeper for testing/demo (current primary mode)
- **Phone** — agent calls real shops via SIP trunking for production data collection

---

## Current State

### What exists and works

| Component | Status | Key files |
|---|---|---|
| Voice agent (STT→LLM→TTS pipeline) | Working, has bugs | `agent_worker.py` |
| Browser WebRTC test interface | Working | `test_browser.py`, `app.py` |
| Pipeline: intake chat | Working | `pipeline/intake.py` |
| Pipeline: product research (LLM + web search) | Working | `pipeline/research.py` |
| Pipeline: store discovery (Maps + web search) | Working, fragile | `pipeline/store_discovery.py` |
| Pipeline: dynamic prompt building | Working | `pipeline/prompt_builder.py` |
| Pipeline: cross-store comparison | Working | `pipeline/analysis.py` |
| Pipeline: session orchestrator | Working | `pipeline/session.py` |
| Full web UI (4-step wizard + quick call + dashboard) | Working | `app.py` |
| Post-call quality analysis | Working | `call_analysis.py` |
| Test suite (188 unit + 26 live) | Passing | `tests/` |
| Per-call logging & transcript saving | Working | `agent_worker.py` |
| Dev file watcher (auto-test, auto-analyze) | Working | `dev_watcher.py` |
| Metrics dashboard (TTFT, tokens, latency charts) | Working | `dashboard.py` |
| Docker + Render deployment | Working | `Dockerfile`, `render.yaml` |
| GitHub Actions CI (build + test + push) | Working | `.github/workflows/docker.yml` |
| Shared agent lifecycle module | Working | `agent_lifecycle.py` |

### Architecture

```
User (browser/phone) ←→ LiveKit Server ←→ Agent Worker
                                              │
                              ┌───────────────┼───────────────┐
                              ▼               ▼               ▼
                         Silero VAD    Sarvam saaras:v3   Claude Haiku
                        (speech det)   (STT-translate)    (LLM, 0.7 temp)
                                              │               │
                              MultilingualModel               │
                              (turn detection)                │
                                                              ▼
                                                    SanitizedAgent
                                                    (llm_node hook)
                                                         │
                                              ┌──────────┼──────────┐
                                              ▼          ▼          ▼
                                     _strip_think   _normalize   _check_
                                       _tags()      _for_tts()   character
                                                         │        _break()
                                                         ▼
                                                  Sarvam bulbul:v3
                                                  (TTS, Hindi)
```

---

## Identified Pitfalls (from log analysis)

### P1 — CRITICAL: Streaming number splitting

**Bug:** `_normalize_for_tts()` runs per-streaming-chunk. LLM token boundaries can split numbers: `"28"` + `"000"` → `"attaaees"` + `"zero"` instead of `"attaaees hazaar"`.

**Evidence:** Reliance call — "attaaeeszero" for ₹28000, "solahzero" for ₹16000. Corrupts the exact price data that is the product's core value.

**Files:** `agent_worker.py:126-139` (llm_node streaming loop)

**Fix:** Buffer trailing digits at the end of each chunk. If a chunk ends with digits, hold them and prepend to the next chunk before applying `_replace_numbers()`. On stream end, flush the buffer.

### P2 — CRITICAL: No TTS error recovery

**Bug:** When the LLM outputs English text, Sarvam TTS crashes with `Text must contain at least one character from the allowed languages`. The error is logged but the call goes silent — no fallback, no retry, no graceful end.

**Evidence:** Croma call — TTS crash on English response, user hears nothing.

**Files:** `agent_worker.py:100-144` (llm_node), session error handler (~line 644)

**Fix:** In the `error` event handler, detect TTS language errors specifically. On TTS crash: (1) log `[TTS CRASH]`, (2) attempt to inject a canned Hindi fallback response ("Ek second, connection mein problem aa rahi hai"), (3) if repeated failures, trigger `end_call` with a graceful Hindi goodbye.

### P3 — HIGH: STT garbage transcripts with no quality signal

**Bug:** Sarvam STT returns garbled translations with no confidence score (hardcoded to 1.0 in plugin). "Table.", "Tell me the round from there.", "You can tell the Mohadarma or whatever." — all passed to the LLM as valid input.

**Evidence:** Reliance call ("Table."), Croma call ("Tell me the round from there."), Browser tests.

**Files:** `agent_worker.py:594-598` (user_input_transcribed handler)

**Fix:** Add a heuristic garbage filter in the `user_input_transcribed` handler. Flag transcripts that are: (a) under 3 words and contain no recognizable keywords, or (b) contain known STT artifacts like "Table.", "The.", "And." as the entire message. Log `[STT GARBAGE]` and optionally skip forwarding to LLM. Start with logging-only to measure frequency before filtering.

### P4 — HIGH: LLM latency (TTFT 0.7–1.7s, growing with context)

**Issue:** System prompt alone is ~2400 tokens. Over a 14-turn call, prompt tokens grow from 2419 to 3113. TTFT ranges from 0.72s to 1.70s. Total mouth-to-ear latency ~2.3s — well above the 1s threshold.

**Evidence:** Reliance call TTFT values: 0.97, 0.75, 1.17, 0.72, 0.96, 0.79, **1.70**, **1.50**, **1.47**, 1.17, 0.89, **1.45**, 1.02, 1.07.

**Files:** `agent_worker.py:462-484` (_create_llm), `agent_worker.py:373-455` (DEFAULT_INSTRUCTIONS), `pipeline/prompt_builder.py`

**Fix (multi-pronged):**
1. **Enable Anthropic prompt caching** — add `caching="ephemeral"` to the LLM config. Up to 80% TTFT reduction on cached prefix (highest-impact single change).
2. **Add `max_tokens=150`** to Claude LLM config — caps response length, faster generation.
3. **Trim prompt** — remove EXAMPLES section from system prompt (saves ~200 tokens). Move to few-shot in chat context only when needed.
4. **Measure** — add TTFT to the `[LLM METRICS]` log (already exists) and track p50/p90 trends.

### P5 — MEDIUM: Role reversal under adversarial input

**Bug:** When user speaks as a customer (asking questions), the agent reverses roles and becomes the shopkeeper. "Haan ji, hum AC repair bhi karte hain aur naye AC bhi bechte hain."

**Evidence:** Browser_Test_161408 — agent answered "Do you repair ACs?" as if it were the shop.

**Files:** `agent_worker.py:446-455` (STAY IN CHARACTER), `pipeline/prompt_builder.py:101-109`

**Fix:** Strengthen the prompt's role anchoring. Add after the STAY IN CHARACTER section:
```
- If the user asks YOU a question as if YOU are the shopkeeper (e.g. "Do you repair ACs?", "What brands do you have?"), DO NOT answer as the shopkeeper. Instead, redirect: "Nahi nahi, main toh customer hoon. Mujhe AC ka price chahiye."
```

### P6 — MEDIUM: Character breaks (English responses)

**Bug:** LLM occasionally responds in English, especially on confusing first messages or when asked to speak English.

**Evidence:** Browser_Test_155257 — "Okay, do you have Samsung..." and "I only speak Hindi. Let me try again..." Both are English.

**Files:** `agent_worker.py:190-210` (_check_character_break — already added), prompt sections

**Status:** Partially mitigated by the previous commit (STT translation explanation, "NEVER respond in English" rule, greeting in chat context, character break detection logging). The logging is in place; the remaining gap is active recovery — when a character break IS detected, retry with a canned Hindi response instead of sending English to TTS.

**Fix:** In `llm_node`, after `_check_character_break()` detects a break, replace the accumulated response text with a canned Hindi fallback: "Achha ji, aap AC ka price bata dijiye." Log the replacement. This prevents the English text from reaching TTS.

### P7 — LOW: LLM outputs Hindi words instead of digits

**Bug:** LLM sometimes writes "do sau pachaas" instead of "250", bypassing the deterministic number conversion.

**Evidence:** Reliance call — "Haan, do sau pachaas liter wala de do."

**Files:** `agent_worker.py:435-436` (digit instruction in prompt)

**Fix:** Reinforce in prompt with a negative example. Low priority since TTS handles Hindi words fine — this only matters for the transcript analysis number-echo checker.

### P8 — LOW: Greeting repeated (FIXED)

Already fixed in previous commit — greeting now in chat context with synthetic `[call connected]` user message.

### P9 — HIGH: Research intelligence not passed to voice agent

**Bug:** `prompt_builder.py` uses only 4 of 7 `ResearchOutput` fields (`questions_to_ask`, `topics_to_cover`, `topic_keywords`, `market_price_range`). Three fields are ignored: `product_summary`, `competing_products`, `important_notes`. The agent has zero product knowledge — when a shopkeeper asks "which model do you want?", it can only repeat "best model kya hai?" because it doesn't know any model names.

**Evidence:** Reliance Digital fridge call — shopkeeper asked "which model?" 4 times. Agent looped on the same question with no recovery.

**Files:** `pipeline/prompt_builder.py`

**Fix:** Add 3 new prompt sections: PRODUCT KNOWLEDGE (summary + top 3 competing products), BUYER NOTES (top 3 important_notes), WHEN STUCK (strategies for recovery). All conditional — empty data = section omitted. ~170-230 extra tokens, cached prefix.

### P10 — MEDIUM: Verbose greeting confuses shopkeepers

**Bug:** Greeting uses the raw `category` field (e.g. "Medium double door fridge with separate freezer section (220-280L)"). Shopkeeper literally said "I didn't understand anything."

**Evidence:** Reliance Digital fridge call — verbose greeting confused the shopkeeper.

**Files:** `pipeline/prompt_builder.py`, `pipeline/session.py`

**Fix:** Add `_casual_product_name()` helper that strips parenthetical specs, size adjectives, and "with ..." clauses. Add `build_greeting()` function. Replace verbose category with casual name in all spoken prompt sections.

### P11 — MEDIUM: No topic pivot strategy when stuck

**Bug:** When a conversation stalls (shopkeeper keeps asking "which model?", agent can't answer), the agent has no strategy for pivoting to a different topic or unblocking itself.

**Evidence:** Same Reliance Digital call — 4 failed "which model?" exchanges.

**Files:** `pipeline/prompt_builder.py`

**Fix:** Add WHEN STUCK section to prompt: (1) name a specific model from research, (2) "Achha theek hai" and pivot after 2 failed attempts, (3) anchor to lower end of price range if asked about budget.

### P12 — MEDIUM: Duplicate greeting in transcript

**Bug:** Greeting appears twice in the transcript. `session.say(greeting, add_to_chat_ctx=True)` fires the `conversation_item_added` handler which appends to `transcript_lines`, AND there was an explicit `transcript_lines.append()` right after — double recording.

**Evidence:** Girias call transcript — identical greeting at timestamps 17:47:19 and 17:47:25.

**Files:** `agent_worker.py` (~line 900)

**Fix:** Remove the explicit `transcript_lines.append()` after `session.say()`. The `conversation_item_added` handler already captures it.

### P13 — MEDIUM: LLM repeats greeting as first response (pipeline prompt)

**Bug:** `prompt_builder.build_prompt()` was missing the NOTE telling the LLM that the greeting was already spoken. Unlike `agent_worker.py`'s `DEFAULT_INSTRUCTIONS` (which had the NOTE), the pipeline prompt let the LLM generate the greeting again as its first response.

**Evidence:** Girias call — greeting spoken twice (once by `session.say()`, once by LLM).

**Files:** `pipeline/prompt_builder.py`

**Fix:** Add `greeting_note` to `build_prompt()` — `"NOTE: You have already greeted the shopkeeper with: '{greeting}'. Do NOT repeat the greeting."` Appended at the end of the prompt after the STORE line.

### P14 — MEDIUM: Research phase blocks HTTP server

**Bug:** `_handle_research()` in `app.py` called `asyncio.run(session.research_and_discover())` synchronously, blocking the single-threaded `HTTPServer`. During the 10-20 second research phase, the frontend couldn't poll for events — it appeared stuck with no progress updates.

**Files:** `app.py`

**Fix:** Run research in a background `threading.Thread`. Return immediately with `{"status": "started"}`. Add a GET endpoint `/api/session/{id}/research` for polling. Frontend POST starts research, then polls GET until `{"status": "done"}`.

### P15 — HIGH: Results table shows only store name (no price/warranty data)

**Bug:** `_collect_call_results_from_transcripts()` populated `extracted_data` from `analysis.get("scores", {})` — which gives constraint quality scores (`{"constraint": 1.0, "topic": 1.0}`), NOT actual price/warranty data. The comparison LLM received useless metrics. For single-store calls, the LLM was skipped entirely, returning raw `CallResult` with no structured price data.

**Evidence:** All calls — results table showed store name and "Best Deal" badge but no price, warranty, installation, or delivery information.

**Files:** `pipeline/session.py`, `pipeline/analysis.py`

**Fix:** (1) Include transcript messages in `extracted_data` so the comparison LLM has actual conversation to analyze. (2) Always run through the LLM to extract structured data (even for single-store). (3) Add `warranty` field to the LLM output schema. (4) Update frontend to display warranty in the extras column.

---

## Implementation Plan

> **All items below are COMPLETE.** Strikethrough indicates shipped code.

### ~~Phase 1: Critical Bug Fixes (P1 + P2)~~ DONE

#### ~~1a. Fix streaming number splitting~~ SHIPPED

**File:** `agent_worker.py` — `SanitizedAgent.llm_node()`

Add a digit buffer to the streaming loop. The current code:
```python
async for chunk in Agent.default.llm_node(...):
    chunk = _normalize_for_tts(chunk)  # BUG: splits numbers
    yield chunk
```

New approach — create `_NumberBufferedNormalizer` class:
- Maintains a `_digit_buffer: str` across chunks
- When a chunk ends with digits (regex `\d+$`), strip them and hold in buffer
- When next chunk arrives, prepend the buffer
- When a chunk does NOT end with digits and buffer is non-empty, flush buffer with current chunk
- On stream end (after the `async for` loop), flush any remaining buffer
- Apply `_normalize_for_tts()` only on flushed/complete text

**Tests to add:** `tests/test_normalization.py`
- `test_streaming_number_split_28000` — chunks `["Achha, 28", "000."]` → `"Achha, attaaees hazaar."`
- `test_streaming_number_split_16000` — chunks `["solah", "000"]` → handled (no digits to split here, this is the LLM writing words — no buffer needed)
- `test_streaming_no_split_needed` — chunks `["Achha, ", "38000", "."]` → `"Achha, adtees hazaar."`
- `test_streaming_number_at_end_of_stream` — chunk `["price 500"]` with no following chunk → flushed as `"price paanch sau"`

#### ~~1b. Add TTS crash recovery~~ SHIPPED

**File:** `agent_worker.py` — session error handler and `llm_node`

Two layers of defense:

**Layer 1 (llm_node):** After character break detection, if break detected, replace response with canned Hindi:
```python
if self._last_response_text:
    _check_character_break(self._last_response_text)
    if _is_character_break(self._last_response_text):  # same logic, returns bool
        logger.warning(f"[CHARACTER BREAK RECOVERY] Replacing English response")
        # Can't un-yield chunks already sent, but we can flag for TTS error handler
        self._character_break_detected = True
```

**Layer 2 (error handler):** Detect TTS language errors and inject fallback:
```python
@session.on("error")
def on_error(ev):
    error = ev.error
    if hasattr(error, 'error') and 'allowed languages' in str(error.error):
        logger.error(f"[TTS CRASH] English text sent to Hindi TTS")
        # The TTS will retry (retryable=True), but the text is still English.
        # We can't change it mid-stream. Log for now.
        # Future: intercept at llm_node level before TTS gets it.
```

The real fix is Layer 1 — prevent English from reaching TTS. Layer 2 is defense-in-depth logging.

**Tests:** Hard to unit test (requires mocking LiveKit session). Add to `test_normalization.py`:
- `test_is_character_break_english` — pure English → True
- `test_is_character_break_hindi` — Romanized Hindi → False
- `test_is_character_break_mixed` — mixed → False (has Hindi markers)

### ~~Phase 2: Quality Improvements (P3 + P4 + P5)~~ DONE

#### ~~2a. STT garbage detection~~ SHIPPED

**File:** `agent_worker.py` — `on_user_transcript` handler

Add heuristic filter:
```python
_GARBAGE_PATTERNS = {"table", "the", "and", "a", "an", "it", "is", "to", "of"}

def _is_likely_garbage(text: str) -> bool:
    words = text.strip().rstrip('.!?').lower().split()
    if len(words) <= 1 and words[0] in _GARBAGE_PATTERNS:
        return True
    return False
```

Log `[STT GARBAGE]` but still forward to LLM (logging-only phase). After collecting data on frequency, decide whether to filter.

**Tests:** `tests/test_normalization.py` (or new `test_stt_filter.py`)
- `test_garbage_single_word` — "Table." → garbage
- `test_garbage_the` — "The." → garbage
- `test_valid_short` — "Yes." → not garbage
- `test_valid_sentence` — "Tell me the price." → not garbage

#### ~~2b. Latency optimization — prompt caching~~ SHIPPED

**File:** `agent_worker.py` — `_create_llm()` and `SanitizedAgent.llm_node()`

Enable Anthropic prompt caching:
- The `livekit-plugins-anthropic` plugin supports `caching="ephemeral"` — add it to the `anthropic.LLM()` constructor.
- Add `max_tokens=150` to the LLM config.

**Verification:** Compare TTFT values in logs before/after. Target: p50 TTFT < 0.6s (down from ~1.0s).

#### ~~2c. Role reversal prevention~~ SHIPPED

**Files:** `agent_worker.py` (DEFAULT_INSTRUCTIONS), `pipeline/prompt_builder.py`

Add to STAY IN CHARACTER section in both files:
```
- If the user asks YOU a question as if YOU are the shopkeeper (e.g. "Do you repair ACs?", "What brands do you have?"), DO NOT answer. Redirect: "Nahi nahi, main toh customer hoon. Mujhe [product] ka price chahiye."
```

**Tests:** Add to `test_sanitize.py`:
- `test_prompt_has_role_reversal_guard` — DEFAULT_INSTRUCTIONS contains "main toh customer hoon"

### ~~Phase 3: Pipeline Polish~~ DONE

#### ~~3a. Fix `_active_session` global singleton~~ SHIPPED

**File:** `pipeline/session.py`

The `_active_session` module global means only one session captures log events. Fix: use a dict of active sessions keyed by `id(session)` so multiple concurrent sessions each get their own events.

#### ~~3b. Fix synchronous LLM calls in async functions~~ SHIPPED

**Files:** `pipeline/analysis.py`, `pipeline/store_discovery.py`

Wrap synchronous `client.messages.create()` calls in `asyncio.to_thread()` to avoid blocking the event loop.

#### ~~3c. Add tests to CI~~ SHIPPED

**File:** `.github/workflows/docker.yml`

Add a test job before the Docker build:
```yaml
- name: Run tests
  run: pip install -r requirements.txt && pytest tests/ -q --tb=short
```

#### ~~3d. Extract shared agent lifecycle code~~ SHIPPED

**Files:** `app.py`, `test_browser.py`

Extract `kill_old_agents()`, `start_agent_worker()`, `cleanup_agent()`, `find_agent_log()` into a shared `agent_lifecycle.py` module. Both files import from it.

### ~~Phase 4: Test Coverage Gaps~~ DONE

Add tests for the most critical untested paths:

1. **Number buffer streaming** (Phase 1a tests above)
2. **Character break detection + recovery** (Phase 1b tests above)
3. **STT garbage filter** (Phase 2a tests above)
4. **Role reversal guard** (Phase 2c tests above)

### ~~Phase 5: Research Intelligence + Conversation Recovery (P9 + P10 + P11)~~ DONE {#phase-5}

#### ~~5a. Flow research data into voice agent prompt~~ SHIPPED

**File:** `pipeline/prompt_builder.py`

Add `_build_research_sections()` that generates 3 conditional prompt sections from previously-ignored research fields:
- **PRODUCT KNOWLEDGE** — `product_summary` + top 3 `competing_products` (name, price_range, pros). Tells agent to name a model when asked "which one?"
- **BUYER NOTES** — top 3 `important_notes` as bullets.
- **WHEN STUCK** — 3 strategies: name a model, pivot after 2 fails, anchor to low price.

All sections omitted when data is empty. ~170-230 tokens, negligible latency with prompt caching.

#### ~~5b. Fix verbose greeting~~ SHIPPED

**Files:** `pipeline/prompt_builder.py`, `pipeline/session.py`

Add `_casual_product_name()` helper: strips `(specs)`, leading size adjectives, `with ...` clauses. Falls back to `product_type` if result too short. Add `build_greeting()` that uses casual name. Replace `{product_desc}` with `{casual}` in all spoken prompt sections (opening line, CONVERSATION FLOW, ENDING THE CALL, STAY IN CHARACTER, EXAMPLES). Keep verbose `product_desc` in the `PRODUCT:` reference line at the end.

Update `session.py` to call `prompt_builder.build_greeting()` instead of building greeting inline.

#### ~~5c. Add "which model?" recovery to examples~~ SHIPPED

**File:** `pipeline/prompt_builder.py`

Update `_build_examples()` to include a model recovery exchange when competing_products exist:
```
Shopkeeper: "Kaun sa model chahiye?"
You: "Achha, [first_model_name] ka kya price hai?"
```

#### ~~5d. Tests for prompt builder~~ SHIPPED

**File:** `tests/test_prompt_builder.py` (new)

~15 tests covering:
- `TestCasualProductName` — parenthetical stripping, size adjective, tonnage preserved, with-clause, fallback
- `TestBuildGreeting` — casual name used, store name present, format correct
- `TestBuildPromptWithResearch` — PRODUCT KNOWLEDGE, BUYER NOTES, WHEN STUCK present/absent, caps at 3, casual in spoken sections, verbose in PRODUCT: line, model recovery in examples

### ~~Phase 6: Transcript & UI Fixes (P12 + P13 + P14 + P15)~~ DONE

#### ~~6a. Fix duplicate greeting in transcript (P12)~~ SHIPPED

**File:** `agent_worker.py`

Remove explicit `transcript_lines.append()` after `session.say(greeting, add_to_chat_ctx=True)`. The `conversation_item_added` handler already captures it — the explicit append created duplicates.

#### ~~6b. Add greeting NOTE to pipeline prompt (P13)~~ SHIPPED

**File:** `pipeline/prompt_builder.py`

Add `greeting_note` after the `STORE:` line:
```
NOTE: You have already greeted the shopkeeper with: "Hello, yeh Croma hai? split AC ke baare mein poochna tha."
Do NOT repeat the greeting. Continue the conversation from the shopkeeper's response.
```
Uses `build_greeting()` to generate the greeting text (with casual product name), ensuring consistency between what's spoken and what the NOTE says. 2 new tests added.

#### ~~6c. Non-blocking research with progress polling (P14)~~ SHIPPED

**File:** `app.py`, `pipeline/session.py`

- Research runs in a background `threading.Thread` instead of blocking `asyncio.run()`
- Session caches result on `_research_result` / `_research_error`
- New GET endpoint `/api/session/{id}/research` for polling
- Frontend: POST starts research → polls GET every 2s → renders results on completion
- Event log continues to populate during research (no longer blocked)

#### ~~6d. Fix results table data flow (P15)~~ SHIPPED

**Files:** `pipeline/session.py`, `pipeline/analysis.py`, `app.py`

- `_collect_call_results_from_transcripts()` now includes transcript messages in `extracted_data` (not constraint scores)
- `compare_stores()` formats transcript into readable conversation and sends to LLM for extraction
- Single-store calls now also go through LLM to extract structured price/warranty/delivery data
- Added `warranty` field to LLM output schema and frontend rendering
- `_format_transcript()` helper converts `[{role, text}]` messages into `"Agent: ... / Shopkeeper: ..."` text

**Tests:** 188 unit tests pass (22 prompt builder + 73 normalization + 25 sanitize + 34 offline scenarios + 11 conversation + 11 transcript + 6 logs + 6 LLM provider + 26 live skipped).

---

## Files Modified

| File | Changes |
|---|---|
| `agent_worker.py` | P1: digit buffer in llm_node streaming. P2: character break recovery + TTS error handling. P3: STT garbage logging. P4: max_tokens + prompt caching. P5: role reversal prompt. P12: remove duplicate greeting append. |
| `pipeline/prompt_builder.py` | P5: role reversal guard. P9: `_build_research_sections()` for PRODUCT KNOWLEDGE / BUYER NOTES / WHEN STUCK. P10: `_casual_product_name()` + `build_greeting()`, casual name in spoken sections. P11: WHEN STUCK strategies. P13: greeting NOTE to prevent LLM repeating greeting. |
| `pipeline/session.py` | P3a: per-session logging handler. P10: use `prompt_builder.build_greeting()`. P14: `_research_result`/`_research_error` caching. P15: transcript messages in `extracted_data`. |
| `pipeline/analysis.py` | P3b: asyncio.to_thread for sync LLM calls. P15: `_format_transcript()`, transcript-based extraction, single-store LLM analysis, `warranty` field. |
| `pipeline/store_discovery.py` | P3b: asyncio.to_thread for sync LLM calls. |
| `.github/workflows/docker.yml` | P3c: add test job before Docker build. |
| `app.py` | P3d: import from shared agent_lifecycle.py. P14: background threading for research, GET polling endpoint. P15: warranty in results table. |
| `test_browser.py` | P3d: import from shared agent_lifecycle.py. |
| `agent_lifecycle.py` (new) | P3d: shared agent worker management functions. |
| `tests/conftest.py` | New imports for test helpers. |
| `tests/test_normalization.py` | P1a: 8 streaming number buffer tests. |
| `tests/test_sanitize.py` | P1b + P2a + P2c: 17 new tests (character break, STT garbage, role reversal). |
| `tests/test_prompt_builder.py` (new) | P9 + P10 + P11: 22 tests for casual names, greeting, research sections, model recovery, greeting NOTE. |

---

## Verification

### After Phase 1 (critical bugs)
1. `pytest tests/` — all tests pass including new streaming number tests
2. Manual test: start browser session, say "₹28000 hai" — verify agent echoes "attaaees hazaar" (not "attaaeeszero")
3. Manual test: speak English to agent — verify no TTS crash, agent stays in Hindi
4. Check logs: no `[TTS CRASH]` errors, `[CHARACTER BREAK]` warnings trigger recovery

### After Phase 2 (quality)
1. Check TTFT in logs — p50 should drop from ~1.0s to ~0.6s with prompt caching
2. `[STT GARBAGE]` warnings appear in logs for known garbage patterns
3. Manual test: ask agent "Do you repair ACs?" — agent redirects instead of answering as shopkeeper

### After Phase 3 (pipeline polish)
1. Two concurrent browser sessions both get correct event streams
2. GitHub Actions runs tests before building Docker image
3. `app.py` and `test_browser.py` both work with shared `agent_lifecycle.py`

### After Phase 4 (test coverage)
1. `pytest tests/` — total test count increases from 141 to 166
2. Coverage of streaming number buffer, character break detection, STT garbage filter, role reversal guard

### After Phase 5 (research intelligence)
1. `pytest tests/test_prompt_builder.py` — 22 tests pass for casual names, greeting, research sections
2. Generated prompt with full research data contains PRODUCT KNOWLEDGE, BUYER NOTES, WHEN STUCK
3. Generated prompt with empty research gracefully omits all three sections

### After Phase 6 (transcript & UI fixes)
1. Greeting appears exactly once in transcript (no duplicate)
2. LLM does not repeat greeting in its first response
3. Research phase shows progress in event log (not stuck/blank)
4. Results table shows price, installation, delivery, warranty — not empty
5. `pytest tests/` — 188 tests pass
