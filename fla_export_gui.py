#!/usr/bin/env python3
"""
fla_export_gui.py — FLA Frame Export GUI

Run: python fla_export_gui.py [optional.fla]

Opens a browser-based GUI for exporting symbol frames as SVGs.
"""

import sys, os, json, threading, webbrowser, time, queue
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse, unquote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fla_inspect import (open_fla, parse_dom, get_symbols, find_roots,
                          t, _active_frame, _render_sym, _bbox_sym, safe_name)

# ── Globals ───────────────────────────────────────────────────────────────────
_state = {'symbols': {}, 'dom': None, 'roots': set(), 'filename': ''}
_lock  = threading.Lock()
_dlq   = queue.Queue()   # dialog request: 'file' | 'dir'
_dlr   = queue.Queue()   # dialog result: str path

INITIAL_PATH = sys.argv[1] if len(sys.argv) > 1 else ''
PORT = 7272

# ── FLA helpers ───────────────────────────────────────────────────────────────

def _frame_count(sym_elem):
    tl = sym_elem.find(t('timeline'))
    if tl is None: return 0
    dt = tl.find(t('DOMTimeline'))
    if dt is None: return 0
    le = dt.find(t('layers'))
    if le is None: return 0
    m = 0
    for layer in le:
        fe = layer.find(t('frames'))
        if fe is None: continue
        for f in fe:
            if not f.tag.endswith('DOMFrame'): continue
            m = max(m, int(f.get('index', 0)) + int(f.get('duration', 1)))
    return m

def _frame_valid(sym_elem, n):
    tl = sym_elem.find(t('timeline'))
    if tl is None: return False
    dt = tl.find(t('DOMTimeline'))
    if dt is None: return False
    le = dt.find(t('layers'))
    if le is None: return False
    for layer in le:
        if layer.get('layerType', 'normal') in ('guide', 'folder'): continue
        fr = _active_frame(layer, n)
        if fr is None: continue
        elems = fr.find(t('elements'))
        if elems is not None and len(list(elems)) > 0:
            return True
    return False

def _make_svg(sym_name, frame, transparent=False):
    syms = _state['symbols']
    if not syms: return None
    bbox = _bbox_sym(sym_name, syms, frame)
    if bbox:
        pad = 80
        x0, y0, x1, y1 = bbox
        vx, vy = x0 - pad, y0 - pad
        vw, vh = (x1 - x0) + pad * 2, (y1 - y0) + pad * 2
    else:
        vx, vy, vw, vh = -2000, -2000, 4000, 4000

    body, defs = _render_sym(sym_name, syms, frame)
    if not body: return None

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vx:.1f} {vy:.1f} {vw:.1f} {vh:.1f}">',
    ]
    if not transparent:
        lines.append(f'  <rect x="{vx:.1f}" y="{vy:.1f}" width="{vw:.1f}" height="{vh:.1f}" fill="#1a1a1a"/>')
    if defs:
        lines += ['  <defs>'] + ['    ' + d for d in defs] + ['  </defs>']
    lines += ['  ' + l for l in body] + ['</svg>']
    return '\n'.join(lines)

# ── Embedded HTML ─────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>FLA Export</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg:    #0f1117;
  --bg2:   #20232c;
  --bg3:   #292b33;
  --border:#3c3e4b;
  --text:  #c8d0ea;
  --dim:   #696c77;
  --bright:#eef1ff;
  --blue:  #aab7da;
  --cyan:  #69d4c8;
  --green: #57c989;
  --red:   #f76060;
  --gold:  #e5a945;
  --pink:  #f778a1;
  --purple:#b085f5;
}
html, body {
  height: 100%;
  font-family: 'Consolas', 'Cascadia Code', 'SF Mono', monospace;
  font-size: 13px;
  background: var(--bg);
  color: var(--text);
  overflow: hidden;
}
body { display: flex; flex-direction: column; }

