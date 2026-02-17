"""
pipeline/session.py — Pipeline orchestrator
============================================
PipelineSession coordinates all phases: intake → research + discovery
(parallel) → call planning → call execution → analysis.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path

from livekit.api import LiveKitAPI, AccessToken, VideoGrants
from livekit.protocol.agent_dispatch import CreateAgentDispatchRequest

from .schemas import (
    ProductRequirements, ResearchOutput, DiscoveredStore,
    CallResult, ComparisonResult,
)
from .intake import IntakeAgent
from . import research as research_module
from . import store_discovery
from . import prompt_builder
from .analysis import compare_stores

logger = logging.getLogger("pipeline.session")

LIVEKIT_URL = os.environ.get("LIVEKIT_URL", "")
LIVEKIT_API_KEY = os.environ.get("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.environ.get("LIVEKIT_API_SECRET", "")


# Track all active sessions for log routing (supports concurrent sessions)
_active_sessions: dict[int, PipelineSession] = {}  # id(session) → session


class SessionLogHandler(logging.Handler):
    """Routes pipeline.* log records into all active PipelineSession event lists."""

    def emit(self, record):
        for session in list(_active_sessions.values()):
            name = record.name  # e.g. "pipeline.research", "pipeline.store_discovery"
            phase = name.replace("pipeline.", "") if name.startswith("pipeline.") else name
            session.add_event(phase, record.getMessage(), record.levelname.lower())


# Install the handler once on the "pipeline" logger
_session_handler = SessionLogHandler()
_session_handler.setLevel(logging.DEBUG)
_pipeline_logger = logging.getLogger("pipeline")
_pipeline_logger.addHandler(_session_handler)
_pipeline_logger.setLevel(logging.DEBUG)


class PipelineSession:
    """Orchestrates the full product research pipeline."""

    def __init__(self, session_id: str | None = None):
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.created_at = datetime.now()
        self.state = "intake"  # intake → researching → ready → calling → analyzing → done

        # Phase outputs
        self.intake_agent = IntakeAgent()
        self.requirements: ProductRequirements | None = None
        self.research: ResearchOutput | None = None
        self.stores: list[DiscoveredStore] = []
        self.call_results: list[CallResult] = []
        self.comparison: ComparisonResult | None = None

        # Track active calls
        self._active_rooms: dict[int, str] = {}  # store_index → room_name

        # Event log for frontend visibility
        self.events: list[dict] = []

        # Cached research result for non-blocking retrieval
        self._research_result: dict | None = None
        self._research_error: str | None = None
        self._research_thread_started = False

    def add_event(self, phase: str, message: str, level: str = "info"):
        """Append a pipeline event for real-time frontend display."""
        self.events.append({
            "idx": len(self.events),
            "time": datetime.now().strftime("%H:%M:%S"),
            "phase": phase,
            "message": message,
            "level": level,
        })

    def _activate(self):
        """Register this session for log capture (supports concurrent sessions)."""
        _active_sessions[id(self)] = self

    def _deactivate(self):
        """Unregister this session from log capture."""
        _active_sessions.pop(id(self), None)

    def chat(self, message: str) -> dict:
        """Intake phase: process user message, return response + status.

        Returns:
            dict with keys: response, done, requirements
        """
        if self.state != "intake":
            return {
                "response": "Intake is already complete. Proceed to research.",
                "done": True,
                "requirements": self.requirements.to_dict() if self.requirements else None,
            }

        self._activate()
        try:
            self.add_event("intake", f"User: {message[:100]}")
            result = self.intake_agent.chat(message)
            self.add_event("intake", f"Agent: {result['response'][:100]}")

            if result["done"] and self.intake_agent.requirements:
                self.requirements = self.intake_agent.requirements
                self.state = "researching"
                req = self.requirements
                self.add_event("intake", f"Requirements extracted: {req.product_type} / {req.category} in {req.location}")
        finally:
            self._deactivate()

        return result

    async def research_and_discover(self) -> dict:
        """Run research + store discovery in parallel.

        Stores the result on self._research_result for non-blocking retrieval.

        Returns:
            dict with research and stores data.
        """
        if not self.requirements:
            return {"error": "Requirements not yet extracted. Complete intake first."}

        self._activate()
        try:
            self._research_thread_started = True
            self.state = "researching"
            self.add_event("research", "Starting product research...")
            self.add_event("store_discovery", "Starting store discovery...")

            # Run both in parallel
            research_result, stores_result = await asyncio.gather(
                research_module.research_product(self.requirements),
                store_discovery.discover_stores(self.requirements),
            )

            self.research = research_result
            self.stores = stores_result
            self.state = "ready"

            self.add_event("research",
                f"Done — {len(self.research.questions_to_ask)} questions, "
                f"price range: {self.research.market_price_range}")
            self.add_event("store_discovery",
                f"Done — {len(self.stores)} stores found")
            for s in self.stores[:5]:
                self.add_event("store_discovery", f"  {s.name} ({s.area}) [{s.source}]")

            result = {
                "research": self.research.to_dict(),
                "stores": [s.to_dict() for s in self.stores],
            }
            self._research_result = result
            return result
        except Exception as e:
            self._research_error = str(e)
            self.add_event("research", f"Error: {e}", "error")
            raise
        finally:
            self._deactivate()

    async def start_call(self, store_index: int) -> dict:
        """Build prompt, dispatch voice agent, return LiveKit token.

        Args:
            store_index: Index into self.stores list.

        Returns:
            dict with token, url, room for WebRTC connection.
        """
        if not self.research or not self.stores:
            return {"error": "Research and discovery not complete yet."}

        if store_index < 0 or store_index >= len(self.stores):
            return {"error": f"Invalid store index: {store_index}"}

        self._activate()
        try:
            store = self.stores[store_index]
            self.state = "calling"

            self.add_event("call", f"Building prompt for store '{store.name}'...")

            # Build the dynamic prompt
            instructions = prompt_builder.build_prompt(
                self.requirements, self.research, store
            )

            # Generate greeting using casual product name
            greeting = prompt_builder.build_greeting(self.requirements, store)
            product_desc = self.requirements.category or self.requirements.product_type

            # Create room and dispatch agent
            room_name = f"pipeline-{self.session_id}-{store_index}-{uuid.uuid4().hex[:6]}"
            user_identity = f"user-{uuid.uuid4().hex[:6]}"

            token = (
                AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
                .with_identity(user_identity)
                .with_name("Browser User")
                .with_grants(VideoGrants(
                    room_join=True,
                    room=room_name,
                    can_publish=True,
                    can_subscribe=True,
                ))
                .to_jwt()
            )

            self.add_event("call", f"Dispatching agent to room '{room_name}'...")

            # Dispatch the agent with the dynamic prompt
            lk = LiveKitAPI()
            try:
                await lk.agent_dispatch.create_dispatch(
                    CreateAgentDispatchRequest(
                        agent_name="price-agent",
                        room=room_name,
                        metadata=json.dumps({
                            "store_name": store.name,
                            "product_description": product_desc,
                            "nearby_area": store.nearby_area or store.area,
                            "instructions_override": instructions,
                            "greeting": greeting,
                            "pipeline_session": self.session_id,
                            "topic_keywords": self.research.topic_keywords,
                        }),
                    )
                )
            finally:
                await lk.aclose()

            self._active_rooms[store_index] = room_name
            self.add_event("call", f"Agent dispatched for '{store.name}' — waiting for connection")

            logger.info(f"Call dispatched for store '{store.name}' in room '{room_name}'")
        finally:
            self._deactivate()

        return {
            "token": token,
            "url": LIVEKIT_URL,
            "room": room_name,
            "store": store.to_dict(),
        }

    def record_call_result(self, store_index: int, transcript_path: str,
                           extracted_data: dict, topics_covered: list[str],
                           quality_score: float):
        """Record the result of a completed call."""
        if store_index < 0 or store_index >= len(self.stores):
            return

        store = self.stores[store_index]
        self.call_results.append(CallResult(
            store=store,
            transcript_path=transcript_path,
            extracted_data=extracted_data,
            topics_covered=topics_covered,
            quality_score=quality_score,
        ))

    def _collect_call_results_from_transcripts(self):
        """Scan transcripts/ for calls from this session and populate call_results."""
        transcripts_dir = Path(__file__).parent.parent / "transcripts"
        if not transcripts_dir.exists():
            return

        for store_index, room_name in self._active_rooms.items():
            # Skip if we already have a result for this store
            if any(r.store.name == self.stores[store_index].name for r in self.call_results):
                continue

            # Find transcript matching this room
            for f in sorted(transcripts_dir.glob("*.json"), reverse=True):
                if f.suffix == ".json" and ".analysis" not in f.name:
                    try:
                        data = json.loads(f.read_text())
                        if data.get("room") == room_name and len(data.get("messages", [])) > 1:
                            # Found the transcript — check for analysis
                            analysis_file = f.with_suffix(".analysis.json")
                            quality_score = 0.0
                            topics_covered = []
                            extracted_data = {}
                            if analysis_file.exists():
                                analysis = json.loads(analysis_file.read_text())
                                quality_score = analysis.get("overall_score", 0.0)
                                topics_covered = analysis.get("topics_covered", [])

                            # Include transcript messages so comparison LLM can extract real data
                            messages = data.get("messages", [])
                            extracted_data = {"transcript": messages}

                            store = self.stores[store_index]
                            self.call_results.append(CallResult(
                                store=store,
                                transcript_path=str(f),
                                extracted_data=extracted_data,
                                topics_covered=topics_covered,
                                quality_score=quality_score,
                            ))
                            logger.info(
                                f"Collected call result for '{store.name}': "
                                f"score={quality_score}, topics={topics_covered}"
                            )
                            break
                    except Exception as e:
                        logger.debug(f"Skipping transcript {f.name}: {e}")

    async def analyze(self) -> dict:
        """Cross-store comparison after all calls.

        Auto-collects call results from saved transcripts if not already recorded.

        Returns:
            dict with comparison results.
        """
        self._activate()
        try:
            self.add_event("analysis", "Starting analysis...")

            # Auto-collect from transcripts if we have active rooms but no results
            if not self.call_results and self._active_rooms:
                self.add_event("analysis", f"Collecting transcripts from {len(self._active_rooms)} rooms...")
                self._collect_call_results_from_transcripts()
                self.add_event("analysis", f"Found {len(self.call_results)} call results")

            if not self.call_results:
                self.add_event("analysis", "No call results found", "warning")
                return {"error": "No call results to analyze. Make sure calls have completed and transcripts were saved."}

            self.state = "analyzing"
            product_desc = self.requirements.category or self.requirements.product_type

            for cr in self.call_results:
                self.add_event("analysis", f"  {cr.store.name}: score={cr.quality_score:.2f}, topics={cr.topics_covered}")

            self.comparison = await compare_stores(self.call_results, product_desc)
            self.state = "done"

            self.add_event("analysis", f"Done — recommended: {self.comparison.recommended_store}")
            logger.info(f"Analysis complete. Recommended: {self.comparison.recommended_store}")
        finally:
            self._deactivate()

        return self.comparison.to_dict()

    def get_status(self) -> dict:
        """Return current pipeline state."""
        return {
            "session_id": self.session_id,
            "state": self.state,
            "created_at": self.created_at.isoformat(),
            "has_requirements": self.requirements is not None,
            "store_count": len(self.stores),
            "calls_completed": len(self.call_results),
            "has_comparison": self.comparison is not None,
        }

    def is_expired(self, max_age_seconds: int = 3600) -> bool:
        """Check if session has expired."""
        elapsed = (datetime.now() - self.created_at).total_seconds()
        return elapsed > max_age_seconds
