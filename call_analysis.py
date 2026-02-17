"""
call_analysis.py — Post-call conversation quality analysis
==========================================================
Shared module used by both agent_worker.py (post-call) and tests/.
No pytest dependency — can be imported standalone.

Usage:
    from call_analysis import analyze_transcript, analyze_and_save

    # Analyze transcript data dict
    result = analyze_transcript(data)

    # Or load from file, analyze, and save companion .analysis.json
    analysis_path = analyze_and_save("transcripts/Store_20260211.json")
"""

import json
import re
from datetime import datetime
from pathlib import Path

from agent_worker import _number_to_hindi, _HINDI_ONES


# ---------------------------------------------------------------------------
# Constraint checking — validates agent responses against behavioral rules
# ---------------------------------------------------------------------------
class ConstraintChecker:
    """Validates agent responses against behavioral rules from DEFAULT_INSTRUCTIONS."""

    _DEVANAGARI_RE = re.compile(r'[\u0900-\u097F]')
    _QUESTION_RE = re.compile(r'\?')
    _ACTION_MARKER_RE = re.compile(r'[\*\(\[][a-zA-Z\s]+[\*\)\]]')
    _ENGLISH_PAREN_RE = re.compile(r'\([A-Z][a-z].*?\)')
    _END_CALL_TEXT_RE = re.compile(r'\[end_call\]', re.IGNORECASE)

    def check_no_devanagari(self, text: str) -> tuple[bool, str]:
        m = self._DEVANAGARI_RE.search(text)
        if m:
            return False, f"Devanagari found: '{m.group()}' in '{text[:80]}'"
        return True, ""

    def check_single_question(self, text: str) -> tuple[bool, str]:
        questions = self._QUESTION_RE.findall(text)
        if len(questions) > 2:
            return False, f"Stacked {len(questions)} questions: '{text[:100]}'"
        return True, ""

    def check_response_length(self, text: str, max_chars: int = 300) -> tuple[bool, str]:
        if len(text) > max_chars:
            return False, f"Response too long ({len(text)} chars): '{text[:80]}...'"
        return True, ""

    def check_no_action_markers(self, text: str) -> tuple[bool, str]:
        m = self._ACTION_MARKER_RE.search(text)
        if m:
            return False, f"Action marker: '{m.group()}'"
        return True, ""

    def check_no_newlines(self, text: str) -> tuple[bool, str]:
        if '\n' in text:
            return False, f"Newline found in: '{text[:80]}'"
        return True, ""

    def check_no_english_translations(self, text: str) -> tuple[bool, str]:
        m = self._ENGLISH_PAREN_RE.search(text)
        if m:
            return False, f"English translation: '{m.group()}'"
        return True, ""

    def check_no_end_call_text(self, text: str) -> tuple[bool, str]:
        m = self._END_CALL_TEXT_RE.search(text)
        if m:
            return False, f"end_call as text: '{m.group()}'"
        return True, ""

    def check_no_invented_details(self, text: str) -> tuple[bool, str]:
        patterns = [
            (r'\b(Voltas|LG|Daikin)\b.*\b(purana|old)\b', "invented old AC brand"),
            (r'\b\d+\s*(saal|year).*\bpurana\b', "invented specific age"),
            (r'\b(Andheri|Borivali|Malad|Bandra|Juhu)\b', "invented specific neighborhood"),
        ]
        for pat, desc in patterns:
            if re.search(pat, text, re.IGNORECASE):
                return False, f"{desc}: '{text[:100]}'"
        return True, ""

    def check_all(self, text: str) -> dict:
        checks = {
            'no_devanagari': self.check_no_devanagari(text),
            'single_question': self.check_single_question(text),
            'response_length': self.check_response_length(text),
            'no_action_markers': self.check_no_action_markers(text),
            'no_newlines': self.check_no_newlines(text),
            'no_english_translations': self.check_no_english_translations(text),
            'no_end_call_text': self.check_no_end_call_text(text),
            'no_invented_details': self.check_no_invented_details(text),
        }
        passed = all(v[0] for v in checks.values())
        failures = {k: v[1] for k, v in checks.items() if not v[0]}
        return {
            'passed': passed,
            'score': sum(1 for v in checks.values() if v[0]) / len(checks),
            'checks': {k: v[0] for k, v in checks.items()},
            'failures': failures,
            'text': text,
        }