/* ── Load bar ─────────────────────────────────────────────────── */
#load-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 14px;
  background: var(--bg2);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.brand { font-weight: 700; color: var(--bright); font-size: 1em; }

input[type=text] {
  background: var(--bg3);
  border: 1px solid var(--border);
  color: var(--text);
  padding: 4px 8px;
  border-radius: 4px;
  font-family: inherit;
  font-size: .82em;
}
input[type=text]:focus { outline: none; border-color: var(--blue); }
input[type=text]::placeholder { color: var(--dim); }

#fla-path { flex: 1; }

button {
  background: var(--bg3);
  border: 1px solid var(--border);
  color: var(--text);
  padding: 4px 12px;
  border-radius: 4px;
  cursor: pointer;
  font-family: inherit;
  font-size: .82em;
  white-space: nowrap;
}
button:hover:not(:disabled) { border-color: var(--blue); color: var(--bright); }
button:disabled { opacity: .35; cursor: default; }
button.primary {
  background: rgba(124,158,248,.15);
  border-color: rgba(170,183,218,.5);
  color: var(--bright);
}
button.primary:hover:not(:disabled) { background: rgba(124,158,248,.25); }
button.danger { border-color: var(--red); color: var(--red); background: rgba(247,96,96,.08); }

#load-status { font-size: .78em; color: var(--dim); white-space: nowrap; }
.s-ok  { color: var(--green) !important; }
.s-err { color: var(--red) !important; }
.s-info { color: var(--cyan) !important; }

/* ── App shell ────────────────────────────────────────────────── */
#app { display: none; flex: 1; overflow: hidden; }
#app.on { display: flex; }

/* ── Symbol panel ────────────────────────────────────────────── */
#sym-panel {
  width: 220px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  border-right: 1px solid var(--border);
  background: var(--bg2);
}
.panel-head {
  padding: 5px 10px;
  border-bottom: 1px solid var(--border);
  font-size: .7em;
  color: var(--dim);
  text-transform: uppercase;
  letter-spacing: .06em;
  flex-shrink: 0;
}
#sym-search {
  border-radius: 0;
  border: none;
  border-bottom: 1px solid var(--border);
  background: var(--bg3);
  flex-shrink: 0;
}
#sym-list { overflow-y: auto; flex: 1; padding: 3px 0; }
.sym-row {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 3px 10px;
  cursor: pointer;
  border-radius: 3px;
  margin: 1px 4px;
  font-size: .8em;
  white-space: nowrap;
  overflow: hidden;
}
.sym-row:hover { background: rgba(255,255,255,.04); }
.sym-row.active { background: rgba(124,158,248,.15); }
.dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
.dot-root { background: var(--pink); }
.dot-mc   { background: var(--blue); }
.dot-gr   { background: var(--cyan); }
.dot-btn  { background: var(--gold); }
.sym-row-name { overflow: hidden; text-overflow: ellipsis; }
.sym-row.is-root .sym-row-name { color: var(--bright); font-weight: 600; }

/* ── Frame panel ─────────────────────────────────────────────── */
#frame-panel {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  min-width: 0;
}
#frame-info-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 12px;
  border-bottom: 1px solid var(--border);
  background: var(--bg2);
  flex-shrink: 0;
  font-size: .8em;
  flex-wrap: wrap;
}
.fi-name  { font-weight: 600; color: var(--bright); max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fi-count { color: var(--cyan); }
.fi-dim   { color: var(--dim); }
.fi-sel   { margin-left: auto; color: var(--gold); font-weight: 600; }

#frame-controls {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 5px 10px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
  flex-wrap: wrap;
}
#frame-controls button { font-size: .73em; padding: 2px 8px; }
#frame-controls label { font-size: .73em; color: var(--dim); display: flex; align-items: center; gap: 4px; margin-left: auto; cursor: pointer; }
#frame-controls label input { cursor: pointer; }

