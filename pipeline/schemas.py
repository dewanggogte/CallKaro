"""
pipeline/schemas.py — Data contracts between pipeline phases
=============================================================
Dataclasses defining the structured data passed between intake,
research, store discovery, call execution, and analysis phases.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ProductRequirements:
    """Extracted from intake chat — what the user wants to buy."""
    product_type: str                          # "AC", "washing machine", "laptop"
    category: str                              # "1.5 ton split AC", "front load 7kg"
    brand_preference: str | None = None
    specs: dict[str, str] = field(default_factory=dict)   # {"tonnage": "1.5", "star_rating": "5"}
    budget_range: tuple[int, int] | None = None
    location: str = ""                         # "Koramangala, Bangalore"
    preferences: list[str] = field(default_factory=list)  # ["energy efficient", "quiet"]

    def to_dict(self) -> dict:
        return {
            "product_type": self.product_type,
            "category": self.category,
            "brand_preference": self.brand_preference,
            "specs": self.specs,
            "budget_range": list(self.budget_range) if self.budget_range else None,
            "location": self.location,
            "preferences": self.preferences,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ProductRequirements":
        budget = d.get("budget_range")
        if budget and isinstance(budget, list) and len(budget) == 2:
            lo = int(budget[0]) if budget[0] is not None else None
            hi = int(budget[1]) if budget[1] is not None else None
            budget = (lo, hi) if (lo is not None or hi is not None) else None
        else:
            budget = None
        return cls(
            product_type=d.get("product_type", ""),
            category=d.get("category", ""),
            brand_preference=d.get("brand_preference"),
            specs=d.get("specs", {}),
            budget_range=budget,
            location=d.get("location", ""),
            preferences=d.get("preferences", []),
        )


@dataclass
class ResearchOutput:
    """Product research results from LLM + web search."""
    product_summary: str = ""
    market_price_range: tuple[int, int] | None = None
    questions_to_ask: list[str] = field(default_factory=list)
    topics_to_cover: list[str] = field(default_factory=list)
    topic_keywords: dict[str, list[str]] = field(default_factory=dict)
    important_notes: list[str] = field(default_factory=list)
    competing_products: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "product_summary": self.product_summary,
            "market_price_range": list(self.market_price_range) if self.market_price_range else None,
            "questions_to_ask": self.questions_to_ask,
            "topics_to_cover": self.topics_to_cover,
            "topic_keywords": self.topic_keywords,
            "important_notes": self.important_notes,
            "competing_products": self.competing_products,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ResearchOutput":
        pr = d.get("market_price_range")
        if pr and isinstance(pr, list) and len(pr) == 2:
            pr = (pr[0], pr[1])
        else:
            pr = None
        return cls(
            product_summary=d.get("product_summary", ""),
            market_price_range=pr,
            questions_to_ask=d.get("questions_to_ask", []),
            topics_to_cover=d.get("topics_to_cover", []),
            topic_keywords=d.get("topic_keywords", {}),
            important_notes=d.get("important_notes", []),
            competing_products=d.get("competing_products", []),
        )


@dataclass
class DiscoveredStore:
    """A store found via Google Maps scraping or web search."""
    name: str
    address: str = ""
    phone: str | None = None
    rating: float | None = None
    review_count: int | None = None
    area: str = ""
    city: str = ""
    nearby_area: str = ""
    source: str = "web_search"  # "google_maps" or "web_search"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "address": self.address,
            "phone": self.phone,
            "rating": self.rating,
            "review_count": self.review_count,
            "area": self.area,
            "city": self.city,
            "nearby_area": self.nearby_area,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DiscoveredStore":
        return cls(
            name=d.get("name", ""),
            address=d.get("address", ""),
            phone=d.get("phone"),
            rating=d.get("rating"),
            review_count=d.get("review_count"),
            area=d.get("area", ""),
            city=d.get("city", ""),
            nearby_area=d.get("nearby_area", ""),
            source=d.get("source", "web_search"),
        )


@dataclass
class CallResult:
    """Result from a single store call."""
    store: DiscoveredStore
    transcript_path: str = ""
    extracted_data: dict = field(default_factory=dict)  # {price, warranty, delivery, etc.}
    topics_covered: list[str] = field(default_factory=list)
    quality_score: float = 0.0

    def to_dict(self) -> dict:
        return {
            "store": self.store.to_dict(),
            "transcript_path": self.transcript_path,
            "extracted_data": self.extracted_data,
            "topics_covered": self.topics_covered,
            "quality_score": self.quality_score,
        }


@dataclass
class ComparisonResult:
    """Cross-store comparison after all calls."""
    recommended_store: str = ""
    ranking: list[dict] = field(default_factory=list)  # [{store_name, total_cost, pros, cons}]
    summary: str = ""
    max_savings: str | None = None  # e.g. "₹2,000"

    def to_dict(self) -> dict:
        d = {
            "recommended_store": self.recommended_store,
            "ranking": self.ranking,
            "summary": self.summary,
        }
        if self.max_savings:
            d["max_savings"] = self.max_savings
        return d
