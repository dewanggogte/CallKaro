"""
pipeline/research.py — Product research with LLM + web search
=============================================================
Uses Anthropic tool-use API to research a product. The LLM decides
what to search, processes results, and outputs structured ResearchOutput.
"""

import asyncio
import json
import logging
import os

from anthropic import Anthropic

from .schemas import ProductRequirements, ResearchOutput
from . import web_search

logger = logging.getLogger("pipeline.research")

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

SYSTEM_PROMPT = """You are a product research assistant. Given a user's product requirements, research the product thoroughly and provide actionable intelligence for someone about to call local stores to negotiate prices.

Use the web_search tool to find:
1. Current market prices (MRP and typical street/online prices)
2. Key specs and features to look for
3. Common issues or things to watch out for
4. What questions to ask the shopkeeper
5. Competing products in the same range

After researching (1-2 searches maximum), output your findings as a JSON block wrapped in <research> tags:

<research>
{
  "product_summary": "Brief description of the product and its market position",
  "market_price_range": [lower_bound, upper_bound],
  "questions_to_ask": [
    "Best price kya doge?",
    "Installation free hai ya alag se?",
    "Warranty kitni hai?",
    "Delivery kitne din mein hogi?",
    "Exchange pe kuch milega kya?"
  ],
  "topics_to_cover": ["price", "warranty", "installation", "delivery", "exchange", "availability"],
  "topic_keywords": {
    "price": ["rate", "price", "kitna", "kitne", "hazaar", "rupay", "\\d{4,}"],
    "warranty": ["warranty", "guarantee", "saal ki"],
    "installation": ["install", "lagwa", "fitting", "pipe"],
    "delivery": ["deliver", "bhej", "ghar pe", "din mein", "kab tak"],
    "exchange": ["exchange", "purana", "old"]
  },
  "important_notes": ["List of buyer warnings or things to watch out for"],
  "competing_products": [
    {"name": "Product Name", "price_range": "30000-35000", "pros": "...", "cons": "..."}
  ]
}
</research>

Important:
- questions_to_ask should be in Romanized Hindi (the voice agent will ask these in Hindi)
- topic_keywords are regex patterns used for post-call analysis (keep them simple)
- Order questions_to_ask by priority: price first, then important details, then optional ones
- Include at least 5-7 questions covering price, warranty, installation, delivery
- market_price_range should be realistic street prices, not MRP
"""

SEARCH_TOOL = {
    "name": "web_search",
    "description": "Search Google for product information, prices, reviews, and specifications.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query to run on Google",
            }
        },
        "required": ["query"],
    },
}

import re
_RESEARCH_RE = re.compile(r"<research>\s*(.*?)\s*</research>", re.DOTALL)


def _format_budget(budget_range: tuple[int, int] | None) -> str:
    """Format budget range for LLM prompt, handling None values."""
    if not budget_range:
        return "Not specified"
    lo, hi = budget_range
    if lo and hi:
        return f"₹{lo:,} - ₹{hi:,}"
    elif hi:
        return f"Under ₹{hi:,}"
    elif lo:
        return f"Above ₹{lo:,}"
    return "Not specified"


async def research_product(requirements: ProductRequirements) -> ResearchOutput:
    """Research a product using LLM + web search tool use.

    Args:
        requirements: Extracted product requirements from intake.

    Returns:
        ResearchOutput with market data, questions, and analysis.
    """
    client = Anthropic()

    user_prompt = f"""Research this product for a buyer in India:

Product: {requirements.category}
Brand preference: {requirements.brand_preference or 'No preference'}
Budget: {_format_budget(requirements.budget_range)}
Location: {requirements.location}
Preferences: {', '.join(requirements.preferences) if requirements.preferences else 'None specified'}
Specs: {json.dumps(requirements.specs) if requirements.specs else 'Not specified'}

Research the current market prices, key features, and prepare questions for calling local stores. The buyer will call stores in Hindi to negotiate — generate questions in Romanized Hindi."""

    messages = [{"role": "user", "content": user_prompt}]

    # Tool-use loop: LLM searches, processes, then outputs research
    max_rounds = 3
    for round_num in range(max_rounds):
        logger.info(f"LLM round {round_num + 1}/{max_rounds}...")
        response = await asyncio.to_thread(
            client.messages.create,
            model=CLAUDE_MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=messages,
            tools=[SEARCH_TOOL],
        )

        # Check if we got a final text response with research output
        if response.stop_reason == "end_turn":
            text_blocks = [b.text for b in response.content if b.type == "text"]
            full_text = "\n".join(text_blocks)

            match = _RESEARCH_RE.search(full_text)
            if match:
                try:
                    data = json.loads(match.group(1))
                    logger.info(f"Research complete after {round_num + 1} rounds")
                    return ResearchOutput.from_dict(data)
                except (json.JSONDecodeError, Exception) as e:
                    logger.warning(f"Failed to parse research JSON: {e}")

            # If no research block found, add the response and ask for structured output
            messages.append({"role": "assistant", "content": response.content})
            if round_num < max_rounds - 1:
                messages.append({
                    "role": "user",
                    "content": "Please provide your research findings in the <research> JSON format as specified.",
                })
            continue

        # Handle tool use
        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if block.type == "tool_use" and block.name == "web_search":
                    query = block.input.get("query", "")
                    logger.info(f"Searching: \"{query}\"")
                    results = await web_search.search(query, max_results=5)
                    logger.info(f"Got {len(results)} results for \"{query[:50]}\"")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(results, ensure_ascii=False),
                    })

            messages.append({"role": "user", "content": tool_results})
            continue

        # Unexpected stop reason
        logger.warning(f"Unexpected stop_reason: {response.stop_reason}")
        break

    # Fallback: return minimal research output
    logger.warning("Research did not produce structured output, returning defaults")
    return ResearchOutput(
        product_summary=f"Research for {requirements.category}",
        questions_to_ask=[
            "Best price kya doge?",
            "Installation free hai ya alag se?",
            "Warranty kitni hai?",
            "Delivery kitne din mein hogi?",
            "Exchange pe kuch milega kya?",
        ],
        topics_to_cover=["price", "warranty", "installation", "delivery", "exchange"],
        topic_keywords={
            "price": [r"rate", r"price", r"kitna", r"kitne", r"hazaar", r"rupay", r"\d{4,}"],
            "warranty": [r"warranty", r"guarantee", r"saal ki"],
            "installation": [r"install", r"lagwa", r"fitting", r"pipe"],
            "delivery": [r"deliver", r"bhej", r"ghar pe", r"din mein", r"kab tak"],
            "exchange": [r"exchange", r"puran[ae]", r"old"],
        },
    )