#frame-grid {
  flex: 1;
  overflow: auto;
  padding: 10px;
  display: flex;
  flex-wrap: wrap;
  align-content: flex-start;
  gap: 3px;
}
.frame-cell {
  width: 38px;
  height: 38px;
  border-radius: 4px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: .62em;
  color: rgba(255,255,255,.35);
  border: 2px solid transparent;
  user-select: none;
  position: relative;
  transition: border-color .08s;
}
.frame-cell.valid   { background: rgba(87,201,137,.20); color: rgba(87,201,137,.7); }
.frame-cell.empty   { background: var(--bg3); color: var(--dim); }
.frame-cell.selected { border-color: var(--blue) !important; color: var(--bright) !important; }
.frame-cell.selected.valid { background: rgba(87,201,137,.30); }
.frame-cell.selected.empty { background: rgba(170,183,218,.10); }
.frame-cell:hover:not(.selected) { border-color: rgba(200,208,234,.25); }
.frame-cell.active-preview { box-shadow: 0 0 0 2px var(--gold); }

/* ── Preview panel ───────────────────────────────────────────── */
#preview-panel {
  width: 300px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  border-left: 1px solid var(--border);
  background: var(--bg2);
}
#preview-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 10px;
  border-bottom: 1px solid var(--border);
  font-size: .7em;
  color: var(--dim);
  text-transform: uppercase;
  letter-spacing: .06em;
  flex-shrink: 0;
}
#preview-frame-lbl { color: var(--cyan); margin-left: auto; text-transform: none; letter-spacing: 0; }
#preview-img {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  background: #111;
}
#preview-img img { max-width: 100%; max-height: 100%; object-fit: contain; }
.preview-placeholder { color: var(--dim); font-size: .78em; text-align: center; padding: 20px; line-height: 1.7; }
#preview-info {
  padding: 5px 10px;
  font-size: .72em;
  color: var(--dim);
  border-top: 1px solid var(--border);
  min-height: 22px;
  flex-shrink: 0;
}

/* ── Export bar ──────────────────────────────────────────────── */
#export-bar {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 6px 14px;
  background: var(--bg2);
  border-top: 1px solid var(--border);
  flex-shrink: 0;
  flex-wrap: wrap;
}
.bar-label { font-size: .73em; color: var(--dim); white-space: nowrap; }
#outdir { flex: 1; min-width: 160px; }
#prefix { width: 110px; }
#export-progress { font-size: .75em; color: var(--dim); white-space: nowrap; min-width: 80px; }

