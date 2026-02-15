"""Live multi-turn scenario tests — call Claude Haiku 3.5 via Anthropic API.

These tests feed scripted shopkeeper messages to the real LLM and validate
that its responses satisfy all behavioral constraints. Non-deterministic by
design: they check RULES, not exact text.

Run with: pytest tests/test_scenario_live.py --live -v
"""

import os
import pytest
from anthropic import Anthropic

from call_analysis import ConstraintChecker, ConversationScorer
from tests.shopkeeper_scenarios import SCENARIOS
from agent_worker import DEFAULT_INSTRUCTIONS, _normalize_for_tts, _strip_think_tags, CLAUDE_MODEL


# ---------------------------------------------------------------------------
# Helpers & fixtures
# ---------------------------------------------------------------------------
def _call_claude(client: Anthropic, messages: list[dict], system: str) -> str:
    """Call Claude Haiku 3.5 and return normalized response text."""
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=300,
        temperature=0.7,
        system=system,
        messages=messages,
    )
    text = response.content[0].text
    text = _strip_think_tags(text)
    text = _normalize_for_tts(text)
    return text


def _build_system_prompt(store_name="Gupta Electronics",
                         ac_model="Samsung 1.5 Ton 5 Star Inverter Split AC"):
    greeting = f"Hello, yeh {store_name} hai? Aap log AC dealer ho?"
    return DEFAULT_INSTRUCTIONS + (
        f"\nPRODUCT: {ac_model}\nSTORE: {store_name}\n"
        f'\nNOTE: You have already greeted the shopkeeper with: "{greeting}"\n'
        "Do NOT repeat the greeting. Continue the conversation from the shopkeeper's response.\n"
    )


