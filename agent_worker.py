"""
agent_worker.py — LiveKit Agent Worker
=======================================
Voice AI agent that calls local AC shops to enquire about prices.
Uses Sarvam AI for Hindi STT/TTS and Claude Haiku 3.5 or Qwen3 for LLM.
Includes SanitizedAgent for chat context sanitization, think-tag stripping,
and English→Hindi phonetic normalization for TTS.

Run with:
  python agent_worker.py dev       # local development
  python agent_worker.py start     # production

Or use test_browser.py which auto-manages the agent worker.
"""

import asyncio
import json
import os
import re
import logging
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(".env.local")

from livekit import api, agents
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    function_tool,
    get_job_context,
    RunContext,
    llm,
)
from livekit.agents.voice.room_io import RoomOptions
from livekit.plugins import anthropic, openai, silero, sarvam

logger = logging.getLogger("ac-price-caller.agent")


# ---------------------------------------------------------------------------
# Per-call file logger — saves all logs for each call session to logs/ dir
# ---------------------------------------------------------------------------
def _setup_call_logger(store_name: str) -> tuple[logging.FileHandler, str]:
    """Create a per-call log file and attach a file handler to the root logger.

    Returns (handler, log_filepath) so the handler can be removed when the call ends.
    """
    logs_dir = Path(__file__).parent / "logs"
    logs_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = logs_dir / f"{store_name.replace(' ', '_')}_{ts}.log"

    handler = logging.FileHandler(str(log_file), encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    ))

    # Attach to root logger so it captures logs from all livekit.* loggers too
    root = logging.getLogger()
    root.addHandler(handler)
    logger.info(f"[LOG] Per-call log file: {log_file}")
    return handler, str(log_file)


# ---------------------------------------------------------------------------
# Custom Agent — sanitizes chat context + normalizes output for TTS
# ---------------------------------------------------------------------------
class SanitizedAgent(Agent):
    """Agent subclass that intercepts every LLM call to:
    1. Sanitize message ordering (vLLM/Qwen requires user-first after system)
    2. Log the exact messages sent to the LLM for debugging
    3. Strip <think>...</think> tags from Qwen3 output before TTS
    4. Clean up output for TTS (action markers, spacing)
    """

    @function_tool()
    async def end_call(self, context: RunContext) -> str:
        """Call this tool when the conversation is complete and you have the price information, or if the shopkeeper refuses to give a price on the phone."""
        logger.info("Agent triggered end_call")
        job_ctx = get_job_context()
        # Wait for TTS to finish speaking the goodbye before killing the room
        await asyncio.sleep(5)
        context.session.shutdown()
        await job_ctx.delete_room()
        return "Call ended. Thank you."

    async def llm_node(self, chat_ctx, tools, model_settings):
        # --- Sanitize chat context ---
        chat_ctx = self._sanitize_chat_ctx(chat_ctx)

        # --- Log what we're sending to the LLM ---
        try:
            messages, _ = chat_ctx.to_provider_format("openai")
            roles = [m.get("role", "?") for m in messages]
            logger.info(f"[LLM REQUEST] roles={roles}, messages={len(messages)}")
            for i, m in enumerate(messages):
                content = m.get("content", "")
                if len(str(content)) > 200:
                    content = str(content)[:200] + "..."
                logger.debug(f"[LLM MSG {i}] role={m.get('role')} content={content}")
        except Exception as e:
            logger.warning(f"[LLM REQUEST] failed to log messages: {e}")

        # --- Forward to default LLM node, cleaning output for TTS ---
        # Per-chunk normalization to keep streaming smooth (no buffering).
        async for chunk in Agent.default.llm_node(self, chat_ctx, tools, model_settings):
            if isinstance(chunk, str):
                chunk = _strip_think_tags(chunk)
                chunk = _normalize_for_tts(chunk)
                if chunk.strip():  # skip empty chunks but preserve leading/trailing spaces
                    yield chunk
            elif hasattr(chunk, "delta") and isinstance(getattr(chunk.delta, "content", None), str):
                chunk.delta.content = _strip_think_tags(chunk.delta.content)
                chunk.delta.content = _normalize_for_tts(chunk.delta.content)
                yield chunk
            else:
                yield chunk

    @staticmethod
    def _sanitize_chat_ctx(chat_ctx: llm.ChatContext) -> llm.ChatContext:
        """Ensure first non-system message is from the user.
        Required by vLLM/Qwen; also prevents stale assistant context with Claude."""
        ctx = chat_ctx.copy()
        items = ctx.items

        # Find first ChatMessage that isn't system
        for i, item in enumerate(items):
            if getattr(item, "type", None) != "message":
                continue
            if item.role == "system":
                continue
            # First non-system message found
            if item.role != "user":
                logger.warning(
                    f"[SANITIZE] First non-system message is role='{item.role}', "
                    f"expected 'user'. Removing it to prevent vLLM 400 error."
                )
                items.pop(i)
            break

        return ctx