/* ── Misc ────────────────────────────────────────────────────── */
.empty-msg { color: var(--dim); font-size: .82em; padding: 12px 16px; }
::-webkit-scrollbar { width: 5px; height: 5px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: #2a3050; border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: #3a4570; }
</style>
</head>
<body>

<div id="load-bar">
  <span class="brand">FLA Export</span>
  <input type="text" id="fla-path" placeholder="Path to .fla file…" spellcheck="false">
  <button onclick="browseFile()">Browse…</button>
  <button class="primary" onclick="loadFLA()">Load</button>
  <span id="load-status"></span>
</div>

<div id="app">

  <div id="sym-panel">
    <div class="panel-head">Symbols</div>
    <input type="text" id="sym-search" placeholder="Filter…" spellcheck="false">
    <div id="sym-list"><div class="empty-msg">Load a FLA first</div></div>
  </div>

  <div id="frame-panel">
    <div id="frame-info-bar">
      <span class="fi-name" id="fi-name">—</span>
      <span class="fi-count" id="fi-count"></span>
      <span class="fi-dim"   id="fi-dim"></span>
      <span class="fi-sel"   id="fi-sel"></span>
    </div>
    <div id="frame-controls">
      <button onclick="selAll()">All</button>
      <button onclick="selNone()">None</button>
      <button onclick="selValid()">Valid only</button>
      <button onclick="selInvert()">Invert</button>
      <label><input type="checkbox" id="chk-transparent"> Transparent bg</label>
    </div>
    <div id="frame-grid"><div class="empty-msg">Select a symbol</div></div>
  </div>

  <div id="preview-panel">
    <div id="preview-header">
      Preview
      <span id="preview-frame-lbl"></span>
    </div>
    <div id="preview-img"><div class="preview-placeholder">Click a frame cell<br>to preview it here</div></div>
    <div id="preview-info"></div>
  </div>

</div>

<div id="export-bar">
  <span class="bar-label">Output dir:</span>
  <input type="text" id="outdir" placeholder="/path/to/output/" spellcheck="false">
  <button onclick="browseDir()">Browse…</button>
  <span class="bar-label">Prefix:</span>
  <input type="text" id="prefix" placeholder="frame">
  <button class="primary" id="export-btn" onclick="startExport()">Export selected</button>
  <span id="export-progress"></span>
</div>

<script>
'use strict';

// ── State ─────────────────────────────────────────────────────────────────────
let allSymbols  = [];
let currentSym  = null;
let frameData   = [];   // [{valid, selected}]
let lastClick   = -1;
let previewSeq  = 0;

// ── Helpers ───────────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const h = s  => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

function setStatus(msg, cls = '') {
  const el = $('load-status');
  el.textContent = msg;
  el.className   = cls;
}

// ── Browse buttons ────────────────────────────────────────────────────────────
async function browseFile() {
  try {
    const r = await fetch('/api/browse-file');
    const d = await r.json();
    if (d.path) $('fla-path').value = d.path;
  } catch(_) {}
}

async function browseDir() {
  try {
    const r = await fetch('/api/browse-dir');
    const d = await r.json();
    if (d.path) $('outdir').value = d.path;
  } catch(_) {}
}

// ── Load FLA ──────────────────────────────────────────────────────────────────
async function loadFLA() {
  const path = $('fla-path').value.trim();
  if (!path) return;
  setStatus('Loading…', 's-info');

  try {
    const resp = await fetch('/api/load', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({path})
    });
    const data = await resp.json();
    if (!resp.ok) { setStatus(data.error || 'Error', 's-err'); return; }

    allSymbols = data.symbols;
    setStatus(`${data.filename}  —  ${data.symbolCount} symbols  ${data.width}×${data.height}  ${data.fps}fps`, 's-ok');
    $('app').classList.add('on');
    buildSymList();

    // Auto-select first root or first symbol
    const first = allSymbols.find(s => s.isRoot) || allSymbols[0];
    if (first) selectSym(first.name);

  } catch(e) {
    setStatus('Error: ' + e.message, 's-err');
  }
}

// ── Symbol list ───────────────────────────────────────────────────────────────
function dotClass(s) {
  if (s.isRoot) return 'dot-root';
  if (s.type === 'graphic') return 'dot-gr';
  if (s.type === 'button')  return 'dot-btn';
  return 'dot-mc';
}

function buildSymList(filter = '') {
  const syms = filter
    ? allSymbols.filter(s => s.name.toLowerCase().includes(filter.toLowerCase()))
    : allSymbols;

  $('sym-list').innerHTML = syms.length
    ? syms.map(s => `
        <div class="sym-row${s.isRoot?' is-root':''}${s.name===currentSym?' active':''}"
             data-name="${h(s.name)}" title="${h(s.name)}">
          <span class="dot ${dotClass(s)}"></span>
          <span class="sym-row-name">${h(s.name)}</span>
        </div>`).join('')
    : '<div class="empty-msg">No matches</div>';
}

$('sym-search').addEventListener('input', function() { buildSymList(this.value); });
$('sym-list').addEventListener('click', e => {
  const row = e.target.closest('.sym-row');
  if (row) selectSym(row.dataset.name);
});

