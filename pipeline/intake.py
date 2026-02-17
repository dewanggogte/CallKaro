"""
pipeline/intake.py — Text chat agent for product requirement extraction
=======================================================================
Multi-turn conversation using Anthropic SDK to understand what product
the user wants to buy and extract structured ProductRequirements.
"""

from __future__ import annotations

import json
import logging
import os
import re

from anthropic import Anthropic

from .schemas import ProductRequirements

logger = logging.getLogger("pipeline.intake")

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

SYSTEM_PROMPT = """You are a friendly shopping assistant that helps users figure out exactly what product they want to buy. You speak naturally and ask clarifying questions.

Your job is to extract these details through conversation:
1. Product type (e.g., AC, washing machine, laptop, refrigerator)
2. Specific category/model (e.g., 1.5 ton split AC, front load 7kg washing machine)
3. Brand preference (if any)
4. Key specs they care about
5. Budget range
6. Location (city and area — needed to find nearby stores)
7. Any other preferences (energy efficient, quiet, specific features)

Guidelines:
- Ask ONE question at a time. Don't overwhelm the user.
- Be conversational, not like a form. Use the user's language style.
- If the user gives a lot of info upfront, don't re-ask what they already told you.
- Location is important — always ask if not provided.
- Budget is helpful but optional — don't push if user is unsure.
- After 2-4 exchanges, when you have enough info, output the requirements.

When you have enough information to proceed, include a JSON block in your response wrapped in <requirements> tags:

<requirements>
{
  "product_type": "AC",
  "category": "1.5 ton 5 star inverter split AC",
  "brand_preference": "Samsung" or null,
  "specs": {"tonnage": "1.5", "star_rating": "5", "type": "inverter split"},
  "budget_range": [30000, 45000] or null,
  "location": "Koramangala, Bangalore",
  "preferences": ["energy efficient", "quiet operation"]
}
</requirements>

Include a natural confirmation message along with the requirements block, like:
"Got it! So you're looking for a 1.5 ton 5 star inverter split AC around 30-45K in Koramangala. Let me research this and find the best stores for you."
"""

_REQUIREMENTS_RE = re.compile(r"<requirements>\s*(.*?)\s*</requirements>", re.DOTALL)


class IntakeAgent:
    """Multi-turn text chat agent for extracting product requirements."""

    def __init__(self):
        self.client = Anthropic()
        self.messages: list[dict] = []
        self.requirements: ProductRequirements | None = None
        self.done = False

    def chat(self, user_message: str) -> dict:
        """Process a user message and return the agent's response.

        Returns:
            dict with keys: response (str), done (bool), requirements (dict|None)
        """
        self.messages.append({"role": "user", "content": user_message})

        response = self.client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=self.messages,
        )

        assistant_text = response.content[0].text
        self.messages.append({"role": "assistant", "content": assistant_text})

        # Check for requirements extraction
        match = _REQUIREMENTS_RE.search(assistant_text)
        if match:
            try:
                req_data = json.loads(match.group(1))
                self.requirements = ProductRequirements.from_dict(req_data)
                self.done = True
                logger.info(f"Intake complete: {self.requirements.product_type} — {self.requirements.category}")
            except (json.JSONDecodeError, Exception) as e:
                logger.warning(f"Failed to parse requirements JSON: {e}")

        # Strip the requirements tags from the displayed response
        display_text = _REQUIREMENTS_RE.sub("", assistant_text).strip()

        return {
            "response": display_text,
            "done": self.done,
            "requirements": self.requirements.to_dict() if self.requirements else None,
        }