# ---------------------------------------------------------------------------
# Conversation scoring — multi-dimensional quality assessment
# ---------------------------------------------------------------------------
class ConversationScorer:
    """Score a full conversation on multiple quality dimensions."""

    DEFAULT_TOPIC_KEYWORDS = {
        'price': [r'rate', r'price', r'kitna', r'kitne', r'hazaar', r'rupay', r'₹', r'\d{4,}'],
        'warranty': [r'warranty', r'guarantee', r'saal ki'],
        'installation': [r'install', r'lagwa', r'fitting', r'pipe'],
        'delivery': [r'deliver', r'bhej', r'ghar pe', r'din mein', r'kab tak'],
        'exchange': [r'exchange', r'puran[ae]', r'old'],
    }

    def __init__(self, checker, topic_keywords=None):
        self.checker = checker
        self.TOPIC_KEYWORDS = topic_keywords or self.DEFAULT_TOPIC_KEYWORDS

    def score_conversation(self, messages: list[dict]) -> dict:
        assistant_msgs = [m for m in messages if m.get('role') == 'assistant']
        if not assistant_msgs:
            return {'overall_score': 0.0, 'per_turn': [], 'topics_covered': set(),
                    'flags': ['no_assistant_messages']}

        per_turn = [self.checker.check_all(m['text']) for m in assistant_msgs]
        constraint_score = sum(r['score'] for r in per_turn) / len(per_turn)

        topics = self.detect_topics(messages)
        topic_score = min(len(topics) / 3.0, 1.0)

        price_echo_score = self.check_price_echo(messages)
        brevity_score = self._brevity_score(assistant_msgs)
        repetition_score = self._no_repetition_score(assistant_msgs)

        overall = (
            constraint_score * 0.40 +
            topic_score * 0.25 +
            price_echo_score * 0.15 +
            brevity_score * 0.10 +
            repetition_score * 0.10
        )

        return {
            'overall_score': round(overall, 3),
            'constraint_score': round(constraint_score, 3),
            'topic_score': round(topic_score, 3),
            'price_echo_score': round(price_echo_score, 3),
            'brevity_score': round(brevity_score, 3),
            'repetition_score': round(repetition_score, 3),
            'topics_covered': topics,
            'per_turn': per_turn,
            'turn_count': len(assistant_msgs),
        }

    def detect_topics(self, messages: list[dict]) -> set:
        all_text = ' '.join(m.get('text', '') for m in messages)
        topics = set()
        for topic, patterns in self.TOPIC_KEYWORDS.items():
            for pat in patterns:
                if re.search(pat, all_text, re.IGNORECASE):
                    topics.add(topic)
                    break
        return topics

    # Reverse mapping: Hindi word -> number (e.g., "adtees" -> 38)
    _HINDI_TO_NUM = {v: k for k, v in _HINDI_ONES.items() if v}

    def check_price_echo(self, messages: list[dict]) -> float:
        for i, msg in enumerate(messages):
            if msg.get('role') != 'user':
                continue
            text = msg.get('text', '')
            price_num = None

            # Try digit-based price: "38000 ka hai"
            price_match = re.search(r'(\d[\d,]*)\s*(?:ka|mein|rupay|₹|hai)', text)
            if price_match:
                price_str = price_match.group(1).replace(',', '')
                try:
                    price_num = int(price_str)
                except ValueError:
                    pass
                if price_num and price_num < 1000:
                    price_num = None

            # Try Hindi word price: "adtees hazaar ka hai"
            if price_num is None:
                hindi_match = re.search(r'(\w+)\s+hazaar', text.lower())
                if hindi_match:
                    word = hindi_match.group(1)
                    num = self._HINDI_TO_NUM.get(word)
                    if num:
                        price_num = num * 1000

            if price_num is None:
                continue

            price_str = str(price_num)
            hindi_form = _number_to_hindi(price_num) if price_num < 100000 else price_str
            # Also get just the thousands word for partial matching
            thousands_word = _HINDI_ONES.get(price_num // 1000, '') if price_num >= 1000 else ''

            for j in range(i + 1, min(i + 3, len(messages))):
                if messages[j].get('role') == 'assistant':
                    resp = messages[j].get('text', '').lower()
                    if (price_str in resp or
                            (hindi_form and hindi_form.lower() in resp) or
                            (thousands_word and thousands_word in resp)):
                        return 1.0
            return 0.0
        return 0.5  # no price given yet

    # Patterns that indicate a number should be echoed by the agent
    _ECHOABLE_CONTEXTS = [
        # Prices: "38000 ka hai", "₹38000", "38000 rupay"
        re.compile(r'(\d[\d,]*)\s*(?:ka\b|mein\b|rupay|₹|hai\b|lagega|denge|hoga|milega)', re.I),
        # Prices in Hindi words: "adtees hazaar"
        re.compile(r'(\w+)\s+hazaar', re.I),
        # Warranty/years: "2 saal", "3 year", "1 saal ki warranty"
        re.compile(r'(\d+\.?\d*)\s*(?:saal|year|varsh)', re.I),
        # Delivery/days: "3 din", "2 days"
        re.compile(r'(\d+)\s*(?:din|day|hafte|week)', re.I),
        # Range prices: "39,000 and 40,000", "39000 se 40000"
        re.compile(r'(\d[\d,]*)\s*(?:se|and|to|aur|ya)\s*(\d[\d,]*)', re.I),
        # Ton: "1.5 ton", "2 ton"
        re.compile(r'(\d+\.?\d*)\s*ton', re.I),
    ]

    def check_number_echoes(self, messages: list[dict]) -> dict:
        """Check if the agent correctly echoes numbers the shopkeeper provides.

        Only tracks numbers in echoable contexts — prices, warranty years,
        delivery days, tonnage. Ignores incidental numbers like percentages,
        addresses, and road names.

        Returns dict with echoed/missed lists and a boolean flag.
        """
        echoed = []
        missed = []

        for i, msg in enumerate(messages):
            if msg.get('role') != 'user':
                continue
            text = msg.get('text', '')

            # Extract echoable numbers from this message
            echoable_nums = set()
            for pattern in self._ECHOABLE_CONTEXTS:
                for m in pattern.finditer(text):
                    for g in m.groups():
                        if g and g[0].isdigit():
                            g = g.rstrip('.,;:')  # strip trailing punctuation
                            clean = g.replace(',', '')
                            try:
                                val = float(clean) if '.' in clean else int(clean)
                                echoable_nums.add((g, val))
                            except ValueError:
                                pass

            if not echoable_nums:
                continue

            # Collect next assistant responses (up to 2 ahead)
            assistant_responses = []
            for j in range(i + 1, min(i + 4, len(messages))):
                if messages[j].get('role') == 'assistant':
                    assistant_responses.append(messages[j].get('text', '').lower())

            if not assistant_responses:
                continue

            combined_resp = ' '.join(assistant_responses)

            for raw, num in echoable_nums:
                clean = raw.replace(',', '')
                int_num = int(num) if num == int(num) else num

                # Generate possible echo forms
                forms = [clean, raw]
                if isinstance(int_num, int) and 0 < int_num < 100000:
                    hindi = _number_to_hindi(int_num)
                    forms.append(hindi.lower())
                    if int_num >= 1000:
                        tw = _HINDI_ONES.get(int_num // 1000, '')
                        if tw:
                            forms.append(tw)

                found = any(f in combined_resp for f in forms if f)
                entry = {"number": raw, "value": num, "user_text": text[:80]}
                if found:
                    echoed.append(entry)
                else:
                    missed.append(entry)

        return {
            "correct_number_echoed": len(missed) == 0 and len(echoed) > 0,
            "echoed": echoed,
            "missed": missed,
        }

    def check_call_readiness(self, messages: list[dict]) -> bool:
        topics = self.detect_topics(messages)
        return 'price' in topics and len(topics) >= 3

    def _brevity_score(self, assistant_msgs: list[dict]) -> float:
        lengths = [len(m.get('text', '')) for m in assistant_msgs]
        avg = sum(lengths) / len(lengths)
        if avg < 100:
            return 1.0
        if avg < 200:
            return 0.7
        if avg < 300:
            return 0.4
        return 0.0

    def _no_repetition_score(self, assistant_msgs: list[dict]) -> float:
        if len(assistant_msgs) < 2:
            return 1.0
        repetitions = 0
        for i in range(1, len(assistant_msgs)):
            prev_words = set(assistant_msgs[i - 1].get('text', '').lower().split())
            curr_words = set(assistant_msgs[i].get('text', '').lower().split())
            if not curr_words:
                continue
            overlap = len(prev_words & curr_words) / len(curr_words)
            if overlap > 0.6:
                repetitions += 1
        return max(0.0, 1.0 - repetitions / len(assistant_msgs))


# ---------------------------------------------------------------------------
# Transcript analysis — run full analysis on a transcript dict
# ---------------------------------------------------------------------------
def _generate_summary(scores: dict, number_echoes: dict) -> dict:
    """Generate a human-readable summary of what went right and what needs improvement."""
    went_right = []
    needs_improvement = []

    # Constraints
    if scores['constraint_score'] == 1.0:
        went_right.append("All 8 behavioral constraints passed on every turn")
    else:
        # Collect unique failures across turns
        failed_checks = set()
        for turn in scores.get('per_turn', []):
            for check_name, passed in turn.get('checks', {}).items():
                if not passed:
                    failed_checks.add(check_name)
        pct = round(scores['constraint_score'] * 100)
        needs_improvement.append(
            f"Constraint score {pct}% — failed checks: {', '.join(sorted(failed_checks))}"
        )

    # Topics
    topics = scores.get('topics_covered', set())
    if isinstance(topics, list):
        topics = set(topics)
    all_topics = {'price', 'warranty', 'installation', 'delivery', 'exchange'}
    missing = sorted(all_topics - topics)
    if len(topics) >= 4:
        went_right.append(f"Covered {len(topics)} topics: {', '.join(sorted(topics))}")
    elif len(topics) >= 3:
        went_right.append(f"Covered {len(topics)} topics: {', '.join(sorted(topics))}")
        if missing:
            needs_improvement.append(f"Did not cover: {', '.join(missing)}")
    else:
        needs_improvement.append(
            f"Only covered {len(topics)} topic(s): {', '.join(sorted(topics)) or 'none'}. "
            f"Missing: {', '.join(missing)}"
        )

    # Number echoing
    if number_echoes['correct_number_echoed']:
        count = len(number_echoes['echoed'])
        went_right.append(f"Correctly echoed all {count} number(s) from shopkeeper")
    elif number_echoes['echoed'] or number_echoes['missed']:
        echoed_count = len(number_echoes['echoed'])
        missed_count = len(number_echoes['missed'])
        missed_nums = [m['number'] for m in number_echoes['missed']]
        needs_improvement.append(
            f"Echoed {echoed_count}, missed {missed_count} number(s): {', '.join(missed_nums)}"
        )

    # Brevity
    if scores['brevity_score'] == 1.0:
        went_right.append("Responses were concise (avg < 100 chars)")
    elif scores['brevity_score'] >= 0.7:
        went_right.append("Response length acceptable (avg < 200 chars)")
    else:
        needs_improvement.append("Responses too long — aim for shorter, more natural replies")

    # Repetition
    if scores['repetition_score'] == 1.0:
        went_right.append("No repetitive responses detected")
    else:
        pct = round((1.0 - scores['repetition_score']) * 100)
        needs_improvement.append(f"Some responses were repetitive ({pct}% overlap detected)")

    # Turn count
    turn_count = scores.get('turn_count', 0)
    if 3 <= turn_count <= 8:
        went_right.append(f"Good conversation length ({turn_count} agent turns)")
    elif turn_count > 8:
        needs_improvement.append(
            f"Conversation ran long ({turn_count} agent turns) — could wrap up sooner"
        )

    return {
        "went_right": went_right,
        "needs_improvement": needs_improvement,
    }


def analyze_transcript(data: dict) -> dict:
    """Run full quality analysis on a transcript data dict.

    Args:
        data: Transcript dict with 'messages' list and metadata fields.

    Returns:
        Dict with scores, checks, topics, number echoes, summary, and metadata.
    """
    messages = data.get("messages", [])
    checker = ConstraintChecker()
    scorer = ConversationScorer(checker)

    scores = scorer.score_conversation(messages)
    # Convert set to sorted list for JSON serialization
    scores['topics_covered'] = sorted(scores['topics_covered'])

    number_echoes = scorer.check_number_echoes(messages)
    summary = _generate_summary(scores, number_echoes)

    return {
        "store_name": data.get("store_name", ""),
        "product_description": data.get("product_description", data.get("ac_model", "")),
        "timestamp": data.get("timestamp", ""),
        "analyzed_at": datetime.now().isoformat(),
        "overall_score": scores['overall_score'],
        "scores": {
            "constraint": scores['constraint_score'],
            "topic": scores['topic_score'],
            "price_echo": scores['price_echo_score'],
            "brevity": scores['brevity_score'],
            "repetition": scores['repetition_score'],
        },
        "topics_covered": scores['topics_covered'],
        "turn_count": scores['turn_count'],
        "correct_number_echoed": number_echoes['correct_number_echoed'],
        "number_echoes": {
            "echoed": number_echoes['echoed'],
            "missed": number_echoes['missed'],
        },
        "per_turn": scores['per_turn'],
        "summary": summary,
    }


def analyze_and_save(path) -> Path:
    """Load transcript JSON, run analysis, write companion .analysis.json.

    Args:
        path: Path to transcript JSON file.

    Returns:
        Path to the written .analysis.json file.
    """
    path = Path(path)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    result = analyze_transcript(data)

    analysis_path = path.with_suffix(".analysis.json")
    with open(analysis_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    return analysis_path
