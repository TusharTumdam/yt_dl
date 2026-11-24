#!/usr/bin/env python3
"""
Standalone Flask + yt-dlp YouTube Video Downloader Web App (No Login Required).
Includes:
  - Strict YouTube-only URL validation (SSRF protection)
  - Security headers (X-Frame-Options, X-Content-Type-Options, Referrer-Policy)
  - Concurrent download limit & automatic 1-hour file cleanup
  - Built-in Cookie Format Converter (Netscape -> JSON & Header String)
"""

import json
import os
import re
import shutil
import socket
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template_string, request, send_file
import yt_dlp

app = Flask(__name__)
DOWNLOAD_ROOT = Path("downloads")
TASKS: dict[str, dict] = {}
MAX_ACTIVE_TASKS = 3
FILE_TTL_SECONDS = 3600  # Auto-delete files after 1 hour
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _convert_netscape_cookies(raw_text: str) -> dict:
    """Convert Netscape cookies.txt content into JSON list and HTTP Header string."""
    cookies_list = []
    header_parts = []
    netscape_lines = [
        "# Netscape HTTP Cookie File",
        "# http://curl.haxx.se/rfc/cookie_spec.html",
        "",
    ]

    for line in (raw_text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            parts = line.split()
        if len(parts) >= 7:
            domain, flag, path, secure, expiration, name, value = parts[:7]
            try:
                exp_int = int(expiration)
            except ValueError:
                exp_int = 0
            cookies_list.append({
                "domain": domain,
                "httpOnly": False,
                "path": path,
                "secure": secure.upper() == "TRUE",
                "expirationDate": exp_int,
                "name": name,
                "value": value,
            })
            header_parts.append(f"{name}={value}")
            netscape_lines.append(f"{domain}\t{flag}\t{path}\t{secure}\t{exp_int}\t{name}\t{value}")

    return {
        "count": len(cookies_list),
        "netscape": "\n".join(netscape_lines),
        "json": json.dumps(cookies_list, indent=2),
        "header": "; ".join(header_parts),
    }


def _get_writable_cookie_file() -> str | None:
    cookie_candidates = [
        os.environ.get("YTDLP_COOKIEFILE", "").strip(),
        "/etc/secrets/cookies.txt",
        str(Path(__file__).resolve().parent / "cookies.txt"),
    ]
    source_cookie = next(
        (candidate for candidate in cookie_candidates if candidate and Path(candidate).is_file()),
        None,
    )
    if not source_cookie:
        return None
    tmp_cookie = Path("/tmp/cookies.txt")
    shutil.copyfile(source_cookie, tmp_cookie)
    return str(tmp_cookie)


def _build_ydl_options(clients: list[str], use_cookies: bool = True, **overrides) -> dict:
    """Shared yt-dlp settings for metadata inspection and downloads."""
    cookie_file = _get_writable_cookie_file() if use_cookies else None

    options = {
        "quiet": True,
        "no_warnings": True,
        "nocolor": True,
        "geo_bypass": True,
        "extractor_args": {
            "youtube": {
                "player_client": clients,
            }
        },
    }
    if cookie_file:
        options["cookiefile"] = cookie_file

    options.update(overrides)
    return options


def _extract_with_fallback(url: str, download: bool = False, **overrides) -> tuple[dict, yt_dlp.YoutubeDL]:
    """Try multiple client combinations so both Shorts and regular videos resolve cleanly."""
    has_cookies = bool(_get_writable_cookie_file())
    strategies: list[tuple[list[str], bool]] = []
    if has_cookies:
        strategies.append((["web", "mweb"], True))
        strategies.append((["tv_embedded", "web"], True))
    strategies.append((["tv_embedded", "mweb", "web"], False))
    strategies.append((["android", "web"], False))

    errors: list[str] = []
    for clients, use_cookies in strategies:
        try:
            ydl = yt_dlp.YoutubeDL(_build_ydl_options(clients=clients, use_cookies=use_cookies, **overrides))
            info = ydl.extract_info(url, download=download)
            return info, ydl
        except Exception as exc:
            errors.append(f"[{'+'.join(clients)}|cookie={use_cookies}]: {exc}")
    raise RuntimeError(errors[0] if errors else "Extraction failed")


ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}

QUALITY_FORMATS = {
    "best": "bv*+ba/b",
    "1080p": "bv*[height<=1080]+ba/bv*[width<=1080]+ba/b[height<=1080]/bv*+ba/b",
    "720p": "bv*[height<=720]+ba/bv*[width<=720]+ba/b[height<=720]/bv*+ba/b",
    "480p": "bv*[height<=480]+ba/bv*[width<=480]+ba/b[height<=480]/bv*+ba/b",
    "360p": "bv*[height<=360]+ba/bv*[width<=360]+ba/b[height<=360]/bv*+ba/b",
}


@app.after_request
def add_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _is_valid_youtube_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and (parsed.hostname or "").lower() in ALLOWED_HOSTS
    except Exception:
        return False


