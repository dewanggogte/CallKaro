"""
caller.py — LiveKit Agent for outbound price enquiry calls
===========================================================
Uses:
  - Sarvam STT (saarika:v2.5) for Hindi/Hinglish speech recognition
  - Sarvam TTS (bulbul:v2) for natural Hindi speech synthesis
  - OpenAI GPT-4o-mini as the conversational LLM
  - LiveKit SIP for telephony (outbound calls)

The agent:
  1. Calls the shop
  2. Greets in Hindi, asks about product price
  3. Handles typical shopkeeper responses (come to shop, depends on model, etc.)
  4. Extracts: price, exchange offer, warranty, installation, availability
  5. Thanks and hangs up
"""

import asyncio
import json
import os
import logging
import time
from typing import Optional

from livekit import api, agents
from livekit.agents import AgentSession, Agent, RoomInputOptions
from livekit.plugins import openai, silero, sarvam

from main import Store, PriceQuote

logger = logging.getLogger("price-caller.caller")

# ---------------------------------------------------------------------------
# Prompt engineering — this is the CORE of your voice agent
# ---------------------------------------------------------------------------
SYSTEM_PROMPT_TEMPLATE = """You are a polite Hindi-speaking customer calling a shop to enquire about a product price. You MUST speak in natural Hindi/Hinglish (the way a normal Indian customer would talk on phone).

PRODUCT YOU'RE ASKING ABOUT: {product_description}

YOUR CONVERSATION GOAL:
1. Greet politely: "Namaste, main {product_description} ke baare mein poochna chahta tha"
2. Ask for the best price they can offer
3. Ask if there's any exchange offer (purana wala dene pe discount)
4. Ask if installation is included in the price
5. Ask about warranty (company warranty + extended warranty)
6. Ask about key features relevant to the product
7. Ask about any freebies or special deals (cashback, EMI offers, extended warranty deals, etc.)
8. Ask about availability (stock mein hai ya order karna padega)
9. Thank them and end the call

CRITICAL RULES:
- Speak naturally in Hindi/Hinglish. Mix English words for product names and technical terms.
- Keep responses SHORT (1-2 sentences max). Shopkeepers are busy.
- If they say "aao shop pe" (come to shop), politely say you're comparing prices first and will visit the best-priced shop. Push gently for a phone quote.
- If they refuse to give price on phone, note it as "refused_phone_price" and politely end.
- If they ask who you are, say you're looking to buy a {product_description} for your home.
- Don't reveal you're an AI. Act as a normal customer.
- NEVER argue or get aggressive. Always be respectful.
- If the line is bad or you can't understand, ask them to repeat ONCE, then move on.
- Complete the call in under 2 minutes.

AFTER THE CALL, you will need to extract this data:
- quoted_price (number or null)
- mrp (number or null)
- exchange_offer (string description or null)
- installation_included (true/false)
- warranty_info (string or null)
- features (key features mentioned — inverter type, copper condenser, Wi-Fi, etc. or null)
- freebies (any freebies/deals — free stabilizer, cashback, EMI offers, etc. or null)
- availability (string or null)
- additional_notes (any other useful info)
"""

EXTRACTION_PROMPT = """Extract structured data from this phone call transcript between a customer and a shop.
The customer was asking about: {product_description}

TRANSCRIPT:
{transcript}

Extract the following as JSON (use null if not mentioned):
{{
  "quoted_price": <number or null>,
  "mrp": <number or null>,
  "exchange_offer": "<description or null>",
  "installation_included": <true/false>,
  "warranty_info": "<description or null>",
  "features": "<key features mentioned or null>",
  "freebies": "<any freebies/deals offered or null>",
  "availability": "<in stock / X days / null>",
  "additional_notes": "<any other useful info or null>",
  "call_outcome": "<completed | refused_phone_price | no_price_given | unclear>"
}}

Return ONLY the JSON, no other text."""