// ── Select symbol → analyze frames ───────────────────────────────────────────
async function selectSym(name) {
  currentSym = name;
  lastClick  = -1;

  document.querySelectorAll('.sym-row').forEach(el =>
    el.classList.toggle('active', el.dataset.name === name));

  $('fi-name').textContent  = name;
  $('fi-count').textContent = '';
  $('fi-dim').textContent   = '';
  $('fi-sel').textContent   = '';
  $('frame-grid').innerHTML = '<div class="empty-msg">Analyzing frames…</div>';
  $('preview-img').innerHTML = '<div class="preview-placeholder">Loading…</div>';
  $('preview-frame-lbl').textContent = '';
  $('preview-info').textContent = '';

  try {
    const resp = await fetch('/api/analyze', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name})
    });
    const data = await resp.json();
    if (!resp.ok) {
      $('frame-grid').innerHTML = `<div class="empty-msg">${h(data.error)}</div>`;
      return;
    }

    frameData = data.frames.map(v => ({valid: v, selected: v}));

    const validN = frameData.filter(f => f.valid).length;
    $('fi-count').textContent = `${data.frameCount} frames`;
    $('fi-dim').textContent   = `(${validN} with content)`;
    $('preview-img').innerHTML = '<div class="preview-placeholder">Click a frame cell<br>to preview it here</div>';

    if (!$('prefix').value)
      $('prefix').value = name.replace(/[^\w-]/g, '_').replace(/^_+|_+$/g, '');

    renderGrid();
    updateSelCount();

  } catch(e) {
    $('frame-grid').innerHTML = `<div class="empty-msg">${h(e.message)}</div>`;
  }
}

// ── Frame grid ────────────────────────────────────────────────────────────────
function renderGrid() {
  if (!frameData.length) {
    $('frame-grid').innerHTML = '<div class="empty-msg">No frames found</div>';
    return;
  }
  $('frame-grid').innerHTML = frameData.map((f, i) => {
    const cls = [
      'frame-cell',
      f.valid ? 'valid' : 'empty',
      f.selected ? 'selected' : ''
    ].filter(Boolean).join(' ');
    return `<div class="${cls}" data-idx="${i}" title="Frame ${i}${f.valid ? '' : ' (empty)'}">${i}</div>`;
  }).join('');
}

function updateSelCount() {
  const n = frameData.filter(f => f.selected).length;
  $('fi-sel').textContent = n ? `${n} selected` : '';
}

$('frame-grid').addEventListener('click', e => {
  const cell = e.target.closest('.frame-cell');
  if (!cell) return;
  const idx = parseInt(cell.dataset.idx, 10);

  if (e.shiftKey && lastClick >= 0) {
    const a = Math.min(lastClick, idx);
    const b = Math.max(lastClick, idx);
    const target = !frameData[lastClick].selected;
    for (let i = a; i <= b; i++) frameData[i].selected = target;
  } else {
    frameData[idx].selected = !frameData[idx].selected;
    lastClick = idx;
  }

  renderGrid();
  updateSelCount();
  previewFrame(idx);
});

// ── Selection helpers ─────────────────────────────────────────────────────────
function selAll()    { frameData.forEach(f => f.selected = true);        renderGrid(); updateSelCount(); }
function selNone()   { frameData.forEach(f => f.selected = false);       renderGrid(); updateSelCount(); }
function selValid()  { frameData.forEach(f => f.selected = f.valid);     renderGrid(); updateSelCount(); }
function selInvert() { frameData.forEach(f => f.selected = !f.selected); renderGrid(); updateSelCount(); }

