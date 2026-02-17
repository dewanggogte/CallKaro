#!/usr/bin/env python3
"""
dashboard.py — Test & Monitoring Dashboard
===========================================
Parses transcripts, logs, and test results to generate a self-contained HTML
dashboard with latency charts, token usage, and conversation analytics.

Usage:
  python dashboard.py          # Generate and serve on http://localhost:9090
  python dashboard.py --port 8000
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from statistics import mean, median

PORT = 9090
BASE_DIR = Path(__file__).parent
TRANSCRIPTS_DIR = BASE_DIR / "transcripts"
LOGS_DIR = BASE_DIR / "logs"


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------
LLM_METRICS_RE = re.compile(
    r"(\d{2}:\d{2}:\d{2}).*\[LLM METRICS\] tokens: (\d+)→(\d+), TTFT: ([\d.]+)s, duration: ([\d.]+)s"
)
USER_RE = re.compile(r"(\d{2}:\d{2}:\d{2}).*\[USER\] (.+)")
LLM_OUTPUT_RE = re.compile(r"(\d{2}:\d{2}:\d{2}).*\[LLM\] (.+)")
ERROR_RE = re.compile(r"(\d{2}:\d{2}:\d{2}).*ERROR:? (.+)")


def _parse_time(t: str) -> float:
    """Convert HH:MM:SS to seconds since midnight."""
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def parse_transcripts() -> list[dict]:
    results = []
    if not TRANSCRIPTS_DIR.exists():
        return results
    for f in sorted(TRANSCRIPTS_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text())
            data["_filename"] = f.name
            user_msgs = [m for m in data["messages"] if m["role"] == "user"]
            asst_msgs = [m for m in data["messages"] if m["role"] == "assistant"]
            data["_user_count"] = len(user_msgs)
            data["_assistant_count"] = len(asst_msgs)
            data["_total_messages"] = len(data["messages"])
            if len(data["messages"]) >= 2:
                first = datetime.fromisoformat(data["messages"][0]["time"])
                last = datetime.fromisoformat(data["messages"][-1]["time"])
                data["_duration_seconds"] = round((last - first).total_seconds(), 1)
            else:
                data["_duration_seconds"] = 0
            results.append(data)
        except Exception:
            continue
    return results


def parse_logs() -> list[dict]:
    results = []
    if not LOGS_DIR.exists():
        return results
    for f in sorted(LOGS_DIR.glob("*.log")):
        try:
            text = f.read_text(errors="replace")
        except Exception:
            continue

        call_data = {
            "_filename": f.name,
            "llm_metrics": [],
            "user_messages": [],
            "llm_outputs": [],
            "errors": [],
        }

        for line in text.splitlines():
            m = LLM_METRICS_RE.search(line)
            if m:
                call_data["llm_metrics"].append({
                    "time": m.group(1),
                    "prompt_tokens": int(m.group(2)),
                    "completion_tokens": int(m.group(3)),
                    "ttft": float(m.group(4)),
                    "duration": float(m.group(5)),
                })
                continue

            m = USER_RE.search(line)
            if m:
                call_data["user_messages"].append({"time": m.group(1), "text": m.group(2)})
                continue

            m = LLM_OUTPUT_RE.search(line)
            if m and "[LLM METRICS]" not in line and "[LLM REQUEST]" not in line and "Using Claude" not in line and "Using Qwen" not in line:
                call_data["llm_outputs"].append({"time": m.group(1), "text": m.group(2)})
                continue

            m = ERROR_RE.search(line)
            if m:
                call_data["errors"].append({"time": m.group(1), "text": m.group(2)})

        results.append(call_data)
    return results


def run_tests() -> dict:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-v", "--tb=short", "-q"],
        capture_output=True, text=True, cwd=str(BASE_DIR), timeout=120,
    )
    output = result.stdout + result.stderr
    # Parse summary line like "47 passed, 2 failed, 3 skipped"
    passed = len(re.findall(r"PASSED", result.stdout))
    failed = len(re.findall(r"FAILED", result.stdout))
    skipped = len(re.findall(r"SKIPPED", result.stdout))
    # Also try the summary line
    summary_match = re.search(r"(\d+) passed", output)
    if summary_match:
        passed = int(summary_match.group(1))
    summary_match = re.search(r"(\d+) failed", output)
    if summary_match:
        failed = int(summary_match.group(1))
    summary_match = re.search(r"(\d+) skipped", output)
    if summary_match:
        skipped = int(summary_match.group(1))

    return {
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "output": result.stdout[-3000:] if len(result.stdout) > 3000 else result.stdout,
        "returncode": result.returncode,
    }


def compute_metrics(transcripts: list, logs: list) -> dict:
    """Compute aggregate metrics from parsed data."""
    all_ttfts = []
    all_durations = []
    all_prompt_tokens = []
    all_completion_tokens = []
    total_errors = 0
    turn_latencies = []

    for log in logs:
        total_errors += len(log["errors"])
        for m in log["llm_metrics"]:
            all_ttfts.append(m["ttft"])
            all_durations.append(m["duration"])
            all_prompt_tokens.append(m["prompt_tokens"])
            all_completion_tokens.append(m["completion_tokens"])

        # Compute turn latency: time from [USER] to next [LLM] output
        user_times = [_parse_time(u["time"]) for u in log["user_messages"]]
        llm_times = [_parse_time(l["time"]) for l in log["llm_outputs"]]
        for ut in user_times:
            next_llm = [lt for lt in llm_times if lt > ut]
            if next_llm:
                turn_latencies.append(next_llm[0] - ut)

    def _stats(arr):
        if not arr:
            return {"avg": 0, "p50": 0, "p95": 0, "min": 0, "max": 0}
        s = sorted(arr)
        p95_idx = min(int(len(s) * 0.95), len(s) - 1)
        return {
            "avg": round(mean(s), 2),
            "p50": round(median(s), 2),
            "p95": round(s[p95_idx], 2),
            "min": round(s[0], 2),
            "max": round(s[-1], 2),
        }

    conv_durations = [t["_duration_seconds"] for t in transcripts if t["_duration_seconds"] > 0]
    msg_counts = [t["_total_messages"] for t in transcripts]

    return {
        "ttft": _stats(all_ttfts),
        "llm_duration": _stats(all_durations),
        "turn_latency": _stats(turn_latencies),
        "prompt_tokens": _stats(all_prompt_tokens),
        "completion_tokens": _stats(all_completion_tokens),
        "total_calls": len(transcripts),
        "total_errors": total_errors,
        "conv_duration": _stats(conv_durations),
        "msg_counts": _stats(msg_counts),
        "all_ttfts": all_ttfts,
        "all_prompt_tokens": all_prompt_tokens,
        "all_completion_tokens": all_completion_tokens,
        "all_turn_latencies": turn_latencies,
    }


# ---------------------------------------------------------------------------
# HTML Generation
# ---------------------------------------------------------------------------
def generate_html(transcripts, logs, test_results, metrics) -> str:
    test_color = "#22c55e" if test_results["failed"] == 0 else "#ef4444"
    test_output_escaped = (
        test_results["output"]
        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )

    # Transcript table rows
    transcript_rows = ""
    for t in transcripts[-10:]:  # Last 10
        transcript_rows += f"""<tr>
            <td>{t['_filename']}</td>
            <td>{t['store_name']}</td>
            <td>{t['_total_messages']}</td>
            <td>{t['_duration_seconds']}s</td>
            <td>{t['phone']}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CallKaro — Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f172a; color: #e2e8f0; padding: 20px; }}
    h1 {{ font-size: 1.5rem; margin-bottom: 20px; color: #f8fafc; }}
    h2 {{ font-size: 1.1rem; margin-bottom: 12px; color: #94a3b8; font-weight: 500; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px; margin-bottom: 20px; }}
    .card {{ background: #1e293b; border-radius: 12px; padding: 20px; border: 1px solid #334155; }}
    .card-wide {{ grid-column: 1 / -1; }}
    .stat {{ font-size: 2rem; font-weight: 700; color: #f8fafc; }}
    .stat-label {{ font-size: 0.85rem; color: #64748b; margin-top: 4px; }}
    .stat-row {{ display: flex; gap: 24px; margin-top: 12px; }}
    .stat-item {{ text-align: center; }}
    .stat-item .value {{ font-size: 1.2rem; font-weight: 600; color: #cbd5e1; }}
    .stat-item .label {{ font-size: 0.75rem; color: #64748b; }}
    .badge {{ display: inline-block; padding: 4px 12px; border-radius: 6px; font-size: 0.85rem; font-weight: 600; }}
    .badge-green {{ background: #166534; color: #86efac; }}
    .badge-red {{ background: #991b1b; color: #fca5a5; }}
    .badge-yellow {{ background: #854d0e; color: #fde047; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
    th {{ text-align: left; padding: 8px; color: #64748b; border-bottom: 1px solid #334155; }}
    td {{ padding: 8px; border-bottom: 1px solid #1e293b; }}
    pre {{ background: #0f172a; padding: 12px; border-radius: 8px; font-size: 0.8rem; overflow-x: auto; max-height: 300px; overflow-y: auto; color: #94a3b8; }}
    canvas {{ max-height: 200px; }}
    .refresh {{ position: fixed; top: 16px; right: 20px; background: #3b82f6; color: white; border: none; padding: 8px 16px; border-radius: 8px; cursor: pointer; font-size: 0.85rem; }}
    .refresh:hover {{ background: #2563eb; }}
</style>
</head>
<body>
<a href="/refresh" class="refresh">Refresh</a>
<h1>CallKaro — Test & Monitoring Dashboard</h1>

<!-- Test Results -->
<div class="grid">
    <div class="card">
        <h2>Test Results</h2>
        <div style="display:flex; gap:12px; align-items:center;">
            <span class="badge badge-green">{test_results['passed']} passed</span>
            <span class="badge {'badge-red' if test_results['failed'] > 0 else 'badge-green'}">{test_results['failed']} failed</span>
            <span class="badge badge-yellow">{test_results['skipped']} skipped</span>
        </div>
    </div>
    <div class="card">
        <h2>Calls Recorded</h2>
        <div class="stat">{metrics['total_calls']}</div>
        <div class="stat-label">Total conversations in transcripts/</div>
    </div>
    <div class="card">
        <h2>Errors</h2>
        <div class="stat" style="color: {'#ef4444' if metrics['total_errors'] > 0 else '#22c55e'}">{metrics['total_errors']}</div>
        <div class="stat-label">Errors across all log files</div>
    </div>
</div>

<!-- Latency Metrics -->
<div class="grid">
    <div class="card">
        <h2>Time to First Token (TTFT)</h2>
        <div class="stat">{metrics['ttft']['avg']}s</div>
        <div class="stat-label">Average TTFT</div>
        <div class="stat-row">
            <div class="stat-item"><div class="value">{metrics['ttft']['p50']}s</div><div class="label">P50</div></div>
            <div class="stat-item"><div class="value">{metrics['ttft']['p95']}s</div><div class="label">P95</div></div>
            <div class="stat-item"><div class="value">{metrics['ttft']['min']}s</div><div class="label">Min</div></div>
            <div class="stat-item"><div class="value">{metrics['ttft']['max']}s</div><div class="label">Max</div></div>
        </div>
    </div>
    <div class="card">
        <h2>LLM Response Duration</h2>
        <div class="stat">{metrics['llm_duration']['avg']}s</div>
        <div class="stat-label">Average full response time</div>
        <div class="stat-row">
            <div class="stat-item"><div class="value">{metrics['llm_duration']['p50']}s</div><div class="label">P50</div></div>
            <div class="stat-item"><div class="value">{metrics['llm_duration']['p95']}s</div><div class="label">P95</div></div>
            <div class="stat-item"><div class="value">{metrics['llm_duration']['min']}s</div><div class="label">Min</div></div>
            <div class="stat-item"><div class="value">{metrics['llm_duration']['max']}s</div><div class="label">Max</div></div>
        </div>
    </div>
    <div class="card">
        <h2>Turn Latency (User → Agent)</h2>
        <div class="stat">{metrics['turn_latency']['avg']}s</div>
        <div class="stat-label">Time from user speech to agent response</div>
        <div class="stat-row">
            <div class="stat-item"><div class="value">{metrics['turn_latency']['p50']}s</div><div class="label">P50</div></div>
            <div class="stat-item"><div class="value">{metrics['turn_latency']['p95']}s</div><div class="label">P95</div></div>
        </div>
    </div>
</div>

<!-- Charts -->
<div class="grid">
    <div class="card">
        <h2>TTFT Distribution</h2>
        <canvas id="ttftChart"></canvas>
    </div>
    <div class="card">
        <h2>Token Usage per Turn</h2>
        <canvas id="tokenChart"></canvas>
    </div>
</div>

<!-- Conversation Analytics -->
<div class="grid">
    <div class="card">
        <h2>Conversation Duration</h2>
        <div class="stat">{metrics['conv_duration']['avg']}s</div>
        <div class="stat-label">Average call length</div>
        <div class="stat-row">
            <div class="stat-item"><div class="value">{metrics['conv_duration']['p50']}s</div><div class="label">P50</div></div>
            <div class="stat-item"><div class="value">{metrics['conv_duration']['max']}s</div><div class="label">Max</div></div>
        </div>
    </div>
    <div class="card">
        <h2>Messages per Call</h2>
        <div class="stat">{metrics['msg_counts']['avg']}</div>
        <div class="stat-label">Average messages per conversation</div>
    </div>
    <div class="card">
        <h2>Token Efficiency</h2>
        <div class="stat">{metrics['prompt_tokens']['avg']}</div>
        <div class="stat-label">Avg prompt tokens per turn</div>
        <div class="stat-row">
            <div class="stat-item"><div class="value">{metrics['completion_tokens']['avg']}</div><div class="label">Avg completion</div></div>
            <div class="stat-item"><div class="value">{metrics['prompt_tokens']['max']}</div><div class="label">Max prompt</div></div>
        </div>
    </div>
</div>

<!-- Recent Transcripts -->
<div class="grid">
    <div class="card card-wide">
        <h2>Recent Transcripts</h2>
        <table>
            <tr><th>File</th><th>Store</th><th>Messages</th><th>Duration</th><th>Channel</th></tr>
            {transcript_rows if transcript_rows else '<tr><td colspan="5" style="color:#64748b">No transcripts found</td></tr>'}
        </table>
    </div>
</div>

<!-- Test Output -->
<div class="grid">
    <div class="card card-wide">
        <h2>Test Output</h2>
        <pre>{test_output_escaped}</pre>
    </div>
</div>

<script>
// TTFT Chart
const ttftData = {json.dumps(metrics['all_ttfts'])};
if (ttftData.length > 0) {{
    new Chart(document.getElementById('ttftChart'), {{
        type: 'bar',
        data: {{
            labels: ttftData.map((_, i) => 'Turn ' + (i+1)),
            datasets: [{{
                label: 'TTFT (seconds)',
                data: ttftData,
                backgroundColor: '#3b82f6',
                borderRadius: 4,
            }}]
        }},
        options: {{
            responsive: true,
            plugins: {{ legend: {{ display: false }} }},
            scales: {{
                y: {{ beginAtZero: true, grid: {{ color: '#1e293b' }}, ticks: {{ color: '#64748b' }} }},
                x: {{ grid: {{ display: false }}, ticks: {{ color: '#64748b', maxRotation: 0, autoSkip: true, maxTicksLimit: 20 }} }}
            }}
        }}
    }});
}}

// Token Chart
const promptTokens = {json.dumps(metrics['all_prompt_tokens'])};
const completionTokens = {json.dumps(metrics['all_completion_tokens'])};
if (promptTokens.length > 0) {{
    new Chart(document.getElementById('tokenChart'), {{
        type: 'bar',
        data: {{
            labels: promptTokens.map((_, i) => 'Turn ' + (i+1)),
            datasets: [
                {{ label: 'Prompt', data: promptTokens, backgroundColor: '#6366f1', borderRadius: 4 }},
                {{ label: 'Completion', data: completionTokens, backgroundColor: '#22c55e', borderRadius: 4 }}
            ]
        }},
        options: {{
            responsive: true,
            plugins: {{ legend: {{ labels: {{ color: '#94a3b8' }} }} }},
            scales: {{
                y: {{ stacked: true, beginAtZero: true, grid: {{ color: '#1e293b' }}, ticks: {{ color: '#64748b' }} }},
                x: {{ stacked: true, grid: {{ display: false }}, ticks: {{ color: '#64748b', maxRotation: 0, autoSkip: true, maxTicksLimit: 20 }} }}
            }}
        }}
    }});
}}
</script>

<div style="text-align:center; margin-top:30px; color:#475569; font-size:0.8rem;">
    Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} &middot;
    <a href="/refresh" style="color:#3b82f6;">Refresh</a>
</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
class DashboardHandler(BaseHTTPRequestHandler):
    html_cache = None

    def do_GET(self):
        if self.path == "/" or self.path == "/dashboard":
            if not DashboardHandler.html_cache:
                DashboardHandler.html_cache = self._generate()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(DashboardHandler.html_cache.encode())

        elif self.path == "/refresh":
            DashboardHandler.html_cache = None
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
        else:
            self.send_error(404)

    def _generate(self):
        print("  Parsing transcripts...")
        transcripts = parse_transcripts()
        print(f"  Found {len(transcripts)} transcripts")
        print("  Parsing logs...")
        logs = parse_logs()
        print(f"  Found {len(logs)} log files")
        print("  Running tests...")
        test_results = run_tests()
        print(f"  Tests: {test_results['passed']} passed, {test_results['failed']} failed, {test_results['skipped']} skipped")
        metrics = compute_metrics(transcripts, logs)
        return generate_html(transcripts, logs, test_results, metrics)

    def log_message(self, format, *args):
        pass  # Suppress default request logging


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CallKaro Dashboard")
    parser.add_argument("--port", type=int, default=PORT, help="Port to serve on")
    args = parser.parse_args()

    print(f"  CallKaro — Dashboard")
    print(f"  {'=' * 40}")
    print(f"  Server:  http://localhost:{args.port}")
    print(f"  Refresh: http://localhost:{args.port}/refresh")
    print()

    server = HTTPServer(("", args.port), DashboardHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Shutting down.")
        server.server_close()
