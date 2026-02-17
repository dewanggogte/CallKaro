# Voice AI Agent Best Practices

Reference document for building production voice AI agents. Based on this project's LiveKit + Sarvam AI + Claude stack and industry research.

---

## 1. Architecture & Pipeline Design

### Cascading Pipeline (STT → LLM → TTS) vs Speech-to-Speech

**Cascading pipeline** is the right choice for Hindi voice agents:

- **Debuggability**: Intermediate text at every stage. When something goes wrong, inspect STT transcript, LLM reasoning, and TTS input independently. Speech-to-speech models are black boxes.
- **Component swappability**: Independently upgrade STT, LLM, or TTS without touching the rest.
- **Language control**: Deterministic text normalization (digit→Hindi words, Devanagari transliteration) is only possible with an intermediate text stage.
- **Hindi support**: Speech-to-speech models (GPT-4o Realtime, Gemini Live) have poor Hindi support and no text normalization hooks.

### Guidelines

| Guideline | Rationale |
|---|---|
| Stream end-to-end: STT streams partial transcripts, LLM streams tokens, TTS streams audio chunks | Reduces perceived latency from 2-4s (sequential) to under 1s (overlapped) |
| Use `llm_node` / `before_tts_cb` hooks for text normalization between LLM and TTS | Where you handle digits-to-words, Devanagari cleanup, think-tag stripping |
| Co-locate all pipeline components in the same region | Each cross-region hop adds 50ms+. Three components = 150ms+ wasted |
| Use sentence-level TTS pipelining | TTS begins synthesizing first sentence while LLM generates second |

### Typical Latency Budget (Twilio's analysis)

| Component | Latency |
|---|---|
| Network ingress/buffering/decoding | ~95ms |
| STT | 350ms |
| LLM | 375ms |
| TTS | 100ms |
| Service hops | ~30ms (10ms each) |
| **Total mouth-to-ear** | **~1.1s** |

---

## 2. Prompting & Character Consistency

Voice agents require fundamentally different prompting than text chatbots. Voice-optimized prompts should produce responses **60-70% shorter** than text equivalents — the average human attention span for spoken information is 8-10 seconds.

### Guidelines

1. **Persona anchoring**: Define the persona in the system prompt AND reinforce it. The LLM's first assistant message (greeting) should be in-context so it anchors the character.

2. **Response length**: Explicitly constrain — "Keep every reply under 25 words. One question per turn."

3. **Voice-safe output format**:
   - No bullet points, numbered lists, or markdown
   - No parenthetical asides "(see above)"
   - No abbreviations that sound wrong: "e.g." → "for example"
   - No URLs, email addresses, or alphanumeric codes

4. **Explicit guardrails reduce hallucination from 27% to under 5%**:
   - What the agent MUST NOT say
   - Escalation triggers with exact response phrases
   - Fallback phrases for confusion

5. **STT translation mismatch**: When STT translates speech to a different language, explain this in the prompt. The LLM needs to understand why a Hindi shopkeeper's words arrive in English.

6. **No "thinking out loud"**: Voice agents should not reason aloud. Strip `<think>` tags and instruct: "Never explain your reasoning. Never say 'Let me think.' Just respond directly."

7. **Filler words for latency hiding**: Strategic fillers like "Hmm," "Achha," or "Ji" maintain conversational flow during LLM generation. Consider prefix-generating these before the full response streams.

---

## 3. Latency Optimization

### Target: Sub-800ms end-to-end, ideally sub-500ms

Human conversation has a 200-400ms response window. Contact centers report **40% higher hang-up rates** when voice agents take longer than 1 second to respond.

### Component Budget

| Component | Target | Notes |
|---|---|---|
| VAD + Endpointing | 300-500ms | Silero VAD + semantic turn detection |
| STT | 100-300ms | Streaming with partial results |
| LLM (TTFT) | 200-400ms | Small model + prompt caching + short prompts |
| TTS (TTFB) | 75-150ms | Streaming TTS |
| Network hops | <50ms | Co-located services |
| **Total** | **~700-1400ms** | |