def _cleanup_old_files() -> None:
    if not DOWNLOAD_ROOT.exists():
        return
    now = time.time()
    for p in DOWNLOAD_ROOT.iterdir():
        try:
            if p.is_file() and (now - p.stat().st_mtime) > FILE_TTL_SECONDS:
                p.unlink(missing_ok=True)
        except OSError:
            pass


def _clean_ansi(text: str) -> str:
    return ANSI_RE.sub("", text or "").strip()


def _fmt_bytes(num: float | int | None) -> str:
    if not num or num <= 0:
        return "Unknown"
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024:
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024
    return f"{num:.2f} TB"


def _estimate_format_size(fmt: dict, duration: float | int | None) -> int:
    size = fmt.get("filesize") or fmt.get("filesize_approx") or 0
    if size:
        return int(size)
    tbr = fmt.get("tbr") or fmt.get("vbr") or fmt.get("abr") or 0
    if tbr and duration:
        return int((tbr * 1000 / 8) * float(duration))
    return 0


def _compute_quality_sizes(info: dict) -> dict[str, str]:
    formats = info.get("formats") or []
    duration = info.get("duration") or 0

    audio_streams = [
        f for f in formats
        if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")
    ]
    best_audio = max(audio_streams, key=lambda f: f.get("abr") or f.get("tbr") or 0, default=None)
    best_audio_size = _estimate_format_size(best_audio, duration) if best_audio else 0

    video_streams = [f for f in formats if f.get("vcodec") not in (None, "none") and f.get("height")]

    def size_for_max_height(max_h: int | None) -> str:
        candidates = [f for f in video_streams if (max_h is None or min(f.get("height") or 0, f.get("width") or 9999) <= max_h)]
        if not candidates:
            return "N/A"
        best_v = max(candidates, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
        v_size = _estimate_format_size(best_v, duration)
        if not v_size:
            return "Unknown"
        needs_audio = best_v.get("acodec") in (None, "none")
        total = v_size + (best_audio_size if needs_audio else 0)
        return f"~{_fmt_bytes(total)}"

    return {
        "best": size_for_max_height(None),
        "1080p": size_for_max_height(1080),
        "720p": size_for_max_height(720),
        "480p": size_for_max_height(480),
        "360p": size_for_max_height(360),
        "audio": f"~{_fmt_bytes(best_audio_size)}" if best_audio_size else "Unknown",
    }


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0" />
  <title>YT Studio Downloader - Free 4K YouTube Video &amp; MP3 Downloader Online</title>
  <meta name="description" content="Download YouTube videos in 4K, 1080p Full HD, MP4, and high-quality 320kbps MP3 audio with live file size preview and full playlist support.">
  <meta name="keywords" content="youtube video downloader, 4k youtube downloader, youtube to mp3, playlist downloader, yt studio downloader">
  <meta name="robots" content="index, follow">
  <!-- Google Tag Manager -->
  <script async src="https://www.googletagmanager.com/gtm.js?id=GTM-NR4DLCTN"></script>
  <script>
    window.dataLayer = window.dataLayer || [];
    function gtag(){dataLayer.push(arguments);}
    gtag('js', new Date());
  </script>
  <!-- End Google Tag Manager -->
  <script src="https://cdn.tailwindcss.com/3.4.17"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet" />
  <style>
    body {
      font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, sans-serif;
      background: radial-gradient(circle at 15% 0%, rgba(244, 63, 94, 0.16), transparent 40%),
                  radial-gradient(circle at 85% 15%, rgba(139, 92, 246, 0.14), transparent 42%),
                  #05070d;
    }
    .tabular-nums { font-variant-numeric: tabular-nums; }
    .glass {
      background: rgba(15, 20, 32, 0.72);
      backdrop-filter: blur(18px);
      -webkit-backdrop-filter: blur(18px);
      border: 1px solid rgba(255, 255, 255, 0.08);
    }
    .glow-btn {
      background: linear-gradient(135deg, #f43f5e 0%, #e11d48 50%, #be123c 100%);
      box-shadow: 0 10px 28px -6px rgba(244, 63, 94, 0.55);
    }
    .glow-btn:hover { filter: brightness(1.08); transform: translateY(-1px); }
    .glow-btn:active { transform: translateY(0); }
    @keyframes shimmer {
      0% { transform: translateX(-100%); }
      100% { transform: translateX(100%); }
    }
    .progress-shimmer::after {
      content: "";
      position: absolute;
      inset: 0;
      background: linear-gradient(90deg, transparent, rgba(255,255,255,0.35), transparent);
      animation: shimmer 1.6s infinite;
    }
  </style>
</head>
<body class="text-slate-100 min-h-screen antialiased pb-14">
  <!-- Google Tag Manager (noscript) -->
  <noscript><iframe src="https://www.googletagmanager.com/ns.html?id=GTM-NR4DLCTN"
    height="0" width="0" style="display:none;visibility:hidden"></iframe></noscript>
  <!-- End Google Tag Manager (noscript) -->
  <div id="toast-container" class="fixed bottom-4 right-4 left-4 sm:left-auto z-50 flex flex-col gap-2 sm:max-w-sm pointer-events-none"></div>

  <header class="sticky top-0 z-30 border-b border-white/10 bg-[#070a12]/80 backdrop-blur-xl">
    <div class="max-w-4xl mx-auto px-4 h-16 flex items-center justify-between gap-3">
      <div class="flex items-center gap-3 min-w-0">
        <div class="w-10 h-10 rounded-2xl bg-gradient-to-br from-rose-500 to-fuchsia-600 flex items-center justify-center shadow-lg shadow-rose-500/30 shrink-0">
          <svg class="w-5 h-5 text-white" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
        </div>
        <div class="min-w-0">
          <h1 class="text-base sm:text-lg font-extrabold tracking-tight text-white truncate">YT Studio Downloader</h1>
          <p class="text-[11px] text-slate-400 truncate">4K Video &middot; MP3 Audio &middot; Live Size</p>
        </div>
      </div>
      <span class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-emerald-500/10 border border-emerald-400/20 text-emerald-300 text-xs font-semibold shrink-0">
        <span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span>
        Online
      </span>
    </div>
  </header>

  <main class="max-w-4xl mx-auto px-4 pt-6 space-y-6">
    <section class="glass rounded-3xl p-4 sm:p-6 shadow-2xl space-y-5">
      <form id="download-form" class="space-y-5">
        <div>
          <div class="flex items-center justify-between mb-2">
            <label for="url-input" class="text-xs font-bold uppercase tracking-wider text-slate-400">Paste YouTube Link</label>
            <button type="button" id="paste-btn" class="text-xs font-semibold text-rose-400 hover:text-rose-300 transition">Paste Clipboard</button>
          </div>
          <div class="flex flex-col sm:flex-row gap-2.5">
            <div class="relative flex-1">
              <input
                id="url-input"
                type="url"
                placeholder="https://youtube.com/watch?v=... or youtu.be/..."
                class="w-full pl-4 pr-10 py-3.5 bg-black/50 border border-white/10 rounded-2xl text-sm text-white placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-rose-500/70 transition"
              />
              <button type="button" id="clear-btn" class="absolute right-3 top-1/2 -translate-y-1/2 text-slate-500 hover:text-white text-sm font-bold px-1.5">&times;</button>
            </div>
            <button type="button" id="fetch-info-btn" class="px-5 py-3.5 rounded-2xl bg-white/5 hover:bg-white/10 border border-white/10 text-slate-100 text-sm font-semibold transition shrink-0">
              Check Size
            </button>
            <button type="submit" class="glow-btn px-6 py-3.5 rounded-2xl text-white text-sm font-bold transition shrink-0">
              Download Now
            </button>
          </div>
        </div>

        <div id="preview-card" class="hidden rounded-2xl bg-black/40 border border-white/10 p-3.5 sm:p-4 flex-col sm:flex-row gap-4">
          <div class="relative w-full sm:w-52 aspect-video rounded-xl overflow-hidden bg-slate-900 shrink-0 shadow-lg">
            <img id="preview-thumb" src="" alt="Thumbnail" class="w-full h-full object-cover" />
            <span id="preview-duration" class="absolute bottom-2 right-2 px-2 py-0.5 rounded-md bg-black/80 text-[11px] font-bold text-white tabular-nums"></span>
          </div>
          <div class="min-w-0 flex-1 flex flex-col justify-center gap-1.5">
            <span class="inline-flex w-fit px-2.5 py-0.5 rounded-full text-[11px] font-bold bg-rose-500/15 text-rose-300 border border-rose-500/25">Video Ready</span>
            <h2 id="preview-title" class="text-sm sm:text-base font-bold text-white line-clamp-2"></h2>
            <p id="preview-channel" class="text-xs text-slate-400"></p>
          </div>
        </div>

        <div class="grid grid-cols-2 gap-2 p-1.5 rounded-2xl bg-black/50 border border-white/10">
          <button type="button" id="mode-video-btn" class="py-2.5 rounded-xl text-xs sm:text-sm font-bold transition">Video (MP4)</button>
          <button type="button" id="mode-audio-btn" class="py-2.5 rounded-xl text-xs sm:text-sm font-bold transition">Audio Only (MP3)</button>
        </div>

        <div id="quality-section" class="space-y-2">
          <div class="flex items-center justify-between">
            <span class="text-xs font-bold uppercase tracking-wider text-slate-400">Select Quality &amp; File Size</span>
            <span class="text-[11px] text-slate-500">Tap Check Size for exact MB</span>
          </div>
          <div id="quality-grid" class="grid grid-cols-2 sm:grid-cols-5 gap-2.5"></div>
        </div>

        <div class="flex flex-wrap items-center gap-4 pt-1">
          <label class="inline-flex items-center gap-2.5 text-xs font-medium text-slate-300 cursor-pointer select-none">
            <input id="subtitles-toggle" type="checkbox" class="w-4 h-4 accent-rose-500 rounded" />
            English Subtitles
          </label>
          <label class="inline-flex items-center gap-2.5 text-xs font-medium text-slate-300 cursor-pointer select-none">
            <input id="playlist-toggle" type="checkbox" class="w-4 h-4 accent-rose-500 rounded" />
            Full Playlist Mode
          </label>
        </div>
      </form>
    </section>

    <section class="glass rounded-3xl p-4 sm:p-6 shadow-2xl space-y-4">
      <div class="flex items-center justify-between">
        <div class="flex items-center gap-2.5">
          <h2 class="text-base font-bold text-white">Downloads</h2>
          <span id="queue-count" class="px-2.5 py-0.5 rounded-full text-xs font-bold bg-white/10 text-slate-200 tabular-nums">0</span>
        </div>
        <button type="button" id="clear-completed-btn" class="px-3 py-1.5 rounded-xl bg-white/5 hover:bg-white/10 text-slate-300 text-xs font-semibold border border-white/10 transition">
          Clear Done
        </button>
      </div>

      <div id="queue-empty" class="py-10 text-center rounded-2xl border border-dashed border-white/10 text-sm text-slate-400">
        Paste a YouTube link above, tap <strong class="text-white">Check Size</strong>, and hit <strong class="text-rose-400">Download Now</strong>.
      </div>
      <div id="queue-list" class="space-y-3 hidden"></div>
    </section>

    <section class="glass rounded-3xl p-4 sm:p-6 shadow-2xl space-y-4">
      <div class="flex items-center justify-between">
        <div>
          <h2 class="text-base font-bold text-white">Cookie Format Converter</h2>
          <p class="text-xs text-slate-400">Convert Netscape cookies.txt to JSON &amp; Header String</p>
        </div>
        <button type="button" id="convert-cookies-btn" class="px-4 py-2 rounded-xl bg-rose-500 hover:bg-rose-400 text-white text-xs font-bold transition">
          Convert
        </button>
      </div>
      <textarea id="cookie-raw-input" rows="4" placeholder="Paste Netscape cookies.txt content here..." class="w-full p-3 bg-black/50 border border-white/10 rounded-2xl text-xs font-mono text-slate-200 placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-rose-500/70"></textarea>
      <div id="cookie-outputs" class="hidden space-y-3">
        <div>
          <div class="flex items-center justify-between mb-1">
            <span class="text-xs font-bold text-slate-300">JSON Format</span>
            <button type="button" data-copy="cookie-json-out" class="copy-cookie-btn text-xs text-rose-400 font-semibold">Copy JSON</button>
          </div>
          <textarea id="cookie-json-out" rows="4" readonly class="w-full p-3 bg-black/60 border border-white/10 rounded-xl text-[11px] font-mono text-emerald-300"></textarea>
        </div>
        <div>
          <div class="flex items-center justify-between mb-1">
            <span class="text-xs font-bold text-slate-300">Header String</span>
            <button type="button" data-copy="cookie-header-out" class="copy-cookie-btn text-xs text-rose-400 font-semibold">Copy Header</button>
          </div>
          <textarea id="cookie-header-out" rows="3" readonly class="w-full p-3 bg-black/60 border border-white/10 rounded-xl text-[11px] font-mono text-sky-300"></textarea>
        </div>
      </div>
    </section>
  </main>

  <section style="max-width: 480px; margin: 24px auto; padding: 20px; background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08); border-radius: 16px; color: #cbd5e1; font-family: sans-serif; font-size: 14px; line-height: 1.6;">
    <h2 style="color: #fff; font-size: 16px; margin-bottom: 12px;">How to Download YouTube Videos &amp; MP3?</h2>
    <ol style="padding-left: 18px; margin-bottom: 16px;">
      <li>Copy the video or playlist link from YouTube and tap <b>Paste Clipboard</b>.</li>
      <li>Select <b>Video (MP4)</b> or <b>Audio Only (MP3)</b> and choose quality (up to 4K, when available).</li>
      <li>Tap <b>Check Size</b> to preview the estimated file size, then hit <b>Download Now</b>.</li>
    </ol>

    <h3 style="color: #fff; font-size: 15px; margin-bottom: 8px;">Frequently Asked Questions</h3>
    <p><b>Is 4K &amp; Playlist download free?</b><br>Yes, this tool is free to use and can offer available video qualities, English subtitles, and playlist downloads. Actual formats depend on the video and available tools.</p>
    <p style="margin-top: 8px;"><b>Does it work on Android &amp; iPhone?</b><br>Yes, it works in modern mobile browsers. Download behavior and supported formats depend on the browser and device.</p>

    <hr style="border: 0; border-top: 1px solid rgba(255,255,255,0.08); margin: 16px 0;">
    <p style="font-size: 12px; color: #94a3b8;">
      <b>Disclaimer:</b> Use this tool only for content you own or are authorized to download, including permitted royalty-free or Creative Commons content. Respect YouTube's Terms of Service and applicable copyright laws.
    </p>
  </section>


  <script>
    (function () {
      const SMARTLINK_URL = "https://asiafilm.org/4/f1ce7d30e055e15a08cdea51a529358f";
      let adShown = false;

      function maybeOpenSmartlink() {
        if (adShown || !/^https?:\/\//i.test(SMARTLINK_URL)) return;
        adShown = true;
        window.open(SMARTLINK_URL, "_blank", "noopener,noreferrer");
        setTimeout(() => { adShown = false; }, 30000);
      }

      const QUALITIES = [
        { id: 'best', label: 'Best', sub: '4K / Max' },
        { id: '1080p', label: '1080p', sub: 'Full HD' },
        { id: '720p', label: '720p', sub: 'HD' },
        { id: '480p', label: '480p', sub: 'SD' },
        { id: '360p', label: '360p', sub: 'Data Saver' },
      ];

      const state = { mode: 'video', quality: 'best', sizes: null, queue: [] };

      const urlInput = document.getElementById('url-input');
      const pasteBtn = document.getElementById('paste-btn');
      const clearBtn = document.getElementById('clear-btn');
      const fetchInfoBtn = document.getElementById('fetch-info-btn');
      const downloadForm = document.getElementById('download-form');
      const modeVideoBtn = document.getElementById('mode-video-btn');
      const modeAudioBtn = document.getElementById('mode-audio-btn');
      const qualitySection = document.getElementById('quality-section');
      const qualityGrid = document.getElementById('quality-grid');
      const subtitlesToggle = document.getElementById('subtitles-toggle');
      const playlistToggle = document.getElementById('playlist-toggle');
      const previewCard = document.getElementById('preview-card');
      const previewThumb = document.getElementById('preview-thumb');
      const previewTitle = document.getElementById('preview-title');
      const previewChannel = document.getElementById('preview-channel');
      const previewDuration = document.getElementById('preview-duration');
      const queueCountEl = document.getElementById('queue-count');
      const queueEmptyEl = document.getElementById('queue-empty');
      const queueListEl = document.getElementById('queue-list');
      const clearCompletedBtn = document.getElementById('clear-completed-btn');

      function showToast(msg, type = 'info') {
        const c = document.getElementById('toast-container');
        const t = document.createElement('div');
        const tone = type === 'error'
          ? 'bg-rose-950/95 border-rose-500/40 text-rose-200'
          : type === 'success'
          ? 'bg-emerald-950/95 border-emerald-500/40 text-emerald-200'
          : 'bg-slate-900/95 border-white/15 text-slate-100';
        t.className = `pointer-events-auto px-4 py-3 rounded-2xl border text-xs font-semibold shadow-2xl backdrop-blur-md ${tone}`;
        t.textContent = msg;
        c.appendChild(t);
        setTimeout(() => t.remove(), 3200);
      }

      function renderQualityCards() {
        qualityGrid.innerHTML = QUALITIES.map((q) => {
          const active = state.quality === q.id;
          const sizeText = state.sizes && state.sizes[q.id] ? state.sizes[q.id] : q.sub;
          return `
            <button type="button" data-q="${q.id}" class="q-card text-left p-3 rounded-2xl border transition ${
              active ? 'bg-rose-500/15 border-rose-500 shadow-lg shadow-rose-500/15' : 'bg-black/40 border-white/10 hover:border-white/25'
            }">
              <div class="flex items-center justify-between">
                <span class="text-sm font-extrabold ${active ? 'text-white' : 'text-slate-200'}">${q.label}</span>
                ${active ? '<span class="w-2 h-2 rounded-full bg-rose-500"></span>' : ''}
              </div>
              <div class="text-[11px] mt-1 font-semibold tabular-nums ${active ? 'text-rose-300' : 'text-slate-400'}">${sizeText}</div>
            </button>
          `;
        }).join('');
      }

      function renderMode() {
        const isVideo = state.mode === 'video';
        modeVideoBtn.className = isVideo
          ? 'py-2.5 rounded-xl text-xs sm:text-sm font-bold bg-gradient-to-r from-rose-500 to-rose-600 text-white shadow-md'
          : 'py-2.5 rounded-xl text-xs sm:text-sm font-bold text-slate-400 hover:text-white';
        modeAudioBtn.className = !isVideo
          ? 'py-2.5 rounded-xl text-xs sm:text-sm font-bold bg-gradient-to-r from-rose-500 to-rose-600 text-white shadow-md'
          : 'py-2.5 rounded-xl text-xs sm:text-sm font-bold text-slate-400 hover:text-white';
        const audioSize = state.sizes && state.sizes.audio ? ` (${state.sizes.audio})` : '';
        modeAudioBtn.textContent = `Audio Only (MP3)${audioSize}`;
        qualitySection.classList.toggle('hidden', !isVideo);
      }

      function renderQueue() {
        queueCountEl.textContent = String(state.queue.length);
        if (!state.queue.length) {
          queueEmptyEl.classList.remove('hidden');
          queueListEl.classList.add('hidden');
          return;
        }
        queueEmptyEl.classList.add('hidden');
        queueListEl.classList.remove('hidden');
        queueListEl.innerHTML = state.queue.map((item) => {
          const pct = Number(item.progress || 0).toFixed(1);
          const isDone = item.status === 'Completed';
          const isFail = item.status === 'Failed';
          const sizeInfo = item.downloaded_str && item.total_str ? `${item.downloaded_str} / ${item.total_str}` : '';
          const barClass = isDone
            ? 'bg-gradient-to-r from-emerald-500 to-teal-400'
            : isFail
            ? 'bg-rose-600'
            : 'bg-gradient-to-r from-rose-500 via-fuchsia-500 to-violet-500 progress-shimmer';

          return `
            <div class="p-4 rounded-2xl bg-black/45 border border-white/10 space-y-3">
              <div class="flex items-start justify-between gap-3">
                <div class="min-w-0">
                  <div class="flex items-center gap-2 mb-1">
                    <span class="px-2 py-0.5 rounded-full text-[10px] font-bold uppercase tracking-wide ${
                      isDone ? 'bg-emerald-500/15 text-emerald-300' : isFail ? 'bg-rose-500/15 text-rose-300' : 'bg-sky-500/15 text-sky-300'
                    }">${item.status}</span>
                    <span class="text-[11px] font-semibold text-slate-400">${item.qualityLabel || ''}</span>
                  </div>
                  <div class="text-sm font-bold text-white truncate">${item.title || item.url}</div>
                </div>
                <div class="text-right shrink-0">
                  <div class="text-base font-extrabold text-white tabular-nums">${pct}%</div>
                  ${sizeInfo ? `<div class="text-[11px] text-slate-400 tabular-nums">${sizeInfo}</div>` : ''}
                </div>
              </div>
              <div class="relative w-full h-3 rounded-full bg-slate-800/90 overflow-hidden">
                <div class="relative h-full rounded-full transition-all duration-300 ${barClass}" style="width:${Math.min(100, Number(item.progress || 0))}%"></div>
              </div>
              <div class="flex flex-wrap items-center justify-between gap-2 text-xs text-slate-400 tabular-nums">
                <span>
                  ${isFail ? `<span class="text-rose-300">${item.error || 'Error'}</span>` : isDone ? `Saved (${item.total_str || 'Ready'})` : `${item.speed || 'Starting...'} &middot; ETA ${item.eta || '...'}`}
                </span>
                ${isDone && item.can_download_file ? `
                  <a href="/api/file/${item.taskId}" download class="px-4 py-2 rounded-xl bg-emerald-500 hover:bg-emerald-400 text-slate-950 text-xs font-extrabold transition shadow-lg shadow-emerald-500/25">
                    Save to Device
                  </a>
                ` : ''}
              </div>
            </div>
          `;
        }).join('');
      }

      async function pollTask(taskId) {
        const item = state.queue.find((x) => x.taskId === taskId);
        if (!item) return;
        try {
          const res = await fetch('/api/status/' + taskId);
          const data = await res.json();
          Object.assign(item, data);
          renderQueue();
          if (data.status === 'Downloading') setTimeout(() => pollTask(taskId), 450);
          else if (data.status === 'Completed') showToast('Download 100% complete! Tap Save to Device.', 'success');
          else if (data.status === 'Failed') showToast(data.error || 'Download failed', 'error');
        } catch {
          item.status = 'Failed';
          item.error = 'Lost connection to server.';
          renderQueue();
        }
      }

      pasteBtn.addEventListener('click', async () => {
        try {
          const text = await navigator.clipboard.readText();
          if (text) { urlInput.value = text; showToast('Link pasted!', 'success'); }
        } catch { showToast('Tap the URL box and paste manually.', 'info'); }
      });

      clearBtn.addEventListener('click', () => { urlInput.value = ''; urlInput.focus(); });

      qualityGrid.addEventListener('click', (e) => {
        const btn = e.target.closest('.q-card');
        if (!btn) return;
        state.quality = btn.dataset.q;
        renderQualityCards();
      });

      modeVideoBtn.addEventListener('click', () => { state.mode = 'video'; renderMode(); });
      modeAudioBtn.addEventListener('click', () => { state.mode = 'audio'; renderMode(); });

      fetchInfoBtn.addEventListener('click', async () => {
        const url = urlInput.value.trim();
        if (!url) return showToast('Paste a YouTube URL first.', 'error');
        fetchInfoBtn.disabled = true;
        fetchInfoBtn.textContent = 'Checking...';
        try {
          const res = await fetch('/api/info', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url }) });
          const data = await res.json();
          if (!res.ok) throw new Error(data.error || 'Could not inspect URL');
          previewThumb.src = data.thumbnail || '';
          previewTitle.textContent = data.title || 'Unknown Title';
          previewChannel.textContent = data.uploader || '';
          const d = Number(data.duration || 0);
          previewDuration.textContent = d ? `${Math.floor(d / 60)}:${String(d % 60).padStart(2, '0')}` : '';
          state.sizes = data.quality_sizes || null;
          renderQualityCards();
          renderMode();
          previewCard.classList.remove('hidden');
          previewCard.classList.add('flex');
          showToast('File sizes updated!', 'success');
        } catch (err) {
          showToast(err.message, 'error');
        } finally {
          fetchInfoBtn.disabled = false;
          fetchInfoBtn.textContent = 'Check Size';
        }
      });

      downloadForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        maybeOpenSmartlink();
        const url = urlInput.value.trim();
        if (!url) return showToast('Paste a YouTube URL first.', 'error');
        try {
          const res = await fetch('/api/download', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              url,
              quality: state.quality,
              audio_only: state.mode === 'audio',
              playlist: playlistToggle.checked,
              subtitles: subtitlesToggle.checked,
            }),
          });
          const data = await res.json();
          if (!res.ok) throw new Error(data.error || 'Could not start download');
          state.queue.unshift({
            taskId: data.task_id,
            url,
            title: previewTitle.textContent || url,
            qualityLabel: state.mode === 'audio' ? 'MP3 Audio' : state.quality.toUpperCase(),
            status: 'Downloading',
            progress: 0,
            speed: 'Starting...',
            eta: '...',
          });
          renderQueue();
          pollTask(data.task_id);
        } catch (err) {
          showToast(err.message, 'error');
        }
      });

      clearCompletedBtn.addEventListener('click', () => {
        state.queue = state.queue.filter((x) => x.status !== 'Completed');
        renderQueue();
      });

      document.getElementById('convert-cookies-btn').addEventListener('click', async () => {
        const text = document.getElementById('cookie-raw-input').value.trim();
        if (!text) return showToast('Paste cookies.txt content first.', 'error');
        try {
          const res = await fetch('/api/cookies/convert', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text }),
          });
          const data = await res.json();
          if (!res.ok) throw new Error(data.error || 'Conversion failed');
          document.getElementById('cookie-json-out').value = data.json;
          document.getElementById('cookie-header-out').value = data.header;
          document.getElementById('cookie-outputs').classList.remove('hidden');
          showToast(`Converted ${data.count} cookies!`, 'success');
        } catch (err) {
          showToast(err.message, 'error');
        }
      });

      document.querySelectorAll('.copy-cookie-btn').forEach((btn) => {
        btn.addEventListener('click', async () => {
          const el = document.getElementById(btn.dataset.copy);
          if (!el) return;
          try {
            await navigator.clipboard.writeText(el.value);
            showToast('Copied!', 'success');
          } catch {
            el.select();
            showToast('Selected — copy manually.', 'info');
          }
        });
      });

      renderQualityCards();
      renderMode();
      renderQueue();
    })();
  </script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/api/cookies/convert", methods=["POST"])
