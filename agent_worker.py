"""
agent_worker.py — LiveKit Agent Worker
=======================================
Voice AI agent that calls local AC shops to enquire about prices.
Uses Sarvam AI for Hindi STT/TTS and Claude Haiku 4.5 or Qwen3 for LLM.
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
from livekit.plugins.turn_detector.multilingual import MultilingualModel

logger = logging.getLogger("ac-price-caller.agent")

# Default Claude model — configurable via CLAUDE_MODEL env var
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")


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
        await context.wait_for_playout()
        # Save transcript before shutdown (backup in case disconnect handler doesn't fire)
        if hasattr(self, '_save_transcript_fn'):
            self._save_transcript_fn()
        context.session.shutdown()
        await job_ctx.delete_room()
        return "Call ended. Thank you."

    async def llm_node(self, chat_ctx, tools, model_settings):
        # Reset text accumulator for this LLM turn
        self._last_response_text = ""

        # --- Sanitize chat context ---
        chat_ctx = self._sanitize_chat_ctx(chat_ctx)

        # --- Annotate interrupted (truncated) assistant messages ---
        for item in chat_ctx.items:
            if (getattr(item, "role", None) == "assistant"
                    and getattr(item, "interrupted", False)):
                text = (item.text_content or "").strip()
                if text and not text.endswith("[interrupted]"):
                    item.content = [text + " [interrupted]"]

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
        # Also accumulate text so end_call can capture it for transcript.
        async for chunk in Agent.default.llm_node(self, chat_ctx, tools, model_settings):
            if isinstance(chunk, str):
                chunk = _strip_think_tags(chunk)
                chunk = _normalize_for_tts(chunk)
                if chunk.strip():  # skip empty chunks but preserve leading/trailing spaces
                    self._last_response_text += chunk
                    yield chunk
            elif hasattr(chunk, "delta") and isinstance(getattr(chunk.delta, "content", None), str):
                chunk.delta.content = _strip_think_tags(chunk.delta.content)
                chunk.delta.content = _normalize_for_tts(chunk.delta.content)
                self._last_response_text += chunk.delta.content
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

# ---------------------------------------------------------------------------
# Devanagari → Romanized Hindi transliteration (safety net for LLM leaks)
# ---------------------------------------------------------------------------
# Static lookup — O(n) per character, zero latency overhead.
# Covers all common Devanagari vowels, consonants, matras, and digits.
_DEVANAGARI_MAP = {
    # Vowels (independent forms)
    'अ': 'a', 'आ': 'aa', 'इ': 'i', 'ई': 'ee', 'उ': 'u', 'ऊ': 'oo',
    'ऋ': 'ri', 'ए': 'e', 'ऐ': 'ai', 'ओ': 'o', 'औ': 'au', 'अं': 'an',
    'अः': 'ah',
    # Consonants
    'क': 'ka', 'ख': 'kha', 'ग': 'ga', 'घ': 'gha', 'ङ': 'nga',
    'च': 'cha', 'छ': 'chha', 'ज': 'ja', 'झ': 'jha', 'ञ': 'nya',
    'ट': 'ta', 'ठ': 'tha', 'ड': 'da', 'ढ': 'dha', 'ण': 'na',
    'त': 'ta', 'थ': 'tha', 'द': 'da', 'ध': 'dha', 'न': 'na',
    'प': 'pa', 'फ': 'pha', 'ब': 'ba', 'भ': 'bha', 'म': 'ma',
    'य': 'ya', 'र': 'ra', 'ल': 'la', 'व': 'va', 'श': 'sha',
    'ष': 'sha', 'स': 'sa', 'ह': 'ha',
    # Nukta variants
    'क़': 'qa', 'ख़': 'kha', 'ग़': 'ga', 'ज़': 'za', 'ड़': 'da', 'ढ़': 'dha', 'फ़': 'fa',
    # Matras (vowel signs on consonants)
    'ा': 'aa', 'ि': 'i', 'ी': 'ee', 'ु': 'u', 'ू': 'oo',
    'े': 'e', 'ै': 'ai', 'ो': 'o', 'ौ': 'au',
    'ं': 'n', 'ः': 'h', 'ँ': 'n',
    # Halant (virama) — suppresses inherent 'a' vowel
    '्': '',
    # Digits
    '०': '0', '१': '1', '२': '2', '३': '3', '४': '4',
    '५': '5', '६': '6', '७': '7', '८': '8', '९': '9',
    # Punctuation
    '।': '.', '॥': '.',
}

# Devanagari consonants have an inherent 'a' vowel. When followed by a matra
# (dependent vowel sign), the matra replaces the inherent 'a'. When followed by
# halant (्), the inherent 'a' is suppressed entirely.
_DEVANAGARI_CONSONANTS = set(
    'कखगघङचछजझञटठडढणतथदधनपफबभमयरलवशषसह'
    'क़ख़ग़ज़ड़ढ़फ़'
)
# Matras (dependent vowel signs) — replace the inherent 'a' of the preceding consonant
_DEVANAGARI_MATRAS = {
    'ा', 'ि', 'ी', 'ु', 'ू', 'ृ', 'े', 'ै', 'ो', 'ौ',
}

def _transliterate_devanagari(text: str) -> str:
    """Replace any Devanagari characters with Romanized equivalents.
    Fast single-pass — only activates if Devanagari is detected.
    Handles consonant+matra combinations correctly (matra replaces inherent 'a')."""
    # Quick check: skip if no Devanagari present (common case)
    if not any('\u0900' <= c <= '\u097F' for c in text):
        return text
    result = []
    i = 0
    while i < len(text):
        ch = text[i]
        if '\u0900' <= ch <= '\u097F':
            roman = _DEVANAGARI_MAP.get(ch, '')
            # If this is a consonant, check if next char is a matra or halant
            if ch in _DEVANAGARI_CONSONANTS and roman.endswith('a'):
                if i + 1 < len(text):
                    next_ch = text[i + 1]
                    if next_ch in _DEVANAGARI_MATRAS or next_ch == '्':
                        # Strip inherent 'a' — the matra/halant will provide the vowel
                        roman = roman[:-1]
            result.append(roman)
        else:
            result.append(ch)
        i += 1
    return ''.join(result)

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
        thousands = n // 1000
        remainder = n % 1000
        if remainder == 500:
            # Natural Hindi: 37500 → "saadhe saintees hazaar" (not "saintees hazaar paanch sau")
            if thousands == 1:
                parts.append("dedh hazaar")
            elif thousands == 2:
                parts.append("dhaai hazaar")
            else:
                parts.append("saadhe " + _number_to_hindi(thousands) + " hazaar")
            n = 0  # fully consumed
        else:
            parts.append(_number_to_hindi(thousands) + " hazaar")
            n = remainder
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
    # Replace newlines with spaces (LLM sometimes inserts \n\n between sentences)
    text = text.replace("\n", " ")
    # Transliterate any Devanagari that leaked through the LLM (safety net)
    text = _transliterate_devanagari(text)
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
- Ask ONE question at a time. Do not stack 2-3 questions in one response.
- Cover these topics naturally: price → warranty → installation → delivery
- After getting the price and at least 2 other details, wrap up and CALL the end_call tool
- Follow the shopkeeper's responses naturally — don't go through a checklist

INTERRUPTIONS:
- If your previous message shows [interrupted], it means the shopkeeper interrupted you mid-sentence.
- Do NOT repeat what you already said. Respond to what the shopkeeper said instead.
- Continue the conversation naturally from the interruption point.

ENDING THE CALL:
- Do NOT call end_call until you have the PRICE plus at least 2 of: warranty, installation cost, delivery time.
- If the shopkeeper says something unclear or off-topic, stay on the line and redirect to AC prices.
- If the shopkeeper says "wait" or "hold on", just say "ji ji, no problem" and wait.
- When you have enough info, say a SHORT goodbye like "Theek hai ji, bahut badiya. Dhanyavaad, namaste." and IMMEDIATELY call end_call.
- Do NOT continue talking after saying goodbye.
- If the shopkeeper asks "anything else?" after you've said bye, say "nahi ji, bas itna hi tha" and call end_call.

STAY IN CHARACTER:
- You are the CUSTOMER calling to ask for prices. The shopkeeper is the one answering.
- NEVER confirm stock availability or offer to show models — that is the SHOPKEEPER's job, not yours.
- The shopkeeper may respond in English, Hindi, or Hinglish. Regardless of their language, YOU always speak in Romanized Hindi and stay in the customer role.
- If the shopkeeper's English response sounds like a question (e.g. "Do you have it in stock?"), they are probably confirming they have it. Respond as a customer: ask for the price.
- You are a regular customer. Sound like a real person with concrete details — don't be evasive.
- If asked where you live, use the area from YOUR AREA (given below). Say it naturally: "[area] mein rehta hoon" or "[area] side se bol raha hoon".
- If asked about your old AC for exchange, say "Voltas ka hai, kaafi purana ho gaya hai" or "LG ka window AC hai purana". Pick ONE brand and stick with it.
- Do NOT invent overly specific details (exact addresses, flat numbers, family members). Keep it natural but concrete enough to build trust.

CRITICAL OUTPUT RULES:
- Your output goes DIRECTLY to a Hindi text-to-speech engine
- Write ONLY in Romanized Hindi using English/Latin letters
- NEVER use Devanagari script. No Hindi letters like हिंदी, आप, कैसे etc.
- NEVER add English translations, explanations, or parenthetical notes. NO "(Yes, I'm listening)" or similar.
- NEVER use newlines in your response. Write everything in a single line.
- Ask only ONE question per response. Do NOT stack 2-3 questions together.
  WRONG: "38000? Thoda zyada nahi? Installation free hai kya?" (3 questions)
  RIGHT: "Achha, 38000. Thoda zyada lag raha hai. Installation free hai kya?" (1 question)
- When the shopkeeper tells you a price, echo it as a STATEMENT, not a question. Say "Achha, 38000." NOT "38000?"
- Put a space between EVERY word: "aap ka rate kya hai" NOT "aapkaratekya hai"
- Write ALL numbers as DIGITS, not words. The system converts digits to Hindi words automatically.
  Say "38000" not "adtees hazaar". Say "1.5 ton" not "dedh ton". Say "2 saal" not "do saal".
- When the shopkeeper tells you ANY number (price, warranty years, delivery days), REPEAT their EXACT number back as digits. Do NOT change the number.
  WRONG: Shopkeeper says "39000" → you say "Achha, 30000" (WRONG number)
  RIGHT: Shopkeeper says "39000" → you say "Achha, 39000." (exact same number)
  WRONG: Shopkeeper says "2 years warranty" → you say "1 saal" (WRONG number)
  RIGHT: Shopkeeper says "2 years warranty" → you say "Achha, 2 saal."
- Do NOT write action markers like *pauses* or (laughs)
- Do NOT write "[end_call]" as text. Use the actual end_call tool function when you want to end the call.
- Only output the exact words you would speak. Nothing else.

EXAMPLES:
You: "Bhai sahab, Samsung 1.5 ton ka 5 star inverter split AC hai aapke paas?"
Shopkeeper: "Haan, 38000 ka hai."
You: "Achha, 38000. Installation free hai kya?"
Shopkeeper: "Haan free hai."
You: "Theek hai. Warranty kitni milegi?"
Shopkeeper: "1 saal ki."
You: "Achha 1 saal. Delivery kitne din mein hogi?"
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
        logger.info(f"[LLM] Using Claude ({CLAUDE_MODEL})")
        return anthropic.LLM(
            model=CLAUDE_MODEL,
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
    nearby_area = metadata.get("nearby_area", "")
    sip_trunk_id = metadata.get("sip_trunk_id", os.environ.get("SIP_OUTBOUND_TRUNK_ID", ""))

    is_browser = not phone_number
    logger.info(f"{'Browser session' if is_browser else f'Calling {store_name} at {phone_number}'} for {ac_model}")

    # Set up per-call log file (captures all agent, LLM, and session logs for this call)
    call_log_handler, call_log_path = _setup_call_logger(store_name)

    # Connect agent to the room
    await ctx.connect()

    # Build custom instructions with the specific AC model, store name, and nearby area
    greeting = f"Hello, yeh {store_name} hai? Aap log AC dealer ho?"
    area_info = f'\nYOUR AREA: {nearby_area} — if asked where you live, say "{nearby_area} mein rehta hoon" or "{nearby_area} side".' if nearby_area else ""
    instructions = DEFAULT_INSTRUCTIONS + f"""
