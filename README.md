# CallKaro — Voice AI Price Enquiry Agent

An automated voice AI agent that calls local stores to collect price quotes in natural Hindi/Hinglish. Supports **browser-based WebRTC** and **SIP phone calls**. Built with **LiveKit** + **Sarvam AI** (Hindi STT/TTS) + **Claude Haiku 4.5** (or self-hosted Qwen3).

## How It Works

```
Browser/Phone ──▶ LiveKit Room ──▶ Agent Worker
                                     │
                              ┌──────┼──────┐
                              ▼      ▼      ▼
                           Sarvam  LLM   Sarvam
                           STT    Claude  TTS
                         (saaras  Haiku  (bulbul
                           v3)    4.5     v3)
```

1. User connects via browser (WebRTC) or phone (SIP)
2. **Sarvam STT** (saaras:v3) transcribes Hindi/Hinglish speech at 16kHz
3. **LLM** (Claude Haiku 4.5 or Qwen3 via vLLM) generates natural Romanized Hindi responses as a customer enquiring about product prices — writes numbers as digits
4. **SanitizedAgent** normalizes LLM output — strips think tags, action markers, transliterates any Devanagari leaks, converts digit numbers to Hindi words (e.g. `39000` → `untaalees hazaar`), fixes spacing
5. **Sarvam TTS** (bulbul:v3, speaker: shubh, `enable_preprocessing=True`) speaks the response back, handling Romanized Hindi pronunciation internally
6. Agent follows a natural conversation flow: confirm shop → ask about product → get price → negotiate → wrap up

## Quick Start

### 1. Install

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env.local
# Fill in your API keys
```

You need:
- **Sarvam AI** key — [dashboard.sarvam.ai](https://dashboard.sarvam.ai) (free credits on signup)
- **LiveKit Cloud** — [cloud.livekit.io](https://cloud.livekit.io) (free tier: 5,000 participant-minutes/month)
- **Anthropic API** key — for Claude Haiku 4.5 (or use self-hosted Qwen3 via vLLM)
- **SIP trunk** (optional, for phone calls) — Telnyx or Twilio

### 3. Run

```bash
python app.py
```

This starts the agent worker and serves the pipeline UI. Open http://localhost:8080 to use the 4-step wizard (Tell Details → Research → Calling → Results). Press Ctrl+C to stop everything.

## Project Structure

```
├── app.py                   # Full pipeline web UI — intake → research → calling → analysis
├── agent_worker.py          # LiveKit agent — SanitizedAgent, LLM switch, TTS normalization
├── agent_lifecycle.py       # Shared agent worker management (kill/start/cleanup)
├── dashboard.py             # Metrics dashboard — parses logs/transcripts, serves on :9090
├── pipeline/                # Pipeline modules
│   ├── intake.py            # Chat-based requirement extraction
│   ├── research.py          # Product research (LLM + web search)
│   ├── store_discovery.py   # Store discovery (Maps + web search)
│   ├── prompt_builder.py    # Dynamic voice agent prompt generator
│   ├── analysis.py          # Cross-store comparison (transcript-based)
│   ├── session.py           # Pipeline orchestrator (PipelineSession)
│   └── schemas.py           # Data contracts between phases
├── stores.json              # Target shops (name, phone, area, nearby_area)
├── tests/                   # pytest test suite (188 unit + 26 live tests)
│   ├── conftest.py          # Fixtures, ConstraintChecker, ConversationScorer
│   ├── shopkeeper_scenarios.py  # 11 scripted multi-turn scenarios from real calls
│   ├── test_normalization.py    # Hindi numbers, Devanagari transliteration, spacing (65 tests)
│   ├── test_sanitize.py         # Chat context sanitization (8 tests)
│   ├── test_llm_provider.py     # LLM provider switching (6 tests)
│   ├── test_conversation.py     # Role adherence, response length (11 tests)
│   ├── test_transcript.py       # JSON schema validation (10 tests)
│   ├── test_prompt_builder.py    # Pipeline prompt builder (22 tests)
│   ├── test_logs.py             # Per-call logging (6 tests)
│   ├── test_scenario_offline.py # Constraint checker + scorer validation (34 tests)
│   ├── test_scenario_live.py    # Live multi-turn Claude tests (20 tests, --live)
│   ├── test_stt_live.py         # Sarvam STT API (2 tests, --live)
│   ├── test_tts_live.py         # Sarvam TTS API (4 tests, --live)
│   └── run_scenario_analysis.py # Diagnostic script for prompt tuning
├── transcripts/             # Saved conversation JSON files
├── logs/                    # Per-call debug logs
├── architecture.md          # Detailed pipeline and component documentation
├── tests.md                 # Testing guide
├── .env.local               # API keys and config (not committed)
└── requirements.txt         # Python dependencies
```

## Tech Stack

| Component | Service | Model/Config |
|-----------|---------|-------------|
| STT | Sarvam AI | saaras:v3, hi-IN, 16kHz |
| TTS | Sarvam AI | bulbul:v3, speaker: shubh, enable_preprocessing=True |
| LLM (default) | Anthropic | Claude Haiku 4.5 (`claude-haiku-4-5-20251001`) |
| LLM (alt) | Self-hosted vLLM | Qwen/Qwen3-4B-Instruct-2507-FP8 |
| Audio | LiveKit | WebRTC (browser, 16kHz) / SIP (phone, 8kHz) |
| VAD | Silero | Via LiveKit agents SDK |
| Turn Detection | Multilingual Model | Transformer-based end-of-utterance prediction (supports Hindi) |

Switch LLM provider via `LLM_PROVIDER` env var (`claude` or `qwen`).

## Testing

```bash
# Run unit tests (no API keys needed)
pytest tests/ -v                          # 188 passed

