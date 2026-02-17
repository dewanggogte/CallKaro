"""
app.py — Product Research Pipeline HTTP Server
===============================================
Multi-step wizard UI + API endpoints for the generalized
product research pipeline. Serves a 4-step wizard frontend
and manages pipeline sessions.

Usage:
  python app.py              # Start on port 8080
  python app.py --port 9000  # Custom port

Endpoints:
  GET  /                           → Wizard UI
  POST /api/session                → Create new session
  POST /api/session/{id}/chat      → Intake chat message
  POST /api/session/{id}/research  → Trigger research + discovery
  POST /api/session/{id}/call/{n}  → Start call simulation
  POST /api/session/{id}/analyze   → Cross-store comparison
  GET  /api/session/{id}/status    → Pipeline state
  GET  /api/token                  → Quick-call agent token
  GET  /api/metrics                → Dashboard metrics
  GET  /api/logs                   → Agent worker logs
"""

import asyncio
import atexit
import json
import os
import sys
import threading
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from dotenv import load_dotenv

import logging

from agent_lifecycle import kill_old_agents, start_agent_worker, cleanup_agent, find_agent_log

load_dotenv(".env.local")

_logger = logging.getLogger("pipeline.app")

LIVEKIT_URL = os.environ.get("LIVEKIT_URL", "")
LIVEKIT_API_KEY = os.environ.get("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.environ.get("LIVEKIT_API_SECRET", "")

PORT = 8080

# In-memory session storage
_sessions: dict = {}  # session_id → PipelineSession


def _get_or_none(session_id: str):
    """Get session, cleaning up expired ones."""
    session = _sessions.get(session_id)
    if session and session.is_expired():
        del _sessions[session_id]
        return None
    return session


# ---------------------------------------------------------------------------
# HTML page — 4-step wizard
# ---------------------------------------------------------------------------
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>CallKaro — Product Research Pipeline</title>
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
    .wrap { max-width: 720px; margin: 0 auto; padding: 2rem 2rem 6rem; }

    /* Header */
    .site-header { margin-bottom: .5rem; }
    .site-header h1 { font-size: 1.5rem; font-weight: 500; }
    .site-header h1 a { color: var(--text); text-decoration: none; }
    .site-header h1 a:hover { color: var(--accent); }

    /* Tab bar */
    .tab-bar {
      display: flex; gap: 2rem;
      border-bottom: 1px solid var(--border); padding: .5rem 0 0; margin-bottom: 1.5rem;
    }
    .tab-btn {
      padding: .5rem 0; border: none; background: none; color: var(--text-light);
      font-family: "Source Serif 4", Georgia, serif;
      font-size: .85rem; font-weight: 500; cursor: pointer;
      text-transform: uppercase; letter-spacing: .08em;
      border-bottom: 2px solid transparent;
    }
    .tab-btn:hover { color: var(--accent); }
    .tab-btn.active { color: var(--text); border-bottom-color: var(--accent); }
    .tab-content { display: none; }
    .tab-content.active { display: block; }

    /* Steps */
    .steps { display: flex; gap: 0; margin-bottom: 2rem; }
    .step-indicator {
      flex: 1; text-align: center; padding: .5rem .25rem;
      font-size: .75rem; color: var(--text-light); position: relative;
      text-transform: uppercase; letter-spacing: .05em; font-weight: 500;
    }
    .step-indicator::after {
      content: ''; position: absolute; bottom: 0; left: 0; right: 0;
      height: 3px; background: var(--border); border-radius: 2px;
    }
    .step-indicator.active { color: var(--accent); }
    .step-indicator.active::after { background: var(--accent); }
    .step-indicator.done { color: var(--green); cursor: pointer; }
    .step-indicator.done::after { background: var(--green); }
    .step-indicator.done:hover { color: var(--accent); }
    .step-indicator.active { cursor: default; }
    .step-panel { display: none; }
    .step-panel.active { display: block; }

    /* Cards */
    .card {
      background: var(--bg); border: 1px solid var(--border);
      border-radius: 6px; padding: 1.25rem; margin-bottom: 1rem;
    }
    .card h3 { font-size: 1rem; font-weight: 500; margin-bottom: .5rem; }
    .card p { font-size: .9rem; color: var(--text-light); }

    /* Chat */
    .chat-messages {
      max-height: 350px; overflow-y: auto; margin-bottom: 1rem;
      padding: .5rem; background: var(--surface); border-radius: 6px;
      border: 1px solid var(--border);
    }
    .chat-msg { margin-bottom: .75rem; font-size: .9rem; line-height: 1.6; }
    .chat-msg.user { text-align: right; }
    .chat-msg .bubble {
      display: inline-block; max-width: 85%; padding: .5rem .85rem;
      border-radius: 12px; text-align: left;
    }
    .chat-msg.user .bubble { background: var(--accent); color: #fff; border-bottom-right-radius: 4px; }
    .chat-msg.assistant .bubble { background: #fff; border: 1px solid var(--border); border-bottom-left-radius: 4px; }
    .chat-input-row { display: flex; gap: .5rem; }
    .chat-input {
      flex: 1; padding: .6rem .85rem; border: 1px solid var(--border);
      border-radius: 6px; font-family: "Source Serif 4", Georgia, serif;
      font-size: .9rem; outline: none;
    }
    .chat-input:focus { border-color: var(--accent); }

    /* Buttons */
    .btn {
      display: inline-block; padding: .55rem 1.2rem; border: 1px solid var(--border);
      border-radius: 4px; font-family: "Source Serif 4", Georgia, serif;
      font-size: .85rem; font-weight: 500; cursor: pointer; transition: .15s;
    }
    .btn:disabled { opacity: .35; cursor: not-allowed; }
    .btn-primary { background: var(--accent); color: #fff; border-color: var(--accent); }
    .btn-primary:hover:not(:disabled) { background: var(--accent-hover); border-color: var(--accent-hover); }
    .btn-secondary { background: var(--bg); color: var(--text); }
    .btn-secondary:hover:not(:disabled) { background: var(--surface); }
    .btn-danger { background: var(--bg); color: var(--red); border-color: var(--border); }
    .btn-danger:hover:not(:disabled) { background: var(--surface); border-color: var(--red); }
    .btn-row { display: flex; gap: .75rem; margin-top: 1rem; }

    /* Store list */
    .store-card {
      border: 1px solid var(--border); border-radius: 6px; padding: 1rem;
      margin-bottom: .75rem; cursor: pointer; transition: .15s;
    }
    .store-card:hover { border-color: var(--accent); }
    .store-card.selected { border-color: var(--accent); background: #faf6f3; }
    .store-card h4 { font-size: .95rem; font-weight: 500; margin-bottom: .25rem; }
    .store-meta { font-size: .8rem; color: var(--text-light); }
    .store-meta span { margin-right: 1rem; }

    /* Research card */
    .research-item { margin-bottom: .5rem; font-size: .9rem; }
    .research-item strong { font-weight: 500; }
    .tag { display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: .75rem; margin-right: .35rem; margin-bottom: .25rem; }
    .tag-topic { background: #e8f4f0; color: #2a7a5a; }
    .tag-note { background: #fef3d6; color: #8a6d0b; }

    /* Voice UI */
    .viz-row { display: flex; gap: 1rem; margin: 1rem 0; }
    .viz-box {
      flex: 1; background: var(--surface); border-radius: 4px;
      padding: .6rem; text-align: center; border: 1px solid var(--border);
    }
    .viz-label { font-size: .7rem; color: var(--text-light); margin-bottom: .25rem; text-transform: uppercase; letter-spacing: .05em; }
    .viz-box canvas { width: 100%; height: 40px; display: block; }
    #call-log {
      background: var(--surface); border: 1px solid var(--border);
      border-radius: 4px; padding: .6rem; max-height: 150px; overflow-y: auto;
      font-family: "SF Mono", "Fira Code", monospace; font-size: .7rem;
      line-height: 1.6; color: var(--text-light); margin-top: .75rem;
    }
    #call-log:empty { display: none; }

    /* Pipeline event log */
    .event-log-wrap {
      position: fixed; bottom: 0; left: 0; right: 0;
      background: #1a1a2e; color: #e0e0e0;
      font-family: "SF Mono", "Fira Code", ui-monospace, monospace;
      font-size: .72rem; line-height: 1.5;
      z-index: 100; transition: height 0.2s;
      border-top: 2px solid var(--accent);
    }
    .event-log-wrap.collapsed { height: 32px; overflow: hidden; }
    .event-log-wrap.expanded { height: 220px; }
    .event-log-header {
      display: flex; justify-content: space-between; align-items: center;
      padding: 4px 12px; cursor: pointer; user-select: none;
      background: #12122a; color: #aaa; font-size: .7rem;
    }
    .event-log-header:hover { background: #1e1e3a; }
    .event-log-body {
      overflow-y: auto; height: calc(100% - 28px); padding: 4px 12px;
    }
    .ev { white-space: nowrap; }
    .ev-time { color: #666; margin-right: 6px; }
    .ev-phase {
      display: inline-block; min-width: 100px; padding: 0 6px;
      border-radius: 3px; text-align: center; margin-right: 6px;
      font-size: .65rem; font-weight: 600; text-transform: uppercase;
    }
    .ev-phase-intake { background: #2a4a6a; color: #7ab8f5; }
    .ev-phase-research { background: #2a5a3a; color: #7af5a5; }
    .ev-phase-store_discovery { background: #5a4a2a; color: #f5c87a; }
    .ev-phase-web_search { background: #5a3a2a; color: #f5a07a; }
    .ev-phase-call { background: #4a2a5a; color: #c87af5; }
    .ev-phase-analysis { background: #2a5a5a; color: #7af5e0; }
    .ev-phase-session { background: #4a4a4a; color: #ccc; }
    .ev-phase-prompt_builder { background: #3a3a5a; color: #a0a0f5; }
    .ev-msg { color: #d0d0d0; }
    .ev-warning .ev-msg { color: #f5c87a; }
    .ev-error .ev-msg { color: #f57a7a; }
    .ev-count { background: var(--accent); color: white; padding: 1px 7px; border-radius: 10px; font-size: .6rem; margin-left: 8px; }
    .call-status {
      padding: .5rem .75rem; border-radius: 4px;
      background: var(--surface); font-size: .85rem;
      border: 1px solid var(--border);
    }
    .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }
    .dot-idle { background: var(--text-light); }
    .dot-connecting { background: var(--yellow); animation: pulse 1s infinite; }
    .dot-connected { background: var(--green); }
    @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.3} }

    /* Results table */
    .results-table { width: 100%; border-collapse: collapse; font-size: .85rem; }
    .results-table th { text-align: left; padding: 8px; color: var(--text-light); border-bottom: 1px solid var(--border); font-weight: 500; }
    .results-table td { padding: 8px; border-bottom: 1px solid var(--border); }
    .rank-badge { display: inline-block; width: 24px; height: 24px; line-height: 24px; text-align: center; border-radius: 50%; font-size: .75rem; font-weight: 600; }
    .rank-1 { background: #ffd700; color: #333; }
    .rank-2 { background: #c0c0c0; color: #333; }
    .rank-3 { background: #cd7f32; color: #fff; }

    /* Loading */
    .loading { text-align: center; padding: 2rem; color: var(--text-light); }
    .spinner { display: inline-block; width: 20px; height: 20px; border: 2px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: spin .8s linear infinite; margin-right: .5rem; vertical-align: middle; }
    @keyframes spin { to { transform: rotate(360deg); } }

    /* Dashboard */
    .dash-wrap { padding: 0; }
    .dgrid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 1rem; margin-bottom: 1.5rem; }
    .dcard { background: var(--bg); border-radius: 4px; padding: 1rem; border: 1px solid var(--border); }
    .dcard-wide { grid-column: 1 / -1; }
    .stat { font-size: 1.8rem; font-weight: 600; }
    .stat-label { font-size: .8rem; color: var(--text-light); margin-top: 2px; }
    .badge { display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: .75rem; font-weight: 500; }
    .badge-green { background: #e6f4ea; color: #1a7a3a; }
    .badge-red { background: #fde8e8; color: #a12828; }

    .links-section { padding-top: 1.5rem; border-top: 1px solid var(--border); margin-top: 2rem; }
    .footer-row { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: center; gap: 2rem; }
    .footer-row > a { font-size: .85rem; font-weight: 500; text-transform: uppercase; letter-spacing: .08em; color: var(--text-light); text-decoration: none; }
    .footer-row > a:hover { color: var(--accent); }

    /* Live transcript panel (Item 1) */
    .call-transcript {
      background: var(--surface); border: 1px solid var(--border);
      border-radius: 6px; padding: .75rem; margin: .75rem 0;
      max-height: 250px; overflow-y: auto;
    }
    .call-transcript:empty { display: none; }
    .transcript-msg { margin-bottom: .5rem; font-size: .85rem; line-height: 1.5; }
    .transcript-msg .speaker {
      font-weight: 600; font-size: .75rem; text-transform: uppercase;
      letter-spacing: .05em; margin-bottom: 2px;
    }
    .transcript-msg.agent .speaker { color: var(--accent); }
    .transcript-msg.user .speaker { color: var(--green); }

    /* Research progress (Item 2) */
    .research-progress {
      margin-top: .75rem; text-align: left; max-width: 600px; margin-left: auto; margin-right: auto;
      max-height: 300px; overflow-y: auto; padding: .5rem;
      background: #f8f7f5; border: 1px solid var(--border); border-radius: 6px;
    }
    .research-progress:empty { display: none; }
    .research-progress .progress-item {
      padding: .15rem 0; font-size: .78rem; color: var(--text);
      display: flex; align-items: baseline; gap: .4rem;
      line-height: 1.5;
    }
    .research-progress .progress-item .check { color: var(--green); font-size: .7rem; }

    /* Call timer (Item 3) */
    .call-timer {
      font-family: "SF Mono", "Fira Code", monospace;
      font-size: .85rem; color: var(--text-light); margin-left: .5rem;
      font-variant-numeric: tabular-nums;
    }
    .call-timer.active { color: var(--accent); font-weight: 600; }

    /* Store selection counter (Item 4) */
    .store-counter {
      font-size: .85rem; color: var(--text-light); margin: .5rem 0;
      padding: .4rem .75rem; background: var(--surface); border-radius: 4px;
      border: 1px solid var(--border);
    }
    .store-counter strong { color: var(--accent); }

    /* Confirmation modal (Item 4) */
    .modal-overlay {
      display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0;
      background: rgba(0,0,0,.45); z-index: 200;
      justify-content: center; align-items: center;
    }
    .modal-overlay.show { display: flex; }
    .modal-box {
      background: var(--bg); border-radius: 8px; padding: 1.5rem;
      max-width: 420px; width: 90%; box-shadow: 0 8px 32px rgba(0,0,0,.2);
    }
    .modal-box h3 { font-size: 1.1rem; font-weight: 500; margin-bottom: .75rem; }
    .modal-store-list { font-size: .85rem; margin: .75rem 0; padding-left: 1.25rem; }
    .modal-store-list li { margin-bottom: .25rem; }
    .modal-actions { display: flex; gap: .75rem; margin-top: 1rem; }

    /* Best deal highlight (Item 5) */
    .results-table tr.best-deal { background: #f0f9f4; }
    .best-deal-badge {
      display: inline-block; padding: 2px 8px; border-radius: 3px;
      font-size: .7rem; font-weight: 600; background: var(--green); color: #fff;
      text-transform: uppercase; letter-spacing: .03em; margin-left: .35rem;
    }
    .savings-note {
      font-size: .85rem; color: #1a7a3a; margin-top: .75rem;
      padding: .5rem .75rem; background: #f0f9f4; border-radius: 4px;
      border: 1px solid #d4edda;
    }

    /* Mobile responsive (Item 6) */
    @media (max-width: 600px) {
      body { font-size: 16px; }
      .wrap { padding: 1rem 1rem 5rem; }
      .tab-bar { gap: 1rem; }
      .tab-btn { font-size: .8rem; }
      .steps { flex-wrap: wrap; }
      .step-indicator { font-size: .65rem; padding: .4rem .15rem; }
      .btn { padding: .65rem 1rem; font-size: .9rem; min-height: 44px; }
      .chat-input { font-size: 16px; min-height: 44px; }
      .store-card { padding: .85rem; }
      .results-table { font-size: .8rem; }
      .results-table th, .results-table td { padding: 6px 4px; }
      .viz-row { flex-direction: column; }
      .btn-row { flex-wrap: wrap; }
      .dgrid { grid-template-columns: 1fr; }
      .event-log-wrap.expanded { height: 160px; }
      .modal-box { padding: 1.25rem; }
      #call-store-tabs { display: flex; flex-wrap: wrap; gap: .35rem; margin-bottom: .75rem; }
      #call-store-tabs .btn { font-size: .8rem; padding: .4rem .7rem; }
      .call-transcript { max-height: 180px; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <header class="site-header">
      <h1><a href="https://dewanggogte.com" target="_blank">Dewang Gogte</a></h1>
    </header>

    <div class="tab-bar">
      <button class="tab-btn active" onclick="switchTab('pipeline')">Research</button>
      <button class="tab-btn" onclick="switchTab('voice')">Quick Call</button>
      <button class="tab-btn" onclick="switchTab('dashboard')">Dashboard</button>
    </div>

    <!-- Pipeline Tab -->
    <div id="tab-pipeline" class="tab-content active">
      <h2 style="font-size:1.3rem;font-weight:500;margin-bottom:.25rem">CallKaro</h2>
      <p style="color:var(--text-light);font-size:.9rem;margin-bottom:1.5rem">
        Tell us what you want to buy. We'll research it, find stores, call them, and compare quotes — all in Hindi.
      </p>

      <div class="steps">
        <div class="step-indicator active" id="si-1" onclick="navToStep(1)">1. Tell Us</div>
        <div class="step-indicator" id="si-2" onclick="navToStep(2)">2. Research</div>
        <div class="step-indicator" id="si-3" onclick="navToStep(3)">3. Call</div>
        <div class="step-indicator" id="si-4" onclick="navToStep(4)">4. Results</div>
      </div>

      <!-- Step 1: Intake -->
      <div class="step-panel active" id="step-1">
        <div class="chat-messages" id="intake-chat"></div>
        <div class="chat-input-row">
          <input class="chat-input" id="intake-input" placeholder="e.g. I want to buy a 1.5 ton split AC around 35-40K in Koramangala..."
                 onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendIntake()}" />
          <button class="btn btn-primary" id="intake-send" onclick="sendIntake()">Send</button>
        </div>
      </div>

      <!-- Step 2: Research & Stores -->
      <div class="step-panel" id="step-2">
        <div id="research-loading" class="loading" style="display:none">
          <span class="spinner"></span>Researching product and finding nearby stores...
          <p style="font-size:.8rem;color:var(--text-light);margin-top:.25rem">This typically takes 15-30 seconds. Progress shown below.</p>
          <div class="research-progress" id="research-progress"></div>
        </div>
        <div id="research-results" style="display:none">
          <div class="card" id="research-card">
            <h3>Research Findings</h3>
          </div>
          <h3 style="font-size:1rem;font-weight:500;margin:1rem 0 .5rem">Stores Found</h3>
          <p style="font-size:.85rem;color:var(--text-light);margin-bottom:.75rem">Select stores to call:</p>
          <div id="store-list"></div>
          <div class="store-counter" id="store-counter" style="display:none">
            <strong id="store-sel-count">0</strong> of <span id="store-total">0</span> stores selected
          </div>
          <div class="btn-row">
            <button class="btn btn-primary" id="btn-start-calls" onclick="showCallConfirm()" disabled>
              Start Calling Selected Stores
            </button>
          </div>
        </div>
      </div>

      <!-- Step 3: Call Simulation -->
      <div class="step-panel" id="step-3">
        <div id="call-store-tabs"></div>
        <div id="call-area">
          <div class="call-status" id="call-status"><span class="dot dot-idle"></span>Select a store to start calling<span class="call-timer" id="call-timer"></span></div>
          <div class="viz-row">
            <div class="viz-box"><div class="viz-label">Your Mic</div><canvas id="micViz2"></canvas></div>
            <div class="viz-box"><div class="viz-label">Agent Audio</div><canvas id="agentViz2"></canvas></div>
          </div>
          <div class="btn-row">
            <button class="btn btn-primary" id="btn-call-start" onclick="startCall()" disabled>Start Call</button>
            <button class="btn btn-danger" id="btn-call-end" onclick="endCall()" disabled>End Call</button>
          </div>
          <div id="call-log"></div>
          <div id="call-transcript" class="call-transcript"></div>
        </div>
        <div class="btn-row" style="margin-top:1rem">
          <button class="btn btn-primary" id="btn-analyze" onclick="goToStep4()" disabled>
            Compare Results
          </button>
        </div>
      </div>

      <!-- Step 4: Results -->
      <div class="step-panel" id="step-4">
        <div id="analysis-loading" class="loading" style="display:none">
          <span class="spinner"></span>Comparing store quotes...
        </div>
        <div id="analysis-results" style="display:none"></div>
        <div class="btn-row" style="margin-top:1.5rem">
          <button class="btn btn-secondary" onclick="startOver()">Start New Research</button>
        </div>
      </div>
    </div>

    <!-- Quick Call Tab (legacy voice test) -->
    <div id="tab-voice" class="tab-content">
      <h2 style="font-size:1.3rem;font-weight:500;margin-bottom:.25rem">Quick Call</h2>
      <p style="color:var(--text-light);font-size:.9rem;margin-bottom:1rem">
        Direct call simulation with the price enquiry agent. Pretend to be a shopkeeper.
      </p>
      <div class="btn-row" style="margin-bottom:1rem">
        <button class="btn btn-primary" id="qc-start" onclick="qcStart()">Start Conversation</button>
        <button class="btn btn-danger" id="qc-end" onclick="qcEnd()" disabled>End</button>
      </div>
      <div class="call-status" id="qc-status"><span class="dot dot-idle"></span>Ready</div>
      <div class="viz-row">
        <div class="viz-box"><div class="viz-label">Your Mic</div><canvas id="qcMicViz"></canvas></div>
        <div class="viz-box"><div class="viz-label">Agent Audio</div><canvas id="qcAgentViz"></canvas></div>
      </div>
      <div id="qc-log" style="background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:.6rem;max-height:150px;overflow-y:auto;font-family:monospace;font-size:.7rem;color:var(--text-light);margin-top:.75rem"></div>
    </div>

    <!-- Dashboard Tab -->
    <div id="tab-dashboard" class="tab-content">
      <div class="dash-wrap">
        <button class="btn btn-secondary" onclick="loadDashboard()" style="margin-bottom:1rem">Refresh</button>
        <div id="dash-content"><div class="loading">Click Dashboard tab to load...</div></div>
      </div>
    </div>

    <section class="links-section">
      <div class="footer-row">
        <a href="https://dewanggogte.com" target="_blank">Home</a>
        <a href="https://dewanggogte.com/blog" target="_blank">Blog</a>
      </div>
    </section>
  </div>

  <!-- Confirmation Modal (Item 4) -->
  <div class="modal-overlay" id="confirm-modal">
    <div class="modal-box">
      <h3 id="confirm-title">Start Calling?</h3>
      <p style="font-size:.9rem;color:var(--text-light)" id="confirm-text"></p>
      <ul class="modal-store-list" id="confirm-store-list"></ul>
      <div class="modal-actions">
        <button class="btn btn-primary" onclick="confirmCalls()">Start Calls</button>
        <button class="btn btn-secondary" onclick="cancelConfirm()">Go Back</button>
      </div>
    </div>
  </div>

  <!-- Pipeline Event Log -->
  <div class="event-log-wrap collapsed" id="event-log">
    <div class="event-log-header" onclick="toggleEventLog()">
      <span>Pipeline Log <span class="ev-count" id="ev-count" style="display:none">0</span></span>
      <span id="ev-toggle-icon">&#9650;</span>
    </div>
    <div class="event-log-body" id="ev-body"></div>
  </div>

  <script src="https://cdn.jsdelivr.net/npm/livekit-client/dist/livekit-client.umd.js"></script>
  <script>
  /* ================================================================
     State
     ================================================================ */
  let sessionId = null;
  let selectedStores = new Set();
  let callResults = {};
  let currentCallStoreIdx = null;
  let room = null;
  let micAnalyser = null, agentAnalyser = null, vizRAF = null;
  let storesData = [];           // Store data from research response
  let callTimerInterval = null;  // Call duration timer
  let callStartTime = null;
  let transcriptPollTimer = null; // Live transcript polling
  let lastTranscriptCount = 0;
  const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  const { Room, RoomEvent, Track } = LivekitClient;

  /* ================================================================
     Pipeline Event Log
     ================================================================ */
  let evSince = 0;
  let evPollTimer = null;
  let evLogExpanded = false;

  function toggleEventLog() {
    const wrap = document.getElementById('event-log');
    evLogExpanded = !evLogExpanded;
    wrap.classList.toggle('collapsed', !evLogExpanded);
    wrap.classList.toggle('expanded', evLogExpanded);
    document.getElementById('ev-toggle-icon').innerHTML = evLogExpanded ? '&#9660;' : '&#9650;';
    if (evLogExpanded) {
      const body = document.getElementById('ev-body');
      body.scrollTop = body.scrollHeight;
    }
  }

  let researchPhaseActive = false;

  function startEventPolling() {
    if (evPollTimer) return;
    evPollTimer = setInterval(pollEvents, 1200);
  }

  function stopEventPolling() {
    if (evPollTimer) { clearInterval(evPollTimer); evPollTimer = null; }
  }

  async function pollEvents() {
    if (!sessionId) return;
    try {
      const resp = await fetch(`/api/session/${sessionId}/events?since=${evSince}`);
      const data = await resp.json();
      if (data.events && data.events.length > 0) {
        const body = document.getElementById('ev-body');
        const wasAtBottom = body.scrollTop + body.clientHeight >= body.scrollHeight - 20;
        const progressEl = researchPhaseActive ? document.getElementById('research-progress') : null;
        data.events.forEach(ev => {
          // Add to bottom pipeline log
          const div = document.createElement('div');
          div.className = 'ev' + (ev.level === 'warning' ? ' ev-warning' : '') + (ev.level === 'error' ? ' ev-error' : '');
          div.innerHTML = `<span class="ev-time">${ev.time}</span><span class="ev-phase ev-phase-${ev.phase}">${ev.phase}</span><span class="ev-msg">${escHtml(ev.message)}</span>`;
          body.appendChild(div);
          // During research, also show inline progress for all pipeline events
          if (progressEl) {
            const item = document.createElement('div');
            item.className = 'progress-item';
            const icon = ev.level === 'error' ? '✗' : '✓';
            const iconCls = ev.level === 'error' ? 'color:var(--red)' : '';
            item.innerHTML = `<span class="check" style="${iconCls}">${icon}</span><span class="ev-phase ev-phase-${ev.phase}" style="font-size:.6rem;padding:1px 5px;border-radius:2px;margin-right:4px">${ev.phase}</span> ${escHtml(ev.message)}`;
            progressEl.appendChild(item);
            progressEl.scrollTop = progressEl.scrollHeight;
          }
        });
        evSince = data.total;
        // Update count badge
        const badge = document.getElementById('ev-count');
        badge.textContent = data.total;
        badge.style.display = 'inline';
        // Auto-scroll if at bottom
        if (wasAtBottom) body.scrollTop = body.scrollHeight;
      }
    } catch(e) { /* ignore polling errors */ }
  }

  function escHtml(s) {
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  /* ================================================================
     Tab switching
     ================================================================ */
  let dashLoaded = false;
  function switchTab(name) {
    ['pipeline','voice','dashboard'].forEach(t => {
      document.getElementById('tab-'+t).classList.toggle('active', t === name);
    });
    document.querySelectorAll('.tab-btn').forEach((b,i) => {
      const tabs = ['pipeline','voice','dashboard'];
      b.classList.toggle('active', tabs[i] === name);
    });
    if (name === 'dashboard' && !dashLoaded) loadDashboard();
  }

  /* ================================================================
     Step navigation
     ================================================================ */
  let highestStep = 1;
  function goToStep(n) {
    if (n > highestStep) highestStep = n;
    for (let i = 1; i <= 4; i++) {
      document.getElementById('step-'+i).classList.toggle('active', i === n);
      const si = document.getElementById('si-'+i);
      si.classList.toggle('active', i === n);
      si.classList.toggle('done', i < n && i <= highestStep);
    }
  }

  function navToStep(n) {
    // Only allow navigating to steps already reached
    if (n > highestStep) return;
    goToStep(n);
  }

  /* ================================================================
     Step 1: Intake chat
     ================================================================ */
  async function sendIntake() {
    const input = document.getElementById('intake-input');
    const msg = input.value.trim();
    if (!msg) return;
    input.value = '';
    document.getElementById('intake-send').disabled = true;

    // Create session on first message
    if (!sessionId) {
      try {
        const resp = await fetch('/api/session', { method: 'POST' });
        const data = await resp.json();
        sessionId = data.session_id;
        startEventPolling();
      } catch(e) { alert('Failed to create session: ' + e.message); return; }
    }

    addChatMsg('intake-chat', msg, 'user');

    try {
      const resp = await fetch(`/api/session/${sessionId}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: msg }),
      });
      const data = await resp.json();
      addChatMsg('intake-chat', data.response, 'assistant');

      if (data.done) {
        // Show requirements summary and move to step 2
        if (data.requirements) {
          const r = data.requirements;
          addChatMsg('intake-chat',
            `Product: ${r.category}\nBudget: ${r.budget_range ? '₹'+r.budget_range[0]+' - ₹'+r.budget_range[1] : 'Not specified'}\nLocation: ${r.location}`,
            'assistant');
        }
        setTimeout(() => { goToStep(2); startResearch(); }, 1500);
      }
    } catch(e) { addChatMsg('intake-chat', 'Error: ' + e.message, 'assistant'); }

    document.getElementById('intake-send').disabled = false;
  }

  function addChatMsg(containerId, text, role) {
    const el = document.getElementById(containerId);
    const div = document.createElement('div');
    div.className = 'chat-msg ' + role;
    div.innerHTML = `<div class="bubble">${text.replace(/\n/g,'<br>')}</div>`;
    el.appendChild(div);
    el.scrollTop = el.scrollHeight;
  }

  /* ================================================================
     Step 2: Research & Store Discovery
     ================================================================ */
  async function startResearch() {
    document.getElementById('research-loading').style.display = 'block';
    document.getElementById('research-results').style.display = 'none';
    document.getElementById('research-progress').innerHTML = '';

    // Enable inline progress feed from the shared event poller
    researchPhaseActive = true;

    // Auto-expand the event log so user can see what's happening
    if (!evLogExpanded) toggleEventLog();

    try {
      // POST triggers research in background thread — returns immediately
      const resp = await fetch(`/api/session/${sessionId}/research`, { method: 'POST' });
      const startData = await resp.json();
      if (startData.error) { alert(startData.error); return; }

      // If research was already done (e.g. page refresh), render immediately
      if (startData.research) {
        researchPhaseActive = false;
        renderResearchResults(startData);
        return;
      }

      // Poll GET endpoint until research completes
      const pollTimer = setInterval(async () => {
        try {
          const r = await fetch(`/api/session/${sessionId}/research`);
          const d = await r.json();
          if (d.status === 'done') {
            clearInterval(pollTimer);
            researchPhaseActive = false;
            renderResearchResults(d);
          } else if (d.status === 'error') {
            clearInterval(pollTimer);
            researchPhaseActive = false;
            document.getElementById('research-loading').innerHTML =
              `<span style="color:var(--red)">Error: ${d.error}</span>`;
          }
          // 'in_progress' → keep polling, events show progress
        } catch(e) {}
      }, 2000);
    } catch(e) {
      researchPhaseActive = false;
      document.getElementById('research-loading').innerHTML = `<span style="color:var(--red)">Error: ${e.message}</span>`;
    }
  }

  function renderResearchResults(data) {
    const rc = document.getElementById('research-card');
    const r = data.research;
    let html = `<h3>Research Findings</h3>`;
    if (r.product_summary) html += `<p>${r.product_summary}</p>`;
    if (r.market_price_range) html += `<div class="research-item"><strong>Market Price:</strong> ₹${r.market_price_range[0].toLocaleString()} - ₹${r.market_price_range[1].toLocaleString()}</div>`;
    if (r.topics_to_cover && r.topics_to_cover.length) {
      html += `<div class="research-item"><strong>Topics:</strong> `;
      r.topics_to_cover.forEach(t => html += `<span class="tag tag-topic">${t}</span>`);
      html += `</div>`;
    }
    if (r.questions_to_ask && r.questions_to_ask.length) {
      html += `<div class="research-item"><strong>Questions to Ask:</strong><ul style="margin:.25rem 0 0 1.5rem;font-size:.85rem">`;
      r.questions_to_ask.forEach(q => html += `<li>${q}</li>`);
      html += `</ul></div>`;
    }
    if (r.important_notes && r.important_notes.length) {
      html += `<div class="research-item" style="margin-top:.5rem">`;
      r.important_notes.forEach(n => html += `<span class="tag tag-note">${n}</span> `);
      html += `</div>`;
    }
    if (r.competing_products && r.competing_products.length) {
      html += `<div class="research-item" style="margin-top:.5rem"><strong>Competing Products:</strong><ul style="margin:.25rem 0 0 1.5rem;font-size:.85rem">`;
      r.competing_products.forEach(p => {
        let li = p.name || '';
        if (p.price_range) li += ` (₹${p.price_range})`;
        if (p.pros) li += ` — ${p.pros}`;
        html += `<li>${li}</li>`;
      });
      html += `</ul></div>`;
    }
    rc.innerHTML = html;

    // Research phase complete — stop inline progress feed
    researchPhaseActive = false;

    storesData = data.stores;

    // Render store list
    const sl = document.getElementById('store-list');
    sl.innerHTML = '';
    data.stores.forEach((s, i) => {
      const div = document.createElement('div');
      div.className = 'store-card';
      div.dataset.idx = i;
      div.onclick = () => toggleStore(i, div);
      div.innerHTML = `
        <h4>${s.name}</h4>
        <div class="store-meta">
          ${s.area ? `<span>${s.area}</span>` : ''}
          ${s.rating ? `<span>★ ${s.rating}</span>` : ''}
          ${s.review_count ? `<span>${s.review_count} reviews</span>` : ''}
          ${s.phone ? `<span>${s.phone}</span>` : ''}
          <span style="color:var(--accent)">${s.source}</span>
        </div>
      `;
      sl.appendChild(div);
    });

    document.getElementById('research-loading').style.display = 'none';
    document.getElementById('research-results').style.display = 'block';
  }

  function toggleStore(idx, el) {
    if (selectedStores.has(idx)) { selectedStores.delete(idx); el.classList.remove('selected'); }
    else { selectedStores.add(idx); el.classList.add('selected'); }
    document.getElementById('btn-start-calls').disabled = selectedStores.size === 0;
    // Update selection counter (Item 4)
    const counter = document.getElementById('store-counter');
    counter.style.display = storesData.length > 0 ? 'block' : 'none';
    document.getElementById('store-sel-count').textContent = selectedStores.size;
    document.getElementById('store-total').textContent = storesData.length;
  }

  /* ================================================================
     Step 3: Call Simulation
     ================================================================ */
  // Item 4: Confirmation modal before starting calls
  function showCallConfirm() {
    const sorted = [...selectedStores].sort();
    const modal = document.getElementById('confirm-modal');
    const list = document.getElementById('confirm-store-list');
    const text = document.getElementById('confirm-text');
    list.innerHTML = '';
    text.textContent = `Call ${sorted.length} store${sorted.length > 1 ? 's' : ''}?`;
    sorted.forEach(idx => {
      const li = document.createElement('li');
      li.textContent = storesData[idx]?.name || ('Store ' + (idx + 1));
      list.appendChild(li);
    });
    modal.classList.add('show');
  }
  function confirmCalls() {
    document.getElementById('confirm-modal').classList.remove('show');
    goToStep3();
  }
  function cancelConfirm() {
    document.getElementById('confirm-modal').classList.remove('show');
  }

  function goToStep3() {
    goToStep(3);
    // Build store tabs with actual store names (Item 3)
    const tabs = document.getElementById('call-store-tabs');
    tabs.innerHTML = '';
    const sorted = [...selectedStores].sort();
    sorted.forEach((idx, i) => {
      const btn = document.createElement('button');
      btn.className = 'btn btn-secondary' + (i === 0 ? ' btn-primary' : '');
      btn.textContent = storesData[idx]?.name || ('Store ' + (idx + 1));
      btn.dataset.idx = idx;
      btn.onclick = () => selectCallStore(idx);
      tabs.appendChild(btn);
      const gap = document.createTextNode(' ');
      tabs.appendChild(gap);
    });
    if (sorted.length > 0) selectCallStore(sorted[0]);
  }

  function selectCallStore(idx) {
    currentCallStoreIdx = idx;
    document.querySelectorAll('#call-store-tabs .btn').forEach(b => {
      b.classList.toggle('btn-primary', parseInt(b.dataset.idx) === idx);
      b.classList.toggle('btn-secondary', parseInt(b.dataset.idx) !== idx);
    });
    const done = callResults[idx];
    document.getElementById('btn-call-start').disabled = false;
    document.getElementById('btn-call-end').disabled = !room;
    if (done) {
      setCallStatus('Call completed — click Start to call again', 'connected');
    } else {
      setCallStatus('Ready to call', 'idle');
    }
    document.getElementById('call-log').innerHTML = '';

    // Enable analyze button if at least one call done
    document.getElementById('btn-analyze').disabled = Object.keys(callResults).length === 0;
  }

  function setCallStatus(text, state) {
    const timerHtml = document.getElementById('call-timer') ? document.getElementById('call-timer').outerHTML : '<span class="call-timer" id="call-timer"></span>';
    document.getElementById('call-status').innerHTML = `<span class="dot dot-${state}"></span>${text}${timerHtml}`;
  }

  // Item 3: Call duration timer
  function startCallTimer() {
    callStartTime = Date.now();
    const timerEl = document.getElementById('call-timer');
    if (timerEl) { timerEl.textContent = '00:00'; timerEl.classList.add('active'); }
    callTimerInterval = setInterval(() => {
      const elapsed = Math.floor((Date.now() - callStartTime) / 1000);
      const mins = String(Math.floor(elapsed / 60)).padStart(2, '0');
      const secs = String(elapsed % 60).padStart(2, '0');
      const el = document.getElementById('call-timer');
      if (el) el.textContent = mins + ':' + secs;
    }, 1000);
  }
  function stopCallTimer() {
    if (callTimerInterval) { clearInterval(callTimerInterval); callTimerInterval = null; }
    const el = document.getElementById('call-timer');
    if (el) el.classList.remove('active');
  }

  // Item 1: Live transcript polling
  function startTranscriptPolling(storeIdx) {
    lastTranscriptCount = 0;
    document.getElementById('call-transcript').innerHTML = '';
    transcriptPollTimer = setInterval(() => pollTranscript(storeIdx), 1500);
  }
  function stopTranscriptPolling() {
    if (transcriptPollTimer) { clearInterval(transcriptPollTimer); transcriptPollTimer = null; }
  }
  async function pollTranscript(storeIdx) {
    try {
      const resp = await fetch(`/api/session/${sessionId}/transcript/${storeIdx}?since=${lastTranscriptCount}`);
      const data = await resp.json();
      if (data.messages && data.messages.length > 0) {
        const el = document.getElementById('call-transcript');
        const wasAtBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 20;
        data.messages.forEach(m => {
          const div = document.createElement('div');
          div.className = 'transcript-msg ' + (m.role === 'assistant' ? 'agent' : 'user');
          div.innerHTML = `<div class="speaker">${m.role === 'assistant' ? 'Agent' : 'Shopkeeper'}</div><div>${escHtml(m.text)}</div>`;
          el.appendChild(div);
        });
        lastTranscriptCount = data.total;
        if (wasAtBottom) el.scrollTop = el.scrollHeight;
      }
    } catch(e) {}
  }

  function callLog(msg, cls) {
    const el = document.getElementById('call-log');
    const ts = new Date().toLocaleTimeString('en', {hour12:false,hour:'2-digit',minute:'2-digit',second:'2-digit'});
    el.innerHTML += `<div style="color:${cls==='error'?'var(--red)':cls==='info'?'#4a7a5b':'var(--text-light)'}"><span style="color:var(--text-light)">${ts}</span> ${msg}</div>`;
    el.scrollTop = el.scrollHeight;
  }

  async function startCall() {
    if (currentCallStoreIdx === null) return;
    document.getElementById('btn-call-start').disabled = true;
    setCallStatus('Starting call...', 'connecting');
    callLog('Requesting token...', '');

    try {
      const resp = await fetch(`/api/session/${sessionId}/call/${currentCallStoreIdx}`, { method: 'POST' });
      const data = await resp.json();
      if (data.error) { callLog('Error: ' + data.error, 'error'); setCallStatus('Error', 'idle'); return; }

      callLog('Room: ' + data.room, 'info');

      room = new Room({ audioCaptureDefaults: { echoCancellation: true, noiseSuppression: true } });
      room.on(RoomEvent.TrackSubscribed, (track, pub, p) => {
        if (track.kind === Track.Kind.Audio) {
          const el = track.attach(); el.id = 'agent-audio-pipe'; document.body.appendChild(el);
          agentAnalyser = createAnalyser(new MediaStream([track.mediaStreamTrack]));
          setCallStatus('Agent connected — speak now!', 'connected');
          callLog('Agent audio connected', 'info');
        }
      });
      room.on(RoomEvent.ParticipantConnected, p => callLog('Joined: ' + p.identity, ''));
      room.on(RoomEvent.ParticipantDisconnected, p => callLog('Left: ' + p.identity, ''));
      room.on(RoomEvent.Disconnected, () => {
        stopCallTimer();
        stopTranscriptPolling();
        setCallStatus('Call ended — click Start to call again', 'idle');
        callLog('Disconnected', '');
        document.getElementById('btn-call-start').disabled = false;
        document.getElementById('btn-call-end').disabled = true;
        callResults[currentCallStoreIdx] = true;
        document.getElementById('btn-analyze').disabled = false;
        cleanupCall();
      });

      await audioCtx.resume();
      await room.connect(data.url, data.token);
      callLog('Connected as ' + room.localParticipant.identity, 'info');
      await room.localParticipant.setMicrophoneEnabled(true);
      callLog('Microphone enabled', 'info');

      initCanvas('micViz2', 'agentViz2');
      const micTrack = room.localParticipant.getTrackPublication(Track.Source.Microphone);
      if (micTrack?.track) micAnalyser = createAnalyser(new MediaStream([micTrack.track.mediaStreamTrack]));
      startViz('micViz2', 'agentViz2');

      setCallStatus('Connected — waiting for agent...', 'connecting');
      document.getElementById('btn-call-end').disabled = false;
      startCallTimer();
      startTranscriptPolling(currentCallStoreIdx);
    } catch(e) {
      callLog('Error: ' + e.message, 'error');
      setCallStatus('Error', 'idle');
      document.getElementById('btn-call-start').disabled = false;
    }
  }

  async function endCall() {
    if (room) { await room.disconnect(); cleanupCall(); }
    callResults[currentCallStoreIdx] = true;
    document.getElementById('btn-call-start').disabled = false;
    document.getElementById('btn-call-end').disabled = true;
    document.getElementById('btn-analyze').disabled = false;
    stopCallTimer();
    stopTranscriptPolling();
    setCallStatus('Call ended', 'idle');
  }

  function cleanupCall() {
    const el = document.getElementById('agent-audio-pipe'); if (el) el.remove();
    room = null; micAnalyser = null; agentAnalyser = null;
    if (vizRAF) { cancelAnimationFrame(vizRAF); vizRAF = null; }
  }

  /* ================================================================
     Step 4: Analysis
     ================================================================ */
  function goToStep4() {
    goToStep(4);
    runAnalysis();
  }

  async function runAnalysis() {
    document.getElementById('analysis-loading').style.display = 'block';
    document.getElementById('analysis-results').style.display = 'none';

    try {
      const resp = await fetch(`/api/session/${sessionId}/analyze`, { method: 'POST' });
      const data = await resp.json();

      const el = document.getElementById('analysis-results');
      if (data.error) {
        el.innerHTML = `<div class="card"><p style="color:var(--red)">${data.error}</p></div>`;
      } else {
        let html = '';
        // Recommendation summary
        if (data.summary) {
          html += `<div class="card"><h3>Recommendation</h3><p>${data.summary}</p></div>`;
        }
        // Best option highlight
        if (data.recommended_store) {
          html += `<div class="card" style="border-color:var(--green)"><h3>Best Option: ${data.recommended_store}<span class="best-deal-badge">Best Deal</span></h3></div>`;
        }
        // Savings note
        if (data.max_savings) {
          html += `<div class="savings-note">You could save <strong>${data.max_savings}</strong> by choosing the recommended store over the most expensive option.</div>`;
        }
        // Store ranking with cost breakdown (Item 5)
        if (data.ranking && data.ranking.length) {
          html += `<div class="card"><h3>Store Comparison</h3><table class="results-table"><tr><th>#</th><th>Store</th><th>Price</th><th>Extras</th><th>Details</th></tr>`;
          data.ranking.forEach((r, i) => {
            const rank = r.rank || (i + 1);
            const cls = rank <= 3 ? `rank-${rank}` : '';
            const isBest = r.store_name === data.recommended_store;
            html += `<tr class="${isBest ? 'best-deal' : ''}">`;
            html += `<td><span class="rank-badge ${cls}">${rank}</span></td>`;
            html += `<td>${r.store_name}${isBest ? '<span class="best-deal-badge" style="margin-left:.3rem">Best</span>' : ''}</td>`;
            // Price column
            html += `<td>`;
            if (r.base_price) html += `${r.base_price}`;
            else if (r.total_estimated_cost) html += `${r.total_estimated_cost}`;
            html += `</td>`;
            // Extras column (installation + delivery + warranty)
            html += `<td style="font-size:.8rem">`;
            if (r.installation_cost) html += `Install: ${r.installation_cost}<br>`;
            if (r.delivery_cost) html += `Delivery: ${r.delivery_cost}<br>`;
            if (r.warranty) html += `Warranty: ${r.warranty}`;
            if (!r.installation_cost && !r.delivery_cost && !r.warranty && r.total_estimated_cost) html += `Total: ${r.total_estimated_cost}`;
            html += `</td>`;
            // Pros/cons
            html += `<td>`;
            if (r.pros && r.pros.length) html += `<span style="color:var(--green);font-size:.8rem">+ ${r.pros.join(', ')}</span><br>`;
            if (r.cons && r.cons.length) html += `<span style="color:var(--red);font-size:.8rem">- ${r.cons.join(', ')}</span>`;
            html += `</td></tr>`;
          });
          html += `</table></div>`;
        }
        el.innerHTML = html;
      }

      document.getElementById('analysis-loading').style.display = 'none';
      document.getElementById('analysis-results').style.display = 'block';
    } catch(e) {
      document.getElementById('analysis-loading').innerHTML = `<span style="color:var(--red)">Error: ${e.message}</span>`;
    }
  }

  function startOver() {
    stopEventPolling();
    stopCallTimer();
    stopTranscriptPolling();
    researchPhaseActive = false;
    sessionId = null;
    selectedStores.clear();
    callResults = {};
    currentCallStoreIdx = null;
    storesData = [];
    lastTranscriptCount = 0;
    evSince = 0;
    document.getElementById('ev-body').innerHTML = '';
    document.getElementById('ev-count').style.display = 'none';
    document.getElementById('intake-chat').innerHTML = '';
    document.getElementById('call-transcript').innerHTML = '';
    document.getElementById('store-counter').style.display = 'none';
    goToStep(1);
  }

  /* ================================================================
     Audio visualization (shared)
     ================================================================ */
  function createAnalyser(stream) {
    const src = audioCtx.createMediaStreamSource(stream);
    const a = audioCtx.createAnalyser(); a.fftSize = 256;
    src.connect(a); return a;
  }
  function drawBars(canvas, analyser, color) {
    if (!analyser) return;
    const ctx = canvas.getContext('2d');
    const W = canvas.width, H = canvas.height;
    const buf = new Uint8Array(analyser.frequencyBinCount);
    analyser.getByteFrequencyData(buf);
    ctx.clearRect(0,0,W,H);
    const bars = 24, step = Math.floor(buf.length/bars), bw = W/bars-1;
    for (let i=0;i<bars;i++) {
      const v = buf[i*step]/255, h = Math.max(2,v*H);
      ctx.fillStyle = v>.05 ? color : '#e8e6e3';
      ctx.fillRect(i*(bw+1),H-h,bw,h);
    }
  }
  function initCanvas(...ids) {
    ids.forEach(id => {
      const c = document.getElementById(id);
      if (c) { c.width = c.offsetWidth*devicePixelRatio; c.height = c.offsetHeight*devicePixelRatio; }
    });
  }
  function startViz(micId, agentId) {
    function loop() {
      drawBars(document.getElementById(micId), micAnalyser, '#4a9');
      drawBars(document.getElementById(agentId), agentAnalyser, '#b85a3b');
      vizRAF = requestAnimationFrame(loop);
    }
    loop();
  }

  /* ================================================================
     Quick Call
     ================================================================ */
  let qcRoom = null, qcMic = null, qcAgent = null, qcViz = null;
  async function qcStart() {
    document.getElementById('qc-start').disabled = true;
    document.getElementById('qc-status').innerHTML = '<span class="dot dot-connecting"></span>Connecting...';
    document.getElementById('qc-log').innerHTML = '';
    try {
      const resp = await fetch('/api/token');
      const data = await resp.json();
      qcRoom = new Room({ audioCaptureDefaults:{echoCancellation:true,noiseSuppression:true} });
      qcRoom.on(RoomEvent.TrackSubscribed, (track) => {
        if (track.kind===Track.Kind.Audio) {
          const el=track.attach(); el.id='qc-audio'; document.body.appendChild(el);
          qcAgent=createAnalyser(new MediaStream([track.mediaStreamTrack]));
          document.getElementById('qc-status').innerHTML='<span class="dot dot-connected"></span>Speaking...';
        }
      });
      qcRoom.on(RoomEvent.Disconnected, () => {
        document.getElementById('qc-status').innerHTML='<span class="dot dot-idle"></span>Ended';
        document.getElementById('qc-start').disabled=false; document.getElementById('qc-end').disabled=true;
        const a=document.getElementById('qc-audio'); if(a) a.remove();
        qcRoom=null; qcMic=null; qcAgent=null;
        if(qcViz){cancelAnimationFrame(qcViz);qcViz=null;}
      });
      await audioCtx.resume();
      await qcRoom.connect(data.url, data.token);
      await qcRoom.localParticipant.setMicrophoneEnabled(true);
      initCanvas('qcMicViz','qcAgentViz');
      const mt=qcRoom.localParticipant.getTrackPublication(Track.Source.Microphone);
      if(mt?.track) qcMic=createAnalyser(new MediaStream([mt.track.mediaStreamTrack]));
      (function loop(){drawBars(document.getElementById('qcMicViz'),qcMic,'#4a9');drawBars(document.getElementById('qcAgentViz'),qcAgent,'#b85a3b');qcViz=requestAnimationFrame(loop)})();
      document.getElementById('qc-end').disabled=false;
    } catch(e) {
      document.getElementById('qc-status').innerHTML='<span class="dot dot-idle"></span>Error: '+e.message;
      document.getElementById('qc-start').disabled=false;
    }
  }
  async function qcEnd() {
    if(qcRoom) await qcRoom.disconnect();
    document.getElementById('qc-start').disabled=false; document.getElementById('qc-end').disabled=true;
  }

  /* ================================================================
     Dashboard
     ================================================================ */
  let ttftChart = null, tokenChart = null, latencyChart = null;
  async function loadDashboard() {
    const el = document.getElementById('dash-content');
    el.innerHTML = '<div class="loading"><span class="spinner"></span>Loading metrics & running tests...</div>';
    try {
      const resp = await fetch('/api/metrics');
      const d = await resp.json();
      dashLoaded = true;
      const m = d.metrics;
      const t = d.tests;
      const transcripts = d.transcripts || [];

      // Transcript table rows (last 10)
      const tRows = transcripts.slice(-10).map(tr =>
        `<tr><td style="font-size:.75rem">${tr._filename || ''}</td><td>${tr.store_name || ''}</td><td>${tr._total_messages || 0}</td><td>${tr._duration_seconds || 0}s</td><td>${tr.phone || ''}</td></tr>`
      ).join('') || '<tr><td colspan="5" style="color:var(--text-light)">No transcripts</td></tr>';

      // Test output (escaped)
      const testOut = (t.output || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

      // Total tokens
      const totalPrompt = (m.all_prompt_tokens || []).reduce((a,b) => a+b, 0);
      const totalCompletion = (m.all_completion_tokens || []).reduce((a,b) => a+b, 0);
      const totalTokens = totalPrompt + totalCompletion;

      el.innerHTML = `
        <!-- Row 1: Overview -->
        <div class="dgrid">
          <div class="dcard">
            <h3 style="font-size:.9rem;font-weight:500">Tests</h3>
            <div style="display:flex;gap:8px;align-items:center;margin-top:.25rem">
              <span class="badge badge-green">${t.passed} passed</span>
              <span class="badge ${t.failed>0?'badge-red':'badge-green'}">${t.failed} failed</span>
            </div>
          </div>
          <div class="dcard">
            <h3 style="font-size:.9rem;font-weight:500">Calls Recorded</h3>
            <div class="stat">${m.total_calls}</div>
            <div class="stat-label">Total conversations</div>
          </div>
          <div class="dcard">
            <h3 style="font-size:.9rem;font-weight:500">Errors</h3>
            <div class="stat" style="color:${m.total_errors>0?'var(--red)':'var(--green)'}">${m.total_errors}</div>
            <div class="stat-label">Across all log files</div>
          </div>
        </div>

        <!-- Row 2: Latency -->
        <div class="dgrid">
          <div class="dcard">
            <h3 style="font-size:.9rem;font-weight:500">Time to First Token</h3>
            <div class="stat">${m.ttft.avg}s</div>
            <div class="stat-label">Average TTFT</div>
            <div style="display:flex;gap:1rem;margin-top:.5rem;font-size:.8rem;color:var(--text-light)">
              <span>P50: ${m.ttft.p50}s</span>
              <span>P95: ${m.ttft.p95}s</span>
              <span>Min: ${m.ttft.min}s</span>
              <span>Max: ${m.ttft.max}s</span>
            </div>
          </div>
          <div class="dcard">
            <h3 style="font-size:.9rem;font-weight:500">LLM Response Duration</h3>
            <div class="stat">${m.llm_duration.avg}s</div>
            <div class="stat-label">Average</div>
            <div style="display:flex;gap:1rem;margin-top:.5rem;font-size:.8rem;color:var(--text-light)">
              <span>P50: ${m.llm_duration.p50}s</span>
              <span>P95: ${m.llm_duration.p95}s</span>
            </div>
          </div>
          <div class="dcard">
            <h3 style="font-size:.9rem;font-weight:500">Turn Latency</h3>
            <div class="stat">${m.turn_latency.avg}s</div>
            <div class="stat-label">User speech to agent response</div>
            <div style="display:flex;gap:1rem;margin-top:.5rem;font-size:.8rem;color:var(--text-light)">
              <span>P50: ${m.turn_latency.p50}s</span>
              <span>P95: ${m.turn_latency.p95}s</span>
            </div>
          </div>
        </div>

        <!-- Row 3: Token Usage -->
        <div class="dgrid">
          <div class="dcard">
            <h3 style="font-size:.9rem;font-weight:500">Total Tokens Used</h3>
            <div class="stat">${totalTokens.toLocaleString()}</div>
            <div class="stat-label">Across all calls</div>
            <div style="display:flex;gap:1.5rem;margin-top:.5rem;font-size:.8rem;color:var(--text-light)">
              <span>Prompt: ${totalPrompt.toLocaleString()}</span>
              <span>Completion: ${totalCompletion.toLocaleString()}</span>
            </div>
          </div>
          <div class="dcard">
            <h3 style="font-size:.9rem;font-weight:500">Tokens per Turn</h3>
            <div class="stat">${m.prompt_tokens.avg}</div>
            <div class="stat-label">Avg prompt tokens</div>
            <div style="display:flex;gap:1.5rem;margin-top:.5rem;font-size:.8rem;color:var(--text-light)">
              <span>Avg completion: ${m.completion_tokens.avg}</span>
              <span>Max prompt: ${m.prompt_tokens.max}</span>
            </div>
          </div>
          <div class="dcard">
            <h3 style="font-size:.9rem;font-weight:500">Conversation</h3>
            <div class="stat">${m.conv_duration.avg}s</div>
            <div class="stat-label">Avg call duration</div>
            <div style="display:flex;gap:1.5rem;margin-top:.5rem;font-size:.8rem;color:var(--text-light)">
              <span>Avg msgs: ${m.msg_counts.avg}</span>
              <span>Max: ${m.conv_duration.max}s</span>
            </div>
          </div>
        </div>

        <!-- Row 4: Charts -->
        <div class="dgrid">
          <div class="dcard"><h3 style="font-size:.9rem;font-weight:500">TTFT Distribution</h3><canvas id="dashTtft" height="120"></canvas></div>
          <div class="dcard"><h3 style="font-size:.9rem;font-weight:500">Token Usage per Turn</h3><canvas id="dashTokens" height="120"></canvas></div>
        </div>
        <div class="dgrid">
          <div class="dcard dcard-wide"><h3 style="font-size:.9rem;font-weight:500">Turn Latency Distribution</h3><canvas id="dashLatency" height="80"></canvas></div>
        </div>

        <!-- Row 5: Transcripts -->
        <div class="dgrid">
          <div class="dcard dcard-wide">
            <h3 style="font-size:.9rem;font-weight:500">Recent Transcripts</h3>
            <table style="width:100%;border-collapse:collapse;font-size:.8rem;margin-top:.5rem">
              <tr><th style="text-align:left;padding:6px;color:var(--text-light);border-bottom:1px solid var(--border);font-weight:500">File</th><th style="text-align:left;padding:6px;color:var(--text-light);border-bottom:1px solid var(--border);font-weight:500">Store</th><th style="text-align:left;padding:6px;color:var(--text-light);border-bottom:1px solid var(--border);font-weight:500">Msgs</th><th style="text-align:left;padding:6px;color:var(--text-light);border-bottom:1px solid var(--border);font-weight:500">Duration</th><th style="text-align:left;padding:6px;color:var(--text-light);border-bottom:1px solid var(--border);font-weight:500">Channel</th></tr>
              ${tRows}
            </table>
          </div>
        </div>

        <!-- Row 6: Test Output -->
        <details style="margin-top:.5rem">
          <summary style="cursor:pointer;color:var(--text-light);font-size:.85rem;font-weight:500">Test Output (click to expand)</summary>
          <pre style="background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:.75rem;max-height:300px;overflow:auto;font-size:.7rem;color:var(--text-light);margin-top:.5rem">${testOut}</pre>
        </details>
      `;

      // Render TTFT chart
      const ttftData = m.all_ttfts || [];
      if (ttftData.length > 0) {
        if (ttftChart) ttftChart.destroy();
        ttftChart = new Chart(document.getElementById('dashTtft'), {
          type: 'bar',
          data: { labels: ttftData.map((_,i) => 'T'+(i+1)), datasets: [{ label: 'TTFT (s)', data: ttftData, backgroundColor: 'rgba(184,90,59,0.7)', borderRadius: 3 }] },
          options: { responsive: true, plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true, grid: { color: 'rgba(0,0,0,.05)' }, ticks: { color: '#999', font:{size:10} } }, x: { grid: { display: false }, ticks: { color: '#999', maxRotation: 0, autoSkip: true, maxTicksLimit: 25, font:{size:10} } } } }
        });
      }

      // Render token chart
      const pt = m.all_prompt_tokens || [], ct = m.all_completion_tokens || [];
      if (pt.length > 0) {
        if (tokenChart) tokenChart.destroy();
        tokenChart = new Chart(document.getElementById('dashTokens'), {
          type: 'bar',
          data: { labels: pt.map((_,i) => 'T'+(i+1)), datasets: [
            { label: 'Prompt', data: pt, backgroundColor: 'rgba(184,90,59,0.7)', borderRadius: 3 },
            { label: 'Completion', data: ct, backgroundColor: 'rgba(74,153,153,0.7)', borderRadius: 3 }
          ]},
          options: { responsive: true, plugins: { legend: { labels: { color: '#999', font:{size:10} } } }, scales: { y: { stacked: true, beginAtZero: true, grid: { color: 'rgba(0,0,0,.05)' }, ticks: { color: '#999', font:{size:10} } }, x: { stacked: true, grid: { display: false }, ticks: { color: '#999', maxRotation: 0, autoSkip: true, maxTicksLimit: 25, font:{size:10} } } } }
        });
      }

      // Render latency chart
      const latData = m.all_turn_latencies || [];
      if (latData.length > 0) {
        if (latencyChart) latencyChart.destroy();
        latencyChart = new Chart(document.getElementById('dashLatency'), {
          type: 'line',
          data: { labels: latData.map((_,i) => 'T'+(i+1)), datasets: [{ label: 'Turn Latency (s)', data: latData, borderColor: '#b85a3b', backgroundColor: 'rgba(184,90,59,0.1)', fill: true, tension: 0.3, pointRadius: 2 }] },
          options: { responsive: true, plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true, grid: { color: 'rgba(0,0,0,.05)' }, ticks: { color: '#999', font:{size:10} } }, x: { grid: { display: false }, ticks: { color: '#999', maxRotation: 0, autoSkip: true, maxTicksLimit: 30, font:{size:10} } } } }
        });
      }

    } catch(e) {
      el.innerHTML = `<div class="loading" style="color:var(--red)">Failed: ${e.message}</div>`;
    }
  }
  </script>
</body>
</html>
"""


def _parse_transcript_from_logs(store_name: str) -> list[dict]:
    """Parse [USER] and [LLM] lines from the most recent call log for a store.

    Returns list of {"role": "user"|"assistant", "text": "..."}.
    """
    import re
    logs_dir = Path(__file__).parent / "logs"
    if not logs_dir.exists() or not store_name:
        return []

    # Find most recent log file for this store
    pattern = store_name.replace(" ", "_") + "_*.log"
    log_files = sorted(logs_dir.glob(pattern), key=lambda f: f.stat().st_mtime, reverse=True)
    if not log_files:
        return []

    messages = []
    try:
        with open(log_files[0], "r", errors="replace") as f:
            for line in f:
                if "[USER]" in line:
                    match = re.search(r"\[USER\]\s*(.*)", line)
                    if match and match.group(1).strip():
                        messages.append({"role": "user", "text": match.group(1).strip()})
                elif "[LLM]" in line:
                    match = re.search(r"\[LLM\]\s*(?:\[TRUNCATED\]\s*)?(.*)", line)
                    if match and match.group(1).strip():
                        messages.append({"role": "assistant", "text": match.group(1).strip()})
    except Exception:
        pass

    return messages


# ---------------------------------------------------------------------------
# HTTP request handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        elif path == "/":
            self._serve_html()
        elif path == "/api/token":
            self._serve_token()
        elif path == "/api/metrics":
            self._serve_metrics()
        elif path.startswith("/api/logs"):
            self._serve_logs()
        elif path.startswith("/api/session/") and path.endswith("/status"):
            self._serve_session_status(path)
        elif path.startswith("/api/session/") and path.endswith("/research"):
            self._serve_research_results(path)
        elif path.startswith("/api/session/") and "/transcript/" in path:
            self._serve_transcript(path)
        elif path.startswith("/api/session/") and "/events" in path:
            self._serve_session_events(path)
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/session":
            self._create_session()
        elif "/chat" in path:
            self._handle_chat(path)
        elif "/research" in path:
            self._handle_research(path)
        elif "/call/" in path:
            self._handle_call(path)
        elif "/analyze" in path:
            self._handle_analyze(path)
        else:
            self.send_error(404)

    def do_HEAD(self):
        """Render health check sends HEAD /."""
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors_headers()
        self.end_headers()

    def handle_one_request(self):
        """Suppress BrokenPipeError when client disconnects mid-response."""
        try:
            super().handle_one_request()
        except BrokenPipeError:
            pass

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json_response(self, data, status=200):
        body = json.dumps(data, default=str, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def _extract_session_id(self, path: str):
        parts = path.strip("/").split("/")
        # /api/session/{id}/...
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "session":
            return parts[2]
        return None

    # --- Endpoints ---

    def _serve_html(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(HTML_PAGE.encode())

    def _create_session(self):
        from pipeline.session import PipelineSession

        # Clean expired sessions
        expired = [k for k, v in _sessions.items() if v.is_expired()]
        for k in expired:
            del _sessions[k]

        session = PipelineSession()
        _sessions[session.session_id] = session
        print(f"  [PIPELINE] New session: {session.session_id}")
        self._json_response({"session_id": session.session_id})

    def _handle_chat(self, path: str):
        sid = self._extract_session_id(path)
        session = _get_or_none(sid) if sid else None
        if not session:
            self._json_response({"error": "Session not found"}, 404)
            return

        body = self._read_body()
        message = body.get("message", "")
        if not message:
            self._json_response({"error": "No message provided"}, 400)
            return

        print(f"  [INTAKE] User: {message[:80]}")
        result = session.chat(message)
        print(f"  [INTAKE] Agent: {result['response'][:80]}...")
        if result['done']:
            req = result.get('requirements', {})
            print(f"  [INTAKE] Done — {req.get('product_type', '?')}: {req.get('category', '?')} in {req.get('location', '?')}")
        self._json_response(result)

    def _handle_research(self, path: str):
        sid = self._extract_session_id(path)
        session = _get_or_none(sid) if sid else None
        if not session:
            self._json_response({"error": "Session not found"}, 404)
            return

        # Already done or in progress?
        if session._research_result:
            self._json_response(session._research_result)
            return
        if session._research_thread_started:
            self._json_response({"status": "in_progress"})
            return

        # Launch research in a background thread so the server can serve
        # event polling requests while research runs
        _logger.info("Starting research + store discovery (background)...")

        def _run():
            try:
                result = asyncio.run(session.research_and_discover())
                if "error" in result:
                    _logger.error("Research error: %s", result['error'])
                else:
                    r = result.get("research", {})
                    stores = result.get("stores", [])
                    price = r.get("market_price_range")
                    _logger.info("Research done — price range: %s, %d questions", price, len(r.get('questions_to_ask', [])))
                    _logger.info("Found %d stores:", len(stores))
                    for s in stores[:5]:
                        _logger.info("  %s (%s) [%s]", s['name'], s.get('area', ''), s.get('source', ''))
            except Exception as e:
                _logger.error("Research error: %s", e)
                session._research_error = str(e)
                session.state = "intake"

        threading.Thread(target=_run, daemon=True).start()
        self._json_response({"status": "started"})

    def _serve_research_results(self, path: str):
        """GET endpoint to poll for research completion."""
        sid = self._extract_session_id(path)
        session = _get_or_none(sid) if sid else None
        if not session:
            self._json_response({"error": "Session not found"}, 404)
            return

        if session._research_error:
            self._json_response({"status": "error", "error": session._research_error})
        elif session._research_result:
            self._json_response({"status": "done", **session._research_result})
        elif session.state == "researching":
            self._json_response({"status": "in_progress"})
        else:
            self._json_response({"status": "not_started"})

    def _handle_call(self, path: str):
        sid = self._extract_session_id(path)
        session = _get_or_none(sid) if sid else None
        if not session:
            self._json_response({"error": "Session not found"}, 404)
            return

        # Extract store index from path: /api/session/{id}/call/{n}
        parts = path.strip("/").split("/")
        try:
            store_idx = int(parts[-1])
        except (ValueError, IndexError):
            self._json_response({"error": "Invalid store index"}, 400)
            return

        store_name = session.stores[store_idx].name if store_idx < len(session.stores) else "?"
        print(f"  [CALL] Dispatching call to store #{store_idx}: {store_name}")
        result = asyncio.run(session.start_call(store_idx))
        if "error" in result:
            print(f"  [CALL] Error: {result['error']}")
        else:
            print(f"  [CALL] Room: {result.get('room', '?')}")
        self._json_response(result)

    def _handle_analyze(self, path: str):
        sid = self._extract_session_id(path)
        session = _get_or_none(sid) if sid else None
        if not session:
            self._json_response({"error": "Session not found"}, 404)
            return

        print(f"  [ANALYZE] Starting cross-store comparison (rooms: {list(session._active_rooms.values())})...")
        result = asyncio.run(session.analyze())
        if "error" in result:
            print(f"  [ANALYZE] Error: {result['error']}")
        else:
            print(f"  [ANALYZE] Done — recommended: {result.get('recommended_store', '?')}")
            print(f"  [ANALYZE] Summary: {result.get('summary', '')[:120]}")
        self._json_response(result)

    def _serve_session_status(self, path: str):
        sid = self._extract_session_id(path)
        session = _get_or_none(sid) if sid else None
        if not session:
            self._json_response({"error": "Session not found"}, 404)
            return
        self._json_response(session.get_status())

    def _serve_session_events(self, path: str):
        sid = self._extract_session_id(path)
        session = _get_or_none(sid) if sid else None
        if not session:
            self._json_response({"error": "Session not found"}, 404)
            return

        params = parse_qs(urlparse(self.path).query)
        since = int(params.get("since", ["0"])[0])
        events = session.events[since:]
        self._json_response({"events": events, "total": len(session.events)})

    def _serve_transcript(self, path: str):
        """Live transcript for an active call — parses agent call log."""
        sid = self._extract_session_id(path)
        session = _get_or_none(sid) if sid else None
        if not session:
            self._json_response({"error": "Session not found"}, 404)
            return

        # Extract store index from /api/session/{id}/transcript/{n}
        parts = path.strip("/").split("/")
        try:
            store_idx = int(parts[-1])
        except (ValueError, IndexError):
            self._json_response({"error": "Invalid store index"}, 400)
            return

        params = parse_qs(urlparse(self.path).query)
        since = int(params.get("since", ["0"])[0])

        store_name = (
            session.stores[store_idx].name
            if store_idx < len(session.stores) else ""
        )
        messages = _parse_transcript_from_logs(store_name)
        new_messages = messages[since:]
        self._json_response({"messages": new_messages, "total": len(messages)})

    def _serve_token(self):
        """Create token for direct quick-call agent."""
        try:
            token_data = asyncio.run(create_legacy_token())
            self._json_response(token_data)
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    def _serve_metrics(self):
        try:
            from dashboard import parse_transcripts, parse_logs, compute_metrics, run_tests
            transcripts = parse_transcripts()
            log_data = parse_logs()
            metrics = compute_metrics(transcripts, log_data)
            tests = run_tests()
            self._json_response({
                "metrics": metrics,
                "tests": tests,
                "transcripts": transcripts,
            })
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    def _serve_logs(self):
        params = parse_qs(urlparse(self.path).query)
        n = int(params.get("n", ["200"])[0])

        log_file = find_agent_log()
        if not log_file:
            self._json_response({"file": None, "lines": ["No agent log file found."]})
            return

        try:
            with open(log_file, "r", errors="replace") as f:
                all_lines = f.readlines()
            lines = [l.rstrip() for l in all_lines[-n:]]
        except Exception as e:
            lines = [f"Error reading log: {e}"]

        self._json_response({"file": log_file, "lines": lines})

    def log_message(self, format, *args):
        if self.path == "/healthz":
            return
        print(f"  [{self.address_string()}] {format % args}")


# ---------------------------------------------------------------------------
# Legacy token generation (for Quick Call tab)
# ---------------------------------------------------------------------------
async def create_legacy_token():
    from livekit.api import LiveKitAPI, AccessToken, VideoGrants
    from livekit.protocol.agent_dispatch import CreateAgentDispatchRequest

    room_name = f"browser-test-{uuid.uuid4().hex[:8]}"
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

    return {"token": token, "url": LIVEKIT_URL, "room": room_name}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()

    # Ensure print() output is visible immediately when piped
    sys.stdout.reconfigure(line_buffering=True)

    print(f"\n  CallKaro — Product Research Pipeline")
    print(f"  {'=' * 44}")

    kill_old_agents()
    start_agent_worker()
    atexit.register(cleanup_agent)

    print(f"  Server:  http://localhost:{args.port}")
    print(f"  LiveKit: {LIVEKIT_URL}")
    print()

    server = HTTPServer(("", args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Shutting down.")
        server.server_close()
