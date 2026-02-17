"""
pipeline/analysis.py — Cross-store comparison after all calls
=============================================================
LLM agent that compares CallResults from multiple stores and
produces a ranked ComparisonResult with recommendation.
"""

import asyncio
import json
import logging
import os

from anthropic import Anthropic

from .schemas import CallResult, ComparisonResult

logger = logging.getLogger("pipeline.analysis")

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")


async def compare_stores(
    call_results: list[CallResult],
    product_description: str,
) -> ComparisonResult:
    """Compare quotes from multiple stores and recommend the best option.

    Args:
        call_results: List of CallResult from completed store calls.
        product_description: What the user is buying.

    Returns:
        ComparisonResult with ranking and recommendation.
    """
    if not call_results:
        return ComparisonResult(
            summary="No store calls were completed.",
        )

    # Build context for LLM — include actual conversation transcripts
    stores_context = []
    for r in call_results:
        transcript = r.extracted_data.get("transcript", [])
        conversation = _format_transcript(transcript)
        stores_context.append({
            "store_name": r.store.name,
            "area": r.store.area,
            "conversation": conversation,
            "topics_covered": r.topics_covered,
            "quality_score": r.quality_score,
        })

    single = len(call_results) == 1
    client = Anthropic()

    prompt = f"""{"Analyze this store call" if single else "Compare these store calls"} for "{product_description}".

{"Store call:" if single else "Store calls:"}
{json.dumps(stores_context, indent=2, ensure_ascii=False)}

Extract from each conversation:
1. Price quoted (exact number from shopkeeper)
2. Installation — included or extra charge
3. Delivery — cost and timeline
4. Warranty details
5. Any other notable information (exchange, AMC, etc.)

Output your analysis as JSON:
{{
  "recommended_store": "Name of the best store",
  "ranking": [
    {{
      "store_name": "Store Name",
      "rank": 1,
      "base_price": "exact price quoted (e.g. '₹55,000') or 'Not quoted'",
      "installation_cost": "charge if mentioned, else 'Included' or 'Not mentioned'",
      "delivery_cost": "charge and timeline (e.g. 'Free, next day') or 'Not mentioned'",
      "warranty": "warranty details (e.g. '1 year + 5 year condenser') or 'Not mentioned'",
      "total_estimated_cost": "total cost including everything (e.g. '₹57,000')",
      "pros": ["list", "of", "advantages"],
      "cons": ["list", "of", "disadvantages"]
    }}
  ],
  "summary": "2-3 sentence natural language summary{' and recommendation' if not single else ''}",
  "max_savings": {"null" if single else '"savings amount or null"'}
}}

Output ONLY the JSON, nothing else."""

    response = await asyncio.to_thread(
        client.messages.create,
        model=CLAUDE_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        import re
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group())
            except json.JSONDecodeError:
                logger.warning("Failed to parse comparison JSON")
                return _fallback_comparison(call_results)
        else:
            logger.warning("No JSON found in comparison response")
            return _fallback_comparison(call_results)

    return ComparisonResult(
        recommended_store=data.get("recommended_store", ""),
        ranking=data.get("ranking", []),
        summary=data.get("summary", ""),
        max_savings=data.get("max_savings"),
    )


def _format_transcript(messages: list[dict]) -> str:
    """Format transcript messages into readable conversation text."""
    lines = []
    for m in messages:
        role = "Agent" if m.get("role") == "assistant" else "Shopkeeper"
        text = m.get("text", "")
        if text:
            lines.append(f"{role}: {text}")
    return "\n".join(lines) if lines else "(no conversation recorded)"


def _fallback_comparison(call_results: list[CallResult]) -> ComparisonResult:
    """Fallback comparison without LLM — rank by quality score."""
    sorted_results = sorted(call_results, key=lambda r: r.quality_score, reverse=True)
    ranking = []
    for i, r in enumerate(sorted_results):
        ranking.append({
            "store_name": r.store.name,
            "rank": i + 1,
            "quality_score": r.quality_score,
            "topics_covered": r.topics_covered,
        })

    best = sorted_results[0]
    return ComparisonResult(
        recommended_store=best.store.name,
        ranking=ranking,
        summary=f"Based on conversation quality, {best.store.name} "
                f"ranked highest with a score of {best.quality_score:.2f}.",
    )
