"""
pipeline/store_discovery.py — Google Maps scraper + web search
=============================================================
Uses Playwright to scrape Google Maps for nearby stores, with
web search as a fallback/supplement. LLM structures raw data
into DiscoveredStore objects.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re

from anthropic import Anthropic

from .schemas import ProductRequirements, DiscoveredStore
from . import web_search

logger = logging.getLogger("pipeline.store_discovery")

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")


async def discover_stores(requirements: ProductRequirements) -> list[DiscoveredStore]:
    """Find relevant stores using Google Maps scraping + web search.

    Runs Maps scraping and web search in parallel, then uses LLM to
    deduplicate and structure results.

    Args:
        requirements: Product requirements with location info.

    Returns:
        List of DiscoveredStore objects.
    """
    product_type = requirements.product_type
    location = requirements.location

    if not location:
        logger.warning("No location provided, using web search only")
        raw_stores = await _web_search_stores(product_type, "India")
        return _structure_stores(raw_stores, location)

    # Run Maps scraping and web search in parallel
    logger.info(f"Searching Google Maps + web for {product_type} in {location}...")
    maps_task = _scrape_google_maps(product_type, location)
    web_task = _web_search_stores(product_type, location)

    maps_results, web_results = await asyncio.gather(
        maps_task, web_task, return_exceptions=True
    )

    # Handle exceptions gracefully
    if isinstance(maps_results, Exception):
        logger.warning(f"Maps scraping failed: {maps_results}")
        maps_results = []
    if isinstance(web_results, Exception):
        logger.warning(f"Web search for stores failed: {web_results}")
        web_results = []

    all_raw = maps_results + web_results

    if not all_raw:
        logger.warning("No stores found from any source")
        return []

    logger.info(f"Deduplicating {len(all_raw)} raw results with LLM...")
    return await _deduplicate_and_structure(all_raw, location, product_type)


async def _scrape_google_maps(product_type: str, location: str) -> list[dict]:
    """Scrape Google Maps for stores selling the product near the location."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning("Playwright not installed, skipping Maps scraping")
        return []

    query = f"{product_type} shops near {location}"
    logger.info(f"Scraping Google Maps: '{query}'")
    stores = []

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()

            # Navigate to Google Maps search
            maps_url = f"https://www.google.com/maps/search/{query.replace(' ', '+')}"
            await page.goto(maps_url, wait_until="domcontentloaded", timeout=15000)

            # Wait for results to render after DOM load
            await page.wait_for_timeout(3000)

            # Scroll the results panel to load more
            results_panel = page.locator('[role="feed"]')
            if await results_panel.count() > 0:
                for _ in range(2):
                    await results_panel.evaluate("el => el.scrollTop = el.scrollHeight")
                    await page.wait_for_timeout(1000)

            # Extract store cards
            cards = page.locator('[role="feed"] > div > div > a')
            count = await cards.count()
            logger.info(f"Found {count} store cards on Maps")

            for i in range(min(count, 8)):
                try:
                    card = cards.nth(i)
                    aria_label = await card.get_attribute("aria-label") or ""

                    # Click to get details
                    await card.click()
                    await page.wait_for_timeout(1000)

                    # Extract details from the side panel
                    name = aria_label
                    address = ""
                    phone = None
                    rating = None
                    review_count = None

                    # Try to get address
                    addr_el = page.locator('[data-item-id="address"]')
                    if await addr_el.count() > 0:
                        address = (await addr_el.first.text_content() or "").strip()

                    # Try to get phone
                    phone_el = page.locator('[data-item-id^="phone:"]')
                    if await phone_el.count() > 0:
                        phone = (await phone_el.first.text_content() or "").strip()

                    # Try to get rating
                    rating_el = page.locator('[role="img"][aria-label*="star"]')
                    if await rating_el.count() > 0:
                        rating_text = await rating_el.first.get_attribute("aria-label") or ""
                        rating_match = re.search(r"([\d.]+)\s*star", rating_text)
                        if rating_match:
                            rating = float(rating_match.group(1))

                    # Try to get review count
                    review_el = page.locator('span[aria-label*="review"]')
                    if await review_el.count() > 0:
                        review_text = await review_el.first.get_attribute("aria-label") or ""
                        review_match = re.search(r"([\d,]+)\s*review", review_text)
                        if review_match:
                            review_count = int(review_match.group(1).replace(",", ""))

                    if name:
                        stores.append({
                            "name": name,
                            "address": address,
                            "phone": phone,
                            "rating": rating,
                            "review_count": review_count,
                            "source": "google_maps",
                        })
                except Exception as e:
                    logger.debug(f"Failed to extract card {i}: {e}")
                    continue

            await browser.close()

    except Exception as e:
        logger.warning(f"Google Maps scraping failed: {e}")

    logger.info(f"Maps scraping returned {len(stores)} stores")
    return stores