// ── Preview ───────────────────────────────────────────────────────────────────
async function previewFrame(idx) {
  if (!currentSym) return;
  const seq = ++previewSeq;

  document.querySelectorAll('.frame-cell').forEach(c =>
    c.classList.toggle('active-preview', parseInt(c.dataset.idx, 10) === idx));

  $('preview-frame-lbl').textContent = `Frame ${idx}`;
  $('preview-img').innerHTML = '<div class="preview-placeholder">Rendering…</div>';
  $('preview-info').textContent = '';

  const t0 = performance.now();
  const transparent = $('chk-transparent').checked ? 1 : 0;

  try {
    const resp = await fetch(
      `/api/render?sym=${encodeURIComponent(currentSym)}&frame=${idx}&transparent=${transparent}`
    );
    if (seq !== previewSeq) return;

    if (!resp.ok) {
      $('preview-img').innerHTML = '<div class="preview-placeholder">Nothing to render</div>';
      return;
    }

    const svg  = await resp.text();
    if (seq !== previewSeq) return;

    const ms   = Math.round(performance.now() - t0);
    const blob = new Blob([svg], {type: 'image/svg+xml'});
    const url  = URL.createObjectURL(blob);
    $('preview-img').innerHTML = `<img src="${url}">`;
    $('preview-info').textContent = `frame ${idx}  —  rendered in ${ms}ms`;

  } catch(e) {
    if (seq !== previewSeq) return;
    $('preview-img').innerHTML = `<div class="preview-placeholder">${h(e.message)}</div>`;
  }
}

// ── Export ────────────────────────────────────────────────────────────────────
async function startExport() {
  if (!currentSym) { alert('Select a symbol first.'); return; }
  const outdir = $('outdir').value.trim();
  if (!outdir)  { alert('Set an output directory.'); return; }

  const sel = frameData.map((f, i) => f.selected ? i : -1).filter(i => i >= 0);
  if (!sel.length) { alert('No frames selected.'); return; }

  const prefix      = $('prefix').value.trim() || currentSym.replace(/[^\w-]/g, '_');
  const transparent = $('chk-transparent').checked;
  const sym         = currentSym;
  const btn         = $('export-btn');
  const prog        = $('export-progress');

  btn.disabled = true;
  let done = 0, errors = 0;

  function tick() {
    prog.textContent = errors
      ? `${done}/${sel.length} (${errors} err)`
      : `${done}/${sel.length}`;
  }
  tick();

  for (const frame of sel) {
    try {
      const resp = await fetch('/api/export-one', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({sym, frame, outdir, prefix, transparent})
      });
      if (resp.ok) done++;
      else errors++;
    } catch(_) { errors++; }
    tick();
  }

  btn.disabled = false;
  prog.textContent = errors
    ? `Done: ${done} exported, ${errors} errors → ${outdir}`
    : `Done: ${done} SVGs → ${outdir}`;
}

// ── Keyboard ──────────────────────────────────────────────────────────────────
document.addEventListener('keydown', e => {
  if (e.key === 'Enter' && e.target === $('fla-path')) loadFLA();
});

