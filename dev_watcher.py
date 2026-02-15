"""
dev_watcher.py — Auto-test + auto-analysis + auto-docs
=======================================================
Watches for file changes and runs appropriate automation:

1. Code changes (*.py) → run pytest → update doc test counts
2. Transcript files (transcripts/*.json) → run quality analysis

Usage:
    python dev_watcher.py              # Watch, auto-test, auto-docs
    python dev_watcher.py --live       # Include live API tests
    python dev_watcher.py --no-docs    # Skip doc count updates
    python dev_watcher.py --no-analysis # Skip transcript analysis
"""

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path
from threading import Timer

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

PROJECT_ROOT = Path(__file__).parent
TRANSCRIPTS_DIR = PROJECT_ROOT / "transcripts"
DOC_FILES = [PROJECT_ROOT / "README.md", PROJECT_ROOT / "tests.md"]


# ---------------------------------------------------------------------------
# Doc count updater — updates test counts in README.md and tests.md
# ---------------------------------------------------------------------------
def _extract_test_count(output: str) -> int | None:
    """Extract passed test count from pytest output."""
    # Match "141 passed" or "141 passed, 2 warnings"
    m = re.search(r'(\d+) passed', output)
    return int(m.group(1)) if m else None


def _update_file_counts(path: Path, count: int):
    """Update test count patterns in a doc file."""
    if not path.exists():
        return
    text = path.read_text()
    original = text

    # "# 141 passed" → "# {count} passed"
    text = re.sub(r'# \d+ passed', f'# {count} passed', text)
    # "141 passed" in inline code or prose
    text = re.sub(r'(\b)\d+ passed\b', f'{count} passed', text)
    # "**Total: 141 passed**"
    text = re.sub(r'\*\*Total: \d+ passed\*\*', f'**Total: {count} passed**', text)
    # "141 + 26 live tests" → preserve live count
    text = re.sub(r'\d+( \+ \d+ live)', f'{count}\\1', text)
    # "pytest test suite (141 unit" → update unit count
    text = re.sub(r'pytest test suite \(\d+ unit', f'pytest test suite ({count} unit', text)
    # "verify all 141 tests"
    text = re.sub(r'verify all \d+ tests', f'verify all {count} tests', text)

    if text != original:
        path.write_text(text)
        print(f"  Updated {path.name} (test count → {count})")


def update_doc_counts(count: int):
    """Update test counts across all doc files."""
    for doc in DOC_FILES:
        _update_file_counts(doc, count)


# ---------------------------------------------------------------------------
# Pytest runner
# ---------------------------------------------------------------------------
def run_tests(include_live: bool = False) -> tuple[bool, int | None]:
    """Run pytest and return (success, test_count)."""
    cmd = [sys.executable, "-m", "pytest", "tests/", "-v", "--tb=short", "-q"]
    if include_live:
        cmd.append("--live")

    print(f"\n{'='*60}")
    print(f"Running: {' '.join(cmd)}")
    print(f"{'='*60}")

    result = subprocess.run(cmd, cwd=str(PROJECT_ROOT), capture_output=False)
    success = result.returncode == 0

    # Re-run capturing output to extract count
    result_capture = subprocess.run(cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True)
    count = _extract_test_count(result_capture.stdout + result_capture.stderr)

    return success, count


# ---------------------------------------------------------------------------
# Transcript analysis
# ---------------------------------------------------------------------------
def run_analysis(path: Path):
    """Run quality analysis on a transcript file."""
    try:
        from call_analysis import analyze_and_save
        analysis_path = analyze_and_save(path)
        # Read back for summary
        import json
        with open(analysis_path) as f:
            result = json.load(f)
        topics = ", ".join(result.get("topics_covered", []))
        print(
            f"  Analysis: score={result['overall_score']}, "
            f"topics=[{topics}], turns={result['turn_count']}"
        )
        print(f"  Saved: {analysis_path.name}")
    except Exception as e:
        print(f"  Analysis failed: {e}")


# ---------------------------------------------------------------------------
# File system event handlers
# ---------------------------------------------------------------------------
class CodeChangeHandler(FileSystemEventHandler):
    """Watches *.py files for changes, debounces, runs pytest."""

    def __init__(self, include_live: bool, update_docs: bool):
        self.include_live = include_live
        self.update_docs = update_docs
        self._timer = None
        self._debounce_sec = 2.0

    def on_modified(self, event):
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix != '.py':
            return
        # Skip generated/venv files
        if 'venv' in path.parts or '__pycache__' in path.parts:
            return

        print(f"\n  Changed: {path.relative_to(PROJECT_ROOT)}")
        self._schedule_test_run()

    def _schedule_test_run(self):
        if self._timer:
            self._timer.cancel()
        self._timer = Timer(self._debounce_sec, self._run)
        self._timer.start()

    def _run(self):
        success, count = run_tests(self.include_live)
        if success and count and self.update_docs:
            update_doc_counts(count)


class TranscriptHandler(FileSystemEventHandler):
    """Watches transcripts/ for new JSON files, runs analysis."""

    def on_created(self, event):
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix != '.json' or path.name.endswith('.analysis.json'):
            return
        print(f"\n  New transcript: {path.name}")
        # Small delay to ensure file is fully written
        time.sleep(0.5)
        run_analysis(path)

    def on_modified(self, event):
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix != '.json' or path.name.endswith('.analysis.json'):
            return
        # Only analyze on modify if no companion analysis exists yet
        analysis_path = path.with_suffix('.analysis.json')
        if not analysis_path.exists():
            print(f"\n  Updated transcript: {path.name}")
            time.sleep(0.5)
            run_analysis(path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Dev watcher: auto-test, auto-analyze, auto-docs")
    parser.add_argument("--live", action="store_true", help="Include live API tests in pytest runs")
    parser.add_argument("--no-docs", action="store_true", help="Skip doc count updates")
    parser.add_argument("--no-analysis", action="store_true", help="Skip transcript analysis")
    args = parser.parse_args()

    update_docs = not args.no_docs
    run_analysis_flag = not args.no_analysis

    print("Dev Watcher")
    print(f"  Live tests: {'yes' if args.live else 'no'}")
    print(f"  Doc updates: {'yes' if update_docs else 'no'}")
    print(f"  Transcript analysis: {'yes' if run_analysis_flag else 'no'}")
    print()

    # Initial test run for baseline
    print("Running initial test suite...")
    success, count = run_tests(args.live)
    if success and count and update_docs:
        update_doc_counts(count)

    # Set up watchers
    observer = Observer()

    # Watch code files
    code_handler = CodeChangeHandler(include_live=args.live, update_docs=update_docs)
    observer.schedule(code_handler, str(PROJECT_ROOT), recursive=True)

    # Watch transcripts
    if run_analysis_flag:
        TRANSCRIPTS_DIR.mkdir(exist_ok=True)
        transcript_handler = TranscriptHandler()
        observer.schedule(transcript_handler, str(TRANSCRIPTS_DIR), recursive=False)

    observer.start()
    print(f"\nWatching for changes... (Ctrl+C to stop)")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping watcher...")
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