def _run_scenario(client, system_prompt, scenario_key, checker):
    """Play out a full multi-turn conversation and return messages + results."""
    scenario = SCENARIOS[scenario_key]
    messages = []
    all_results = []
    interrupt_turns = set(scenario.get("interrupt_after_turns", []))

    for i, shopkeeper_msg in enumerate(scenario["shopkeeper_turns"]):
        messages.append({"role": "user", "content": shopkeeper_msg})
        agent_response = _call_claude(client, messages, system_prompt)
        result = checker.check_all(agent_response)
        all_results.append(result)

        # Simulate interruption: truncate agent response and add [interrupted]
        if i in interrupt_turns:
            words = agent_response.split()
            truncated = " ".join(words[:max(len(words) // 2, 2)]) + " [interrupted]"
            messages.append({"role": "assistant", "content": truncated})
        else:
            messages.append({"role": "assistant", "content": agent_response})

    return messages, all_results


@pytest.fixture(scope="module")
def claude_client():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key or api_key == "test-dummy-key":
        pytest.skip("ANTHROPIC_API_KEY not set")
    return Anthropic(api_key=api_key)


@pytest.fixture
def system_prompt():
    return _build_system_prompt()


@pytest.fixture
def checker():
    return ConstraintChecker()


@pytest.fixture
def scorer():
    c = ConstraintChecker()
    return ConversationScorer(c)


# ---------------------------------------------------------------------------
# HARD constraint checks — must ALWAYS pass for any single response
# ---------------------------------------------------------------------------
def _assert_hard_constraints(result: dict):
    """Assert all hard constraints pass. Soft constraints are scored, not asserted."""
    hard = ['no_devanagari', 'no_action_markers', 'no_newlines',
            'no_english_translations', 'no_end_call_text', 'response_length']
    for name in hard:
        assert result['checks'].get(name, True), \
            f"Hard constraint '{name}' failed: {result['failures'].get(name, '')} | text: '{result['text'][:100]}'"


# ---------------------------------------------------------------------------
# TestSingleTurnConstraints — one shopkeeper msg → one Claude response
# ---------------------------------------------------------------------------
@pytest.mark.live
class TestSingleTurnConstraints:

    def test_cooperative_opener(self, claude_client, system_prompt, checker):
        messages = [{"role": "user", "content": "Haan ji, Samsung hai humare paas. Boliye."}]
        response = _call_claude(claude_client, messages, system_prompt)
        result = checker.check_all(response)
        _assert_hard_constraints(result)

    def test_price_echo(self, claude_client, system_prompt, checker):
        messages = [
            {"role": "user", "content": "Haan ji boliye."},
            {"role": "assistant", "content": "Bhai sahab, Samsung dedh ton ka paanch star inverter split AC ka rate bataaiye."},
            {"role": "user", "content": "Adtees hazaar ka hai."},
        ]
        response = _call_claude(claude_client, messages, system_prompt)
        result = checker.check_all(response)
        _assert_hard_constraints(result)
        # Agent should echo the price back (adtees or 38)
        resp_lower = response.lower()
        assert "adtees" in resp_lower or "38" in resp_lower, \
            f"Agent did not echo price 38000: '{response}'"

    def test_wrong_brand_redirect(self, claude_client, system_prompt, checker):
        messages = [
            {"role": "user", "content": "Samsung nahi hai humare paas. Sirf Voltas rakhte hain."},
        ]
        response = _call_claude(claude_client, messages, system_prompt)
        result = checker.check_all(response)
        _assert_hard_constraints(result)
        # Agent should stay on topic — mention AC or rate or Voltas
        resp_lower = response.lower()
        assert any(w in resp_lower for w in ["ac", "rate", "voltas", "price", "kitna"]), \
            f"Agent went off-topic: '{response}'"

    def test_question_reversal(self, claude_client, system_prompt, checker):
        messages = [
            {"role": "user", "content": "Haan hai Samsung. Purana AC hai kya? Kaun sa model hai?"},
        ]
        response = _call_claude(claude_client, messages, system_prompt)
        result = checker.check_all(response)
        _assert_hard_constraints(result)
        # Should NOT invent specific details
        ok, _ = checker.check_no_invented_details(response)
        assert ok, f"Agent invented details: '{response}'"

    def test_hold_request(self, claude_client, system_prompt, checker):
        messages = [{"role": "user", "content": "Ek minute ruko bhai."}]
        response = _call_claude(claude_client, messages, system_prompt)
        result = checker.check_all(response)
        _assert_hard_constraints(result)
        # Response should be short and patient
        assert len(response) < 100, f"Hold response too long ({len(response)} chars): '{response}'"

    def test_off_topic(self, claude_client, system_prompt, checker):
        messages = [
            {"role": "user", "content": "Arey bhai, aaj kal market mein bahut competition hai. Sab online kharid rahe hain."},
        ]
        response = _call_claude(claude_client, messages, system_prompt)
        result = checker.check_all(response)
        _assert_hard_constraints(result)
        # Agent should redirect — mention AC, price, Samsung, or rate
        resp_lower = response.lower()
        assert any(w in resp_lower for w in ["ac", "rate", "samsung", "price", "kitna", "dedh ton"]), \
            f"Agent did not redirect to AC: '{response}'"

    def test_exchange_refusal(self, claude_client, system_prompt, checker):
        messages = [
            {"role": "user", "content": "Haan ji boliye."},
            {"role": "assistant", "content": "Samsung dedh ton ka AC ka rate bataaiye."},
            {"role": "user", "content": "Chaalees hazaar. Exchange nahi karte hum bhai."},
        ]
        response = _call_claude(claude_client, messages, system_prompt)
        result = checker.check_all(response)
        _assert_hard_constraints(result)
        # Should NOT ask about exchange again
        resp_lower = response.lower()
        assert "exchange" not in resp_lower, \
            f"Agent asked about exchange after refusal: '{response}'"


# ---------------------------------------------------------------------------
# TestMultiTurnScenarios — full conversation playthrough
# ---------------------------------------------------------------------------
@pytest.mark.live
class TestMultiTurnScenarios:

    def test_cooperative_direct(self, claude_client, system_prompt, checker, scorer):
        messages, results = _run_scenario(claude_client, system_prompt, "cooperative_direct", checker)
        for r in results:
            _assert_hard_constraints(r)
        score = scorer.score_conversation(
            [{"role": m["role"], "text": m["content"]} for m in messages]
        )
        assert score['overall_score'] >= 0.6
        assert 'price' in score['topics_covered']

    def test_cooperative_price_bundle(self, claude_client, system_prompt, checker, scorer):
        messages, results = _run_scenario(claude_client, system_prompt, "cooperative_price_bundle", checker)
        for r in results:
            _assert_hard_constraints(r)
        score = scorer.score_conversation(
            [{"role": m["role"], "text": m["content"]} for m in messages]
        )
        assert score['overall_score'] >= 0.6

    def test_defensive_price_firm(self, claude_client, system_prompt, checker, scorer):
        messages, results = _run_scenario(claude_client, system_prompt, "defensive_price_firm", checker)
        for r in results:
            _assert_hard_constraints(r)
        # Agent should not repeat exact same negotiation text after rejection
        assistant_msgs = [m["content"] for m in messages if m["role"] == "assistant"]
        for i in range(1, len(assistant_msgs)):
            assert assistant_msgs[i] != assistant_msgs[i - 1], \
                f"Agent repeated same response: '{assistant_msgs[i][:80]}'"

    def test_defensive_warranty(self, claude_client, system_prompt, checker):
        messages, results = _run_scenario(claude_client, system_prompt, "defensive_warranty", checker)
        for r in results:
            _assert_hard_constraints(r)

    def test_wrong_brand(self, claude_client, system_prompt, checker):
        messages, results = _run_scenario(claude_client, system_prompt, "wrong_brand", checker)
        for r in results:
            _assert_hard_constraints(r)
        # After shopkeeper says "Samsung nahi", agent should NOT keep insisting on Samsung
        assistant_msgs = [m["content"] for m in messages if m["role"] == "assistant"]
        # Skip first response (may mention Samsung before learning)
        for resp in assistant_msgs[1:]:
            samsung_count = resp.lower().count("samsung")
            assert samsung_count == 0, \
                f"Agent insisted on Samsung after told unavailable: '{resp[:100]}'"

    def test_evasive_nonsensical(self, claude_client, system_prompt, checker):
        messages, results = _run_scenario(claude_client, system_prompt, "evasive_nonsensical", checker)
        for r in results:
            _assert_hard_constraints(r)

    def test_question_reversal(self, claude_client, system_prompt, checker):
        messages, results = _run_scenario(claude_client, system_prompt, "question_reversal", checker)
        for r in results:
            _assert_hard_constraints(r)
            # Should not invent specific personal details
            ok, reason = checker.check_no_invented_details(r['text'])
            assert ok, f"Invented details in response to personal question: {reason}"

    def test_hold_wait(self, claude_client, system_prompt, checker):
        messages, results = _run_scenario(claude_client, system_prompt, "hold_wait", checker)
        for r in results:
            _assert_hard_constraints(r)
        # First response (to "ek minute ruko") should be short and patient
        first_response = results[0]['text']
        assert len(first_response) < 120, \
            f"Response to 'hold' too long ({len(first_response)} chars): '{first_response}'"

    def test_exchange_refusal(self, claude_client, system_prompt, checker, scorer):
        messages, results = _run_scenario(claude_client, system_prompt, "exchange_refusal", checker)
        for r in results:
            _assert_hard_constraints(r)
        score = scorer.score_conversation(
            [{"role": m["role"], "text": m["content"]} for m in messages]
        )
        assert score['overall_score'] >= 0.6

    def test_frequent_interruptions(self, claude_client, system_prompt, checker):
        messages, results = _run_scenario(claude_client, system_prompt, "frequent_interruptions", checker)
        for r in results:
            _assert_hard_constraints(r)
        # Check that agent doesn't repeat the interrupted text
        for i, msg in enumerate(messages):
            if msg["role"] == "assistant" and "[interrupted]" in msg["content"]:
                interrupted_words = set(msg["content"].replace("[interrupted]", "").lower().split())
                # Find next assistant response
                for j in range(i + 1, len(messages)):
                    if messages[j]["role"] == "assistant":
                        next_words = set(messages[j]["content"].lower().split())
                        content_overlap = interrupted_words & next_words - {
                            "ka", "ke", "ki", "hai", "ji", "bhai", "haan", "achha", "aur"
                        }
                        assert len(content_overlap) < 5, \
                            f"Agent repeated interrupted text. Overlap: {content_overlap}"
                        break


# ---------------------------------------------------------------------------
# TestNonDeterminismResilience — run same scenario 3x, check stability
# ---------------------------------------------------------------------------
@pytest.mark.live
class TestNonDeterminismResilience:

    def _run_n_times(self, client, system_prompt, scenario_key, checker, scorer, n=3):
        """Run a scenario n times and return list of overall scores."""
        scores = []
        for _ in range(n):
            messages, results = _run_scenario(client, system_prompt, scenario_key, checker)
            # Check hard constraints in every run
            for r in results:
                _assert_hard_constraints(r)
            score = scorer.score_conversation(
                [{"role": m["role"], "text": m["content"]} for m in messages]
            )
            scores.append(score['overall_score'])
        return scores

    def test_cooperative_stable(self, claude_client, system_prompt, checker, scorer):
        scores = self._run_n_times(claude_client, system_prompt, "cooperative_direct", checker, scorer)
        assert min(scores) >= 0.5, f"Score too low in some run: {scores}"

    def test_defensive_stable(self, claude_client, system_prompt, checker, scorer):
        scores = self._run_n_times(claude_client, system_prompt, "defensive_price_firm", checker, scorer)
        assert min(scores) >= 0.5, f"Score too low in some run: {scores}"

    def test_question_reversal_no_invented_details(self, claude_client, system_prompt, checker, scorer):
        """Run question_reversal 3x — agent should never invent details."""
        for _ in range(3):
            messages, results = _run_scenario(
                claude_client, system_prompt, "question_reversal", checker
            )
            for r in results:
                _assert_hard_constraints(r)
                ok, reason = checker.check_no_invented_details(r['text'])
                assert ok, f"Invented details across runs: {reason}"
