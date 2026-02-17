# Architecture — Voice Price Enquiry Agent

## Pipeline Overview

```
Browser (mic) ──WebRTC──▶ LiveKit Cloud ──▶ Agent Worker
                                                │
                                          ┌─────┴─────┐
                                          │  1. VAD    │  silero
                                          │            │  detects speech start/stop
                                          └─────┬─────┘
                                                │ audio frames
                                          ┌─────┴─────┐
                                          │  2. STT    │  Sarvam saaras:v3
                                          │            │  Hindi speech → text
                                          └─────┬─────┘
                                                │ text transcript
                                          ┌─────┴─────┐
                                          │  3. LLM    │  Claude Haiku 4.5 (default)
                                          │            │  or Qwen3-4B via vLLM
                                          └─────┬─────┘
                                                │ streamed Romanized Hindi tokens
                                                │ (numbers as digits)
                                          ┌─────┴──────┐
                                          │ 3a. Normalize │  SanitizedAgent
                                          │    for TTS    │  strip think tags,
                                          │               │  transliterate Devanagari,
                                          │               │  digits → Hindi words,
                                          │               │  strip action markers,
                                          │               │  fix spacing
                                          └─────┬──────┘
                                                │ cleaned text
                                          ┌─────┴─────┐
                                          │  4. TTS    │  Sarvam bulbul:v3
                                          │            │  speaker: shubh
                                          │            │  enable_preprocessing=True
                                          │            │  Romanized Hindi → speech
                                          └─────┬─────┘
                                                │ audio frames
                                                ▼
                                          LiveKit Cloud ──WebRTC──▶ Browser (speaker)
```

## Components

| Stage | Provider | Model | Config |
|-------|----------|-------|--------|
| VAD | silero | silero-vad | Local inference, no API call |
| STT | Sarvam AI | saaras:v3 | hi-IN, 16kHz sample rate |
| LLM (default) | Anthropic | claude-haiku-4-5-20251001 | Via `livekit-plugins-anthropic` |
| LLM (alt) | Self-hosted vLLM | Qwen/Qwen3-4B-Instruct-2507-FP8 | `192.168.0.42:8000`, OpenAI-compatible |
| TTS | Sarvam AI | bulbul:v3 | Speaker: shubh (male), enable_preprocessing=True, 16kHz browser / 8kHz SIP |
| Transport | LiveKit Cloud | — | WebRTC (browser) / SIP via Telnyx (phone) |

### Sarvam TTS Voice Properties (bulbul:v3)

| Parameter | Default | Range | Notes |
|-----------|---------|-------|-------|
| `speaker` | shubh | 39 voices | See full list below |
| `pace` | 1.0 | 0.5–2.0 | Speech speed multiplier |
| `enable_preprocessing` | True | — | Mixed-language pronunciation handling |
| `speech_sample_rate` | 16000/8000 | 8000, 16000, 22050, 24000 | Browser/SIP respectively |

Parameters NOT exposed by LiveKit plugin (supported by Sarvam API for v3):

| Parameter | Default | Range | Notes |
|-----------|---------|-------|-------|
| `temperature` | 0.6 | 0.01–2.0 | Expressiveness — higher = more natural, lower = more consistent |
| `pitch` | 0.0 | -20 to 20 | Only works via direct API, not LiveKit plugin |
| `loudness` | 1.0 | 0.5–2.0 | Only works via direct API, not LiveKit plugin |

Available v3 speakers (39): Shubh, Aditya, Rahul, Rohan, Amit, Dev, Ratan, Varun, Manan, Sumit, Kabir, Aayan, Ashutosh, Advait, Anand, Tarun, Sunny, Mani, Gokul, Vijay, Mohit, Rehan, Soham (male); Ritu, Priya, Neha, Pooja, Simran, Kavya, Ishita, Shreya, Roopa, Amelia, Sophia, Tanya, Shruti, Suhani, Kavitha, Rupali (female).

## Per-Turn Data Flow