# Regex to strip Qwen3 thinking blocks from streamed text (only applies when using Qwen LLM)
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_THINK_OPEN_RE = re.compile(r"<think>.*", re.DOTALL)


def _strip_think_tags(text: str) -> str:
    """Remove <think>...</think> blocks from Qwen3 output so TTS doesn't read them."""
    text = _THINK_RE.sub("", text)
    text = _THINK_OPEN_RE.sub("", text)  # handle unclosed tag (streaming)
    return text


# ---------------------------------------------------------------------------
# TTS text normalization — cleanup + Hindi number conversion
# ---------------------------------------------------------------------------
# No Devanagari word replacements. The LLM outputs Romanized Hindi,
# and Sarvam TTS with enable_preprocessing=True handles pronunciation.
# We handle: action markers, spacing fixes, and number→Hindi word conversion
# so the TTS doesn't read "36000" as "thirty-six thousand".

_ACTION_RE = re.compile(r"[\*\(\[][a-zA-Z\s]+[\*\)\]]")

# Hindi number words
_HINDI_ONES = {
    0: "", 1: "ek", 2: "do", 3: "teen", 4: "chaar", 5: "paanch",
    6: "chheh", 7: "saat", 8: "aath", 9: "nau", 10: "das",
    11: "gyaarah", 12: "baarah", 13: "terah", 14: "chaudah", 15: "pandrah",
    16: "solah", 17: "satrah", 18: "athaarah", 19: "unees", 20: "bees",
    21: "ikkees", 22: "baaees", 23: "teyees", 24: "chaubees", 25: "pachchees",
    26: "chhabbees", 27: "sattaaees", 28: "attaaees", 29: "untees", 30: "tees",
    31: "ikattees", 32: "battees", 33: "taintees", 34: "chauntees", 35: "paintees",
    36: "chhatees", 37: "saintees", 38: "adtees", 39: "untaalees", 40: "chaalees",
    41: "iktaalees", 42: "bayaalees", 43: "taintaalees", 44: "chauvaalees", 45: "paintaalees",
    46: "chhiyaalees", 47: "saintaalees", 48: "adtaalees", 49: "unchaas", 50: "pachaas",
    51: "ikyaavan", 52: "baavan", 53: "tirpan", 54: "chauvan", 55: "pachpan",
    56: "chhappan", 57: "sattaavan", 58: "atthaavan", 59: "unsath", 60: "saath",
    61: "iksath", 62: "baasath", 63: "tirsath", 64: "chaunsath", 65: "painsath",
    66: "chhiyaasath", 67: "sadsath", 68: "adsath", 69: "unhattar", 70: "sattar",
    71: "ikhattar", 72: "bahattar", 73: "tihattar", 74: "chauhattar", 75: "pachhattar",
    76: "chhihattar", 77: "satattar", 78: "athattar", 79: "unaasi", 80: "assi",
    81: "ikyaasi", 82: "bayaasi", 83: "tiraasi", 84: "chauraasi", 85: "pachaasi",
    86: "chhiyaasi", 87: "sataasi", 88: "athaasi", 89: "navaasi", 90: "nabbe",
    91: "ikyaanbe", 92: "baanbe", 93: "tirranbe", 94: "chauranbe", 95: "pachranbe",
    96: "chhiyanbe", 97: "sattanbe", 98: "atthanbe", 99: "ninyanbe",
}


