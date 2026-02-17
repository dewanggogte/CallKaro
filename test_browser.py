"""
test_browser.py — Browser-Based Voice Agent Test
=================================================
Talk to the price enquiry agent from your browser (WebRTC).
No SIP trunk or phone calls needed.

Usage:
  1. Start this server:  python test_browser.py
     (automatically kills old agent workers and starts a fresh one)
  2. Open http://localhost:8080 in your browser
  3. Click "Start Conversation" and allow microphone access
"""

import asyncio
import atexit
import json
import os
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

from dotenv import load_dotenv

# Import dashboard metrics parsers
from dashboard import parse_transcripts, parse_logs, compute_metrics, run_tests
from agent_lifecycle import kill_old_agents, start_agent_worker, cleanup_agent, find_agent_log

load_dotenv(".env.local")

LIVEKIT_URL = os.environ["LIVEKIT_URL"]
LIVEKIT_API_KEY = os.environ["LIVEKIT_API_KEY"]
LIVEKIT_API_SECRET = os.environ["LIVEKIT_API_SECRET"]

PORT = 8080

# ---------------------------------------------------------------------------
# HTML page served at GET /
# ---------------------------------------------------------------------------
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>CallKaro — Price Agent</title>
  <link href="https://fonts.googleapis.com/css2?family=Source+Serif+4:ital,opsz,wght@0,8..60,400;0,8..60,500;0,8..60,600;1,8..60,400&display=swap" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
  <style>
    :root {
      --bg: #fdfcfb; --text: #2c2c2c; --text-light: #666;
      --accent: #b85a3b; --accent-hover: #9a4830;
      --border: #e8e6e3; --surface: #f5f3f0; --surface-dark: #eae7e3;
      --green: #4a9; --red: #c0392b; --yellow: #b8860b;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: "Source Serif 4", Georgia, serif;
      background: var(--bg); color: var(--text);
      font-size: 18px; line-height: 1.7; min-height: 100vh;
    }

    /* ---- Site header ---- */
    .site-header { margin-bottom: 1rem; }
    .site-header h1 { font-size: 1.5rem; font-weight: 500; margin-bottom: .25rem; }
    .site-header h1 a { color: var(--text); text-decoration: none; }
    .site-header h1 a:hover { color: var(--accent); }

    /* ---- Tab bar ---- */
    .tab-bar {
      display: flex; gap: 2rem;
      border-bottom: 1px solid var(--border); padding: .5rem 0 0;
    }
    .tab-btn {
      padding: .5rem 0; border: none; background: none; color: var(--text-light);
      font-family: "Source Serif 4", Georgia, serif;
      font-size: .85rem; font-weight: 500; cursor: pointer; transition: .15s;
      text-transform: uppercase; letter-spacing: .08em;
      border-bottom: 2px solid transparent;
    }
    .tab-btn:hover { color: var(--accent); }
    .tab-btn.active { color: var(--text); border-bottom-color: var(--accent); }
    .tab-content { display: none; }
    .tab-content.active { display: block; }

    /* ---- Voice tab ---- */
    .voice-wrap { max-width: 640px; margin: 0 auto; padding: 4rem 3.5rem; }
    .card { width: 100%; }
    .card h1 { font-size: 1.5rem; font-weight: 500; margin-bottom: .25rem; }
    .subtitle { color: var(--text-light); font-size: 1rem; margin-bottom: .5rem; }
    .bio { margin-bottom: 2rem; }
    .bio p { margin-bottom: 1rem; }
    .bio p:last-child { margin-bottom: 0; }
    .btn-row { display: flex; gap: .75rem; margin-bottom: 1.5rem; }
    .btn {
      display: inline-block; padding: .6rem 1.5rem; border: 1px solid var(--border);
      border-radius: 4px; font-family: "Source Serif 4", Georgia, serif;
      font-size: .9rem; font-weight: 500; cursor: pointer; transition: .15s;
      flex: 1; text-align: center;
    }
    .btn:disabled { opacity: .35; cursor: not-allowed; }
    .btn-start { background: var(--accent); color: #fff; border-color: var(--accent); }
    .btn-start:hover:not(:disabled) { background: var(--accent-hover); border-color: var(--accent-hover); }
    .btn-end { background: var(--bg); color: var(--red); border-color: var(--border); }
    .btn-end:hover:not(:disabled) { background: var(--surface); border-color: var(--red); }
    #status {
      padding: .75rem 1rem; border-radius: 4px;
      background: var(--surface); font-size: .9rem;
      min-height: 2.5rem; line-height: 1.6; border: 1px solid var(--border);
    }
    .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
           margin-right: 8px; vertical-align: middle; }
    .dot-idle { background: var(--text-light); }
    .dot-connecting { background: var(--yellow); animation: pulse 1s infinite; }
    .dot-connected { background: var(--green); }
    .dot-error { background: var(--red); }
    @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .3; } }
    .viz-row { display: flex; gap: 1rem; margin-top: 1.5rem; }
    .viz-box {
      flex: 1; background: var(--surface); border-radius: 4px;
      padding: .75rem; text-align: center; border: 1px solid var(--border);
    }
    .viz-label {
      font-size: .7rem; color: var(--text-light); margin-bottom: .35rem;
      text-transform: uppercase; letter-spacing: .08em;
    }
    .viz-box canvas { width: 100%; height: 48px; display: block; border-radius: 2px; }
    #log {
      margin-top: 1.5rem; background: var(--surface); border-radius: 4px;
      padding: .75rem 1rem; max-height: 180px; overflow-y: auto;
      font-family: "SF Mono", "Fira Code", ui-monospace, monospace;
      font-size: .7rem; line-height: 1.7; color: var(--text-light);
      border: 1px solid var(--border);
    }
    #log:empty { display: none; }
    .log-event { color: #5b7fa4; }
    .log-error { color: var(--red); }
    .log-info { color: #4a7a5b; }
    details { margin-top: 1.5rem; padding-top: 1.5rem; border-top: 1px solid var(--border); }
    details summary { cursor: pointer; color: var(--text-light); font-size: .85rem; font-weight: 500; }
    details summary:hover { color: var(--accent); }
    .log-controls { margin-top: .5rem; display: flex; gap: .75rem; align-items: center; }
    .log-controls button {
      background: var(--surface-dark); color: var(--text); border: 1px solid var(--border);
      border-radius: 4px; padding: .25rem .75rem; cursor: pointer;
      font-family: "Source Serif 4", Georgia, serif; font-size: .75rem;
    }
    .log-controls button:hover { background: var(--border); }
    .log-controls label { color: var(--text-light); font-size: .75rem; display: flex; align-items: center; gap: .3rem; }
    #agentLogs {
      margin-top: .75rem; background: var(--surface); border: 1px solid var(--border);
      border-radius: 4px; padding: .75rem; max-height: 300px; overflow: auto;
      font-family: "SF Mono", "Fira Code", ui-monospace, monospace;
      font-size: .65rem; color: var(--text-light); line-height: 1.5;
      white-space: pre-wrap; word-break: break-all;
    }
    .links-section { padding-top: 1.5rem; border-top: 1px solid var(--border); margin-top: 2rem; }
    .footer-row {
      display: flex; flex-wrap: wrap; align-items: baseline;
      justify-content: center; gap: 2rem;
    }
    .footer-row > a {
      font-size: .85rem; font-weight: 500; text-transform: uppercase;
      letter-spacing: .08em; color: var(--text-light); text-decoration: none;
    }
    .footer-row > a:hover { color: var(--accent); text-decoration: none; }
    .elsewhere {
      display: inline-flex; flex-wrap: wrap; align-items: center; gap: .5rem;
    }
    .elsewhere-label {
      font-size: .85rem; font-weight: 500; text-transform: uppercase;
      letter-spacing: .08em; color: var(--text-light);
    }
    .elsewhere a { font-size: .7rem; color: var(--text-light); text-decoration: none; }
    .elsewhere a:hover { color: var(--accent); }
    .elsewhere a.icon-link {
      display: inline-flex; align-items: center; margin-left: .5rem;
    }
    .elsewhere a.icon-link svg { width: 16px; height: 16px; }

    /* ---- Dashboard tab ---- */
    .dash-wrap { padding: 2rem 1.5rem; max-width: 960px; margin: 0 auto; }
    .dash-wrap h2 { font-size: 1rem; margin-bottom: .75rem; color: var(--text); font-weight: 500; }
    .dgrid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 1rem; margin-bottom: 1.5rem; }
    .dcard { background: var(--bg); border-radius: 4px; padding: 1.25rem; border: 1px solid var(--border); }
    .dcard-wide { grid-column: 1 / -1; }
    .stat { font-size: 2rem; font-weight: 600; color: var(--text); }
    .stat-label { font-size: .85rem; color: var(--text-light); margin-top: 4px; }
    .stat-row { display: flex; gap: 1.5rem; margin-top: .75rem; }
    .stat-item { text-align: center; }
    .stat-item .value { font-size: 1.1rem; font-weight: 500; color: var(--text); }
    .stat-item .label { font-size: .7rem; color: var(--text-light); text-transform: uppercase; letter-spacing: .05em; }
    .badge { display: inline-block; padding: 3px 10px; border-radius: 3px; font-size: .8rem; font-weight: 500; }
    .badge-green { background: #e6f4ea; color: #1a7a3a; }
    .badge-red { background: #fde8e8; color: #a12828; }
    .badge-yellow { background: #fef3d6; color: #8a6d0b; }
    .dash-wrap table { width: 100%; border-collapse: collapse; font-size: .85rem; }
    .dash-wrap th { text-align: left; padding: 8px; color: var(--text-light); border-bottom: 1px solid var(--border); font-weight: 500; }
    .dash-wrap td { padding: 8px; border-bottom: 1px solid var(--border); }
    .dash-wrap pre {
      background: var(--surface); padding: 1rem; border-radius: 4px; font-size: .75rem;
      overflow-x: auto; max-height: 300px; overflow-y: auto; color: var(--text-light);
      border: 1px solid var(--border);
    }
    .dash-wrap canvas { max-height: 200px; }
    .dash-loading { text-align: center; padding: 3rem; color: var(--text-light); }
    .dash-refresh {
      background: var(--surface-dark); color: var(--text); border: 1px solid var(--border);
      padding: 6px 14px; border-radius: 4px; cursor: pointer;
      font-family: "Source Serif 4", Georgia, serif; font-size: .8rem; margin-bottom: 1rem;
    }
    .dash-refresh:hover { background: var(--border); }
  </style>
</head>
<body>

  <div class="voice-wrap">
    <div class="card">
      <header class="site-header">
        <h1><a href="https://dewanggogte.com" target="_blank">Dewang Gogte</a></h1>
      </header>

      <!-- Tab bar -->
      <div class="tab-bar">
        <button class="tab-btn active" onclick="switchTab('voice')">Voice</button>
        <button class="tab-btn" onclick="switchTab('dashboard')">Dashboard</button>
      </div>
    </div>
  </div>

  <!-- Voice Tab -->
  <div id="tab-voice" class="tab-content active">
    <div class="voice-wrap" style="padding-top:0">
      <div class="card">
        <h1>CallKaro</h1>
        <p class="subtitle">Voice price enquiry agent</p>

        <section class="bio">
          <p>This is a voice AI agent that calls shops to ask about prices, availability, and installation details — in Hindi. It uses LiveKit for real-time audio, Sarvam AI for Hindi speech recognition and synthesis, and Claude as the conversational brain.</p>
          <p>To test it, click <strong>Start Conversation</strong> and pretend you're a shopkeeper. The agent will greet you, ask about prices, enquire about warranty and installation, and then end the call. Speak in Hindi or Hinglish.</p>
        </section>

        <div class="btn-row">
          <button class="btn btn-start" id="btnStart" onclick="startConversation()">
            Start Conversation
          </button>
          <button class="btn btn-end" id="btnEnd" onclick="endConversation()" disabled>
            End Conversation
          </button>
        </div>

        <div id="status"><span class="dot dot-idle"></span>Ready</div>

        <div class="viz-row">
          <div class="viz-box">
            <div class="viz-label">Your Mic</div>
            <canvas id="micViz"></canvas>
          </div>
          <div class="viz-box">
            <div class="viz-label">Agent Audio</div>
            <canvas id="agentViz"></canvas>
          </div>
        </div>

        <div id="log"></div>

        <details>
          <summary>Agent Worker Logs (click to load)</summary>
          <div class="log-controls">
            <button onclick="loadAgentLogs()">Refresh</button>
            <label>
              <input type="checkbox" id="autoRefresh" onchange="toggleAutoRefresh()"> Auto-refresh
            </label>
          </div>
          <pre id="agentLogs"></pre>
        </details>

        <section class="links-section">
          <div class="footer-row">
            <a href="https://dewanggogte.com" target="_blank">Home</a>
            <a href="https://dewanggogte.com/blog" target="_blank">Blog</a>
            <span class="elsewhere">
              <span class="elsewhere-label">Elsewhere:</span>
              <a href="mailto:dewanggogte@gmail.com" class="icon-link" aria-label="Email"><svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="20" height="16" x="2" y="4" rx="2"/><path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/></svg></a>
              <a href="https://linkedin.com/in/dewanggogte" target="_blank" class="icon-link" aria-label="LinkedIn"><svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433a2.062 2.062 0 0 1-2.063-2.065 2.064 2.064 0 1 1 2.063 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z"/></svg></a>
              <a href="https://instagram.com/dewangraphy" target="_blank" class="icon-link" aria-label="Instagram"><svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="20" height="20" x="2" y="2" rx="5" ry="5"/><path d="M16 11.37A4 4 0 1 1 12.63 8 4 4 0 0 1 16 11.37z"/><line x1="17.5" x2="17.51" y1="6.5" y2="6.5"/></svg></a>
            </span>
          </div>
        </section>
      </div>
    </div>
  </div>

  <!-- Dashboard Tab -->
  <div id="tab-dashboard" class="tab-content">
    <div class="dash-wrap">
      <button class="dash-refresh" onclick="loadDashboard()">Refresh Dashboard</button>
      <div id="dash-content"><div class="dash-loading">Click the Dashboard tab to load metrics...</div></div>
    </div>
  </div>

  <script src="https://cdn.jsdelivr.net/npm/livekit-client/dist/livekit-client.umd.js"></script>
  <script>
    /* ---- Tab switching ---- */
    let dashLoaded = false;
    function switchTab(name) {
      document.querySelectorAll('.tab-btn').forEach((b, i) => {
        b.classList.toggle('active', (name === 'voice' && i === 0) || (name === 'dashboard' && i === 1));
      });
      document.getElementById('tab-voice').classList.toggle('active', name === 'voice');
      document.getElementById('tab-dashboard').classList.toggle('active', name === 'dashboard');
      if (name === 'dashboard' && !dashLoaded) loadDashboard();
    }

    /* ---- Dashboard ---- */
    let ttftChartInst = null, tokenChartInst = null;
    async function loadDashboard() {
      const el = document.getElementById('dash-content');
      el.innerHTML = '<div class="dash-loading">Loading metrics & running tests...</div>';
      try {
        const resp = await fetch('/api/metrics');
        const d = await resp.json();
        dashLoaded = true;
        renderDashboard(d, el);
      } catch(e) {
        el.innerHTML = '<div class="dash-loading" style="color:var(--red)">Failed to load: ' + e.message + '</div>';
      }
    }

    function renderDashboard(d, el) {
      const m = d.metrics;
      const t = d.tests;
      const transcripts = d.transcripts;
      const tRows = transcripts.slice(-10).map(tr =>
        `<tr><td>${tr._filename}</td><td>${tr.store_name}</td><td>${tr._total_messages}</td><td>${tr._duration_seconds}s</td><td>${tr.phone}</td></tr>`
      ).join('') || '<tr><td colspan="5" style="color:var(--text-light)">No transcripts</td></tr>';

      const testOut = (t.output || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

      el.innerHTML = `
        <div class="dgrid">
          <div class="dcard">
            <h2>Test Results</h2>
            <div style="display:flex;gap:12px;align-items:center;">
              <span class="badge badge-green">${t.passed} passed</span>
              <span class="badge ${t.failed > 0 ? 'badge-red' : 'badge-green'}">${t.failed} failed</span>
              <span class="badge badge-yellow">${t.skipped} skipped</span>
            </div>
          </div>
          <div class="dcard">
            <h2>Calls Recorded</h2>
            <div class="stat">${m.total_calls}</div>
            <div class="stat-label">Total conversations</div>
          </div>
          <div class="dcard">
            <h2>Errors</h2>
            <div class="stat" style="color:${m.total_errors > 0 ? 'var(--red)' : 'var(--green)'}">${m.total_errors}</div>
            <div class="stat-label">Across all log files</div>
          </div>
        </div>
        <div class="dgrid">
          <div class="dcard">
            <h2>Time to First Token (TTFT)</h2>
            <div class="stat">${m.ttft.avg}s</div>
            <div class="stat-label">Average</div>
            <div class="stat-row">
              <div class="stat-item"><div class="value">${m.ttft.p50}s</div><div class="label">P50</div></div>
              <div class="stat-item"><div class="value">${m.ttft.p95}s</div><div class="label">P95</div></div>
              <div class="stat-item"><div class="value">${m.ttft.min}s</div><div class="label">Min</div></div>
              <div class="stat-item"><div class="value">${m.ttft.max}s</div><div class="label">Max</div></div>
            </div>
          </div>
          <div class="dcard">
            <h2>LLM Response Duration</h2>
            <div class="stat">${m.llm_duration.avg}s</div>
            <div class="stat-label">Average</div>
            <div class="stat-row">
              <div class="stat-item"><div class="value">${m.llm_duration.p50}s</div><div class="label">P50</div></div>
              <div class="stat-item"><div class="value">${m.llm_duration.p95}s</div><div class="label">P95</div></div>
            </div>
          </div>
          <div class="dcard">
            <h2>Turn Latency</h2>
            <div class="stat">${m.turn_latency.avg}s</div>
            <div class="stat-label">User speech to agent response</div>
            <div class="stat-row">
              <div class="stat-item"><div class="value">${m.turn_latency.p50}s</div><div class="label">P50</div></div>
              <div class="stat-item"><div class="value">${m.turn_latency.p95}s</div><div class="label">P95</div></div>
            </div>
          </div>
        </div>
        <div class="dgrid">
          <div class="dcard"><h2>TTFT Distribution</h2><canvas id="dashTtft"></canvas></div>
          <div class="dcard"><h2>Token Usage per Turn</h2><canvas id="dashTokens"></canvas></div>
        </div>
        <div class="dgrid">
          <div class="dcard">
            <h2>Conversation Duration</h2>
            <div class="stat">${m.conv_duration.avg}s</div>
            <div class="stat-label">Average call length</div>
          </div>
          <div class="dcard">
            <h2>Messages per Call</h2>
            <div class="stat">${m.msg_counts.avg}</div>
            <div class="stat-label">Average</div>
          </div>
          <div class="dcard">
            <h2>Token Efficiency</h2>
            <div class="stat">${m.prompt_tokens.avg}</div>
            <div class="stat-label">Avg prompt tokens/turn</div>
            <div class="stat-row">
              <div class="stat-item"><div class="value">${m.completion_tokens.avg}</div><div class="label">Avg completion</div></div>
              <div class="stat-item"><div class="value">${m.prompt_tokens.max}</div><div class="label">Max prompt</div></div>
            </div>
          </div>
        </div>
        <div class="dgrid">
          <div class="dcard dcard-wide">
            <h2>Recent Transcripts</h2>
            <table><tr><th>File</th><th>Store</th><th>Messages</th><th>Duration</th><th>Channel</th></tr>${tRows}</table>
          </div>
        </div>
        <div class="dgrid">
          <div class="dcard dcard-wide">
            <h2>Test Output</h2>
            <pre>${testOut}</pre>
          </div>
        </div>
      `;

      // Render charts
      const ttftData = m.all_ttfts || [];
      if (ttftData.length > 0) {
        if (ttftChartInst) ttftChartInst.destroy();
        ttftChartInst = new Chart(document.getElementById('dashTtft'), {
          type: 'bar',
          data: { labels: ttftData.map((_,i) => 'T'+(i+1)), datasets: [{ label: 'TTFT (s)', data: ttftData, backgroundColor: '#b85a3b', borderRadius: 4 }] },
          options: { responsive: true, plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true, grid: { color: '#e8e6e3' }, ticks: { color: '#666' } }, x: { grid: { display: false }, ticks: { color: '#666', maxRotation: 0, autoSkip: true, maxTicksLimit: 20 } } } }
        });
      }
      const pt = m.all_prompt_tokens || [], ct = m.all_completion_tokens || [];
      if (pt.length > 0) {
        if (tokenChartInst) tokenChartInst.destroy();
        tokenChartInst = new Chart(document.getElementById('dashTokens'), {
          type: 'bar',
          data: { labels: pt.map((_,i) => 'T'+(i+1)), datasets: [
            { label: 'Prompt', data: pt, backgroundColor: '#b85a3b', borderRadius: 4 },
            { label: 'Completion', data: ct, backgroundColor: '#4a9', borderRadius: 4 }
          ]},
          options: { responsive: true, plugins: { legend: { labels: { color: '#666' } } }, scales: { y: { stacked: true, beginAtZero: true, grid: { color: '#e8e6e3' }, ticks: { color: '#666' } }, x: { stacked: true, grid: { display: false }, ticks: { color: '#666', maxRotation: 0, autoSkip: true, maxTicksLimit: 20 } } } }
        });
      }
    }

    /* ---- Voice test ---- */
    const { Room, RoomEvent, Track, ParticipantEvent } = LivekitClient;
    let room = null;
    let micAnalyser = null, agentAnalyser = null;
    let vizRAF = null;
    const audioCtx = new (window.AudioContext || window.webkitAudioContext)();

    function setStatus(text, state) {
      document.getElementById('status').innerHTML =
        `<span class="dot dot-${state}"></span>${text}`;
    }

    function log(msg, cls = 'log-event') {
      const el = document.getElementById('log');
      const ts = new Date().toLocaleTimeString('en', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
      el.innerHTML += `<div class="${cls}"><span style="color:var(--text-light)">${ts}</span> ${msg}</div>`;
      el.scrollTop = el.scrollHeight;
    }

    function createAnalyser(mediaStream) {
      const src = audioCtx.createMediaStreamSource(mediaStream);
      const analyser = audioCtx.createAnalyser();
      analyser.fftSize = 256;
      src.connect(analyser);
      return analyser;
    }

    function drawBars(canvas, analyser, color) {
      if (!analyser) return;
      const ctx = canvas.getContext('2d');
      const W = canvas.width, H = canvas.height;
      const buf = new Uint8Array(analyser.frequencyBinCount);
      analyser.getByteFrequencyData(buf);
      ctx.clearRect(0, 0, W, H);
      const bars = 32, step = Math.floor(buf.length / bars), barW = W / bars - 1;
      for (let i = 0; i < bars; i++) {
        const v = buf[i * step] / 255, h = Math.max(2, v * H);
        ctx.fillStyle = v > 0.05 ? color : '#e8e6e3';
        ctx.fillRect(i * (barW + 1), H - h, barW, h);
      }
    }

    function vizLoop() {
      drawBars(document.getElementById('micViz'), micAnalyser, '#4a9');
      drawBars(document.getElementById('agentViz'), agentAnalyser, '#b85a3b');
      vizRAF = requestAnimationFrame(vizLoop);
    }

    function initCanvasSize() {
      for (const id of ['micViz', 'agentViz']) {
        const c = document.getElementById(id);
        c.width = c.offsetWidth * devicePixelRatio;
        c.height = c.offsetHeight * devicePixelRatio;
      }
    }

    async function startConversation() {
      const btnStart = document.getElementById('btnStart');
      const btnEnd = document.getElementById('btnEnd');
      btnStart.disabled = true;
      document.getElementById('log').innerHTML = '';
      setStatus('Requesting token...', 'connecting');
      log('Requesting token...');
      try {
        const resp = await fetch('/api/token');
        if (!resp.ok) throw new Error(await resp.text());
        const data = await resp.json();
        log(`Room: ${data.room}`, 'log-info');
        room = new Room({ audioCaptureDefaults: { echoCancellation: true, noiseSuppression: true }, audioOutput: { deviceId: 'default' } });
        room.on(RoomEvent.TrackSubscribed, (track, pub, participant) => {
          log(`Track subscribed: ${track.kind} from ${participant.identity}`);
          if (track.kind === Track.Kind.Audio) {
            const el = track.attach(); el.id = 'agent-audio'; document.body.appendChild(el);
            agentAnalyser = createAnalyser(new MediaStream([track.mediaStreamTrack]));
            setStatus('Agent connected — speak now!', 'connected');
            log('Agent audio playing', 'log-info');
          }
        });
        room.on(RoomEvent.TrackUnsubscribed, (track, pub, p) => log(`Track unsubscribed: ${track.kind} from ${p.identity}`));
        room.on(RoomEvent.ParticipantConnected, p => log(`Participant joined: ${p.identity}`));
        room.on(RoomEvent.ParticipantDisconnected, p => log(`Participant left: ${p.identity}`));
        room.on(RoomEvent.Disconnected, reason => { log(`Disconnected: ${reason}`, 'log-error'); setStatus('Disconnected', 'idle'); btnStart.disabled = false; btnEnd.disabled = true; cleanup(); });
        room.on(RoomEvent.AudioPlaybackStatusChanged, () => log(`Audio playback allowed: ${room.canPlaybackAudio}`));
        setStatus('Connecting to LiveKit...', 'connecting');
        log('Connecting to LiveKit...');
        await audioCtx.resume();
        await room.connect(data.url, data.token);
        log(`Connected as ${room.localParticipant.identity}`, 'log-info');
        await room.localParticipant.setMicrophoneEnabled(true);
        log('Microphone enabled', 'log-info');
        initCanvasSize();
        const micTrack = room.localParticipant.getTrackPublication(Track.Source.Microphone);
        if (micTrack && micTrack.track) micAnalyser = createAnalyser(new MediaStream([micTrack.track.mediaStreamTrack]));
        vizLoop();
        setStatus('Connected — waiting for agent...', 'connecting');
        btnEnd.disabled = false;
      } catch (err) {
        console.error(err); log(`Error: ${err.message}`, 'log-error');
        setStatus('Error: ' + err.message, 'error'); btnStart.disabled = false; btnEnd.disabled = true;
      }
    }

    async function endConversation() {
      if (room) { log('Ending conversation...'); await room.disconnect(); cleanup(); }
      document.getElementById('btnStart').disabled = false;
      document.getElementById('btnEnd').disabled = true;
      setStatus('Ended', 'idle');
    }

    function cleanup() {
      const el = document.getElementById('agent-audio'); if (el) el.remove();
      room = null; micAnalyser = null; agentAnalyser = null;
      if (vizRAF) { cancelAnimationFrame(vizRAF); vizRAF = null; }
    }

    /* ---- Agent logs viewer ---- */
    let autoRefreshId = null;
    async function loadAgentLogs() {
      const el = document.getElementById('agentLogs');
      try {
        const resp = await fetch('/api/logs?n=150');
        const data = await resp.json();
        el.innerHTML = data.lines.map(l => {
          let cls = 'color:var(--text-light)';
          if (/ERROR|error|ERRO/i.test(l)) cls = 'color:#c0392b';
          else if (/WARN/i.test(l)) cls = 'color:#b8860b';
          else if (/INFO/i.test(l)) cls = 'color:#666';
          else if (/user_transcript|transcript/i.test(l)) cls = 'color:#4a7a5b';
          else if (/TTS|tts|bulbul/i.test(l)) cls = 'color:#5b7fa4';
          return `<span style="${cls}">${l.replace(/</g,'&lt;')}</span>`;
        }).join('\n');
        el.scrollTop = el.scrollHeight;
        if (data.file) el.title = data.file;
      } catch(e) { el.textContent = 'Failed to fetch logs: ' + e.message; }
    }
    function toggleAutoRefresh() {
      if (document.getElementById('autoRefresh').checked) {
        loadAgentLogs(); autoRefreshId = setInterval(loadAgentLogs, 3000);
      } else { clearInterval(autoRefreshId); autoRefreshId = null; }
    }
    document.querySelector('details').addEventListener('toggle', e => { if (e.target.open) loadAgentLogs(); });
  </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP request handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self._serve_html()
        elif self.path == "/api/token":
            self._serve_token()
        elif self.path == "/api/metrics":
            self._serve_metrics()
        elif self.path.startswith("/api/logs"):
            self._serve_logs()
        else:
            self.send_error(404)

    def _serve_html(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(HTML_PAGE.encode())

    def _serve_token(self):
        try:
            token_data = asyncio.run(create_token_and_dispatch())
            body = json.dumps(token_data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(f"Token error: {e}".encode())

    def _serve_metrics(self):
        """Return dashboard metrics (transcripts, logs, test results) as JSON."""
        try:
            transcripts = parse_transcripts()
            log_data = parse_logs()
            metrics = compute_metrics(transcripts, log_data)
            tests = run_tests()
            body = json.dumps({
                "metrics": metrics,
                "tests": tests,
                "transcripts": transcripts,
            }, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def _serve_logs(self):
        """Return the last N lines of agent worker logs as JSON."""
        from urllib.parse import urlparse, parse_qs
        params = parse_qs(urlparse(self.path).query)
        n = int(params.get("n", ["200"])[0])

        log_file = find_agent_log()
        if not log_file:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"file": None, "lines": ["No agent log file found. Is the agent worker running?"]}).encode())
            return

        try:
            with open(log_file, "r", errors="replace") as f:
                all_lines = f.readlines()
            lines = [l.rstrip() for l in all_lines[-n:]]
        except Exception as e:
            lines = [f"Error reading log: {e}"]

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({"file": log_file, "lines": lines}).encode())

    def log_message(self, format, *args):
        # Friendlier log format
        print(f"  [{self.address_string()}] {format % args}")


# ---------------------------------------------------------------------------
# Token generation + agent dispatch
# ---------------------------------------------------------------------------
async def create_token_and_dispatch():
    from livekit.api import LiveKitAPI, AccessToken, VideoGrants
    from livekit.protocol.agent_dispatch import CreateAgentDispatchRequest

    room_name = f"browser-test-{uuid.uuid4().hex[:8]}"
    user_identity = f"user-{uuid.uuid4().hex[:6]}"

    # 1. Create a user token for the browser participant
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

    # 2. Dispatch the agent into the room
    lk = LiveKitAPI()
    try:
        await lk.agent_dispatch.create_dispatch(
            CreateAgentDispatchRequest(
                agent_name="price-agent",
                room=room_name,
                metadata=json.dumps({
                    "store_name": "Browser Test",
                    "product_description": "appliance",
                    "nearby_area": "Koramangala 4th Block",
                }),
            )
        )
    finally:
        await lk.aclose()

    return {
        "token": token,
        "url": LIVEKIT_URL,
        "room": room_name,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"\n  CallKaro — Price Agent")
    print(f"  {'=' * 40}")

    # Auto-manage agent worker
    kill_old_agents()
    start_agent_worker()
    atexit.register(cleanup_agent)

    print(f"  Server:  http://localhost:{PORT}")
    print(f"  LiveKit: {LIVEKIT_URL}")
    print()

    server = HTTPServer(("", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Shutting down.")
        server.server_close()