1. **VAD** (silero) runs locally on the agent worker. Detects end-of-speech → triggers STT with the buffered audio chunk.
2. **STT** (Sarvam `saaras:v3`) receives 16kHz PCM audio, returns Hindi text transcript. Supports 22+ Indian languages with auto-detection.
3. **LLM** (Claude Haiku 4.5 or Qwen3 via vLLM) receives the full chat context: `[system, user₁, assistant₁, user₂, ...]`. Streams back a Romanized Hindi text response with numbers as digits.
4. **SanitizedAgent post-processing** intercepts streamed LLM tokens:
   - Strips `<think>...</think>` reasoning blocks (Qwen3-specific)
   - Strips roleplay action markers (`*confused*`, `(laughs)`, etc.)
   - Replaces newlines with spaces
   - Transliterates any leaked Devanagari to Romanized Hindi (`usका` → `uskaa`)
   - Converts digit numbers to Hindi words (`39000` → `untaalees hazaar`, `37500` → `saadhe saintees hazaar`, `1.5` → `dedh`)
   - Fixes spacing at case transitions (`puraneAC` → `purane AC`)
   - Preserves leading/trailing spaces on tokens (critical for TTS sentence splitting)
5. **TTS** (Sarvam `bulbul:v3` with `enable_preprocessing=True`) receives cleaned Romanized Hindi text chunked into sentences by the LiveKit SDK's `SentenceTokenizer`. Each sentence gets a separate WebSocket session for audio generation. Sarvam's preprocessing handles Romanized Hindi → native pronunciation internally.

## Number Conversion Pipeline

The LLM writes all numbers as digits (e.g. `39000`, `1.5 ton`, `2 saal`). The `_replace_numbers()` function deterministically converts them to Hindi words before TTS:

| Input | Output | Pattern |
|-------|--------|---------|
| `39000` | `untaalees hazaar` | Standard thousands |
| `37500` | `saadhe saintees hazaar` | Half-thousands (`saadhe`) |
| `1500` | `dedh hazaar` | Special: 1.5 thousand |
| `2500` | `dhaai hazaar` | Special: 2.5 thousand |
| `1.5` | `dedh` | Special case decimal |
| `2.5` | `dhaai` | Special case decimal |
| `36,000` | `chhatees hazaar` | Comma-separated |

The Hindi number system uses unique words for every number 1-99 (unlike English "twenty-one" pattern). `_HINDI_ONES` maps all 99 values. Higher denominations use `hazaar` (1000), `lakh` (100,000), `crore` (10,000,000).

## Devanagari Transliteration Safety Net

Despite the prompt instructing "never use Devanagari", the LLM occasionally leaks Devanagari characters (e.g. `usका` instead of `uska`). The `_transliterate_devanagari()` function catches these:

- Static lookup table: vowels, consonants, matras, digits, punctuation
- Handles consonant+matra combinations correctly (`का` → `kaa`, not `kaaa`)
- Fast path: skips entirely if no Devanagari detected (checks Unicode range `U+0900-U+097F`)
- Zero latency cost for normal (all-Latin) text

## Greeting Flow

The agent speaks first to simulate initiating a phone call:

```
session.say(greeting_text, add_to_chat_ctx=True)
    │
    ├── Skips VAD, STT, LLM entirely
    ├── Sends text straight to TTS → audio → browser
    ├── add_to_chat_ctx=True adds the greeting to chat context
    ├── conversation_item_added handler records it in transcript (single source of truth)
    └── LLM knows about the greeting via a NOTE in the system instructions:
        "NOTE: You have already greeted the shopkeeper with: '...'"
        "Do NOT repeat the greeting."
```

The greeting uses `_casual_product_name()` to strip verbose specs — "Medium double door fridge with separate freezer section (220-280L)" becomes "double door fridge". The greeting confirms the shop identity ("Hello, yeh [store] hai? [product] ke baare mein poochna tha.") before asking about products — matching natural Indian phone call conventions.

## SanitizedAgent

Custom `Agent` subclass (`agent_worker.py`) that wraps every LLM call with these protections:

### 1. Chat Context Sanitization

vLLM rejects requests where the first non-system message isn't from the user (HTTP 400). The agent inspects the chat context before each call and removes any stray assistant message that appears before the first user message.

```
Before: [system, assistant, user, ...]  → 400 error from vLLM
After:  [system, user, ...]             → works
```

### 2. Think-Tag Stripping

Qwen3 emits `<think>reasoning here</think>` blocks in its output. These are stripped via regex before text reaches TTS, so the agent doesn't speak its internal reasoning aloud. Handles both complete and streaming partial cases.

### 3. TTS Text Normalization

The LLM outputs Romanized Hindi (Latin script) with numbers as digits. Normalization handles:

