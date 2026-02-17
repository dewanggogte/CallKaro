"""
pipeline/web_search.py — Web search wrapper (async)
====================================================
Uses the ddgs library (DuckDuckGo Search) for reliable web search.
No API key needed.
"""

import asyncio
import logging

logger = logging.getLogger("pipeline.web_search")


async def search(query: str, max_results: int = 5) -> list[dict]:
    """Search the web and return structured results.

    Args:
        query: Search query string.
        max_results: Maximum number of results to return.

    Returns:
        List of dicts with keys: title, url, snippet.
    """
    return await asyncio.to_thread(_search_sync, query, max_results)


def _search_sync(query: str, max_results: int) -> list[dict]:
    """Synchronous web search using ddgs."""
    try:
        from ddgs import DDGS
        raw = DDGS().text(query, max_results=max_results)
        results = [
            {
                "title": r.get("title", ""),
                "url": r.get("href", ""),
                "snippet": r.get("body", ""),
            }
            for r in raw
        ]
        logger.info(f"Search '{query}' returned {len(results)} results")
        return results
    except Exception as e:
        logger.error(f"Search failed for '{query}': {e}")
        return []