def api_convert_cookies():
    data = request.get_json(silent=True) or {}
    raw_text = data.get("text") or ""
    if not raw_text.strip():
        return jsonify({"error": "Cookie text is empty"}), 400
    result = _convert_netscape_cookies(raw_text)
    if result["count"] == 0:
        return jsonify({"error": "No valid Netscape cookie lines found"}), 400
    return jsonify(result)


@app.route("/api/info", methods=["POST"])
def api_info():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not _is_valid_youtube_url(url):
        return jsonify({"error": "Only valid YouTube URLs are allowed"}), 400
    try:
        info, _ = _extract_with_fallback(url, download=False, noplaylist=True)
        return jsonify({
            "id": info.get("id"),
            "title": info.get("title"),
            "uploader": info.get("uploader"),
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "quality_sizes": _compute_quality_sizes(info),
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


def _run_download(task_id: str, url: str, quality: str, audio_only: bool, playlist: bool, subtitles: bool):
    DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    _cleanup_old_files()
    has_ffmpeg = shutil.which("ffmpeg") is not None

    def hook(status):
        if status.get("status") == "downloading":
            downloaded = status.get("downloaded_bytes") or 0
            total = status.get("total_bytes") or status.get("total_bytes_estimate") or 0
            if total > 0:
                pct = min(99.9, round((downloaded / total) * 100, 1))
                TASKS[task_id]["progress"] = pct
                TASKS[task_id]["downloaded_str"] = _fmt_bytes(downloaded)
                TASKS[task_id]["total_str"] = _fmt_bytes(total)
            else:
                raw_pct = _clean_ansi(status.get("_percent_str", "0%")).replace("%", "")
                try:
                    TASKS[task_id]["progress"] = round(float(raw_pct), 1)
                except ValueError:
                    pass
                TASKS[task_id]["downloaded_str"] = _fmt_bytes(downloaded) if downloaded else ""
            TASKS[task_id]["speed"] = _clean_ansi(status.get("_speed_str", "N/A"))
            TASKS[task_id]["eta"] = _clean_ansi(status.get("_eta_str", "N/A"))
        elif status.get("status") == "finished":
            total = status.get("total_bytes") or status.get("downloaded_bytes") or 0
            if total:
                TASKS[task_id]["total_str"] = _fmt_bytes(total)
                TASKS[task_id]["downloaded_str"] = _fmt_bytes(total)
            TASKS[task_id]["progress"] = 100.0
            if status.get("filename"):
                TASKS[task_id]["filepath"] = status["filename"]

    opts: dict = {
        "outtmpl": str(DOWNLOAD_ROOT / "%(title)s [%(id)s].%(ext)s"),
        "progress_hooks": [hook],
        "noplaylist": not playlist,
    }
    if audio_only:
        opts["format"] = "bestaudio/best"
        if has_ffmpeg:
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
    else:
        if has_ffmpeg:
            opts["format"] = QUALITY_FORMATS.get(quality, QUALITY_FORMATS["best"])
            opts["merge_output_format"] = "mp4"
        else:
            opts["format"] = "b[ext=mp4]/b/best"

    if subtitles:
        opts["writesubtitles"] = True
        opts["writeautomaticsub"] = True
        opts["subtitleslangs"] = ["en"]

    try:
        info, ydl = _extract_with_fallback(url, download=True, **opts)
        if isinstance(info, dict) and not playlist:
            prepared = Path(ydl.prepare_filename(info))
            if audio_only and has_ffmpeg:
                mp3_path = prepared.with_suffix(".mp3")
                if mp3_path.exists():
                    prepared = mp3_path
            elif has_ffmpeg:
                mp4_path = prepared.with_suffix(".mp4")
                if mp4_path.exists():
                    prepared = mp4_path
            if prepared.exists():
                TASKS[task_id]["filepath"] = str(prepared.resolve())
                TASKS[task_id]["total_str"] = _fmt_bytes(prepared.stat().st_size)
            if info.get("title"):
                TASKS[task_id]["title"] = info["title"]
        TASKS[task_id]["status"] = "Completed"
        TASKS[task_id]["progress"] = 100.0
    except Exception as exc:
        TASKS[task_id]["status"] = "Failed"
        TASKS[task_id]["error"] = str(exc)


@app.route("/api/download", methods=["POST"])
def api_download():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not _is_valid_youtube_url(url):
        return jsonify({"error": "Only valid YouTube URLs are allowed"}), 400

    active_count = sum(1 for t in TASKS.values() if t.get("status") == "Downloading")
    if active_count >= MAX_ACTIVE_TASKS:
        return jsonify({"error": "Too many active downloads. Wait for one to finish."}), 429

    task_id = uuid.uuid4().hex[:10]
    TASKS[task_id] = {
        "status": "Downloading",
        "progress": 0.0,
        "speed": "0 MB/s",
        "eta": "...",
        "downloaded_str": "",
        "total_str": "",
    }
    t = threading.Thread(
        target=_run_download,
        args=(
            task_id,
            url,
            data.get("quality", "best"),
            bool(data.get("audio_only", False)),
            bool(data.get("playlist", False)),
            bool(data.get("subtitles", False)),
        ),
        daemon=True,
    )
    t.start()
    return jsonify({"task_id": task_id})


@app.route("/api/status/<task_id>")
def api_status(task_id: str):
    task = TASKS.get(task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404
    return jsonify({
        "status": task.get("status"),
        "title": task.get("title"),
        "progress": task.get("progress", 0),
        "speed": task.get("speed", ""),
        "eta": task.get("eta", ""),
        "downloaded_str": task.get("downloaded_str", ""),
        "total_str": task.get("total_str", ""),
        "error": task.get("error"),
        "can_download_file": bool(task.get("filepath") and Path(task["filepath"]).exists()),
    })


@app.route("/api/file/<task_id>")
def api_file(task_id: str):
    task = TASKS.get(task_id)
    if not task or not task.get("filepath"):
        return jsonify({"error": "File not available"}), 404
    file_path = Path(task["filepath"])
    if not file_path.exists():
        return jsonify({"error": "File not found on disk"}), 404
    return send_file(file_path, as_attachment=True, download_name=file_path.name)


def _get_lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def _pick_port(preferred: int = 5000) -> int:
    for port in (preferred, 5050, 8000, 8080):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return preferred


if __name__ == "__main__":
    port = _pick_port(5000)
    lan_ip = _get_lan_ip()
    print(f"\n  Local URL (this device):  http://127.0.0.1:{port}")
    print(f"  Mobile URL (same Wi-Fi):  http://{lan_ip}:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