def _number_to_hindi(n: int) -> str:
    """Convert an integer to Hindi word form."""
    if n == 0:
        return "zero"
    if n < 0:
        return "minus " + _number_to_hindi(-n)

    parts = []
    if n >= 10000000:  # crore
        parts.append(_number_to_hindi(n // 10000000) + " crore")
        n %= 10000000
    if n >= 100000:  # lakh
        parts.append(_number_to_hindi(n // 100000) + " lakh")
        n %= 100000
    if n >= 1000:  # hazaar
        parts.append(_number_to_hindi(n // 1000) + " hazaar")
        n %= 1000
    if n >= 100:  # sau
        parts.append(_HINDI_ONES[n // 100] + " sau")
        n %= 100
    if n > 0:
        parts.append(_HINDI_ONES[n])

    return " ".join(parts)


# Match standalone numbers: integers and decimals (not inside words)
_NUMBER_RE = re.compile(r"\b(\d[\d,]*\.?\d*)\b")


def _replace_numbers(text: str) -> str:
    """Replace digit numbers with Hindi words for natural TTS pronunciation."""
    def _repl(m):
        raw = m.group(1).replace(",", "")
        # Handle decimals: "1.5" → "dedh" (special case) or "ek point paanch"
        if "." in raw:
            if raw == "1.5":
                return "dedh"
            if raw == "2.5":
                return "dhaai"
            int_part, dec_part = raw.split(".", 1)
            result = _number_to_hindi(int(int_part)) if int_part else ""
            result += " point " + " ".join(_HINDI_ONES[int(d)] for d in dec_part if d.isdigit())
            return result.strip()
        try:
            return _number_to_hindi(int(raw))
        except (ValueError, KeyError):
            return m.group(0)  # leave as-is if conversion fails
    return _NUMBER_RE.sub(_repl, text)


def _normalize_for_tts(text: str) -> str:
    """Clean up LLM output for TTS — strip markers, fix spacing, convert numbers."""
    # Strip roleplay action markers
    text = _ACTION_RE.sub("", text)
    # Convert digit numbers to Hindi words
    text = _replace_numbers(text)
    # Insert space between lowercase→uppercase transitions (fixes "puraneAC" → "purane AC")
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    # Insert space before digit→letter or letter→digit transitions (fixes "5star" → "5 star")
    text = re.sub(r"([a-zA-Z])(\d)", r"\1 \2", text)
    text = re.sub(r"(\d)([a-zA-Z])", r"\1 \2", text)
    # Collapse multiple spaces
    text = re.sub(r" {2,}", " ", text)
    return text

# ---------------------------------------------------------------------------
# Conversation prompt
# ---------------------------------------------------------------------------
DEFAULT_INSTRUCTIONS = """You are a regular middle-class Indian guy calling a local AC shop to ask about prices. You speak the way a normal person speaks on the phone in Hindi — casual, natural, with filler words.

VOICE & TONE:
- Speak in natural spoken Hindi/Hinglish. NOT formal Hindi, NOT written Hindi.
- Use fillers naturally: "haan", "achha", "hmm", "ji"
- Keep answers SHORT — 1 line, max 2. Don't give speeches.
- React naturally to what the shopkeeper says.
- Use "bhai sahab" ONLY ONCE at the beginning. After that just say "ji" or nothing.

WHAT YOU CARE ABOUT:
- Price — "Best price kya doge?" / "Final kitna lagega?"
- Installation — "Installation free hai ya alag se?"
- Warranty — "Warranty kitni hai?"
- Exchange — "Purana AC hai, exchange pe kuch milega kya?" (optional)
- Availability — "Stock mein hai?" (optional)

WHAT YOU DON'T CARE ABOUT (don't ask):
- Technical specs (copper vs aluminium, cooling capacity, inverter details)
- Wi-Fi, smart features, brand comparisons, energy rating details
If the shopkeeper mentions these, just say "achha" and move on.

CONVERSATION FLOW:
- Start by confirming the shop and asking about the AC
- Ask the price, then negotiate naturally
- Once you have price + 1-2 extras, wrap up and CALL the end_call tool
- Don't go through a checklist — follow the shopkeeper's responses naturally

ENDING THE CALL:
- ONLY call end_call AFTER you have asked about the price. Do NOT end the call early.
- If the shopkeeper says something unclear or off-topic, stay on the line and redirect to AC prices.
- If the shopkeeper says "wait" or "hold on", just say "ji ji, no problem" and wait.
- When done, say goodbye and IMMEDIATELY call the end_call tool function
- Do NOT continue talking after saying goodbye
- If the shopkeeper asks "anything else?" after you've said bye, say "nahi ji, bas itna hi tha" and call end_call

CRITICAL OUTPUT RULES:
- Your output goes DIRECTLY to a Hindi text-to-speech engine
- Write ONLY in Romanized Hindi using English/Latin letters
- NEVER use Devanagari script. No Hindi letters like हिंदी, आप, कैसे etc.
- NEVER add English translations, explanations, or parenthetical notes. NO "(Yes, I'm listening)" or similar.
- NEVER use newlines in your response. Write everything in a single line.
- Put a space between EVERY word: "aap ka rate kya hai" NOT "aapkaratekya hai"
- Write numbers as Hindi words, NOT digits. Say "chhatees hazaar" not "36000" or "36 hazaar". Say "dedh ton" not "1.5 ton".
- When the shopkeeper tells you a price, REPEAT their exact number back to confirm. Do NOT substitute a different number.
- Do NOT write action markers like *pauses* or (laughs)
- Do NOT write "[end_call]" as text. Use the actual end_call tool function when you want to end the call.
- Only output the exact words you would speak. Nothing else.

EXAMPLES:
You: "Bhai sahab, Samsung dedh ton ka paanch star inverter split AC hai aapke paas?"
You: "Achha, uska kya rate chal raha hai?"
You: "Hmm, thoda zyada lag raha hai. Online pe toh kam mein dikha raha tha."
You: "Theek hai ji, main soch ke bataata hoon. Dhanyavaad." → then call end_call tool
"""



# ---------------------------------------------------------------------------
# LLM provider selection
# ---------------------------------------------------------------------------
def _create_llm():
    """Create the LLM instance based on LLM_PROVIDER env var.

    LLM_PROVIDER=qwen  (default) — Qwen3-4B via self-hosted vLLM
    LLM_PROVIDER=claude          — Claude Haiku 3.5 via Anthropic API
    """
    provider = os.environ.get("LLM_PROVIDER", "qwen").lower()

    if provider == "claude":
        logger.info("[LLM] Using Claude Haiku 3.5 (Anthropic)")
        return anthropic.LLM(
            model="claude-3-5-haiku-20241022",
            api_key=os.environ.get("ANTHROPIC_API_KEY"),
            temperature=0.7,
        )
    else:
        logger.info("[LLM] Using Qwen3-4B-Instruct (vLLM)")
        return openai.LLM(
            model=os.environ.get("LLM_MODEL", "Qwen/Qwen3-4B-Instruct-2507-FP8"),
            base_url=os.environ.get("LLM_BASE_URL", "http://192.168.0.42:8000/v1"),
            api_key=os.environ.get("LLM_API_KEY", "unused"),
            temperature=0.7,
        )


# ---------------------------------------------------------------------------
# Agent entrypoint
# ---------------------------------------------------------------------------
async def entrypoint(ctx: JobContext):
    """
    Called by LiveKit when a dispatch is created for this agent.
    Handles the full lifecycle: connect → dial → converse → hangup.
    """
    logger.info(f"Agent entrypoint called. Room: {ctx.room.name}")

    # Parse metadata from the dispatch
    metadata = json.loads(ctx.job.metadata or "{}")
    phone_number = metadata.get("phone", "")
    store_name = metadata.get("store_name", "Unknown Store")
    ac_model = metadata.get("ac_model", "Samsung 1.5 Ton Split AC")
    sip_trunk_id = metadata.get("sip_trunk_id", os.environ.get("SIP_OUTBOUND_TRUNK_ID", ""))

    is_browser = not phone_number
    logger.info(f"{'Browser session' if is_browser else f'Calling {store_name} at {phone_number}'} for {ac_model}")

    # Set up per-call log file (captures all agent, LLM, and session logs for this call)
    call_log_handler, call_log_path = _setup_call_logger(store_name)

    # Connect agent to the room
    await ctx.connect()

    # Build custom instructions with the specific AC model and store name
    greeting = f"Hello, yeh {store_name} hai? Aap log AC dealer ho?"
    instructions = DEFAULT_INSTRUCTIONS + f"""
PRODUCT: {ac_model}
STORE: {store_name}

NOTE: You have already greeted the shopkeeper with: "{greeting}"
Do NOT repeat the greeting. Continue the conversation from the shopkeeper's response.
"""

    # Create the agent session with Sarvam STT/TTS + switchable LLM (Claude or Qwen)
    session = AgentSession(
        # Voice Activity Detection — detect when someone is speaking
        vad=silero.VAD.load(
            min_speech_duration=0.08,    # 80ms — filter out short noise bursts (default 50ms)
            min_silence_duration=0.8,    # 800ms — wait longer before ending speech turn (default 550ms)
            activation_threshold=0.5,    # default — speech probability to start detection
        ),
        # Speech-to-Text — Sarvam saaras:v3 for Hindi/Hinglish
        stt=sarvam.STT(
            language="hi-IN",
            model="saaras:v3",
            api_key=os.environ.get("SARVAM_API_KEY"),
            sample_rate=16000,
        ),
        # LLM — switchable via LLM_PROVIDER env var (qwen or claude)
        llm=_create_llm(),
        # Text-to-Speech — Sarvam bulbul:v3 for natural Hindi voice
        tts=sarvam.TTS(
            model="bulbul:v3",
            target_language_code="hi-IN",
            speaker="aditya",  # v3 male voice; others: rahul, rohan, amit, dev, varun, ratan; female: ritu, priya, neha, pooja, simran
            api_key=os.environ.get("SARVAM_API_KEY"),
            pace=1.0,
            pitch=0,
            loudness=1.5,
            speech_sample_rate=16000 if is_browser else 8000,  # 16kHz browser / 8kHz telephony
            enable_preprocessing=True,  # Let Sarvam handle Romanized Hindi → native pronunciation
        ),
    )

    # Start the agent
    await session.start(
        room=ctx.room,
        agent=SanitizedAgent(instructions=instructions),
        room_options=RoomOptions(
            # Audio-only — no text or video input
            text_input=False,
            video_input=False,
        ),
    )

    # ---- Transcript collection & conversation logging ----
    transcript_lines = []  # Collect messages for saving to file

    @session.on("user_input_transcribed")
    def on_user_transcript(ev):
        if ev.is_final:
            logger.info(f"[USER] {ev.transcript}")
            transcript_lines.append({"role": "user", "text": ev.transcript, "time": datetime.now().isoformat()})

    @session.on("conversation_item_added")
    def on_conversation_item(ev):
        item = ev.item
        if item.role == "assistant":
            text = "".join(str(c) for c in item.content)
            logger.info(f"[LLM] {text}")
            transcript_lines.append({"role": "assistant", "text": text, "time": datetime.now().isoformat()})

    @session.on("function_tools_executed")
    def on_tools_executed(ev):
        for fc in ev.function_calls:
            logger.info(f"[TOOL CALL] {fc.name}({fc.arguments})")
        for out in ev.function_call_outputs:
            if out:
                logger.info(f"[TOOL RESULT] {out.name} → {out.output}")

    @session.on("metrics_collected")
    def on_metrics(ev):
        m = ev.metrics
        # Only log LLM metrics (has prompt_tokens attribute)
        if hasattr(m, "prompt_tokens"):
            logger.info(
                f"[LLM METRICS] tokens: {m.prompt_tokens}→{m.completion_tokens}, "
                f"TTFT: {m.ttft:.2f}s, duration: {m.duration:.2f}s"
            )

    @session.on("error")
    def on_error(ev):
        logger.error(f"[SESSION ERROR] source={type(ev.source).__name__}, error={ev.error}")

    # ---- Save transcript when a participant disconnects ----
    def _save_transcript():
        if not transcript_lines:
            return
        transcript_dir = Path(__file__).parent / "transcripts"
        transcript_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = transcript_dir / f"{store_name.replace(' ', '_')}_{ts}.json"
        data = {
            "store_name": store_name,
            "ac_model": ac_model,
            "room": ctx.room.name,
            "phone": phone_number or "browser",
            "timestamp": datetime.now().isoformat(),
            "messages": transcript_lines,
        }
        try:
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.info(f"[TRANSCRIPT] Saved to {filename}")
        except Exception as e:
            logger.error(f"[TRANSCRIPT] Failed to save: {e}")

    @ctx.room.on("participant_disconnected")
    def on_participant_left(participant):
        logger.info(f"Participant {participant.identity} left — saving transcript and closing call log")
        _save_transcript()
        # Close per-call log handler so the file is flushed and released
        logging.getLogger().removeHandler(call_log_handler)
        call_log_handler.close()
        logger.info(f"[LOG] Call log saved to {call_log_path}")

    # Now dial the store (or wait for browser participant)
    if phone_number and sip_trunk_id:
        logger.info(f"Dialing {phone_number} via SIP trunk {sip_trunk_id}")
        try:
            await ctx.api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    sip_trunk_id=sip_trunk_id,
                    sip_call_to=phone_number,
                    room_name=ctx.room.name,
                    participant_identity=f"phone-{phone_number}",
                    participant_name=store_name,
                )
            )
            logger.info(f"SIP call initiated to {phone_number}")
        except Exception as e:
            logger.error(f"Failed to initiate SIP call: {e}")
            return
    elif not phone_number:
        # Browser session — wait for browser participant, then greet.
        # Greeting is spoken via TTS but NOT added to chat context (to avoid
        # sanitizer stripping it as an assistant-first message). The LLM knows
        # the greeting was said via the NOTE in system instructions.
        logger.info("Browser session — waiting for browser participant to join")
        await ctx.wait_for_participant()
        logger.info("Browser participant joined — sending greeting")
        session.say(greeting, add_to_chat_ctx=False)
        transcript_lines.append({"role": "assistant", "text": greeting, "time": datetime.now().isoformat()})

    if not is_browser:
        # Set a maximum call duration timer (SIP calls only)
        async def call_timeout():
            await asyncio.sleep(120)  # 2 minutes max
            logger.info("Call timeout reached, ending call")
            for participant in ctx.room.remote_participants.values():
                try:
                    await ctx.api.room.remove_participant(
                        api.RoomParticipantIdentity(
                            room=ctx.room.name,
                            identity=participant.identity,
                        )
                    )
                except Exception:
                    pass

        asyncio.create_task(call_timeout())


# ---------------------------------------------------------------------------
# Run the agent worker
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="ac-price-agent",  # Must match dispatch requests
        )
    )