async def _web_search_stores(product_type: str, location: str) -> list[dict]:
    """Search web for store recommendations."""
    queries = [
        f"best {product_type} shops in {location}",
        f"{product_type} dealer store {location} phone number",
    ]

    all_results = []
    for query in queries:
        results = await web_search.search(query, max_results=5)
        for r in results:
            all_results.append({
                "name": r.get("title", ""),
                "address": "",
                "phone": None,
                "rating": None,
                "review_count": None,
                "snippet": r.get("snippet", ""),
                "url": r.get("url", ""),
                "source": "web_search",
            })

    logger.info(f"Web search returned {len(all_results)} store results")
    return all_results


async def _deduplicate_and_structure(
    raw_stores: list[dict], location: str, product_type: str
) -> list[DiscoveredStore]:
    """Use LLM to deduplicate and structure raw store data."""
    client = Anthropic()

    prompt = f"""Given these raw store results for "{product_type}" shops near "{location}", deduplicate and structure them into a clean list.

Raw data:
{json.dumps(raw_stores, indent=2, ensure_ascii=False)}

Output a JSON array of stores. For each store, extract:
- name: Clean store name
- address: Full address if available
- phone: Phone number if available (in +91XXXXXXXXXX format if Indian)
- rating: Numeric rating if available
- review_count: Number of reviews if available
- area: The area/neighborhood of the store
- city: The city
- nearby_area: A nearby residential area (for the voice agent to say "I live near...")
- source: "google_maps" or "web_search"

Rules:
- Remove duplicate stores (same store, different sources)
- Prefer Google Maps data when there's a duplicate
- Only include actual stores/dealers, not news articles or blog posts
- Maximum 10 stores
- If a store seems irrelevant to {product_type}, exclude it

Output ONLY the JSON array, nothing else."""

    response = await asyncio.to_thread(
        client.messages.create,
        model=CLAUDE_MODEL,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text.strip()

    # Extract JSON array from response
    try:
        # Try to parse directly
        stores_data = json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON array in the text
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            try:
                stores_data = json.loads(match.group())
            except json.JSONDecodeError:
                logger.warning("Failed to parse LLM store structuring output")
                return _structure_stores(raw_stores, location)
        else:
            logger.warning("No JSON array found in LLM response")
            return _structure_stores(raw_stores, location)

    return [DiscoveredStore.from_dict(s) for s in stores_data]


def _structure_stores(raw_stores: list[dict], location: str) -> list[DiscoveredStore]:
    """Fallback: structure raw stores without LLM."""
    stores = []
    seen_names = set()
    for raw in raw_stores:
        name = raw.get("name", "").strip()
        if not name or name.lower() in seen_names:
            continue
        seen_names.add(name.lower())

        # Extract city from location
        parts = [p.strip() for p in location.split(",")]
        city = parts[-1] if len(parts) > 1 else location
        area = parts[0] if len(parts) > 1 else ""

        stores.append(DiscoveredStore(
            name=name,
            address=raw.get("address", ""),
            phone=raw.get("phone"),
            rating=raw.get("rating"),
            review_count=raw.get("review_count"),
            area=area,
            city=city,
            nearby_area=area,
            source=raw.get("source", "web_search"),
        ))

    return stores[:10]
