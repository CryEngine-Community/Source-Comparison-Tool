#!/usr/bin/env python3
"""
UE Source Diff Tool
====================
A GUI application that compares two Unreal Engine source builds and generates
an interactive HTML diff report.

Features:
  - Browse and select two UE engine directories
  - Multithreaded file comparison with real-time progress
  - Generates a .json data file and an interactive .html report
  - Open report directly in browser from the app
  - Can be compiled to .exe with PyInstaller

Compile to .exe:
  pip install pyinstaller
  pyinstaller --onefile --windowed --name "UE Source Diff" --icon=NONE ue_source_diff.py
"""

import difflib
import hashlib
import html as html_module
import json
import os
import platform
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

DEFAULT_EXTENSIONS = {
    ".h", ".hpp", ".hxx",
    ".c", ".cpp", ".cxx", ".cc",
    ".inl", ".inc",
    ".cs",
    ".usf", ".ush",
    ".py",
    ".xml", ".json",
    ".ini", ".cfg",
    ".uproject", ".uplugin",
}

COMPOUND_EXTENSIONS = (".build.cs", ".target.cs")

APP_NAME = "UE Source Diff Tool"
VERSION = "1.0.0"


# ══════════════════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class FileDiff:
    relative_path: str
    status: str
    location_a: Optional[str]
    location_b: Optional[str]
    lines_added: int = 0
    lines_removed: int = 0
    diff: list = field(default_factory=list)


# ══════════════════════════════════════════════════════════════════════════════
# COMPARISON ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def fast_hash(filepath: str) -> str:
    h = hashlib.md5()
    try:
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def read_lines(filepath: str) -> list:
    for enc in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            with open(filepath, "r", encoding=enc, errors="replace") as f:
                return f.readlines()
        except Exception:
            continue
    return []


def collect_source_files(root: str, extensions: set) -> dict:
    result = {}
    root = os.path.abspath(root)
    for dirpath, _, filenames in os.walk(root):
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext in extensions or any(fname.lower().endswith(ce) for ce in COMPOUND_EXTENSIONS):
                abs_path = os.path.join(dirpath, fname)
                rel_path = os.path.relpath(abs_path, root)
                result[rel_path] = abs_path
    return result


def compare_file(rel_path: str, path_a: str, path_b: str, context_lines: int = 3) -> Optional[FileDiff]:
    exists_a = os.path.isfile(path_a)
    exists_b = os.path.isfile(path_b)

    if exists_a and exists_b:
        if fast_hash(path_a) == fast_hash(path_b):
            return None
        lines_a = read_lines(path_a)
        lines_b = read_lines(path_b)
        diff_lines = list(difflib.unified_diff(
            lines_a, lines_b,
            fromfile=f"A/{rel_path}", tofile=f"B/{rel_path}",
            n=context_lines,
        ))
        if not diff_lines:
            return None
        added = sum(1 for l in diff_lines if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in diff_lines if l.startswith("-") and not l.startswith("---"))
        return FileDiff(
            relative_path=rel_path, status="modified",
            location_a=path_a, location_b=path_b,
            lines_added=added, lines_removed=removed,
            diff=[l.rstrip("\n") for l in diff_lines],
        )
    elif exists_a and not exists_b:
        return FileDiff(relative_path=rel_path, status="removed", location_a=path_a, location_b=None)
    elif not exists_a and exists_b:
        return FileDiff(relative_path=rel_path, status="added", location_a=None, location_b=path_b)
    return None


# ══════════════════════════════════════════════════════════════════════════════
# HTML REPORT BUILDER
# ══════════════════════════════════════════════════════════════════════════════

def esc(text: str) -> str:
    return html_module.escape(text, quote=True)