PRODUCT: {ac_model}
STORE: {store_name}{area_info}

NOTE: You have already greeted the shopkeeper with: "{greeting}"
Do NOT repeat the greeting. Continue the conversation from the shopkeeper's response.
"""

    # Create the agent session with Sarvam STT/TTS + switchable LLM (Claude or Qwen)
    session = AgentSession(
        # Turn detection — multilingual transformer model predicts end-of-utterance
        # using conversation context (supports Hindi). Runs on top of VAD signals.
        turn_detection=MultilingualModel(),
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
            flush_signal=True,           # Emit start/end of speech events for turn detection
        ),
        # LLM — switchable via LLM_PROVIDER env var (qwen or claude)
        llm=_create_llm(),
        # Text-to-Speech — Sarvam bulbul:v3 for natural Hindi voice
        tts=sarvam.TTS(
            model="bulbul:v3",
            target_language_code="hi-IN",
            speaker="shubh",  # v3 male voice; others: aditya, rahul, rohan, amit, dev, varun, ratan; female: ritu, priya, neha, pooja, simran
            api_key=os.environ.get("SARVAM_API_KEY"),
            pace=1.0,
            pitch=0,
            loudness=1.5,
            speech_sample_rate=16000 if is_browser else 8000,  # 16kHz browser / 8kHz telephony
            enable_preprocessing=True,  # Let Sarvam handle Romanized Hindi → native pronunciation
        ),
        # Interruption handling
        min_interruption_duration=0.8,   # 800ms speech before triggering barge-in (default 500ms)
        min_interruption_words=2,        # Require 2+ words — filters "hmm", "haan" backchannels
        false_interruption_timeout=2.0,  # Wait 2s before declaring false interruption
        resume_false_interruption=True,  # Resume speaking after false interruption
        min_endpointing_delay=0.5,       # 500ms after last speech before declaring turn complete
    )

    # ---- Transcript collection & conversation logging ----
    transcript_lines = []  # Collect messages for saving to file

    # Create agent and wire up transcript reference for end_call capture
    agent = SanitizedAgent(instructions=instructions)
    agent._transcript_lines = transcript_lines
    agent._last_response_text = ""

    # Start the agent
    await session.start(
        room=ctx.room,
        agent=agent,
        room_options=RoomOptions(
            # Audio-only — no text or video input
            text_input=False,
            video_input=False,
        ),
    )

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
            was_interrupted = getattr(item, "interrupted", False)
            if was_interrupted:
                logger.warning(f"[INTERRUPTED] Agent speech truncated: '{text}'")
            logger.info(f"[LLM] {'[TRUNCATED] ' if was_interrupted else ''}{text}")
            transcript_lines.append({
                "role": "assistant", "text": text, "time": datetime.now().isoformat(),
                **({"interrupted": True} if was_interrupted else {}),
            })

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

    @session.on("agent_state_changed")
    def on_agent_state_changed(ev):
        """Dynamically adjust interruption sensitivity based on agent state."""
        if ev.new_state == "speaking":
            # While speaking, require stronger evidence of real interruption
            session.options.min_interruption_words = 2
            session.options.min_interruption_duration = 1.0
        elif ev.new_state == "thinking":
            # While thinking, be responsive to any speech
            session.options.min_interruption_words = 0
            session.options.min_interruption_duration = 0.3

    @session.on("error")
    def on_error(ev):
        error = ev.error
        source_name = type(ev.source).__name__
        if hasattr(error, 'recoverable'):
            if error.recoverable:
                logger.warning(
                    f"[SESSION ERROR] (recoverable) source={source_name}, "
                    f"label={error.label}, error={error.error}"
                )
            else:
                logger.error(
                    f"[SESSION ERROR] (non-recoverable) source={source_name}, "
                    f"label={error.label}, error={error.error}"
                )
                _save_transcript()
        else:
            logger.error(f"[SESSION ERROR] source={source_name}, error={error}")

    # ---- Transcript & log cleanup (idempotent — safe to call multiple times) ----
    _transcript_saved = False
    _log_closed = False

    def _save_transcript():
        nonlocal _transcript_saved
        if _transcript_saved or not transcript_lines:
            return
        _transcript_saved = True
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
            return

        # Post-call quality analysis — writes companion .analysis.json
        try:
            from call_analysis import analyze_transcript
            analysis = analyze_transcript(data)
            analysis_file = filename.with_suffix(".analysis.json")
            with open(analysis_file, "w", encoding="utf-8") as f:
                json.dump(analysis, f, ensure_ascii=False, indent=2)
            topics = ", ".join(analysis.get("topics_covered", []))
            logger.info(
                f"[ANALYSIS] Score: {analysis['overall_score']}, "
                f"Topics: [{topics}], Turns: {analysis['turn_count']}"
            )
        except Exception as e:
            logger.warning(f"[ANALYSIS] Post-call analysis failed (non-fatal): {e}")

    def _close_log():
        nonlocal _log_closed
        if _log_closed:
            return
        _log_closed = True
        logging.getLogger().removeHandler(call_log_handler)
        call_log_handler.close()
        logger.info(f"[LOG] Call log saved to {call_log_path}")

    # Wire save function onto agent so end_call can use it
    agent._save_transcript_fn = _save_transcript

    @session.on("close")
    def on_close(ev):
        logger.info(f"[SESSION CLOSE] reason={ev.reason}")
        _save_transcript()
        _close_log()

    @ctx.room.on("participant_disconnected")
    def on_participant_left(participant):
        logger.info(f"Participant {participant.identity} left — saving transcript and closing call log")
        _save_transcript()
        _close_log()

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
        try:
            await asyncio.wait_for(ctx.wait_for_participant(), timeout=30.0)
        except asyncio.TimeoutError:
            logger.error("Browser participant did not join within 30 seconds — shutting down")
            return
        logger.info("Browser participant joined — sending greeting")
        session.say(greeting, add_to_chat_ctx=False)
        transcript_lines.append({"role": "assistant", "text": greeting, "time": datetime.now().isoformat()})

    if not is_browser:
        # Set a maximum call duration timer (SIP calls only)
        async def call_timeout():
            await asyncio.sleep(120)  # 2 minutes max
            logger.info("Call timeout reached, saving transcript and ending call")
            _save_transcript()
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
