"""Run all scenarios and print detailed analysis for prompt tuning.

Usage: ANTHROPIC_API_KEY=... python tests/run_scenario_analysis.py
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Must set env before importing agent_worker
os.environ.setdefault("SARVAM_API_KEY", "test-dummy-key")
os.environ.setdefault("LIVEKIT_URL", "ws://localhost:7880")
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "devsecret")

from anthropic import Anthropic
from call_analysis import ConstraintChecker, ConversationScorer
from tests.shopkeeper_scenarios import SCENARIOS
from agent_worker import DEFAULT_INSTRUCTIONS, _normalize_for_tts, _strip_think_tags, CLAUDE_MODEL


def _call_claude(client, messages, system):
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


def _build_system_prompt():
    store_name = "Gupta Electronics"
    product_description = "Samsung 1.5 Ton 5 Star Inverter Split AC"
    greeting = f"Hello, yeh {store_name} hai? {product_description} ke baare mein poochna tha."
    return DEFAULT_INSTRUCTIONS + (
        f"\nPRODUCT: {product_description}\nSTORE: {store_name}\n"
        f'\nNOTE: You have already greeted the shopkeeper with: "{greeting}"\n'
        "Do NOT repeat the greeting. Continue the conversation from the shopkeeper's response.\n"
    )


def run_all():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key or api_key == "test-dummy-key":
        print("ERROR: Set ANTHROPIC_API_KEY env var")
        sys.exit(1)

    client = Anthropic(api_key=api_key)
    system_prompt = _build_system_prompt()
    checker = ConstraintChecker()
    scorer = ConversationScorer(checker)

    all_responses = []  # collect every response for aggregate analysis

    for scenario_key, scenario in SCENARIOS.items():
        print(f"\n{'='*70}")
        print(f"SCENARIO: {scenario_key}")
        print(f"  {scenario['description']}")
        print(f"{'='*70}")

        messages = []
        turn_results = []
        interrupt_turns = set(scenario.get("interrupt_after_turns", []))

        for i, shopkeeper_msg in enumerate(scenario["shopkeeper_turns"]):
            messages.append({"role": "user", "content": shopkeeper_msg})
            agent_response = _call_claude(client, messages, system_prompt)
            result = checker.check_all(agent_response)
            turn_results.append(result)
            all_responses.append({"scenario": scenario_key, "turn": i, "text": agent_response, "result": result})

            print(f"\n  [SHOP {i+1}] {shopkeeper_msg}")
            print(f"  [AGENT]  {agent_response}")

            # Print failures
            if result['failures']:
                for name, reason in result['failures'].items():
                    print(f"    !! FAIL: {name} — {reason}")

            # Simulate interruption
            if i in interrupt_turns:
                words = agent_response.split()
                truncated = " ".join(words[:max(len(words) // 2, 2)]) + " [interrupted]"
                messages.append({"role": "assistant", "content": truncated})
                print(f"    [INTERRUPTED → '{truncated}']")
            else:
                messages.append({"role": "assistant", "content": agent_response})

        # Score the full conversation
        conv_messages = [{"role": m["role"], "text": m["content"]} for m in messages]
        score = scorer.score_conversation(conv_messages)

        print(f"\n  SCORE: {score['overall_score']:.3f}")
        print(f"    constraint: {score['constraint_score']:.3f}")
        print(f"    topics:     {score['topic_score']:.3f} — {score['topics_covered']}")
        print(f"    price_echo: {score['price_echo_score']:.3f}")
        print(f"    brevity:    {score['brevity_score']:.3f}")
        print(f"    repetition: {score['repetition_score']:.3f}")

    # Aggregate analysis
    print(f"\n{'='*70}")
    print("AGGREGATE ANALYSIS")
    print(f"{'='*70}")

    total = len(all_responses)
    failures_by_check = {}
    total_chars = []
    question_counts = []

    for r in all_responses:
        result = r['result']
        total_chars.append(len(r['text']))
        question_counts.append(r['text'].count('?'))
        for name, passed in result['checks'].items():
            if name not in failures_by_check:
                failures_by_check[name] = {'fail': 0, 'total': 0}
            failures_by_check[name]['total'] += 1
            if not passed:
                failures_by_check[name]['fail'] += 1

    print(f"\nTotal responses analyzed: {total}")
    print(f"\nAvg response length: {sum(total_chars)/len(total_chars):.0f} chars")
    print(f"Max response length: {max(total_chars)} chars")
    print(f"Avg questions per response: {sum(question_counts)/len(question_counts):.1f}")
    print(f"Max questions in single response: {max(question_counts)}")

    print(f"\nConstraint failure rates:")
    for name, data in sorted(failures_by_check.items()):
        rate = data['fail'] / data['total'] * 100
        status = "OK" if data['fail'] == 0 else f"FAIL ({data['fail']}/{data['total']})"
        print(f"  {name:30s}: {status:>20s} ({rate:.1f}%)")

    # Print all failures with context
    failures = [r for r in all_responses if r['result']['failures']]
    if failures:
        print(f"\n{'='*70}")
        print(f"ALL FAILURES ({len(failures)} responses with issues)")
        print(f"{'='*70}")
        for r in failures:
            print(f"\n  Scenario: {r['scenario']}, Turn: {r['turn']}")
            print(f"  Text: '{r['text'][:150]}'")
            for name, reason in r['result']['failures'].items():
                print(f"    {name}: {reason}")
    else:
        print("\n  No failures!")

    # Print longest responses
    print(f"\n{'='*70}")
    print("LONGEST RESPONSES (potential verbosity issues)")
    print(f"{'='*70}")
    sorted_by_len = sorted(all_responses, key=lambda x: len(x['text']), reverse=True)[:5]
    for r in sorted_by_len:
        print(f"\n  [{r['scenario']} turn {r['turn']}] ({len(r['text'])} chars)")
        print(f"  '{r['text']}'")

    # Print responses with most questions
    print(f"\n{'='*70}")
    print("MOST QUESTIONS IN SINGLE RESPONSE")
    print(f"{'='*70}")
    sorted_by_q = sorted(all_responses, key=lambda x: x['text'].count('?'), reverse=True)[:5]
    for r in sorted_by_q:
        qcount = r['text'].count('?')
        print(f"\n  [{r['scenario']} turn {r['turn']}] ({qcount} questions)")
        print(f"  '{r['text']}'")


if __name__ == "__main__":
    run_all()