def build_html_report(report: dict) -> str:
    summary = report["summary"]
    diffs = report["differences"]

    meta_list = []
    diff_list = []
    for d in diffs:
        meta_list.append({
            "r": d["relative_path"], "n": os.path.basename(d["relative_path"]),
            "d": os.path.dirname(d["relative_path"]) or ".",
            "s": d["status"], "la": d.get("lines_added", 0), "lr": d.get("lines_removed", 0),
            "pa": d.get("location_a") or "", "pb": d.get("location_b") or "",
        })
        diff_list.append(d.get("diff", []))

    meta_json = json.dumps(meta_list, ensure_ascii=False, separators=(",", ":"))
    diff_json_str = json.dumps(diff_list, ensure_ascii=False, separators=(",", ":"))
    diff_json_escaped = (diff_json_str
        .replace("\\", "\\\\")
        .replace("`", "\\`")
        .replace("${", "\\${")
        .replace("</script>", "<\\/script>")
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>UE Source Diff Report</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=DM+Sans:wght@400;500;600;700&display=swap');
:root {{
  --bg-root:#0c0e12;--bg-surface:#13161c;--bg-card:#181c24;--bg-hover:#1e2330;
  --border:#262d3a;--border-light:#2e3748;--text-primary:#e4e8f0;--text-secondary:#8892a6;
  --text-muted:#555f73;--accent-blue:#4fa3e8;--accent-cyan:#36d6c8;
  --diff-add-bg:rgba(56,212,120,0.08);--diff-add-text:#5ce890;
  --diff-del-bg:rgba(232,80,80,0.08);--diff-del-text:#f07070;
  --diff-hunk-bg:rgba(79,163,232,0.06);--diff-hunk-text:#4fa3e8;
  --diff-header-text:#8892a6;--badge-modified:#4fa3e8;--badge-added:#38d478;
  --badge-removed:#e85050;--radius:8px;
  --font-body:'DM Sans',sans-serif;--font-mono:'JetBrains Mono',monospace;
}}
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:var(--font-body);background:var(--bg-root);color:var(--text-primary);line-height:1.6;-webkit-font-smoothing:antialiased}}
.container{{max-width:1320px;margin:0 auto;padding:40px 32px 80px}}
.report-header{{margin-bottom:40px;padding-bottom:32px;border-bottom:1px solid var(--border)}}
.report-header h1{{font-size:28px;font-weight:700;letter-spacing:-0.5px}}
.report-header h1 span{{color:var(--accent-cyan)}}
.report-header .subtitle{{font-size:13px;color:var(--text-muted);font-family:var(--font-mono);margin-top:6px}}
.summary-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px;margin-bottom:36px}}
.stat-card{{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);padding:20px}}
.stat-label{{font-size:11px;text-transform:uppercase;letter-spacing:1.2px;color:var(--text-muted);margin-bottom:8px;font-weight:600}}
.stat-value{{font-size:28px;font-weight:700;font-family:var(--font-mono)}}
.blue{{color:var(--accent-blue)}}.green{{color:var(--badge-added)}}.red{{color:var(--badge-removed)}}.cyan{{color:var(--accent-cyan)}}
.toolbar{{display:flex;gap:10px;margin-bottom:20px;flex-wrap:wrap;align-items:center}}
.search-box{{flex:1;min-width:240px;position:relative}}
.search-box input{{width:100%;padding:10px 16px 10px 40px;background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);color:var(--text-primary);font-family:var(--font-mono);font-size:13px;outline:none}}
.search-box input:focus{{border-color:var(--accent-blue)}}
.search-box input::placeholder{{color:var(--text-muted)}}
.search-icon{{position:absolute;left:14px;top:50%;transform:translateY(-50%);color:var(--text-muted);pointer-events:none}}
.fbtn{{padding:9px 16px;border:1px solid var(--border);border-radius:var(--radius);background:var(--bg-card);color:var(--text-secondary);font-size:13px;font-weight:500;cursor:pointer;font-family:var(--font-body);user-select:none}}
.fbtn:hover{{border-color:var(--border-light);color:var(--text-primary)}}
.fbtn.active{{background:var(--accent-blue);border-color:var(--accent-blue);color:#fff}}
.result-count{{font-size:12px;color:var(--text-muted);font-family:var(--font-mono);margin-left:auto}}
#loading{{padding:60px;text-align:center;color:var(--text-muted);font-family:var(--font-mono);font-size:14px}}
#loading .spinner{{display:inline-block;width:20px;height:20px;border:2px solid var(--border);border-top-color:var(--accent-cyan);border-radius:50%;animation:spin .8s linear infinite;margin-right:12px;vertical-align:middle}}
@keyframes spin{{to{{transform:rotate(360deg)}}}}
.frow{{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);margin-bottom:5px;overflow:hidden}}
.frow:hover{{border-color:var(--border-light)}}
.fhdr{{display:flex;justify-content:space-between;align-items:center;padding:13px 18px;cursor:pointer;user-select:none}}
.fhdr:hover{{background:var(--bg-hover)}}
.fmeta{{display:flex;align-items:center;gap:12px;min-width:0;flex:1}}
.fnb{{display:flex;flex-direction:column;min-width:0}}
.fname{{font-family:var(--font-mono);font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.fdir{{font-size:11px;color:var(--text-muted);font-family:var(--font-mono);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:600px}}
.fstats{{display:flex;align-items:center;gap:8px;flex-shrink:0}}
.arrow{{color:var(--text-muted);font-size:13px;transition:transform .2s;display:inline-block}}
.arrow.open{{transform:rotate(90deg)}}
.badge{{font-size:10px;font-weight:700;letter-spacing:.8px;padding:3px 10px;border-radius:100px;text-transform:uppercase;font-family:var(--font-mono);flex-shrink:0}}
.badge-modified{{background:rgba(79,163,232,.12);color:var(--badge-modified)}}
.badge-added{{background:rgba(56,212,120,.12);color:var(--badge-added)}}
.badge-removed{{background:rgba(232,80,80,.12);color:var(--badge-removed)}}
.chip{{font-size:12px;font-family:var(--font-mono);font-weight:600;padding:2px 8px;border-radius:5px}}
.ca{{color:var(--diff-add-text);background:var(--diff-add-bg)}}.cr{{color:var(--diff-del-text);background:var(--diff-del-bg)}}
.dpanel{{border-top:1px solid var(--border);display:none}}.dpanel.open{{display:block}}
.flocs{{display:flex;flex-direction:column;gap:2px;padding:10px 18px;font-size:11px;font-family:var(--font-mono);color:var(--text-muted);background:var(--bg-surface);border-bottom:1px solid var(--border)}}
.dblock{{overflow-x:auto;background:var(--bg-surface)}}
.dblock pre{{margin:0;padding:14px 18px;font-family:var(--font-mono);font-size:12.5px;line-height:1.65;tab-size:4}}
.dblock code{{display:block}}.dblock span{{display:block;padding:0 5px;border-radius:3px}}
.da{{background:var(--diff-add-bg);color:var(--diff-add-text)}}
.dd{{background:var(--diff-del-bg);color:var(--diff-del-text)}}
.dh{{background:var(--diff-hunk-bg);color:var(--diff-hunk-text);margin-top:6px;font-weight:500}}
.dhd{{color:var(--diff-header-text);font-weight:700}}.dc{{color:var(--text-secondary)}}
::-webkit-scrollbar{{width:8px;height:8px}}::-webkit-scrollbar-track{{background:var(--bg-root)}}::-webkit-scrollbar-thumb{{background:var(--border);border-radius:4px}}
@media(max-width:768px){{.container{{padding:20px 14px 60px}}.summary-grid{{grid-template-columns:repeat(2,1fr)}}.fdir{{max-width:180px}}}}
</style>
</head>
<body>
<div class="container">
  <div class="report-header">
    <h1>Unreal Engine <span>Source Diff</span></h1>
    <div class="subtitle">A: {esc(summary['engine_a'])}<br>B: {esc(summary['engine_b'])}</div>
  </div>
  <div class="summary-grid">
    <div class="stat-card"><div class="stat-label">Files Scanned</div><div class="stat-value cyan">{summary['total_files_scanned']:,}</div></div>
    <div class="stat-card"><div class="stat-label">Modified</div><div class="stat-value blue">{summary['files_modified']:,}</div></div>
    <div class="stat-card"><div class="stat-label">Added</div><div class="stat-value green">{summary['files_added']:,}</div></div>
    <div class="stat-card"><div class="stat-label">Removed</div><div class="stat-value red">{summary['files_removed']:,}</div></div>
    <div class="stat-card"><div class="stat-label">Lines Added</div><div class="stat-value green">{summary['total_lines_added']:,}</div></div>
    <div class="stat-card"><div class="stat-label">Lines Removed</div><div class="stat-value red">{summary['total_lines_removed']:,}</div></div>
  </div>
  <div class="toolbar" id="toolbar" style="display:none">
    <div class="search-box"><span class="search-icon">&#x2315;</span><input type="text" id="searchInput" placeholder="Search by file name or path… (Ctrl+K)"></div>
    <button class="fbtn active" onclick="setFilter('all',this)">All ({len(diffs):,})</button>
    <button class="fbtn" onclick="setFilter('modified',this)">Modified ({summary['files_modified']:,})</button>
    <button class="fbtn" onclick="setFilter('added',this)">Added ({summary['files_added']:,})</button>
    <button class="fbtn" onclick="setFilter('removed',this)">Removed ({summary['files_removed']:,})</button>
    <button class="fbtn" onclick="collapseAll()">Collapse All</button>
    <span class="result-count" id="resultCount"></span>
  </div>
  <div id="loading"><span class="spinner"></span>Parsing diff data…</div>
  <div id="vscroll" style="display:none"></div>
</div>
<script>const META={meta_json};</script>
<script>let DIFFS=null;const _DIFF_RAW=`{diff_json_escaped}`;</script>
<script>
(function(){{
const searchInput=document.getElementById('searchInput'),vscroll=document.getElementById('vscroll'),
loadingEl=document.getElementById('loading'),toolbarEl=document.getElementById('toolbar'),
resultCountEl=document.getElementById('resultCount');
const PATHS_LOWER=META.map(m=>m.r.toLowerCase()),N=META.length;
let filtered=[],activeFilter='all',searchQuery='',expandedSet=new Set();
function init(){{setTimeout(()=>{{try{{DIFFS=JSON.parse(_DIFF_RAW)}}catch(e){{console.error(e);DIFFS=new Array(N).fill([])}};loadingEl.style.display='none';toolbarEl.style.display='flex';vscroll.style.display='block';refilter()}},50)}}
function refilter(){{const q=searchQuery,f=activeFilter;filtered=[];for(let i=0;i<N;i++){{if(f!=='all'&&META[i].s!==f)continue;if(q&&!PATHS_LOWER[i].includes(q))continue;filtered.push(i)}};expandedSet.clear();resultCountEl.textContent=filtered.length.toLocaleString()+' files';renderList()}}
window.refilter=refilter;
let sTimer=null;searchInput.addEventListener('input',e=>{{clearTimeout(sTimer);sTimer=setTimeout(()=>{{searchQuery=e.target.value.toLowerCase().trim();refilter()}},100)}});
window.setFilter=function(f,btn){{activeFilter=f;document.querySelectorAll('.fbtn').forEach(b=>b.classList.remove('active'));btn.classList.add('active');refilter()}};
window.collapseAll=function(){{expandedSet.clear();vscroll.querySelectorAll('.dpanel.open').forEach(p=>p.classList.remove('open'));vscroll.querySelectorAll('.arrow.open').forEach(a=>a.classList.remove('open'))}};
function esc(s){{const d=document.createElement('div');d.textContent=s;return d.innerHTML}}
function buildDiff(idx){{if(!DIFFS)return'<div style="padding:20px;color:#555f73;text-align:center">Loading…</div>';const lines=DIFFS[idx];if(!lines||!lines.length)return'<div style="padding:20px;color:#555f73;text-align:center;font-style:italic">No diff content (file added or removed)</div>';const buf=['<div class="dblock"><pre><code>'];for(let i=0;i<lines.length;i++){{const l=lines[i],c=l.charCodeAt(0);let cls;if(c===43)cls=(l.charCodeAt(1)===43&&l.charCodeAt(2)===43)?'dhd':'da';else if(c===45)cls=(l.charCodeAt(1)===45&&l.charCodeAt(2)===45)?'dhd':'dd';else if(c===64)cls='dh';else cls='dc';buf.push('<span class="'+cls+'">'+esc(l)+'</span>')}};buf.push('</code></pre></div>');return buf.join('')}}
const PAGE_SIZE=100;let renderedCount=0,sentinel=null,observer=null;
function renderList(){{vscroll.innerHTML='';renderedCount=0;if(!filtered.length){{vscroll.innerHTML='<div style="padding:40px;text-align:center;color:#555f73">No matching files.</div>';return}};renderNextPage();setupObserver()}}
function renderNextPage(){{const end=Math.min(renderedCount+PAGE_SIZE,filtered.length),frag=document.createDocumentFragment();for(let vi=renderedCount;vi<end;vi++){{const di=filtered[vi];frag.appendChild(createRow(di,META[di]))}};if(sentinel&&sentinel.parentNode)sentinel.parentNode.removeChild(sentinel);vscroll.appendChild(frag);renderedCount=end;if(renderedCount<filtered.length){{sentinel=document.createElement('div');sentinel.style.height='1px';vscroll.appendChild(sentinel);if(observer)observer.observe(sentinel)}}}}
function setupObserver(){{if(observer)observer.disconnect();observer=new IntersectionObserver(entries=>{{if(entries[0].isIntersecting&&renderedCount<filtered.length)renderNextPage()}},{{rootMargin:'800px'}});if(sentinel)observer.observe(sentinel)}}
function createRow(di,m){{const row=document.createElement('div');row.className='frow';const chips=m.s==='modified'?`<span class="chip ca">+${{m.la.toLocaleString()}}</span><span class="chip cr">\\u2212${{m.lr.toLocaleString()}}</span>`:'';row.innerHTML=`<div class="fhdr"><div class="fmeta"><span class="badge badge-${{m.s}}">${{m.s.toUpperCase()}}</span><div class="fnb"><span class="fname">${{esc(m.n)}}</span><span class="fdir">${{esc(m.d)}}</span></div></div><div class="fstats">${{chips}}<span class="arrow">&#x25B8;</span></div></div><div class="dpanel" data-di="${{di}}"></div>`;const hdr=row.querySelector('.fhdr'),panel=row.querySelector('.dpanel'),arrow=row.querySelector('.arrow');hdr.addEventListener('click',()=>{{const isOpen=panel.classList.contains('open');if(isOpen){{panel.classList.remove('open');arrow.classList.remove('open');expandedSet.delete(di)}}else{{if(!panel.dataset.loaded){{const locA=m.pa?`<span><b>A:</b> ${{esc(m.pa)}}</span>`:'',locB=m.pb?`<span><b>B:</b> ${{esc(m.pb)}}</span>`:'';panel.innerHTML=`<div class="flocs">${{locA}}${{locB}}</div>${{buildDiff(di)}}`;panel.dataset.loaded='1'}};panel.classList.add('open');arrow.classList.add('open');expandedSet.add(di)}}}});return row}}
document.addEventListener('keydown',e=>{{if((e.metaKey||e.ctrlKey)&&e.key==='k'){{e.preventDefault();searchInput.focus()}}}});
init();
}})();
</script>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════════════
# GUI APPLICATION
# ══════════════════════════════════════════════════════════════════════════════

class UESourceDiffApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("820x720")
        self.root.minsize(700, 600)
        self.root.configure(bg="#1a1d23")

        # State
        self.engine_a_path = tk.StringVar()
        self.engine_b_path = tk.StringVar()
        self.output_dir = tk.StringVar(value=str(Path.home() / "Desktop"))
        self.thread_count = tk.IntVar(value=os.cpu_count() or 8)
        self.context_lines = tk.IntVar(value=3)
        self.is_running = False
        self.should_cancel = False
        self.last_report_path = None

        self._setup_styles()
        self._build_ui()

    # ─── Styles ───────────────────────────────────────────────────────────

    def _setup_styles(self):
        self.style = ttk.Style()
        self.style.theme_use("clam")

        # Colors
        bg = "#1a1d23"
        surface = "#22262e"
        card = "#2a2f3a"
        border = "#3a4050"
        text = "#e4e8f0"
        muted = "#8892a6"
        accent = "#4fa3e8"
        cyan = "#36d6c8"
        green = "#38d478"
        red = "#e85050"

        self.colors = {
            "bg": bg, "surface": surface, "card": card, "border": border,
            "text": text, "muted": muted, "accent": accent, "cyan": cyan,
            "green": green, "red": red,
        }

        self.style.configure("TFrame", background=bg)
        self.style.configure("Card.TFrame", background=surface)
        self.style.configure("TLabel", background=bg, foreground=text, font=("Segoe UI", 10))
        self.style.configure("Header.TLabel", background=bg, foreground=text, font=("Segoe UI", 18, "bold"))
        self.style.configure("Sub.TLabel", background=bg, foreground=muted, font=("Segoe UI", 9))
        self.style.configure("Card.TLabel", background=surface, foreground=text, font=("Segoe UI", 10))
        self.style.configure("CardSub.TLabel", background=surface, foreground=muted, font=("Segoe UI", 9))
        self.style.configure("Accent.TButton", font=("Segoe UI", 11, "bold"))
        self.style.configure("TSpinbox", fieldbackground=card, foreground=text)

        # Progress bar
        self.style.configure(
            "Custom.Horizontal.TProgressbar",
            troughcolor=surface, background=accent, thickness=8,
        )

    # ─── UI Layout ────────────────────────────────────────────────────────

    def _build_ui(self):
        c = self.colors
        main = ttk.Frame(self.root, padding=24)
        main.pack(fill=tk.BOTH, expand=True)

        # Header
        ttk.Label(main, text="UE Source Diff", style="Header.TLabel").pack(anchor="w")
        ttk.Label(main, text=f"Compare Unreal Engine source builds  •  v{VERSION}", style="Sub.TLabel").pack(anchor="w", pady=(0, 20))

        # ── Engine A ──
        self._build_path_row(main, "Engine A (Base)", self.engine_a_path)

        # ── Engine B ──
        self._build_path_row(main, "Engine B (Target)", self.engine_b_path)

        # ── Output directory ──
        self._build_path_row(main, "Output Directory", self.output_dir, is_output=True)

        # ── Settings row ──
        settings_frame = ttk.Frame(main)
        settings_frame.pack(fill=tk.X, pady=(12, 0))

        # Threads
        tf = ttk.Frame(settings_frame)
        tf.pack(side=tk.LEFT, padx=(0, 24))
        ttk.Label(tf, text="Threads", style="Sub.TLabel").pack(anchor="w")
        thread_spin = tk.Spinbox(
            tf, from_=1, to=64, textvariable=self.thread_count, width=6,
            bg=c["card"], fg=c["text"], insertbackground=c["text"],
            highlightthickness=0, buttonbackground=c["surface"],
            font=("Consolas", 11), relief="flat",
        )
        thread_spin.pack(anchor="w", pady=(2, 0))

        # Context lines
        cf = ttk.Frame(settings_frame)
        cf.pack(side=tk.LEFT, padx=(0, 24))
        ttk.Label(cf, text="Context Lines", style="Sub.TLabel").pack(anchor="w")
        ctx_spin = tk.Spinbox(
            cf, from_=0, to=20, textvariable=self.context_lines, width=6,
            bg=c["card"], fg=c["text"], insertbackground=c["text"],
            highlightthickness=0, buttonbackground=c["surface"],
            font=("Consolas", 11), relief="flat",
        )
        ctx_spin.pack(anchor="w", pady=(2, 0))

        # ── Buttons ──
        btn_frame = ttk.Frame(main)
        btn_frame.pack(fill=tk.X, pady=(24, 0))

        self.run_btn = tk.Button(
            btn_frame, text="▶  Run Comparison", font=("Segoe UI", 12, "bold"),
            bg=c["accent"], fg="#ffffff", activebackground="#3d8ad0", activeforeground="#ffffff",
            relief="flat", padx=24, pady=10, cursor="hand2",
            command=self._start_comparison,
        )
        self.run_btn.pack(side=tk.LEFT)

        self.cancel_btn = tk.Button(
            btn_frame, text="Cancel", font=("Segoe UI", 10),
            bg=c["surface"], fg=c["muted"], activebackground=c["card"], activeforeground=c["text"],
            relief="flat", padx=16, pady=10, cursor="hand2",
            command=self._cancel, state=tk.DISABLED,
        )
        self.cancel_btn.pack(side=tk.LEFT, padx=(12, 0))

        self.open_btn = tk.Button(
            btn_frame, text="Open Report", font=("Segoe UI", 10),
            bg=c["surface"], fg=c["cyan"], activebackground=c["card"], activeforeground=c["cyan"],
            relief="flat", padx=16, pady=10, cursor="hand2",
            command=self._open_report, state=tk.DISABLED,
        )
        self.open_btn.pack(side=tk.RIGHT)

        # ── Progress ──
        prog_frame = ttk.Frame(main)
        prog_frame.pack(fill=tk.X, pady=(20, 0))

        self.progress_bar = ttk.Progressbar(
            prog_frame, style="Custom.Horizontal.TProgressbar",
            mode="determinate", maximum=100,
        )
        self.progress_bar.pack(fill=tk.X)

        self.status_label = ttk.Label(main, text="Ready", style="Sub.TLabel")
        self.status_label.pack(anchor="w", pady=(6, 0))

        self.file_label = ttk.Label(main, text="", style="Sub.TLabel")
        self.file_label.pack(anchor="w", pady=(2, 0))

        # ── Log area ──
        ttk.Label(main, text="Log", style="Sub.TLabel").pack(anchor="w", pady=(16, 4))

        log_frame = tk.Frame(main, bg=c["surface"], highlightthickness=1, highlightbackground=c["border"])
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_text = tk.Text(
            log_frame, bg=c["surface"], fg=c["text"], insertbackground=c["text"],
            font=("Consolas", 9), relief="flat", padx=12, pady=10,
            state=tk.DISABLED, wrap=tk.WORD, selectbackground=c["accent"],
        )
        scrollbar = tk.Scrollbar(log_frame, command=self.log_text.yview, bg=c["surface"], troughcolor=c["surface"])
        self.log_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.pack(fill=tk.BOTH, expand=True)

        # Color tags for log
        self.log_text.tag_configure("info", foreground=c["text"])
        self.log_text.tag_configure("success", foreground=c["green"])
        self.log_text.tag_configure("warn", foreground="#e8944f")
        self.log_text.tag_configure("error", foreground=c["red"])
        self.log_text.tag_configure("muted", foreground=c["muted"])

    def _build_path_row(self, parent, label: str, var: tk.StringVar, is_output=False):
        c = self.colors
        frame = ttk.Frame(parent)
        frame.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(frame, text=label, style="Sub.TLabel").pack(anchor="w")

        row = tk.Frame(frame, bg=c["bg"])
        row.pack(fill=tk.X, pady=(2, 0))

        entry = tk.Entry(
            row, textvariable=var, font=("Consolas", 10),
            bg=c["card"], fg=c["text"], insertbackground=c["text"],
            highlightthickness=1, highlightbackground=c["border"],
            highlightcolor=c["accent"], relief="flat",
        )
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=6)

        browse_btn = tk.Button(
            row, text="Browse", font=("Segoe UI", 9),
            bg=c["surface"], fg=c["muted"], activebackground=c["card"], activeforeground=c["text"],
            relief="flat", padx=12, cursor="hand2",
            command=lambda: self._browse_dir(var),
        )
        browse_btn.pack(side=tk.LEFT, padx=(8, 0))

    def _browse_dir(self, var: tk.StringVar):
        path = filedialog.askdirectory(title="Select Directory", initialdir=var.get() or None)
        if path:
            var.set(path)

    # ─── Logging ──────────────────────────────────────────────────────────

    def _log(self, message: str, tag: str = "info"):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, message + "\n", tag)
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _set_status(self, text: str):
        self.status_label.configure(text=text)

    def _set_file(self, text: str):
        display = text
        if len(display) > 100:
            display = "…" + display[-97:]
        self.file_label.configure(text=display)

    def _set_progress(self, value: float):
        self.progress_bar["value"] = value

    # ─── Comparison logic ─────────────────────────────────────────────────

    def _start_comparison(self):
        ea = self.engine_a_path.get().strip()
        eb = self.engine_b_path.get().strip()
        od = self.output_dir.get().strip()

        errors = []
        if not ea or not os.path.isdir(ea):
            errors.append("Engine A path is invalid or does not exist.")
        if not eb or not os.path.isdir(eb):
            errors.append("Engine B path is invalid or does not exist.")
        if not od:
            errors.append("Output directory is not set.")

        if errors:
            messagebox.showerror("Validation Error", "\n".join(errors))
            return

        os.makedirs(od, exist_ok=True)

        self.is_running = True
        self.should_cancel = False
        self.run_btn.configure(state=tk.DISABLED)
        self.cancel_btn.configure(state=tk.NORMAL)
        self.open_btn.configure(state=tk.DISABLED)
        self._set_progress(0)
        self._set_status("Starting…")
        self._set_file("")

        # Clear log
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)

        thread = threading.Thread(target=self._run_comparison_thread, args=(ea, eb, od), daemon=True)
        thread.start()

    def _cancel(self):
        self.should_cancel = True
        self._log("Cancellation requested…", "warn")
        self._set_status("Cancelling…")

    def _run_comparison_thread(self, engine_a: str, engine_b: str, output_dir: str):
        try:
            self._comparison_worker(engine_a, engine_b, output_dir)
        except Exception as e:
            self.root.after(0, lambda: self._log(f"ERROR: {e}", "error"))
            self.root.after(0, lambda: self._set_status(f"Failed: {e}"))
        finally:
            self.root.after(0, self._finish)

    def _finish(self):
        self.is_running = False
        self.run_btn.configure(state=tk.NORMAL)
        self.cancel_btn.configure(state=tk.DISABLED)
        if self.last_report_path and os.path.isfile(self.last_report_path):
            self.open_btn.configure(state=tk.NORMAL)

    def _comparison_worker(self, engine_a: str, engine_b: str, output_dir: str):
        ea = os.path.abspath(engine_a)
        eb = os.path.abspath(engine_b)
        threads = self.thread_count.get()
        ctx = self.context_lines.get()
        extensions = DEFAULT_EXTENSIONS.copy()

        self.root.after(0, lambda: self._log(f"Engine A : {ea}"))
        self.root.after(0, lambda: self._log(f"Engine B : {eb}"))
        self.root.after(0, lambda: self._log(f"Threads  : {threads}"))
        self.root.after(0, lambda: self._log(f"Context  : {ctx} lines"))
        self.root.after(0, lambda: self._log(""))

        # ── Scan ──
        self.root.after(0, lambda: self._set_status("Scanning Engine A…"))
        self.root.after(0, lambda: self._log("Scanning Engine A…", "muted"))
        files_a = collect_source_files(ea, extensions)
        self.root.after(0, lambda: self._log(f"  → {len(files_a):,} files found", "muted"))

        if self.should_cancel:
            self.root.after(0, lambda: self._log("Cancelled.", "warn"))
            return

        self.root.after(0, lambda: self._set_status("Scanning Engine B…"))
        self.root.after(0, lambda: self._log("Scanning Engine B…", "muted"))
        files_b = collect_source_files(eb, extensions)
        self.root.after(0, lambda: self._log(f"  → {len(files_b):,} files found", "muted"))

        all_paths = sorted(set(files_a.keys()) | set(files_b.keys()))
        total = len(all_paths)
        self.root.after(0, lambda: self._log(f"\nTotal unique paths: {total:,}\n"))

        if total == 0:
            self.root.after(0, lambda: self._log("No source files found. Check your paths.", "warn"))
            return

        # ── Compare ──
        self.root.after(0, lambda: self._set_status(f"Comparing 0/{total:,} files…"))
        diffs = []
        diffs_lock = threading.Lock()
        processed = [0]
        start_time = time.time()

        def update_ui(rel_path):
            processed[0] += 1
            p = processed[0]
            if p % 50 == 0 or p == total:  # throttle UI updates
                pct = (p / total) * 100
                elapsed = time.time() - start_time
                rate = p / elapsed if elapsed > 0 else 0
                self.root.after(0, lambda: self._set_progress(pct))
                self.root.after(0, lambda: self._set_status(
                    f"Comparing {p:,}/{total:,} files  •  {rate:.0f} files/s  •  {pct:.1f}%"
                ))
                self.root.after(0, lambda: self._set_file(rel_path))

        def worker(rel_path):
            if self.should_cancel:
                return
            path_a = files_a.get(rel_path, os.path.join(ea, rel_path))
            path_b = files_b.get(rel_path, os.path.join(eb, rel_path))
            result = compare_file(rel_path, path_a, path_b, ctx)
            update_ui(rel_path)
            if result:
                with diffs_lock:
                    diffs.append(result)

        with ThreadPoolExecutor(max_workers=threads) as pool:
            futures = [pool.submit(worker, rp) for rp in all_paths]
            for f in as_completed(futures):
                if self.should_cancel:
                    pool.shutdown(wait=False, cancel_futures=True)
                    break
                f.result()

        if self.should_cancel:
            self.root.after(0, lambda: self._log("Comparison cancelled by user.", "warn"))
            self.root.after(0, lambda: self._set_status("Cancelled"))
            return

        elapsed = time.time() - start_time
        self.root.after(0, lambda: self._set_progress(100))
        self.root.after(0, lambda: self._log(f"Comparison complete in {elapsed:.1f}s", "success"))

        # ── Sort and build report ──
        diffs.sort(key=lambda d: d.relative_path)

        summary = {
            "engine_a": ea, "engine_b": eb,
            "total_files_scanned": total,
            "files_modified": sum(1 for d in diffs if d.status == "modified"),
            "files_added": sum(1 for d in diffs if d.status == "added"),
            "files_removed": sum(1 for d in diffs if d.status == "removed"),
            "total_lines_added": sum(d.lines_added for d in diffs),
            "total_lines_removed": sum(d.lines_removed for d in diffs),
        }

        report = {"summary": summary, "differences": [asdict(d) for d in diffs]}

        # ── Save JSON ──
        self.root.after(0, lambda: self._set_status("Saving JSON report…"))
        json_path = os.path.join(output_dir, "ue_diff_report.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        json_mb = os.path.getsize(json_path) / (1024 * 1024)
        self.root.after(0, lambda: self._log(f"JSON saved: {json_path} ({json_mb:.1f} MB)", "muted"))

        # ── Build HTML ──
        self.root.after(0, lambda: self._set_status("Building HTML report…"))
        self.root.after(0, lambda: self._log("Building HTML report…", "muted"))
        html_content = build_html_report(report)
        html_path = os.path.join(output_dir, "ue_diff_report.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_content)
        html_mb = os.path.getsize(html_path) / (1024 * 1024)
        self.root.after(0, lambda: self._log(f"HTML saved: {html_path} ({html_mb:.1f} MB)", "muted"))

        self.last_report_path = html_path

        # ── Summary ──
        self.root.after(0, lambda: self._log(""))
        self.root.after(0, lambda: self._log("══════════════════════════════════════", "success"))
        self.root.after(0, lambda: self._log(f"  Files scanned : {summary['total_files_scanned']:>10,}", "success"))
        self.root.after(0, lambda: self._log(f"  Modified      : {summary['files_modified']:>10,}", "success"))
        self.root.after(0, lambda: self._log(f"  Added         : {summary['files_added']:>10,}", "success"))
        self.root.after(0, lambda: self._log(f"  Removed       : {summary['files_removed']:>10,}", "success"))
        self.root.after(0, lambda: self._log(f"  Lines added   : {summary['total_lines_added']:>10,}", "success"))
        self.root.after(0, lambda: self._log(f"  Lines removed : {summary['total_lines_removed']:>10,}", "success"))
        self.root.after(0, lambda: self._log("══════════════════════════════════════", "success"))
        self.root.after(0, lambda: self._set_status("Done — click 'Open Report' to view results"))
        self.root.after(0, lambda: self._set_file(""))

    # ─── Open report ──────────────────────────────────────────────────────

    def _open_report(self):
        if self.last_report_path and os.path.isfile(self.last_report_path):
            if platform.system() == "Windows":
                os.startfile(self.last_report_path)
            elif platform.system() == "Darwin":
                subprocess.run(["open", self.last_report_path])
            else:
                subprocess.run(["xdg-open", self.last_report_path])


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main():
    root = tk.Tk()

    # High DPI awareness on Windows
    if platform.system() == "Windows":
        try:
            from ctypes import windll
            windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    app = UESourceDiffApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()