# Run everything including live API tests
pytest tests/ --live -v                   # 188 + 26 live tests

# Run scenario analysis for prompt tuning
python tests/run_scenario_analysis.py     # Runs all 11 scenarios, prints constraint analysis
```

### Conversation Quality Testing

The test suite includes a constraint-based conversation quality framework:

- **ConstraintChecker** — validates individual agent responses against 8 behavioral rules (no Devanagari, single question, response length, no action markers, no newlines, no English translations, no end_call text, no invented details)
- **ConversationScorer** — scores full conversations on 5 dimensions: constraint compliance (40%), topic coverage (25%), price echo (15%), brevity (10%), no-repetition (10%)
- **11 shopkeeper scenarios** derived from real call transcripts — cooperative, defensive, evasive, question-reversing, interruption-heavy

Live tests feed scripted shopkeeper messages to the real LLM and validate constraints hold. Non-deterministic by design.

## Pipeline Features

- **4-step wizard** — Tell Details → Research → Calling → Results
- **Intake chat** — Conversational requirement gathering (product, budget, location, brand)
- **Product research** — LLM + web search for product knowledge and buyer tips
- **Store discovery** — Finds local stores via web search
- **Voice calling** — LiveKit agent calls stores in Hindi, real-time transcript view
- **Cross-store analysis** — LLM compares quotes across stores
- **Auto agent management** — starts/stops agent worker automatically
- **Metrics dashboard** via `/api/metrics` endpoint

## Configuration

The agent prompt in `agent_worker.py` uses plain-text sections (VOICE & TONE, CONVERSATION FLOW, CRITICAL OUTPUT RULES, EXAMPLES) for natural LLM behavior. The agent speaks casual Hindi/Hinglish as a regular customer calling to ask about product prices.

Key design decisions:
- LLM outputs **Romanized Hindi** (Latin script) with **digit numbers** — no Devanagari, no Hindi number words
- Numbers converted deterministically to Hindi words before TTS (e.g. `39000` → `untaalees hazaar`, `37500` → `saadhe saintees hazaar`, `1.5` → `dedh`)
- Devanagari safety net: any leaked Devanagari chars are transliterated to Romanized Hindi (e.g. `usका` → `uskaa`)
- Sarvam TTS with `enable_preprocessing=True` handles pronunciation internally
- `end_call` tool is a method on `SanitizedAgent` for automatic registration with the LLM
- `end_call` uses `RunContext.wait_for_playout()` to wait for TTS completion (no blind sleep)
- Transcript saving is idempotent with multiple save points (session close, participant disconnect, timeout)
- Errors are classified as recoverable/non-recoverable for targeted logging
- Per-store `nearby_area` in `stores.json` gives the agent a concrete location to mention when asked

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

See [architecture.md](architecture.md) for detailed pipeline documentation.