// ── Auto-load if server was started with a path ───────────────────────────────
fetch('/api/initial').then(r => r.json()).then(d => {
  if (d.path) { $('fla-path').value = d.path; loadFLA(); }
}).catch(() => {});
</script>
</body>
</html>
"""

# ── HTTP Handler ──────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def _json(self, data, code=200):
        b = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _read_json(self):
        n = int(self.headers.get('Content-Length', 0))
        return json.loads(self.rfile.read(n)) if n else {}

    def do_GET(self):
        p  = urlparse(self.path)
        qs = parse_qs(p.query)

        if p.path == '/':
            b = HTML.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return

        if p.path == '/api/initial':
            self._json({'path': INITIAL_PATH})
            return

        if p.path == '/api/render':
            sym         = unquote(qs.get('sym',  [''])[0])
            frame       = int(qs.get('frame', ['0'])[0])
            transparent = qs.get('transparent', ['0'])[0] == '1'
            with _lock:
                svg = _make_svg(sym, frame, transparent)
            if svg:
                b = svg.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'image/svg+xml')
                self.send_header('Content-Length', str(len(b)))
                self.end_headers()
                self.wfile.write(b)
            else:
                self._json({'error': 'nothing to render'}, 404)
            return

        if p.path == '/api/browse-file':
            _dlq.put('file')
            try:    path = _dlr.get(timeout=60)
            except queue.Empty: path = ''
            self._json({'path': path})
            return

        if p.path == '/api/browse-dir':
            _dlq.put('dir')
            try:    path = _dlr.get(timeout=60)
            except queue.Empty: path = ''
            self._json({'path': path})
            return

        self._json({'error': 'not found'}, 404)

    def do_POST(self):
        path = urlparse(self.path).path

        if path == '/api/load':
            data = self._read_json()
            fla  = data.get('path', '').strip()
            if not fla or not Path(fla).exists():
                self._json({'error': f'File not found: {fla}'}, 400); return
            try:
                p    = Path(fla)
                zd   = open_fla(p)
                dom  = parse_dom(zd)
                syms = get_symbols(zd)
                roots = set(find_roots(syms))
                with _lock:
                    _state.update({'symbols': syms, 'dom': dom, 'roots': roots,
                                   'filename': p.name})
                sym_list = [
                    {'name': n,
                     'type': syms[n].get('symbolType', 'movie clip'),
                     'isRoot': n in roots}
                    for n in sorted(syms)
                ]
                self._json({
                    'filename':    p.name,
                    'width':       dom.get('width', '?'),
                    'height':      dom.get('height', '?'),
                    'fps':         dom.get('frameRate', '24'),
                    'symbolCount': len(syms),
                    'symbols':     sym_list,
                })
            except Exception as e:
                self._json({'error': str(e)}, 500)
            return

        if path == '/api/analyze':
            data = self._read_json()
            name = data.get('name', '')
            with _lock:
                sym = _state['symbols'].get(name)
            if sym is None:
                self._json({'error': f'Symbol not found: {name}'}, 404); return
            count  = _frame_count(sym)
            frames = [_frame_valid(sym, i) for i in range(count)]
            self._json({'name': name, 'frameCount': count, 'frames': frames})
            return

        if path == '/api/export-one':
            data        = self._read_json()
            sym         = data.get('sym', '')
            frame       = int(data.get('frame', 0))
            outdir      = data.get('outdir', '').strip()
            prefix      = data.get('prefix', '') or safe_name(sym)
            transparent = bool(data.get('transparent', False))
            if not outdir:
                self._json({'error': 'No output directory'}, 400); return
            out = Path(outdir)
            try:
                out.mkdir(parents=True, exist_ok=True)
                with _lock:
                    svg = _make_svg(sym, frame, transparent)
                if svg:
                    dest = out / f'{prefix}_{frame:04d}.svg'
                    dest.write_text(svg, encoding='utf-8')
                    self._json({'saved': str(dest)})
                else:
                    self._json({'error': 'nothing to render'}, 400)
            except Exception as e:
                self._json({'error': str(e)}, 500)
            return

        self._json({'error': 'not found'}, 404)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    srv   = HTTPServer(('127.0.0.1', PORT), Handler)
    t_srv = threading.Thread(target=srv.serve_forever, daemon=True)
    t_srv.start()
    print(f'FLA Export GUI  →  http://127.0.0.1:{PORT}')
    if INITIAL_PATH: print(f'Pre-loading: {INITIAL_PATH}')
    print('Press Ctrl+C to stop.\n')

    # Open browser after a short delay
    threading.Thread(
        target=lambda: (time.sleep(0.4), webbrowser.open(f'http://127.0.0.1:{PORT}')),
        daemon=True
    ).start()

    # Run tkinter on the main thread to service native file dialogs
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        while True:
            try:
                req = _dlq.get_nowait()
                if req == 'file':
                    path = filedialog.askopenfilename(
                        parent=root,
                        title='Open FLA file',
                        filetypes=[('FLA files', '*.fla'), ('All files', '*.*')]
                    )
                    _dlr.put(path or '')
                elif req == 'dir':
                    path = filedialog.askdirectory(parent=root, title='Select output directory')
                    _dlr.put(path or '')
            except queue.Empty:
                root.update()
                time.sleep(0.02)
    except ImportError:
        print('(tkinter not available — Browse buttons will not work, type paths manually)')
        try:
            while True: time.sleep(1)
        except KeyboardInterrupt:
            pass
    except KeyboardInterrupt:
        pass

    print('Stopped.')


if __name__ == '__main__':
    main()