- **Devanagari transliteration** — Leaked Devanagari chars converted to Romanized Hindi via static lookup table with correct consonant+matra handling.
- **Number → Hindi word conversion** — Full Hindi number system (1-99 unique words + hazaar/lakh/crore). Special cases: 1.5 → "dedh", 2.5 → "dhaai", X500 → "saadhe X hazaar" (e.g. 37500 → "saadhe saintees hazaar").
- **Action marker stripping** — Removes `*confused*`, `(laughs)`, `[pauses]` etc.
- **Newline removal** — Replaces `\n` with spaces.
- **Spacing fixes** — Inserts spaces at lowercase→uppercase transitions (`puraneAC` → `purane AC`), digit↔letter transitions (`5star` → `5 star`). Collapses multiple spaces.
- **No `.strip()` on tokens** — Leading/trailing spaces from LLM tokens are preserved. This is critical: the LLM tokenizer places word-boundary spaces (e.g. `" Samsung"`) that must not be removed, otherwise TTS receives concatenated text and can't split sentences properly.

### 4. end_call Tool

The `end_call` function tool is defined as a method on `SanitizedAgent` using the `@function_tool()` decorator. This ensures it is automatically registered with the LLM (module-level `@function_tool()` does NOT auto-register). When called:

1. Waits for TTS to finish speaking via `context.wait_for_playout()`
2. Saves transcript (backup save point)
3. Shuts down the session
4. Deletes the room

### 5. LLM Request Logging

Every call logs:
- Message roles sent to the LLM (`[system, user, assistant, ...]`)
- Message count
- Truncated content at DEBUG level

Combined with event handlers for `user_input_transcribed`, `conversation_item_added`, `function_tools_executed`, `metrics_collected`, `agent_state_changed`, `error`, and `close` — gives full visibility into what's happening at each stage.

## System Prompt

The agent prompt uses plain-text sections for natural LLM behavior:

- **VOICE & TONE** — Casual spoken Hindi/Hinglish, natural fillers, short 1-2 line responses.
- **WHAT YOU CARE ABOUT** — Price, installation, warranty, exchange, availability.
- **WHAT YOU DON'T CARE ABOUT** — Technical specs, Wi-Fi, smart features.
- **CONVERSATION FLOW** — Confirm shop → ask about product → get price → negotiate → wrap up → call end_call tool.
- **STAY IN CHARACTER** — Customer role enforced even when shopkeeper speaks English. Uses nearby_area for location, generic brand for exchange.
- **CRITICAL OUTPUT RULES** — Romanized Hindi only (no Devanagari), numbers as digits (system converts), no action markers, no `[end_call]` as text.
- **EXAMPLES** — Few-shot examples showing correct tone, digit-based numbers, and format.
- **Per-call context** — PRODUCT, STORE, and YOUR AREA appended at dispatch time.

## LLM Provider Selection

Controlled by `LLM_PROVIDER` env var:

| Value | Provider | Model | Notes |
|-------|----------|-------|-------|
| `claude` | Anthropic API | claude-haiku-4-5-20251001 | Better role adherence, structured prompt support |
| `qwen` (default) | Self-hosted vLLM | Qwen/Qwen3-4B-Instruct-2507-FP8 | Free, requires GPU server at 192.168.0.42 |

## Full Pipeline (app.py)

The pipeline web UI (`app.py`) orchestrates the end-to-end flow:

```
Browser ──▶ app.py (HTTPServer on :8080)
              │
     ┌────────┼──────────┬──────────────┬──────────────┐
     ▼        ▼          ▼              ▼              ▼
  1. Intake  2. Research  3. Store      4. Calling     5. Analysis
  (chat)     (LLM+web)   Discovery     (LiveKit)      (LLM compare)
              │           (Maps+web)    │              │
              └─── parallel ───┘        │              │
                     │                  │              │
              PipelineSession    agent dispatch   compare_stores()
              caches result     via LiveKit API   reads transcripts
              for polling                         extracts prices
```

Key design decisions:
- **Non-blocking research**: Runs in a background thread. Frontend polls GET `/api/session/{id}/research` every 2s.
- **Transcript-based analysis**: `_collect_call_results_from_transcripts()` includes actual conversation messages (not just quality scores). The comparison LLM reads the conversation to extract price, warranty, installation, and delivery data.
- **Single-store analysis**: Even with one store, the LLM is called to extract structured data from the conversation.
- **Agent dispatch**: `prompt_builder.build_prompt()` generates a store-specific system prompt with research intelligence (product knowledge, buyer notes, recovery strategies).

## Session Modes

