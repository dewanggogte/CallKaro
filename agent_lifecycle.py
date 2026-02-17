"""
agent_lifecycle.py — Shared agent worker management
====================================================
Functions for starting, stopping, and finding agent worker processes and logs.
Used by both app.py and test_browser.py.
"""

import glob
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


_agent_proc = None


def kill_old_agents():
    """Kill any existing agent_worker.py processes."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "agent_worker.py"],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        # pgrep not available (e.g. slim Docker images) — skip cleanup
        return
    pids = result.stdout.strip().split("\n")
    my_pid = str(os.getpid())
    for pid in pids:
        pid = pid.strip()
        if pid and pid != my_pid:
            print(f"  Killing old agent worker (PID {pid})")
            try:
                os.kill(int(pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
    if any(p.strip() and p.strip() != my_pid for p in pids):
        time.sleep(1)


def start_agent_worker():
    """Start a new agent_worker.py dev process in the background."""
    global _agent_proc
    python = sys.executable
    script = Path(__file__).parent / "agent_worker.py"
    _agent_proc = subprocess.Popen(
        [python, str(script), "dev"],
        cwd=str(Path(__file__).parent),
        stdout=sys.stderr,
        stderr=sys.stderr,
    )
    print(f"  Agent worker started (PID {_agent_proc.pid})")
    time.sleep(3)


def cleanup_agent():
    """Terminate agent worker on exit."""
    global _agent_proc
    if _agent_proc and _agent_proc.poll() is None:
        print(f"\n  Stopping agent worker (PID {_agent_proc.pid})")
        _agent_proc.terminate()
        try:
            _agent_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _agent_proc.kill()


def find_agent_log():
    """Find the most recent LiveKit agent log file."""
    patterns = [
        "/tmp/livekit-agents-*.log",
        "/private/tmp/livekit-agents-*.log",
        os.path.expanduser("~/.livekit/agents/*.log"),
    ]
    task_dir = "/private/tmp/claude-501/-Users-dg-Documents-lab-hyperlocal-discovery/tasks"
    if os.path.isdir(task_dir):
        outputs = sorted(Path(task_dir).glob("*.output"), key=lambda p: p.stat().st_mtime, reverse=True)
        for f in outputs:
            try:
                content = f.read_text(errors="replace")
                if "price-agent" in content or "livekit.agents" in content:
                    return str(f)
            except Exception:
                continue
    for pat in patterns:
        files = sorted(glob.glob(pat), key=os.path.getmtime, reverse=True)
        if files:
            return files[0]
    return None