# ---------------------------------------------------------------------------
# Call handler
# ---------------------------------------------------------------------------
class PriceEnquiryAgent(Agent):
    """Custom agent that tracks conversation for later extraction."""

    def __init__(self, product_description: str, store_name: str):
        super().__init__(
            instructions=SYSTEM_PROMPT_TEMPLATE.format(product_description=product_description),
        )
        self.product_description = product_description
        self.store_name = store_name
        self.transcript_lines: list[str] = []


async def make_price_enquiry_call(
    store: Store,
    product_description: str,
    sip_trunk_id: str,
    timeout_sec: float = 120.0,  # 2 min max call duration
) -> PriceQuote:
    """
    Make a single outbound call to a store and return a PriceQuote.

    This function:
    1. Creates a LiveKit room
    2. Dispatches the voice agent
    3. Dials the store via SIP
    4. Waits for the conversation to complete
    5. Extracts structured data from the transcript
    """
    quote = PriceQuote(store=store, product_description=product_description)
    start_time = time.time()

    try:
        # Create LiveKit API client
        lk_api = api.LiveKitAPI(
            url=os.environ["LIVEKIT_URL"],
            api_key=os.environ["LIVEKIT_API_KEY"],
            api_secret=os.environ["LIVEKIT_API_SECRET"],
        )

        room_name = f"call-{store.phone.replace('+', '')}-{int(time.time())}"

        # Step 1: Create a dispatch for the agent
        logger.info(f"Dispatching agent to room: {room_name}")
        await lk_api.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name="price-agent",
                room=room_name,
                metadata=json.dumps({
                    "phone": store.phone,
                    "store_name": store.name,
                    "product_description": product_description,
                    "sip_trunk_id": sip_trunk_id,
                }),
            )
        )

        # Step 2: Wait for agent to connect, then dial
        # The agent itself handles dialing (see agent_worker.py)
        # We wait for the room to be created and the call to complete

        # Poll for call completion
        call_completed = False
        while time.time() - start_time < timeout_sec:
            await asyncio.sleep(5)
            try:
                rooms = await lk_api.room.list_rooms(api.ListRoomsRequest(names=[room_name]))
                if not rooms.rooms:
                    # Room was deleted = call ended
                    call_completed = True
                    break
                room = rooms.rooms[0]
                if room.num_participants == 0:
                    call_completed = True
                    break
            except Exception:
                break

        quote.call_duration_sec = time.time() - start_time

        if call_completed:
            # Step 3: Retrieve transcript from the room's metadata or logs
            # In production, you'd use LiveKit's webhook or egress for transcripts
            # For POC, we use the agent's tracked transcript
            quote.call_status = "completed"
            logger.info(f"Call to {store.name} completed in {quote.call_duration_sec:.0f}s")
        else:
            quote.call_status = "timeout"
            logger.warning(f"Call to {store.name} timed out")

        await lk_api.aclose()

    except Exception as e:
        quote.call_status = "failed"
        quote.additional_notes = str(e)
        logger.error(f"Call to {store.name} failed: {e}")

    return quote


# ---------------------------------------------------------------------------
# Transcript extraction (post-call processing)
# ---------------------------------------------------------------------------
async def extract_price_data(transcript: str, product_description: str) -> dict:
    """Use Sarvam-M (OpenAI-compatible) to extract structured price data from a call transcript."""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        api_key=os.environ.get("SARVAM_API_KEY"),
        base_url=os.environ.get("LLM_BASE_URL", "https://api.sarvam.ai/v1"),
    )

    prompt = EXTRACTION_PROMPT.format(product_description=product_description, transcript=transcript)

    response = await client.chat.completions.create(
        model=os.environ.get("LLM_MODEL", "sarvam-m"),
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        max_tokens=500,
    )

    text = response.choices[0].message.content.strip()
    # Clean potential markdown fencing
    text = text.replace("```json", "").replace("```", "").strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.error(f"Failed to parse extraction: {text}")
        return {}