### Optimization Strategies (ranked by impact)

1. **Prompt caching**: Anthropic's prompt caching gives up to 80% latency reduction on the cached prefix. System prompt is static per call — only conversation history is "new" tokens. Highest-impact single optimization.

2. **Cap `max_tokens` to 100-150**: Faster generation, shorter responses better for voice.

3. **Prompt length under 2000 tokens**: LLM inference is 60-70% of total latency. Every prompt token increases TTFT.

4. **Verify true streaming at every stage**: STT partial transcripts, LLM token-by-token streaming, TTS audio chunking. The pipeline framework handles this but verify each component is actually streaming.

5. **Sentence-level TTS pipelining**: TTS begins synthesizing first sentence while LLM generates second. Preserve sentence boundaries in text normalization (don't `.strip()` leading spaces).

6. **Region deployment**: Deploy close to target users. Each cross-region hop adds 50-150ms. Cerebrium reports co-located services reduce inter-service latency from 50ms+ to ~2ms.

7. **Precompute common responses**: For greetings and fixed phrases, consider pre-synthesized audio instead of running through the full pipeline.

---

## 4. Turn-Taking & Interruptions

Poor turn-taking makes agents feel robotic. Good turn-taking reduces conversation duration by **28%** while improving satisfaction by **35%**.

### Guidelines

1. **VAD Silence Threshold**: 300-500ms standard range.
   - For shopkeeper calls (may be busy/distracted): use **500ms** — prevents premature endpointing when shopkeeper pauses to check a price.
   - For fast-paced conversations: **300ms**.

2. **Semantic turn detection**: Use transformer-based turn detection (LiveKit `MultilingualModel`) on top of VAD.
   - Distinguishes pauses (checking price) from completed turns
   - Reduces false endpointing on fillers ("umm," "woh," "matlab")
   - Significant quality improvement for Hindi with frequent mid-sentence pauses

3. **Interruption (barge-in) handling**:
   - Agent should stop speaking within **200ms** when interrupted
   - Filter false barge-ins: single-letter artifacts, background noise, filler words
   - Require minimum 2 words and 800ms+ for real interruption
   - Shopkeeper calls = background shop noise → raise VAD activation threshold

4. **After-interruption recovery**:
   - Do NOT repeat interrupted content from the beginning
   - If interruption was acknowledgment ("haan"): continue from where cut off
   - If new topic: abandon previous response and address new input
   - Mark interrupted responses with `[interrupted]` tag in context for LLM awareness

5. **False interruption handling**:
   - Wait 1-2s before declaring false interruption
   - Resume speaking after false interruption (backchannel "hmm" shouldn't kill the response)

---

## 5. Error Handling & Recovery

### Design for Failure at Every Stage

| Component | Error Type | Recovery Pattern |
|---|---|---|
| STT | Timeout/no transcript | Retry once, then: "Kya aap dobara bol sakte hain?" |
| STT | Low confidence (<0.3) | Discard transcript, wait for next utterance |
| LLM | Timeout (>5s) | Filler response: "Ek second, main check kar raha hoon" |
| LLM | Rate limit | Exponential backoff: 100ms → 200ms → 400ms. Max 3 retries |
| LLM | Malformed output | Strip and retry. Fallback to canned response |
| TTS | Synthesis failure | Retry with simplified text. Fallback to pre-cached audio |
| TTS | Timeout | Skip response, log it, continue conversation |
| Network | WebSocket disconnect | Auto-reconnect with exponential backoff |

### Guidelines

1. **Distinguish recoverable vs non-recoverable errors**: Recoverable (transient timeout, rate limit) → retry. Non-recoverable (auth failure, invalid input) → graceful shutdown.

2. **Pre-defined recovery phrases** in Hindi:
   - STT failure: "Maaf kijiye, aapki awaaz thodi clear nahi aayi. Dobara boliye?"
   - General error: "Ek second please."
   - Give-up: "Maaf kijiye, abhi kuch problem aa rahi hai. Baad mein call karta hoon."

3. **Circuit breakers**: If a component fails 3+ times in 30 seconds, stop calling it and use fallback. Either end the call gracefully or switch to backup provider.

4. **Idempotent transcript saving**: Multiple save points (close, disconnect, timeout) with a guard flag to prevent duplicates.

---

## 6. Testing & QA

### Multi-Layer Evaluation

1. **Component-level metrics**:

   | Metric | Target |
   |---|---|
   | STT Word Error Rate (WER) | <5% for Hindi |
   | TTS Mean Opinion Score (MOS) | >4.0 / 5.0 |
   | LLM response relevance | >90% on-topic |
   | End-to-end latency (p50) | <800ms |
   | End-to-end latency (p90) | <1200ms |

2. **Scenario-based testing**: Cover:
   - Happy path (clear prices)
   - Edge cases ("busy", "kaun bol raha hai?", price ranges, "call back later")
   - Adversarial (background noise, English-speaking shopkeeper, hang-up mid-call)
   - Accent/dialect variation

3. **Constraint-based evaluation** (hard pass/fail checks per response):
   - `no_devanagari`: Output stays in Romanized Hindi
   - `single_question`: Only one question per turn
   - `response_length`: Under word limit
   - `no_hallucinated_prices`: Agent never invents prices
   - `persona_consistency`: Agent stays in character
   - `character_break_detection`: Flag responses in wrong language

4. **Weighted scoring rubric**:
   - Constraint compliance: 40%
   - Topic coverage: 25%
   - Price echo accuracy: 15%
   - Brevity: 10%
   - Non-repetition: 10%

5. **Automated regression**: Run scenario tests in CI. Simulate shopkeeper with a second LLM, score with constraint checker, fail build if score drops below threshold (70%).

6. **Production monitoring per call**:
   - Conversation completion rate
   - Average call duration
   - Interruption count
   - STT confidence distribution
   - Error rate by component
   - Character break warnings

---

## 7. Deployment & Operations

### Monitoring Framework

1. **Four-layer monitoring**:
   - Layer 1 (Audio): Packet loss, jitter, audio quality, connection success rate
   - Layer 2 (STT): Transcription latency, WER, confidence scores
   - Layer 3 (LLM): TTFT, token count, response quality
   - Layer 4 (TTS): Synthesis latency, pronunciation accuracy

2. **Key SLAs**:
   - p50/p90/p99 latency per component AND end-to-end
   - Error rate per component: <1% each
   - Call completion rate: >95%
   - Uptime: 99.9%

3. **Per-call logging**: Log every STT transcript (with confidence + timestamp), every LLM response (with token count + latency), every TTS synthesis (with input text + latency). Save full conversation transcripts.

4. **Auto-scaling**: Each component has different concurrency capacity. STT ~180 concurrent connections on A10 GPU. Self-hosted LLM ~10 on H100. Design with backpressure: queue incoming calls, cap concurrency.

5. **Graceful shutdown**: Drain remaining audio before shutting down. `session.shutdown(drain=True)`.

6. **Health checks**: Verify STT/LLM/TTS providers are reachable and responding within latency budget.

---

## 8. Hindi/Indic Language Specifics

### Code-Switching (Hindi-English / Hinglish)

- 56% of Indians prefer regional language support, but urban shopkeepers code-switch freely
- Shopkeeper might say "AC ka price thirty-five thousand hai" — mixing Hindi grammar with English numbers
- Do NOT force monolingual Hindi input. Accept code-switched audio and let STT handle it
- Sarvam STT handles code-mixed audio with automatic language detection

### STT Translation Mode

- Sarvam `saaras:v3` translates Hindi speech → English text (uses `speech-to-text-translate` endpoint)
- The LLM sees English text but must respond in Romanized Hindi
- **Must explain this in the prompt**: "The shopkeeper's Hindi speech is automatically translated to English. Even though you see English text, always respond in Romanized Hindi."

### Romanized Hindi Spelling Inconsistency

- NO standard spellings: "main" = "mein" = "mien" = "men" (all valid for मैं)
- Do NOT try to standardize spellings programmatically
- Let TTS with `enable_preprocessing=True` handle pronunciation from any reasonable variant

### Number Handling

- Hindi has irregular number words (39 = "untaalees", not "tees-nau")
- Best approach: LLM outputs DIGITS, deterministic conversion to Hindi words
- Handle `saadhe` pattern: 37500 → "saadhe saintees hazaar", 1500 → "dedh hazaar", 2500 → "dhaai hazaar"
- Price ranges ("30000-35000"): ensure converter handles ranges without breaking

### Devanagari Leakage

- Even with explicit instructions, LLMs occasionally output Devanagari
- Safety net: static lookup transliteration covering vowels, consonants, matras, and digits
- Handle consonant+matra combinations (matra replaces inherent 'a' vowel)

### TTS Pronunciation

- Sarvam `bulbul:v3` with `enable_preprocessing=True` handles most pronunciation issues
- Known challenges: English brand names (Voltas, LG, Daikin), technical terms (inverter, split AC)
- For consistently mispronounced terms: add explicit pronunciation mappings in text normalization

### Audio Quality

- Phone calls: 8kHz (poor). Browser: 16kHz (good). Configure STT sample rate accordingly
- Background shop noise degrades STT accuracy significantly
- Set explicit `hi-IN` language code rather than relying on auto-detection for phone calls

### Cultural Conversational Patterns

- Hindi phone conversations have specific patterns: greeting ("Namaste" / "Haan bolo"), politeness markers ("ji", "aap"), closing ("Achha theek hai, dhanyavaad")
- Match the shopkeeper's energy level — over-politeness sounds robotic
- Use fillers naturally: "haan", "achha", "hmm" — they signal cultural fluency

---

## Sources

- [The Voice AI Stack for Building Agents - AssemblyAI](https://www.assemblyai.com/blog/the-voice-ai-stack-for-building-agents)
- [Real-Time vs Turn-Based Voice Agent Architecture - Softcery](https://softcery.com/lab/ai-voice-agents-real-time-vs-turn-based-tts-stt-architecture)
- [Core Latency in AI Voice Agents - Twilio](https://www.twilio.com/en-us/blog/developers/best-practices/guide-core-latency-ai-voice-agents)
- [Deploying Global Scale Voice Agent with 500ms Latency - Cerebrium](https://www.cerebrium.ai/blog/deploying-a-global-scale-ai-voice-agent-with-500ms-latency)
- [Mastering Turn Detection and Interruption Handling - Famulor](https://www.famulor.io/blog/the-art-of-listening-mastering-turn-detection-and-interruption-handling-in-voice-ai-applications)
- [Turn Detection and Interruptions - LiveKit Docs](https://docs.livekit.io/agents/build/turns/)
- [Improving Turn Detection with Transformers - LiveKit Blog](https://blog.livekit.io/using-a-transformer-to-improve-end-of-turn-detection/)
- [Voice AI Prompting Guide - Vapi](https://docs.vapi.ai/prompting-guide)
- [Voice AI Prompt Engineering Guide - VoiceInfra](https://voiceinfra.ai/blog/voice-ai-prompt-engineering-complete-guide)
- [How to Evaluate Voice Agents - Braintrust](https://www.braintrust.dev/articles/how-to-evaluate-voice-agents)
- [Demystifying Evals for AI Agents - Anthropic](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)
- [Evaluating and Monitoring Voice AI Agents - Langfuse](https://langfuse.com/blog/2025-01-22-evaluating-voice-ai-agents)
- [Build Voice Agent with LiveKit - Sarvam Docs](https://docs.sarvam.ai/api-reference-docs/integration/build-voice-agent-with-live-kit)
- [LiveKit Agents Documentation](https://docs.livekit.io/agents/)