### Browser (WebRTC)

```
python app.py
    │
    ├── Kills old agent workers automatically
    ├── Starts a fresh agent_worker.py dev process
    ├── Serves HTTP on port 8080
    │     ├── GET /                          → 4-step pipeline wizard UI
    │     ├── POST /api/session              → Create new session
    │     ├── POST /api/session/{id}/chat    → Intake chat message
    │     ├── POST /api/session/{id}/research → Trigger research + discovery
    │     ├── POST /api/session/{id}/call/{n} → Start call to store
    │     ├── POST /api/session/{id}/analyze  → Cross-store comparison
    │     ├── GET /api/session/{id}/status    → Pipeline state
    │     ├── GET /api/token                  → Quick-call agent token
    │     └── GET /api/metrics                → Dashboard metrics JSON
    └── Cleans up agent worker on exit (Ctrl+C)
```

Single command to start everything. Audio flows over WebRTC at 16kHz.

### SIP (Phone)

- Agent worker receives dispatch with phone number in metadata
- Creates SIP participant via Telnyx trunk → dials the shop
- Audio flows at 8kHz (telephony standard)
- 2-minute timeout auto-disconnects
- `end_call` function tool lets the LLM hang up when done

## Per-Call Logging & Transcripts

Each call creates:
- **Log file** in `logs/` — DEBUG-level logs from all components (agent, LLM, STT, TTS, LiveKit SDK)
- **Transcript JSON** in `transcripts/` — Structured record with store name, product description, room, phone, timestamped messages

Transcript saving is idempotent (multiple save points, flag prevents duplicate writes):
- `@session.on("close")` — primary save point
- `@ctx.room.on("participant_disconnected")` — backup
- `call_timeout()` — backup for SIP calls
- Non-recoverable errors — emergency save

Log file naming: `{Store_Name}_{YYYYMMDD_HHMMSS}.log`
Transcript naming: `{Store_Name}_{YYYYMMDD_HHMMSS}.json`

## Store Data

`stores.json` contains target shops with metadata:

```json
{
  "name": "Pai International - Jayanagar",
  "phone": "+918042464343",
  "area": "Jayanagar 2nd Block",
  "city": "Bangalore",
  "nearby_area": "JP Nagar 5th Phase"
}
```

The `nearby_area` field provides a realistic residential neighborhood near the shop. When the shopkeeper asks "Where do you live?", the agent uses this instead of evasive "paas mein hi rehta hoon".

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `SARVAM_API_KEY` | Sarvam AI API key (STT + TTS) |
| `LLM_PROVIDER` | LLM backend: `claude` or `qwen` (default: `qwen`) |
| `CLAUDE_MODEL` | Claude model ID (default: `claude-haiku-4-5-20251001`) |
| `ANTHROPIC_API_KEY` | Anthropic API key (when LLM_PROVIDER=claude) |
| `LLM_BASE_URL` | vLLM server endpoint (when LLM_PROVIDER=qwen) |
| `LLM_MODEL` | Model name on vLLM (when LLM_PROVIDER=qwen) |
| `LLM_API_KEY` | vLLM auth key (when LLM_PROVIDER=qwen) |
| `LIVEKIT_URL` | LiveKit Cloud WebSocket URL |
| `LIVEKIT_API_KEY` | LiveKit API key |
| `LIVEKIT_API_SECRET` | LiveKit API secret |
| `SIP_OUTBOUND_TRUNK_ID` | LiveKit SIP trunk ID (for phone calls) |

## Files

| File | Role |
|------|------|
| `app.py` | Full pipeline web UI — 4-step wizard (intake → research → calling → analysis), non-blocking research with polling, results table |
| `agent_worker.py` | Core — SanitizedAgent, LLM provider switch, TTS normalization, Devanagari transliteration, end_call tool, logging |
| `agent_lifecycle.py` | Shared agent worker management — kill, start, cleanup, find log |
| `dashboard.py` | Metrics dashboard — parses logs/transcripts, serves HTML on port 9090 |
| `stores.json` | Target shops — name, phone, area, city, nearby_area |
| `pipeline/` | Pipeline modules — intake, research, store discovery, prompt builder, analysis, session orchestrator, schemas |
| `call_analysis.py` | Post-call quality analysis — ConstraintChecker, ConversationScorer |
| `tests/` | pytest test suite — 188 unit tests + 26 live API tests |
| `.env.local` | API keys and config (gitignored) |
