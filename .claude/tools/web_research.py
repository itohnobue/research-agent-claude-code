#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["scrapling[fetchers]", "ddgs", "trafilatura", "rank-bm25", "httpx"]
# ///
# -*- coding: utf-8 -*-
"""
Web Research Tool - Autonomous Search + Fetch + Report

Unified tool combining search and fetch into a single optimized workflow:
1. Search via DuckDuckGo + Brave (fallback) for maximum coverage
2. Filter and deduplicate URLs during search (early filtering)
3. Fetch content in parallel via Scrapling (TLS fingerprinting, anti-bot bypass)
4. Scrapling text extraction fallback for "Too short" pages
5. Output combined results (streaming or batched)

Usage:
    python web_research.py "search query"
    python web_research.py "Mac Studio M3 Ultra LLM" --fetch 50
    python web_research.py "AI trends 2025" -o markdown
    python web_research.py "query" --stream  # Stream output as results arrive
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import types
import urllib.parse
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass
from html import unescape
from io import StringIO
from pathlib import Path
from typing import (
    AsyncIterator,
    Iterator,
    List,
    Optional,
    Set,
    Tuple,
)

# =============================================================================
# LOGGING CONFIGURATION
# =============================================================================

# Suppress ALL library logging before any imports touch the root logger.
# Scrapling uses logging.info() (root logger) and named loggers — silence both.
logging.basicConfig(level=logging.CRITICAL, stream=sys.stderr)
logging.getLogger().setLevel(logging.CRITICAL)
for _lib in ("scrapling", "curl_cffi", "httpx", "hpack", "httpcore", "asyncio"):
    logging.getLogger(_lib).setLevel(logging.CRITICAL)

# Our own logger — restored to WARNING after imports
logger = logging.getLogger("web_research")
logger.setLevel(logging.WARNING)
_handler = logging.StreamHandler(sys.stderr)
_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
logger.addHandler(_handler)
logger.propagate = False

# =============================================================================
# CONSTANTS
# =============================================================================

BLOCKED_DOMAINS: Tuple[str, ...] = (
    "facebook.com", "tiktok.com", "instagram.com", "linkedin.com", "youtube.com",
    "msn.com",  # redirects to stub/privacy pages, no usable content
    # Consistently HTTP 403 — wasted fetch slots
    "forbes.com", "edmunds.com", "cars.com", "nytimes.com",
    "percona.com", "mctlaw.com", "zenodo.org", "amjmed.com", "dl.acm.org",
    "nejm.org", "cell.com", "sciencedirect.com", "onlinelibrary.wiley.com",
    # twitter.com, x.com: unblocked — FxTwitter API for tweet text
    # reddit.com: unblocked — DDG snippets for bonus search, scraping fallback for DDG-found URLs
    # medium.com: unblocked — full articles extract cleanly
)

SKIP_URL_PATTERNS: Tuple[str, ...] = (
    r"\.jpg$", r"\.png$", r"\.gif$", r"\.svg$", r"\.webp$",
    r"/login", r"/signin", r"/signup", r"/cart", r"/checkout",
    r"/tag/", r"/tags/", r"/category/", r"/categories/",
    r"/archive/", r"/page/\d+",
    r"bing\.com/aclick",  # Bing ad redirects — marketing/booking noise
    r"www\.yahoo\.com/",  # EU privacy consent walls, no usable content
    r"finance\.yahoo\.com/",  # EU privacy consent walls
    r"www\.aol\.com/",  # cookie/privacy consent walls
    # tech.yahoo.com: unblocked — returns actual article content
    # .pdf: now handled via pdftotext extraction
)


# CAPTCHA/blocked page detection markers
BLOCKED_CONTENT_MARKERS: Tuple[str, ...] = (
    "verify you are human",
    "access to this page has been denied",
    "please complete the security check",
    "cloudflare ray id:",
    "checking your browser",
    "enable javascript and cookies",
    "unusual traffic from your computer",
    "are you a robot",
    "captcha",
    "perimeterx",
    "distil networks",
    "blocked by",
)

# Brave Search API key: set BRAVE_API_KEY env var, or place key in ~/.config/brave/api_key
BRAVE_API_KEY_PATH = Path(os.environ.get("BRAVE_API_KEY_FILE", str(Path.home() / ".config" / "brave" / "api_key")))

# =============================================================================
# COMPILED REGEX PATTERNS
# =============================================================================

# URL filtering - single combined pattern for performance
_BLOCKED_URL_PATTERN = re.compile(
    r'(?:' + '|'.join(re.escape(d) for d in BLOCKED_DOMAINS) + r')|(?:' + '|'.join(SKIP_URL_PATTERNS) + r')',
    re.IGNORECASE
)

# HTML extraction - simple fast patterns (optimized for speed)
RE_STRIP_TAGS = re.compile(
    r"<(script|style|nav|footer|header|aside|noscript|iframe|svg|form)[^>]*>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)
RE_COMMENTS = re.compile(r"<!--.*?-->", re.DOTALL)
RE_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
RE_JSON_LD = re.compile(
    r"<script[^>]*type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.DOTALL | re.IGNORECASE,
)
RE_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
RE_BLOCK_END = re.compile(r"</(p|div|h[1-6]|li|tr|article|section)>", re.IGNORECASE)
RE_LI = re.compile(r"<li[^>]*>", re.IGNORECASE)
RE_ALL_TAGS = re.compile(r"<[^>]+>")
RE_SPACES = re.compile(r"[ \t]+")
RE_LEADING_SPACE = re.compile(r"\n[ \t]+")
RE_MULTI_NEWLINE = re.compile(r"\n{3,}")
RE_WHITESPACE = re.compile(r"\s+")
# Sentence boundary: period/exclamation/question + space + uppercase letter
# Handles common abbreviations by requiring 2+ chars before the period
RE_SENT_SPLIT = re.compile(r'(?<=[.!?])\s+(?=[A-Z])')
# Wikipedia cleanup patterns
RE_WIKI_CITE = re.compile(r'\[\[?\d+\]?\](?:\(#cite_note[^)]*\))?')  # [[20]](#cite_note-22), [21]
RE_WIKI_CITE_NAMED = re.compile(r'\[\[?[a-z]\]?\](?:\(#cite_note[^)]*\))?')  # [[b]](#cite_note-b-13)
RE_WIKI_LINK = re.compile(r'\[([^\]]+)\]\(/wiki/[^)]+\)')  # [Battle](/wiki/Battle) -> Battle
RE_WIKI_REFLIST = re.compile(r'\n(?:\*\s*)?(?:\[?\d+\]?\s*)?(?:\^.*)?(?:ISBN|ISSN|doi:|JSTOR|S2CID|OCLC).*', re.IGNORECASE)
# Forum noise: lines that are pure metadata (likes, timestamps, user roles)
RE_FORUM_NOISE = re.compile(
    r'^\s*(?:'
    r'\d+\s+Likes?\b'               # "1 Like", "2 Likes"
    r'|Like\s*$'                     # standalone "Like"
    r'|\d+\s*(?:yr|mo|hr|min|sec)s?\s+ago\b'  # "2 yr ago"
    r'|(?:Community\s+Expert|Author|Moderator|Admin)\s*$'  # user roles
    r'|(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}\d*\s*(?:yr|mo)?\s*$'  # "March 19, 20232 yr"
    r'|\d{1,2}\s+(?:hours?|minutes?|days?|weeks?|months?|years?)\s+ago'  # "8 hours ago"
    r'|said:\s*$'                    # "X said:"
    r'|Quote\s*$'                    # standalone "Quote"
    r'|Share\s*$'                    # standalone "Share"
    r'|Reply\s*$'                    # standalone "Reply"
    r'|Report\s*$'                   # standalone "Report"
    r')',
    re.MULTILINE | re.IGNORECASE,
)

# Domains where curl_cffi c-ares DNS resolver fails (Windows).
# Populated at runtime; domains in this set skip straight to httpx fallback.
_CURL_DNS_FAIL_DOMAINS: set = set()

# External tool availability (checked once at import)
PDFTOTEXT_PATH = shutil.which("pdftotext")

# =============================================================================
# REQUIRED DEPENDENCIES (managed by uv)
# =============================================================================

from scrapling.fetchers import AsyncFetcher
from ddgs import DDGS

# Scrapling adds its own StreamHandler at INFO — remove it post-import
_scrapling_logger = logging.getLogger("scrapling")
_scrapling_logger.handlers.clear()
_scrapling_logger.setLevel(logging.CRITICAL)

# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class ResearchConfig:
    """Configuration for research workflow."""
    query: str
    fetch_count: int = 0
    max_content_length: int = 8000
    timeout: int = 20
    quiet: bool = False
    min_content_length: int = 600
    max_concurrent: int = 50  # Match default search count
    search_results: int = 50
    stream: bool = False
    scientific: bool = False
    medical: bool = False
    tech: bool = False


@dataclass
class FetchResult:
    """Single fetch result."""
    url: str
    success: bool
    content: str = ""
    title: str = ""
    error: Optional[str] = None
    source: str = "scrapling"


@dataclass
class ResearchStats:
    """Statistics for research run."""
    query: str = ""
    urls_searched: int = 0
    urls_fetched: int = 0
    urls_filtered: int = 0
    content_chars: int = 0
    bonus_sources: dict = None  # {source_name: count}


def _quality_fields(results: Optional[List[FetchResult]]) -> dict:
    """Extract quality-related fields from fetch results."""
    if not results:
        return {"short_pages": 0, "domains": []}
    return {
        "short_pages": sum(1 for r in results if r.success and len(r.content) < 200),
        "domains": list({urllib.parse.urlparse(r.url).netloc for r in results if r.success}),
    }


def log_usage(event: dict) -> None:
    """Append one JSONL event to ~/.web-research/usage.jsonl."""
    try:
        log_dir = os.path.join(os.path.expanduser("~"), ".web-research")
        os.makedirs(log_dir, exist_ok=True)
        event["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with open(os.path.join(log_dir, "usage.jsonl"), "a") as f:
            f.write(json.dumps(event) + "\n")
    except Exception:
        pass


def print_usage_stats(quality: bool = False) -> None:
    """Print usage statistics from ~/.web-research/usage.jsonl."""
    log_path = os.path.join(os.path.expanduser("~"), ".web-research", "usage.jsonl")
    if not os.path.exists(log_path):
        print("No usage data yet", file=sys.stderr)
        sys.exit(0)

    from collections import Counter
    from datetime import datetime, timedelta

    cutoff = datetime.now().astimezone() - timedelta(days=30)
    events = []
    errors: Counter = Counter()
    modes: Counter = Counter()
    days: Counter = Counter()
    domain_ok: Counter = Counter()    # domain → successful fetches

    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                ts = datetime.fromisoformat(ev["ts"])
                if ts < cutoff:
                    continue
            except (KeyError, ValueError):
                continue
            events.append(ev)
            modes[ev.get("mode", "unknown")] += 1
            day = ev["ts"][:10]
            days[day] += 1
            if not ev.get("ok") and ev.get("error"):
                errors[ev["error"]] += 1
            for d in ev.get("domains", []):
                domain_ok[d] += 1

    if not events:
        print("No usage data in last 30 days")
        sys.exit(0)

    total = len(events)
    ok_count = sum(1 for e in events if e.get("ok"))
    avg_ms = sum(e.get("ms", 0) for e in events) / total
    timeouts = sum(1 for e in events if e.get("timeout"))
    avg_fetched = sum(e.get("urls_fetched", 0) for e in events) / total
    avg_chars = sum(e.get("content_chars", 0) for e in events) / total
    total_short = sum(e.get("short_pages", 0) for e in events)
    total_fetched = sum(e.get("urls_fetched", 0) for e in events)
    print(f"Web Research Usage (last 30 days)")
    print(f"{'='*40}")
    print(f"Total searches:    {total}")
    print(f"Success rate:      {ok_count}/{total} ({100*ok_count/total:.0f}%)")
    print(f"Avg latency:       {avg_ms/1000:.1f}s")
    print(f"Timeouts:          {timeouts}")
    print()
    print(f"Mode breakdown:")
    for mode, count in modes.most_common():
        print(f"  {mode:15s} {count:4d} ({100*count/total:.0f}%)")
    print()
    print(f"Fetch efficiency:")
    print(f"  Avg URLs fetched:  {avg_fetched:.1f}")
    print(f"  Avg content chars: {avg_chars:.0f}")

    if quality:
        print()
        print(f"Output quality:")
        print(f"  Short pages (<200 chars): {total_short}/{total_fetched}" +
              (f" ({100*total_short/total_fetched:.0f}%)" if total_fetched else ""))
        print()
        print(f"Top domains (by fetch count):")
        for domain, count in domain_ok.most_common(10):
            print(f"  {count:4d}x {domain}")

    if errors:
        print()
        print(f"Top errors:")
        for err, count in errors.most_common(5):
            print(f"  {count:4d}x {err[:80]}")

    if days:
        print()
        print(f"Busiest days:")
        for day, count in days.most_common(5):
            print(f"  {day}  {count} searches")


# =============================================================================
# PROGRESS REPORTER (Unified)
# =============================================================================

class ProgressReporter:
    """Progress reporting with timing and per-URL diagnostics."""

    def __init__(self, quiet: bool = False, verbose: bool = False):
        self.quiet = quiet
        self.verbose = verbose
        self._last_line_len = 0
        self._phase_start: float = 0
        self._total_start: float = time.monotonic()
        self._ok_count = 0
        self._failures: List[Tuple[str, str, float]] = []  # (url, error, elapsed)

    def message(self, msg: str) -> None:
        if not self.quiet:
            print(msg, file=sys.stderr)

    def phase_start(self, name: str) -> None:
        self._phase_start = time.monotonic()

    def phase_end(self, name: str) -> None:
        elapsed = time.monotonic() - self._phase_start
        if not self.quiet:
            print(f"  [{name}] {elapsed:.1f}s", file=sys.stderr)

    def url_result(self, url: str, success: bool, elapsed: float, error: str = "") -> None:
        if success:
            self._ok_count += 1
            if self.verbose and not self.quiet:
                domain = urllib.parse.urlparse(url).netloc
                print(f"    OK  {elapsed:4.1f}s  {domain}", file=sys.stderr)
        else:
            self._failures.append((url, error, elapsed))
            if self.verbose and not self.quiet:
                domain = urllib.parse.urlparse(url).netloc
                print(f"    --  {elapsed:4.1f}s  {domain} ({error})", file=sys.stderr)

    def update(self, phase: str, current: int, total: int) -> None:
        if self.quiet or self.verbose:
            return
        elapsed = time.monotonic() - self._phase_start
        line = f"\r    {phase}: {current}/{total} ({self._ok_count} ok, {elapsed:.0f}s)"
        padding = max(0, self._last_line_len - len(line))
        print(f"{line}{' ' * padding}", end="", file=sys.stderr)
        self._last_line_len = len(line)

    def newline(self) -> None:
        if not self.quiet and not self.verbose:
            print(file=sys.stderr)
            self._last_line_len = 0

    def summary(self, fetched_ok: int, total: int, chars: int) -> None:
        if self.quiet:
            return
        total_elapsed = time.monotonic() - self._total_start
        rate = (fetched_ok / total * 100) if total > 0 else 0
        rate_indicator = ""
        if rate < 50:
            rate_indicator = " !! LOW"
        elif rate < 70:
            rate_indicator = " !"
        print(f"  Done: {fetched_ok}/{total} ok ({rate:.0f}%{rate_indicator}) -- {chars:,} chars in {total_elapsed:.1f}s", file=sys.stderr)

        if self._failures:
            by_error: dict[str, int] = {}
            slow: List[Tuple[str, float]] = []
            for url, error, elapsed in self._failures:
                by_error[error] = by_error.get(error, 0) + 1
                if elapsed >= 5.0:
                    slow.append((url, elapsed))
            parts = [f"{count} {err}" for err, count in sorted(by_error.items(), key=lambda x: -x[1])]
            print(f"  Skipped: {', '.join(parts)}", file=sys.stderr)
            if slow:
                print(f"  Slow (>5s):", file=sys.stderr)
                for url, elapsed in sorted(slow, key=lambda x: -x[1])[:5]:
                    domain = urllib.parse.urlparse(url).netloc
                    print(f"    {elapsed:4.1f}s  {domain}", file=sys.stderr)


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def clean_text(text: str) -> str:
    """Clean HTML entities and normalize whitespace."""
    if not text:
        return ""
    text = unescape(text)
    text = RE_ALL_TAGS.sub("", text)
    text = RE_WHITESPACE.sub(" ", text)
    return text.strip()


def is_blocked_url(url: str) -> bool:
    """Check if URL should be blocked (optimized single-regex check)."""
    return bool(_BLOCKED_URL_PATTERN.search(url))


def is_valid_url(url: str) -> bool:
    """Validate URL format."""
    try:
        result = urllib.parse.urlparse(url)
        return all([result.scheme in ('http', 'https'), result.netloc])
    except Exception:
        return False


def is_blocked_content(content: str) -> bool:
    """Check if content is a CAPTCHA/blocked page (returns True if blocked)."""
    if not content or len(content) < 50:
        return False
    content_lower = content[:2000].lower()  # Only check first 2KB for speed
    return any(marker in content_lower for marker in BLOCKED_CONTENT_MARKERS)


def _strip_wiki_tables(html: str) -> str:
    """Remove Wikipedia infobox/navbox tables (may contain nested tables)."""
    for css_class in ("infobox", "navbox"):
        pattern = re.compile(
            rf'<table\b[^>]*class="[^"]*{css_class}[^"]*"[^>]*>',
            re.IGNORECASE,
        )
        while True:
            m = pattern.search(html)
            if not m:
                break
            # Find matching </table> accounting for nesting
            depth = 1
            pos = m.end()
            while depth > 0 and pos < len(html):
                next_open = html.find("<table", pos)
                next_close = html.find("</table>", pos)
                if next_close == -1:
                    break
                if next_open != -1 and next_open < next_close:
                    depth += 1
                    pos = next_open + 6
                else:
                    depth -= 1
                    pos = next_close + 8
            html = html[:m.start()] + html[pos:]
    return html

def _extract_with_trafilatura(html: str) -> str:
    """Extract article text using trafilatura (content-area detection + boilerplate removal)."""
    # Strip Wikipedia infobox/navbox tables before extraction (they render as messy pipe-tables)
    html = _strip_wiki_tables(html)
    import trafilatura
    text = trafilatura.extract(
        html,
        include_links=True,
        include_formatting=True,
        include_tables=True,
        include_comments=False,
        output_format="txt",
    )
    return text or ""


def _extract_with_regex(html: str) -> str:
    """Fallback: extract text from HTML using regex (for when trafilatura returns nothing)."""
    # Strip boilerplate tags
    html = RE_STRIP_TAGS.sub("", html)
    html = RE_COMMENTS.sub("", html)

    html = RE_BR.sub("\n", html)
    html = RE_BLOCK_END.sub("\n\n", html)
    html = RE_LI.sub("\u2022 ", html)

    text = RE_ALL_TAGS.sub(" ", html)
    text = unescape(text)
    text = RE_SPACES.sub(" ", text)
    text = RE_LEADING_SPACE.sub("\n", text)
    return RE_MULTI_NEWLINE.sub("\n\n", text)


def extract_text(html: str) -> str:
    """Extract readable text from HTML. Trafilatura primary, regex fallback."""
    text = _extract_with_trafilatura(html)

    if not text or len(text) < 100:
        text = _extract_with_regex(html)

    # Extract title for prepending
    title_match = RE_TITLE.search(html)
    if title_match:
        raw_title = unescape(title_match.group(1).strip())
        title = re.sub(r'\s*[\|\-\u2013\u2014]\s*[^|\-\u2013\u2014]{3,50}$', '', raw_title)
    else:
        title = ""

    text = text.strip()
    # Strip forum noise lines (likes, timestamps, user roles)
    text = RE_FORUM_NOISE.sub("", text)
    # Clean Wikipedia artifacts: citation refs, internal links, reference lists
    text = RE_WIKI_CITE.sub("", text)
    text = RE_WIKI_CITE_NAMED.sub("", text)
    text = RE_WIKI_LINK.sub(r"\1", text)
    text = RE_WIKI_REFLIST.sub("", text)
    text = RE_MULTI_NEWLINE.sub("\n\n", text)
    # Prepend title if not already present
    if title and not text.startswith(f"# {title}"):
        text = f"# {title}\n\n{text}"
    return text


def extract_title_from_content(content: str) -> str:
    """Extract title from markdown-formatted content."""
    if content.startswith("# "):
        newline = content.find("\n")
        if newline > 0:
            return content[2:newline]
    return ""


MAX_CONTENT_BYTES = 2_000_000  # 2MB max content size

def extract_jsonld_metadata(html: str) -> str:
    """Extract only high-value metadata from JSON-LD that page text doesn't provide:
    dateModified (staleness signal) and FAQPage Q&A pairs (hard to parse from DOM)."""
    blocks = RE_JSON_LD.findall(html)
    if not blocks:
        return ""

    for raw in blocks:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue

        if isinstance(data, list):
            data = data[0] if data else {}
        if not isinstance(data, dict):
            continue

        ld_type = data.get("@type", "")
        if isinstance(ld_type, list):
            ld_type = ld_type[0] if ld_type else ""

        parts = []

        # FAQPage: Q&A pairs are genuinely hard to extract from rendered HTML
        if ld_type == "FAQPage":
            entities = data.get("mainEntity", [])
            # Flatten nested lists (e.g. AWS uses [[{...}, {...}]])
            if entities and isinstance(entities[0], list):
                entities = [e for sub in entities for e in sub]
            for entity in entities[:5]:
                if not isinstance(entity, dict):
                    continue
                q = entity.get("name", "")
                a_obj = entity.get("acceptedAnswer", {})
                a = a_obj.get("text", "") if isinstance(a_obj, dict) else ""
                if q and a:
                    parts.append(f"Q: {q}")
                    parts.append(f"A: {a[:300]}")

        # dateModified: staleness signal not always visible in page text
        date_mod = data.get("dateModified", "")
        if date_mod:
            if "T" in str(date_mod):
                date_mod = str(date_mod).split("T")[0]
            parts.append(f"updated: {date_mod}")

        if parts:
            return "[meta] " + " | ".join(parts) + "\n\n" if len(parts) == 1 else "[meta]\n" + "\n".join(parts) + "\n[/meta]\n\n"

    return ""


# =============================================================================
# URL FETCHER (Scrapling-based)
# =============================================================================

def _split_sentences(text: str) -> List[str]:
    """Split text into sentences. Two-level: split on newlines, then on sentence boundaries."""
    sentences: List[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        # Short lines (headings, list items) stay as-is
        if len(line) < 150:
            sentences.append(line)
        else:
            # Split long lines on sentence boundaries
            parts = RE_SENT_SPLIT.split(line)
            sentences.extend(p.strip() for p in parts if p.strip())
    return sentences


def _compress_with_bm25(content: str, query: str, max_length: int) -> str:
    """Query-focused extraction: keep sentences most relevant to query via BM25."""
    from rank_bm25 import BM25Okapi

    # Split into paragraphs first to identify headers
    blocks = content.split("\n\n")
    header_parts: List[str] = []
    body_text_parts: List[str] = []
    for block in blocks:
        stripped = block.strip()
        if not stripped:
            continue
        if not body_text_parts and (stripped.startswith("# ") or stripped.startswith("[meta")):
            header_parts.append(stripped)
        else:
            body_text_parts.append(stripped)

    if not body_text_parts:
        return content[:max_length]

    # Split body into sentences for granular selection
    body_text = "\n\n".join(body_text_parts)
    sentences = _split_sentences(body_text)

    if not sentences:
        return content[:max_length]

    # BM25 rank sentences by query relevance
    tokenized = [s.lower().split() for s in sentences]
    bm25 = BM25Okapi(tokenized)
    bm25_scores = bm25.get_scores(query.lower().split())

    # Centrality scoring: sentences similar to many others are "hub" sentences
    # (captures important context that BM25 misses when it lacks query terms)
    # Cap at 200 sentences to keep O(n²) manageable (~40K comparisons max)
    word_sets = [set(t) for t in tokenized]
    n = len(sentences)
    centrality = [0.0] * n
    n_cap = min(n, 200)
    if n_cap > 1:
        for i in range(n_cap):
            if not word_sets[i]:
                continue
            total_sim = 0.0
            for j in range(n_cap):
                if i == j or not word_sets[j]:
                    continue
                # Jaccard similarity
                intersection = len(word_sets[i] & word_sets[j])
                union = len(word_sets[i] | word_sets[j])
                if union:
                    total_sim += intersection / union
            centrality[i] = total_sim / (n_cap - 1)

    # Blend: 70% BM25 relevance + 30% centrality importance
    max_bm25 = max(bm25_scores) if max(bm25_scores) > 0 else 1.0
    max_cent = max(centrality) if max(centrality) > 0 else 1.0
    scores = [
        0.7 * (b / max_bm25) + 0.3 * (c / max_cent)
        for b, c in zip(bm25_scores, centrality)
    ]

    # Select top-scoring sentences within budget
    ranked = sorted(zip(scores, range(len(sentences)), sentences), reverse=True)
    budget = max_length - sum(len(h) + 2 for h in header_parts)
    selected: List[Tuple[int, str]] = []  # (original_index, text)
    chars = 0
    # Minimum score threshold: at least 10% of max blended score
    min_score = 0.1
    for score, idx, sent in ranked:
        if score < min_score or chars >= budget:
            break
        selected.append((idx, sent))
        chars += len(sent) + 1

    if not selected:
        return content[:max_length]

    # Restore original order
    selected.sort(key=lambda x: x[0])
    parts = header_parts + [sent for _, sent in selected]
    result = "\n".join(parts)
    if chars >= budget:
        result += "\n[Compressed...]"
    return result


def _create_fetch_result(
    url: str,
    content: Optional[str],
    min_length: int,
    max_length: int,
    query: str = "",
) -> FetchResult:
    """Create FetchResult from content, applying length checks and truncation."""
    if content and len(content) >= min_length:
        if len(content) > max_length:
            if query:
                content = _compress_with_bm25(content, query, max_length)
            else:
                content = content[:max_length] + "\n\n[Truncated...]"
        return FetchResult(
            url=url,
            success=True,
            content=content,
            title=extract_title_from_content(content),
        )
    return FetchResult(url=url, success=False, error="Too short")


def _extract_with_scrapling_fallback(page, min_length: int) -> str:
    """Try Scrapling's get_all_text() when w3m/regex extraction is too short.

    This handles JS-heavy pages where our regex extraction strips too much
    but Scrapling's DOM parser preserves the text content.
    """
    try:
        text = page.get_all_text(separator='\n', strip=True)
        if text and len(text) >= min_length:
            # Add title if available
            title = ""
            title_el = page.css('title')
            if title_el:
                raw_title = title_el[0].text.strip() if hasattr(title_el[0], 'text') else ""
                if raw_title:
                    title = re.sub(r'\s*[\|\-\u2013\u2014]\s*[^|\-\u2013\u2014]{3,50}$', '', raw_title)
            if title:
                return f"# {title}\n\n{text}"
            return text
    except Exception:
        pass
    return ""


def _is_pdf(raw: str, url: str) -> bool:
    """Detect PDF content by magic bytes (not URL — .pdf URLs may return HTML 404)."""
    return "%PDF" in raw[:50]


def _extract_pdf(raw_bytes: bytes) -> str:
    """Extract text from PDF using pdftotext (poppler). Writes to temp file since pdftotext needs seekable input."""
    if not PDFTOTEXT_PATH:
        return ""
    import tempfile
    tmp_path = None
    try:
        # Use delete=False and manual cleanup: on Windows, NamedTemporaryFile(delete=True)
        # keeps the file locked, preventing pdftotext from reading it
        f = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        tmp_path = f.name
        f.write(raw_bytes)
        f.close()
        result = subprocess.run(
            [PDFTOTEXT_PATH, "-layout", tmp_path, "-"],
            capture_output=True,
            timeout=30,
        )
        if result.returncode == 0:
            return result.stdout.decode("utf-8", errors="replace").strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    return ""


def _extract_content(raw_html: str) -> Tuple[str, str]:
    """CPU-bound: extract text + JSON-LD from HTML. Runs in process pool."""
    try:
        structured = extract_jsonld_metadata(raw_html)
    except Exception:
        structured = ""
    content = extract_text(raw_html)
    return content, structured


# Shared process pool for CPU-bound text extraction (avoids blocking event loop)
_extract_pool: Optional[ProcessPoolExecutor] = None


def _get_extract_pool() -> ProcessPoolExecutor:
    global _extract_pool
    if _extract_pool is None:
        _extract_pool = ProcessPoolExecutor(max_workers=4)
    return _extract_pool


def _shutdown_extract_pool() -> None:
    """Shut down process pool to prevent hang on exit."""
    global _extract_pool
    if _extract_pool is not None:
        _extract_pool.shutdown(wait=False, cancel_futures=True)
        _extract_pool = None


import atexit
atexit.register(_shutdown_extract_pool)


RE_WIKIPEDIA_URL = re.compile(r'https?://(\w+)\.wikipedia\.org/wiki/(.+?)(?:#.*)?$')
RE_GITHUB_REPO_URL = re.compile(r'https?://github\.com/([^/]+)/([^/]+?)(?:/?|/tree/[^/]+/?)?$')
RE_ARXIV_URL = re.compile(r'https?://arxiv\.org/(?:abs|pdf)/(\d+\.\d+)')
RE_TWITTER_URL = re.compile(r'https?://(?:twitter\.com|x\.com)/([^/]+)/status/(\d+)')
RE_REDDIT_URL = re.compile(r'https?://(?:www\.)?reddit\.com(/r/[^?#]+)')
RE_SEMANTIC_SCHOLAR_URL = re.compile(r'https?://(?:www\.)?semanticscholar\.org/paper/(?:.+/)?([a-f0-9]{40})')

def _fetch_wikipedia_api(lang: str, title: str, max_length: int) -> Optional[str]:
    """Fetch clean text from Wikipedia API (no scraping needed)."""
    import urllib.request
    api_url = f"https://{lang}.wikipedia.org/w/api.php?action=query&titles={urllib.parse.quote(title)}&prop=extracts&explaintext=true&format=json"
    try:
        req = urllib.request.Request(api_url, headers={"User-Agent": "web-research-tool/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        pages = data.get("query", {}).get("pages", {})
        for page_data in pages.values():
            text = page_data.get("extract", "")
            if text:
                page_title = page_data.get("title", title)
                return f"# {page_title}\n\n{text[:max_length]}"
    except Exception:
        pass
    return None

def _fetch_github_readme(owner: str, repo: str, max_length: int) -> Optional[str]:
    """Fetch README from GitHub API as rendered HTML, then extract text."""
    import urllib.request
    api_url = f"https://api.github.com/repos/{owner}/{repo}/readme"
    try:
        req = urllib.request.Request(api_url, headers={
            "Accept": "application/vnd.github.html+json",
            "User-Agent": "web-research-tool/1.0",
        })
        with urllib.request.urlopen(req, timeout=5) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        if not html:
            return None
        # Use our existing text extraction on the rendered HTML
        text = _extract_with_regex(html)
        text = RE_MULTI_NEWLINE.sub("\n\n", text).strip()
        if text:
            return f"# {owner}/{repo}\n\n{text[:max_length]}"
    except Exception:
        pass
    return None

def _fetch_arxiv_api(paper_id: str, max_length: int) -> Optional[str]:
    """Fetch ArXiv paper metadata + abstract via Atom API."""
    import urllib.request
    import xml.etree.ElementTree as ET
    api_url = f"http://export.arxiv.org/api/query?id_list={paper_id}"
    try:
        req = urllib.request.Request(api_url, headers={"User-Agent": "web-research-tool/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            xml_data = resp.read().decode("utf-8", errors="replace")
        root = ET.fromstring(xml_data)
        ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
        entry = root.find("atom:entry", ns)
        if entry is None:
            return None
        title = (entry.findtext("atom:title", "", ns) or "").strip().replace("\n", " ")
        abstract = (entry.findtext("atom:summary", "", ns) or "").strip()
        authors = [a.findtext("atom:name", "", ns) for a in entry.findall("atom:author", ns)]
        published = (entry.findtext("atom:published", "", ns) or "")[:10]
        categories = [c.get("term", "") for c in entry.findall("atom:category", ns)]

        parts = [f"# {title}\n"]
        if authors:
            parts.append(f"Authors: {', '.join(authors[:10])}")
        if published:
            parts.append(f"Published: {published}")
        if categories:
            parts.append(f"Categories: {', '.join(categories[:5])}")
        parts.append(f"\n## Abstract\n\n{abstract}")

        text = "\n".join(parts)
        return text[:max_length] if text else None
    except Exception:
        pass
    return None

def _fetch_semantic_scholar_api(paper_hash: str, max_length: int) -> Optional[str]:
    """Fetch Semantic Scholar paper metadata + abstract via API (free, no key)."""
    import urllib.request
    api_url = f"https://api.semanticscholar.org/graph/v1/paper/{paper_hash}?fields=title,abstract,authors,year,citationCount,venue"
    try:
        req = urllib.request.Request(api_url, headers={"User-Agent": "web-research-tool/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        title = data.get("title", "Unknown")
        abstract = data.get("abstract") or ""
        authors = [a.get("name", "") for a in (data.get("authors") or [])]
        year = data.get("year")
        citations = data.get("citationCount")
        venue = data.get("venue") or ""
        parts = [f"# {title}\n"]
        if authors:
            parts.append(f"Authors: {', '.join(authors[:10])}")
        if year:
            parts.append(f"Year: {year}")
        if venue:
            parts.append(f"Venue: {venue}")
        if citations is not None:
            parts.append(f"Citations: {citations}")
        if abstract:
            parts.append(f"\n## Abstract\n\n{abstract}")
        text = "\n".join(parts)
        return text[:max_length] if text else None
    except Exception:
        pass
    return None


def _fetch_twitter_api(screen_name: str, tweet_id: str, max_length: int) -> Optional[str]:
    """Fetch tweet text via FxTwitter API (no auth required)."""
    import urllib.request
    api_url = f"https://api.fxtwitter.com/status/{tweet_id}"
    try:
        req = urllib.request.Request(api_url, headers={"User-Agent": "web-research-tool/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        tweet = data.get("tweet", {})
        author = tweet.get("author", {})
        name = author.get("name", screen_name)
        handle = author.get("screen_name", screen_name)
        text = tweet.get("text", "")
        created = tweet.get("created_at", "")
        likes = tweet.get("likes", 0)
        retweets = tweet.get("retweets", 0)
        replies = tweet.get("replies", 0)
        if not text:
            return None
        parts = [f"# @{handle} ({name})\n"]
        if created:
            parts.append(f"Date: {created}")
        parts.append(f"Likes: {likes} | Retweets: {retweets} | Replies: {replies}\n")
        parts.append(text)
        quote = tweet.get("quote", {})
        if quote and quote.get("text"):
            q_handle = quote.get("author", {}).get("screen_name", "?")
            parts.append(f"\n> Quoting @{q_handle}:\n> {quote['text']}")
        result = "\n".join(parts)
        return result[:max_length]
    except Exception:
        pass
    return None

def _fetch_reddit_json(reddit_path: str, max_length: int) -> Optional[str]:
    """Fetch Reddit post + comments via Reddit's public JSON API (no auth)."""
    import urllib.request
    # Reddit serves JSON when .json is appended to any URL
    api_url = f"https://www.reddit.com{reddit_path}.json"
    try:
        req = urllib.request.Request(api_url, headers={"User-Agent": "web-research-tool/1.0 (research)"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        if not isinstance(data, list) or not data:
            return None
        post = data[0]["data"]["children"][0]["data"]
        title = post.get("title", "")
        selftext = post.get("selftext", "")
        score = post.get("score", 0)
        subreddit = post.get("subreddit", "")
        author = post.get("author", "")
        parts = [f"# {title}\n"]
        parts.append(f"r/{subreddit} | u/{author} | {score} points\n")
        if selftext:
            parts.append(selftext)
        # Extract top comments
        if len(data) >= 2:
            comments = data[1]["data"]["children"]
            for c in comments[:5]:
                if c.get("kind") != "t1":
                    continue
                cd = c["data"]
                c_score = cd.get("score", 0)
                c_author = cd.get("author", "")
                c_body = cd.get("body", "")
                if c_body:
                    parts.append(f"\n---\n**u/{c_author}** ({c_score} points):\n{c_body}")
        text = "\n".join(parts)
        return text[:max_length] if text else None
    except Exception:
        pass
    return None

def _fetch_wayback_fallback(url: str, max_length: int) -> Optional[str]:
    """Try Wayback Machine for a recent cached version of the page."""
    import urllib.request
    api_url = f"https://archive.org/wayback/available?url={urllib.parse.quote(url, safe='')}"
    try:
        req = urllib.request.Request(api_url, headers={"User-Agent": "web-research-tool/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        snapshot = data.get("archived_snapshots", {}).get("closest", {})
        if not snapshot.get("available"):
            return None
        archive_url = snapshot.get("url", "")
        if not archive_url:
            return None
        req = urllib.request.Request(archive_url, headers={"User-Agent": "web-research-tool/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        text = _extract_with_regex(html)
        text = RE_MULTI_NEWLINE.sub("\n\n", text).strip()
        if text and len(text) > 200:
            return text[:max_length]
    except Exception:
        pass
    return None

async def fetch_single_async(
    url: str,
    timeout: int,
    min_content_length: int,
    max_content_length: int,
    progress: Optional[ProgressReporter] = None,
    query: str = "",
) -> FetchResult:
    """Fetch single URL using Scrapling's AsyncFetcher (TLS fingerprinting)."""
    t0 = time.monotonic()
    try:
        # API fast-path: use native APIs for sites that produce cleaner output than scraping
        api_content = None
        loop = asyncio.get_running_loop()
        wiki_match = RE_WIKIPEDIA_URL.match(url)
        if wiki_match:
            lang, title = wiki_match.group(1), wiki_match.group(2)
            api_content = await loop.run_in_executor(
                None, _fetch_wikipedia_api, lang, title, max_content_length
            )
        gh_match = RE_GITHUB_REPO_URL.match(url) if not api_content else None
        if gh_match:
            owner, repo = gh_match.group(1), gh_match.group(2)
            api_content = await loop.run_in_executor(
                None, _fetch_github_readme, owner, repo, max_content_length
            )
        arxiv_match = RE_ARXIV_URL.match(url) if not api_content else None
        if arxiv_match:
            paper_id = arxiv_match.group(1)
            api_content = await loop.run_in_executor(
                None, _fetch_arxiv_api, paper_id, max_content_length
            )
        # Semantic Scholar: try regex first, then fallback to string extraction
        if not api_content and 'semanticscholar.org/paper/' in url:
            s2_match = RE_SEMANTIC_SCHOLAR_URL.match(url)
            paper_hash = s2_match.group(1) if s2_match else None
            if not paper_hash:
                # Fallback: extract last path segment as paperId
                path = url.split('semanticscholar.org/paper/')[-1].split('?')[0].rstrip('/')
                candidate = path.split('/')[-1]
                if len(candidate) == 40 and all(c in '0123456789abcdef' for c in candidate.lower()):
                    paper_hash = candidate
            if paper_hash:
                api_content = await loop.run_in_executor(
                    None, _fetch_semantic_scholar_api, paper_hash, max_content_length
                )
            if not api_content:
                # S2 website returns HTTP 202 for programmatic access — skip Scrapling
                elapsed = time.monotonic() - t0
                s2_domain = urllib.parse.urlparse(url).netloc
                if progress:
                    progress.message(f"    --  {elapsed:5.1f}s  {s2_domain} (S2 API unavailable)")
                return FetchResult(url=url, success=False, error="S2 API unavailable")
        tw_match = RE_TWITTER_URL.match(url) if not api_content else None
        if tw_match:
            screen_name, tweet_id = tw_match.group(1), tw_match.group(2)
            api_content = await loop.run_in_executor(
                None, _fetch_twitter_api, screen_name, tweet_id, max_content_length
            )
        # Reddit: JSON API blocked (403) — skip API, fall through to scraping
        # For API-routed domains, if API failed, scraping won't help — bail early
        api_only = tw_match
        if api_content:
            elapsed = time.monotonic() - t0
            result = _create_fetch_result(url, api_content, min_content_length, max_content_length, query=query)
            if progress:
                progress.url_result(url, result.success, elapsed, result.error or "")
            return result
        if api_only:
            elapsed = time.monotonic() - t0
            result = FetchResult(url=url, success=False, error="API extraction failed")
            if progress:
                progress.url_result(url, False, elapsed, "API failed")
            return result
        _host = urllib.parse.urlparse(url).hostname or ""
        _use_httpx = _host in _CURL_DNS_FAIL_DOMAINS
        if not _use_httpx:
            try:
                page = await asyncio.wait_for(
                    AsyncFetcher.get(url, timeout=timeout, stealthy_headers=True),
                    timeout=min(timeout, 5),  # hard cutoff — DNS should resolve in <1s
                )
            except (asyncio.TimeoutError, Exception) as _fetch_err:
                if isinstance(_fetch_err, asyncio.TimeoutError) or "Resolving timed out" in str(_fetch_err):
                    _CURL_DNS_FAIL_DOMAINS.add(_host)
                    _use_httpx = True
                else:
                    raise
        if _use_httpx:
            # curl_cffi c-ares DNS fails for this domain — use httpx (system DNS)
            import httpx
            async with httpx.AsyncClient(
                follow_redirects=True, timeout=timeout,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            ) as _hx:
                _resp = await _hx.get(url)
            page = types.SimpleNamespace(
                status=_resp.status_code,
                html_content=_resp.text,
                body=_resp.content,
            )
        elapsed = time.monotonic() - t0

        if page.status != 200:
            if progress:
                progress.url_result(url, False, elapsed, f"HTTP {page.status}")
            return FetchResult(url=url, success=False, error=f"HTTP {page.status}")

        try:
            raw_html = page.html_content
        except (UnicodeDecodeError, AttributeError):
            # Scrapling failed to decode — try common encodings on raw bytes
            raw_bytes = page.body if hasattr(page, 'body') else b""
            raw_html = ""
            for enc in ("utf-8", "latin-1", "windows-1252", "iso-8859-1"):
                try:
                    raw_html = raw_bytes.decode(enc, errors="replace")
                    break
                except Exception:
                    continue
            if not raw_html:
                if progress:
                    progress.url_result(url, False, elapsed, "Encoding error")
                return FetchResult(url=url, success=False, error="Encoding error")
        if len(raw_html) > MAX_CONTENT_BYTES:
            # Truncate HTML but still try to extract text
            raw_html = raw_html[:MAX_CONTENT_BYTES]

        if _is_pdf(raw_html, url):
            # PDF: extract via pdftotext in process pool
            raw_body = page.body if isinstance(page.body, bytes) else raw_html.encode("utf-8", errors="replace")
            loop = asyncio.get_running_loop()
            content = await loop.run_in_executor(
                _get_extract_pool(), _extract_pdf, raw_body
            )
            if not content:
                if progress:
                    progress.url_result(url, False, elapsed, "PDF extraction failed")
                return FetchResult(url=url, success=False, error="PDF extraction failed")
            result = _create_fetch_result(url, content, min_content_length, max_content_length, query=query)
            if progress:
                progress.url_result(url, result.success, elapsed, result.error or "")
            return result

        if is_blocked_content(raw_html):
            if progress:
                progress.url_result(url, False, elapsed, "CAPTCHA/blocked")
            return FetchResult(url=url, success=False, error="CAPTCHA/blocked")

        # Extract text + JSON-LD in process pool (CPU-bound, don't block event loop)
        loop = asyncio.get_running_loop()
        content, structured = await loop.run_in_executor(
            _get_extract_pool(), _extract_content, raw_html
        )

        # Fallback: Scrapling's DOM parser when primary extraction is too short
        if len(content) < min_content_length:
            scrapling_content = _extract_with_scrapling_fallback(page, min_content_length)
            if scrapling_content:
                content = scrapling_content

        # Prepend structured data to content
        if structured:
            content = structured + content

        result = _create_fetch_result(url, content, min_content_length, max_content_length, query=query)
        # Wayback Machine fallback for failed/paywalled content
        if not result.success:
            wb_content = await loop.run_in_executor(
                None, _fetch_wayback_fallback, url, max_content_length
            )
            if wb_content:
                result = _create_fetch_result(url, wb_content, min_content_length, max_content_length, query=query)
        if progress:
            progress.url_result(url, result.success, elapsed, result.error or "")
        return result

    except asyncio.TimeoutError:
        elapsed = time.monotonic() - t0
        if progress:
            progress.url_result(url, False, elapsed, "Timeout")
        return FetchResult(url=url, success=False, error="Timeout")
    except Exception as e:
        elapsed = time.monotonic() - t0
        error_msg = str(e)[:50] if str(e) else type(e).__name__
        logger.debug(f"Fetch error for {url}: {e}")
        if progress:
            progress.url_result(url, False, elapsed, error_msg)
        return FetchResult(url=url, success=False, error=error_msg)


# =============================================================================
# SEARCH BACKENDS
# =============================================================================

def _load_brave_api_key() -> Optional[str]:
    """Load Brave Search API key from env var or config file."""
    key = os.environ.get("BRAVE_API_KEY", "")
    if key:
        return key
    try:
        return BRAVE_API_KEY_PATH.read_text().strip()
    except (FileNotFoundError, PermissionError):
        return None


_RE_HTML_TAGS = re.compile(r"<[^>]+>")

def _snippet_relevance(query: str, title: str, snippet: str) -> float:
    """Score snippet relevance to query by substring match. Returns 0.0-1.0.

    Uses substring matching instead of word-set intersection because DDG's
    ddgs library strips <b> tags without adding spaces, concatenating adjacent
    words (e.g. "theCRISPRsicklecelltherapy"). Substring match handles this.
    """
    query_words = set(query.lower().split())
    text = _RE_HTML_TAGS.sub(" ", (title + " " + snippet)).lower()
    if not query_words:
        return 1.0
    return sum(1 for w in query_words if w in text) / len(query_words)


class BraveSearch:
    """Brave Search API backend."""

    def __init__(self, api_key: str):
        self.api_key = api_key

    def search(
        self,
        query: str,
        num_results: int = 20,
    ) -> Iterator[Tuple[str, str, str]]:
        """Search Brave and yield (url, title, snippet) tuples."""
        import urllib.request

        encoded = urllib.parse.quote_plus(query)
        url = f"https://api.search.brave.com/res/v1/web/search?q={encoded}&count={min(num_results, 20)}"
        req = urllib.request.Request(url, headers={
            "X-Subscription-Token": self.api_key,
            "Accept": "application/json",
        })

        seen_urls: Set[str] = set()
        count = 0
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            for r in data.get("web", {}).get("results", []):
                result_url = r.get("url", "")
                if result_url and result_url not in seen_urls and is_valid_url(result_url) and not is_blocked_url(result_url):
                    seen_urls.add(result_url)
                    yield result_url, r.get("title", ""), r.get("description", "")
                    count += 1
                    if count >= num_results:
                        return
        except Exception as e:
            logger.debug(f"Brave search failed: {e}")
            return


_ACADEMIC_STRONG = (
    "paper", "papers", "preprint", "arxiv", "pubmed", "doi:",
    "meta-analysis", "systematic review", "clinical trial",
    "literature review", "peer-review", "journal article",
)
_ACADEMIC_WEAK = (
    "research", "study", "studies", "algorithm", "neural",
    "genome", "protein", "quantum", "theorem", "benchmark",
    "dataset", "experiment", "hypothesis", "molecular",
    "computational", "optimization", "evaluation", "survey",
    "simulation", "methodology", "technique",
    "prediction", "detection", "classification",
    "learning", "training", "computing",
    "correction", "encoding", "decoding", "synthesis",
    "imaging", "catalyst", "receptor", "enzyme",
    "biodiversity", "ecosystem", "acidification",
    "emission", "photovoltaic", "semiconductor",
    "neuroscience", "cortex", "cognitive",
    "clinical", "therapeutic", "diagnostic",
    "mechanism", "architecture", "model",
    "reinforcement", "robotics", "autonomous",
    "generative", "diffusion", "transformer",
)


def _is_academic_query(query: str) -> bool:
    """Heuristic: does this query likely seek academic/scientific content?
    Strong signals (any 1): paper, arxiv, clinical trial, etc.
    Weak signals (need 2+): research, algorithm, neural, model, etc."""
    q = query.lower()
    if any(s in q for s in _ACADEMIC_STRONG):
        return True
    return sum(1 for w in _ACADEMIC_WEAK if w in q) >= 2


def _detect_ddg_region(query: str) -> Optional[str]:
    """Detect DDG region from query script (Unicode ranges). Returns None for Latin."""
    scripts = {"ja": 0, "zh": 0, "ko": 0}
    for ch in query:
        cp = ord(ch)
        if 0x3040 <= cp <= 0x30FF or 0x31F0 <= cp <= 0x31FF:  # Hiragana + Katakana
            scripts["ja"] += 1
        elif 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF:  # CJK Unified
            scripts["zh"] += 1  # tentative — overridden by ja if kana present
        elif 0xAC00 <= cp <= 0xD7AF or 0x1100 <= cp <= 0x11FF:  # Hangul
            scripts["ko"] += 1
    # If kana detected, CJK chars are also Japanese
    if scripts["ja"] > 0:
        scripts["ja"] += scripts["zh"]
        scripts["zh"] = 0
    top = max(scripts, key=scripts.get)
    if scripts[top] == 0:
        return None
    region_map = {"ja": "jp-jp", "zh": "zh-cn", "ko": "kr-kr"}
    return region_map.get(top)


class DuckDuckGoSearch:
    """DuckDuckGo search with early URL filtering."""

    def search(
        self,
        query: str,
        num_results: int = 50,
        region: Optional[str] = None,
    ) -> Iterator[Tuple[str, str, str]]:
        """Search DuckDuckGo and yield (url, title, snippet) tuples."""
        seen_urls: Set[str] = set()
        count = 0

        ddg = DDGS(verify=False)
        ddg_kwargs = {}
        if region:
            ddg_kwargs["region"] = region
        for r in ddg.text(query, max_results=num_results * 2, **ddg_kwargs):
            url = r.get("href", "")
            if url and url not in seen_urls and is_valid_url(url) and not is_blocked_url(url):
                seen_urls.add(url)
                yield url, r.get("title", ""), r.get("body", "")
                count += 1
                if count >= num_results:
                    return


class MultiSearch:
    """Combined search: DDG primary, Brave fallback for coverage gaps."""

    def __init__(self):
        self._brave_key = _load_brave_api_key()

    def search(
        self,
        query: str,
        num_results: int = 20,
    ) -> Iterator[Tuple[str, str, str]]:
        """Search DDG first. If under target, supplement with Brave."""
        seen_urls: Set[str] = set()
        count = 0
        region = _detect_ddg_region(query)

        # Phase 1: DuckDuckGo (primary)
        ddg = DuckDuckGoSearch()
        try:
            for url, title, snippet in ddg.search(query, num_results, region=region):
                if url not in seen_urls:
                    seen_urls.add(url)
                    yield url, title, snippet
                    count += 1
        except Exception as e:
            logger.debug(f"DDG search failed: {e}")
            print(f"  DDG failed ({type(e).__name__}), trying Brave...", file=sys.stderr)

        # Phase 2: Brave (supplement if DDG fell short)
        shortfall = num_results - count
        if shortfall > 0 and self._brave_key:
            brave = BraveSearch(self._brave_key)
            for url, title, snippet in brave.search(query, shortfall + 5):
                if url not in seen_urls:
                    seen_urls.add(url)
                    yield url, title, snippet
                    count += 1
                    if count >= num_results:
                        return


# =============================================================================
# STREAMING OUTPUT
# =============================================================================

def format_result_raw(result: FetchResult) -> str:
    """Format single result as raw text."""
    return f"=== {result.url} ===\n{result.content}\n"


def format_result_json(result: FetchResult) -> str:
    """Format single result as JSON line."""
    return json.dumps({
        "url": result.url,
        "title": result.title,
        "content": result.content,
        "source": result.source
    }, ensure_ascii=False)


def stream_results(
    results: Iterator[FetchResult],
    output_format: str = "raw"
) -> Iterator[str]:
    """Stream formatted results."""
    formatter = format_result_json if output_format == "json" else format_result_raw
    for result in results:
        if result.success:
            yield formatter(result)


# =============================================================================
# RESEARCH WORKFLOW
# =============================================================================

async def run_research_async(
    config: ResearchConfig,
    progress: ProgressReporter,
    global_seen_urls: Optional[Set[str]] = None,
) -> AsyncIterator[FetchResult]:
    """
    Async streaming research workflow.
    Yields FetchResult objects as they complete.
    Pass global_seen_urls to dedup across multiple parallel queries.
    """
    progress.message(f'Researching: "{config.query}"')

    urls: List[str] = []
    fetch_queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
    result_queue: asyncio.Queue[Optional[FetchResult]] = asyncio.Queue()
    stats = ResearchStats(query=config.query)

    async def search_producer() -> None:
        loop = asyncio.get_running_loop()
        searcher = MultiSearch()
        t0 = time.monotonic()

        skipped = 0

        def search_and_stream():
            nonlocal skipped
            enqueued = 0
            seen_in_search: Set[str] = set()
            for url, title, snippet in searcher.search(config.query, config.search_results):
                if global_seen_urls is not None:
                    if url in global_seen_urls:
                        continue
                    global_seen_urls.add(url)
                seen_in_search.add(url)
                urls.append(url)
                stats.urls_searched = len(urls)
                # Snippet relevance gate: skip URLs with zero query word overlap
                # Always enqueue at least 5 URLs (safety net for edge cases)
                relevance = _snippet_relevance(config.query, title, snippet)
                if relevance == 0 and enqueued >= 5:
                    if progress.verbose:
                        _host = urllib.parse.urlparse(url).hostname or url
                        print(f"  [skip] {_host} relevance=0 | {title[:50]}", flush=True)
                    skipped += 1
                    continue
                loop.call_soon_threadsafe(fetch_queue.put_nowait, url)
                enqueued += 1

            # Supplement with DDG video + news results (bonus URLs not in web search)
            stats.bonus_sources = {}

            def _enqueue_bonus(url: str, source: str = "") -> None:
                nonlocal enqueued
                if url in seen_in_search or not is_valid_url(url) or is_blocked_url(url):
                    return
                if global_seen_urls is not None:
                    if url in global_seen_urls:
                        return
                    global_seen_urls.add(url)
                seen_in_search.add(url)
                urls.append(url)
                stats.urls_searched = len(urls)
                if source:
                    stats.bonus_sources[source] = stats.bonus_sources.get(source, 0) + 1
                loop.call_soon_threadsafe(fetch_queue.put_nowait, url)
                enqueued += 1

            # Run bonus searches in parallel (news + reddit)
            region = _detect_ddg_region(config.query)
            def _bonus_news():
                try:
                    ddg = DDGS(verify=False)
                    news_kwargs = {}
                    if region:
                        news_kwargs["region"] = region
                    for r in ddg.news(config.query, max_results=5, **news_kwargs):
                        url = r.get("url", "")
                        if url:
                            _enqueue_bonus(url, "news")
                except Exception:
                    pass

            def _bonus_reddit():
                """Search DDG for reddit discussions, inject snippet content directly
                (reddit blocks all scraping/API access, so we use DDG snippets)."""
                try:
                    ddg = DDGS(verify=False)
                    count = 0
                    for r in ddg.text(f"{config.query} site:reddit.com", max_results=5):
                        url = r.get("href", "")
                        if not url or "reddit.com" not in url:
                            continue
                        if url in seen_in_search or not is_valid_url(url):
                            continue
                        if global_seen_urls is not None:
                            if url in global_seen_urls:
                                continue
                            global_seen_urls.add(url)
                        seen_in_search.add(url)
                        title = r.get("title", "")
                        body = r.get("body", "")
                        content = f"# {title}\n\n{body}" if title else body
                        if not content or len(content) < 50:
                            continue
                        result = FetchResult(url=url, success=True, content=content[:config.max_content_length])
                        loop.call_soon_threadsafe(result_queue.put_nowait, result)
                        urls.append(url)
                        stats.urls_searched = len(urls)
                        stats.bonus_sources["reddit"] = stats.bonus_sources.get("reddit", 0) + 1
                        count += 1
                except Exception:
                    pass

            def _bonus_arxiv():
                """Search arXiv API, fallback to Semantic Scholar if arXiv fails."""
                arxiv_ok = False
                try:
                    import urllib.request
                    import xml.etree.ElementTree as ET
                    # Strip dates/filler, use AND between core terms for precision
                    _arxiv_skip = {"recent", "latest", "new", "advances", "applications",
                                   "current", "overview", "update", "the", "and", "for", "with", "from"}
                    words = [w for w in config.query.split()
                             if not re.match(r'^\d{4}$', w) and w.lower() not in _arxiv_skip]
                    core = words[:4]  # max 4 key terms
                    arxiv_query = " AND ".join(f"all:{w}" for w in core) if core else config.query
                    encoded = urllib.parse.quote_plus(arxiv_query)
                    api_url = f"http://export.arxiv.org/api/query?search_query={encoded}&start=0&max_results=5&sortBy=relevance"
                    req = urllib.request.Request(api_url, headers={"User-Agent": "web-research-tool/1.0"})
                    with urllib.request.urlopen(req, timeout=8) as resp:
                        xml_data = resp.read().decode("utf-8", errors="replace")
                    root = ET.fromstring(xml_data)
                    ns = {"atom": "http://www.w3.org/2005/Atom"}
                    for entry in root.findall("atom:entry", ns):
                        for link in entry.findall("atom:link", ns):
                            href = link.get("href", "")
                            if "arxiv.org/abs/" in href:
                                _enqueue_bonus(href, "arxiv")
                                arxiv_ok = True
                                break
                except Exception:
                    pass
                # Fallback: Semantic Scholar if arXiv returned nothing
                if not arxiv_ok:
                    try:
                        import urllib.request
                        encoded = urllib.parse.quote_plus(config.query)
                        api_url = f"https://api.semanticscholar.org/graph/v1/paper/search?query={encoded}&limit=10&fields=url,externalIds"
                        req = urllib.request.Request(api_url, headers={"User-Agent": "web-research-tool/1.0"})
                        with urllib.request.urlopen(req, timeout=8) as resp:
                            data = json.loads(resp.read().decode("utf-8", errors="replace"))
                        for paper in (data.get("data") or []):
                            ext_ids = paper.get("externalIds") or {}
                            arxiv_id = ext_ids.get("ArXiv")
                            if arxiv_id:
                                _enqueue_bonus(f"https://arxiv.org/abs/{arxiv_id}", "scholar")
                            else:
                                paper_id = paper.get("paperId", "")
                                if paper_id:
                                    _enqueue_bonus(f"https://www.semanticscholar.org/paper/{paper_id}", "scholar")
                    except Exception:
                        pass

            def _bonus_pubmed():
                """Search PubMed via NCBI E-utilities (free, no key)."""
                try:
                    import urllib.request
                    encoded = urllib.parse.quote_plus(config.query)
                    api_url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term={encoded}&retmax=5&retmode=json&sort=relevance"
                    req = urllib.request.Request(api_url, headers={"User-Agent": "web-research-tool/1.0"})
                    with urllib.request.urlopen(req, timeout=8) as resp:
                        data = json.loads(resp.read().decode("utf-8", errors="replace"))
                    for pmid in (data.get("esearchresult", {}).get("idlist") or []):
                        _enqueue_bonus(f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/", "pubmed")
                except Exception:
                    pass

            def _bonus_openalex():
                """Search OpenAlex for papers (free, no key for basic search)."""
                try:
                    import urllib.request
                    encoded = urllib.parse.quote_plus(config.query)
                    api_url = f"https://api.openalex.org/works?search={encoded}&per_page=5&mailto=web-research-tool@example.com"
                    req = urllib.request.Request(api_url, headers={"User-Agent": "web-research-tool/1.0"})
                    with urllib.request.urlopen(req, timeout=8) as resp:
                        data = json.loads(resp.read().decode("utf-8", errors="replace"))
                    for work in (data.get("results") or []):
                        # Prefer open access URL, then DOI, then landing page
                        oa = work.get("open_access") or {}
                        url = oa.get("oa_url")
                        if not url:
                            doi = work.get("doi")
                            if doi:
                                url = doi  # DOI URLs like https://doi.org/10.1234/...
                        if not url:
                            loc = work.get("primary_location") or {}
                            url = loc.get("landing_page_url")
                        if url:
                            _enqueue_bonus(url, "openalex")
                except Exception:
                    pass

            def _bonus_europepmc():
                """Search Europe PMC for papers (free, no key, more OA full-text than PubMed)."""
                try:
                    import urllib.request
                    encoded = urllib.parse.quote_plus(config.query)
                    api_url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?query={encoded}&format=json&pageSize=5&sort=CITED%20desc"
                    req = urllib.request.Request(api_url, headers={"User-Agent": "web-research-tool/1.0"})
                    with urllib.request.urlopen(req, timeout=8) as resp:
                        data = json.loads(resp.read().decode("utf-8", errors="replace"))
                    for result in (data.get("resultList", {}).get("result") or []):
                        # Prefer full-text URL, then DOI, then Europe PMC page
                        url = None
                        doi = result.get("doi")
                        if doi:
                            url = f"https://doi.org/{doi}"
                        if not url:
                            pmcid = result.get("pmcid")
                            if pmcid:
                                url = f"https://europepmc.org/article/PMC/{pmcid}"
                            else:
                                pmid = result.get("pmid")
                                if pmid:
                                    url = f"https://europepmc.org/article/MED/{pmid}"
                        if url:
                            _enqueue_bonus(url, "europepmc")
                except Exception:
                    pass

            def _bonus_hackernews():
                """Search Hacker News via Algolia API (free, no key, 10K/hr)."""
                try:
                    import urllib.request
                    # Use top 3 key terms to avoid zero-result long queries
                    _hn_skip = {"best", "practices", "latest", "recent", "new", "how", "what",
                                "the", "and", "for", "with", "from", "using", "guide", "tutorial"}
                    words = [w for w in config.query.split() if w.lower() not in _hn_skip][:3]
                    hn_query = " ".join(words) if words else config.query
                    encoded = urllib.parse.quote_plus(hn_query)
                    api_url = f"https://hn.algolia.com/api/v1/search?query={encoded}&tags=story&hitsPerPage=5"
                    req = urllib.request.Request(api_url, headers={"User-Agent": "web-research-tool/1.0"})
                    with urllib.request.urlopen(req, timeout=8) as resp:
                        data = json.loads(resp.read().decode("utf-8", errors="replace"))
                    for hit in (data.get("hits") or []):
                        url = hit.get("url")
                        if url:
                            _enqueue_bonus(url, "hackernews")
                        else:
                            # Ask HN / Show HN posts without external URL — link to HN discussion
                            story_id = hit.get("objectID")
                            if story_id:
                                _enqueue_bonus(f"https://news.ycombinator.com/item?id={story_id}", "hackernews")
                except Exception:
                    pass

            def _bonus_stackoverflow():
                """Search Stack Overflow API (free, no key, 300/day unauth)."""
                try:
                    import urllib.request
                    encoded = urllib.parse.quote_plus(config.query)
                    api_url = f"https://api.stackexchange.com/2.3/search/excerpts?order=desc&sort=relevance&q={encoded}&site=stackoverflow&pagesize=5&filter=default"
                    req = urllib.request.Request(api_url, headers={
                        "User-Agent": "web-research-tool/1.0",
                        "Accept-Encoding": "gzip",
                    })
                    with urllib.request.urlopen(req, timeout=8) as resp:
                        # SO API always returns gzip
                        raw = resp.read()
                        if resp.headers.get("Content-Encoding") == "gzip":
                            import gzip
                            raw = gzip.decompress(raw)
                        data = json.loads(raw.decode("utf-8", errors="replace"))
                    for item in (data.get("items") or []):
                        qid = item.get("question_id")
                        if qid:
                            _enqueue_bonus(f"https://stackoverflow.com/questions/{qid}", "stackoverflow")
                except Exception:
                    pass

            def _bonus_devto():
                """Search Dev.to API for articles (free, no key)."""
                try:
                    import urllib.request
                    encoded = urllib.parse.quote_plus(config.query)
                    # Dev.to doesn't have keyword search in API, but per_page+tag works
                    # Use page=1&per_page=5 with the query as tag approximation
                    # Actually, the /articles endpoint does support a hidden 'q' param via Forem
                    api_url = f"https://dev.to/api/articles?per_page=5&top=365"
                    # Try tag-based search with first keyword
                    words = config.query.split()
                    if words:
                        tag = re.sub(r'[^a-zA-Z0-9]', '', words[0]).lower()
                        if tag:
                            api_url += f"&tag={tag}"
                    req = urllib.request.Request(api_url, headers={"User-Agent": "web-research-tool/1.0"})
                    with urllib.request.urlopen(req, timeout=8) as resp:
                        data = json.loads(resp.read().decode("utf-8", errors="replace"))
                    for article in (data if isinstance(data, list) else []):
                        url = article.get("url")
                        if url:
                            _enqueue_bonus(url, "devto")
                except Exception:
                    pass

            def _bonus_github_repos():
                """Search GitHub repositories API (free, no key, 10/min unauth)."""
                try:
                    import urllib.request
                    encoded = urllib.parse.quote_plus(config.query)
                    api_url = f"https://api.github.com/search/repositories?q={encoded}&sort=stars&per_page=5"
                    req = urllib.request.Request(api_url, headers={
                        "User-Agent": "web-research-tool/1.0",
                        "Accept": "application/vnd.github.v3+json",
                    })
                    with urllib.request.urlopen(req, timeout=8) as resp:
                        data = json.loads(resp.read().decode("utf-8", errors="replace"))
                    for repo in (data.get("items") or []):
                        url = repo.get("html_url")
                        if url:
                            _enqueue_bonus(url, "github")
                except Exception:
                    pass

            bonus_fns = [_bonus_news, _bonus_reddit]
            if config.scientific:
                bonus_fns.extend([_bonus_arxiv, _bonus_openalex])
            if config.medical:
                bonus_fns.extend([_bonus_pubmed, _bonus_europepmc])
                if not config.scientific:
                    bonus_fns.append(_bonus_openalex)
            if config.tech:
                bonus_fns.extend([_bonus_hackernews, _bonus_stackoverflow, _bonus_devto, _bonus_github_repos])
            with ThreadPoolExecutor(max_workers=len(bonus_fns)) as bonus_pool:
                list(bonus_pool.map(lambda f: f(), bonus_fns))

        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                await loop.run_in_executor(executor, search_and_stream)

            search_elapsed = time.monotonic() - t0
            source_info = f"{stats.urls_searched} URLs"
            if searcher._brave_key:
                source_info += " (DDG+Brave)"
            else:
                source_info += " (DDG)"
            if stats.bonus_sources:
                bonus_parts = [f"{v} {k}" for k, v in sorted(stats.bonus_sources.items())]
                source_info += f" + bonus: {', '.join(bonus_parts)}"
            if skipped:
                source_info += f", {skipped} filtered"
            progress.message(f"  [search] {source_info} in {search_elapsed:.1f}s")
        except Exception as e:
            search_elapsed = time.monotonic() - t0
            progress.message(f"  [search] failed after {search_elapsed:.1f}s: {e}")
        finally:
            await fetch_queue.put(None)

    async def fetch_consumer() -> None:
        semaphore = asyncio.Semaphore(config.max_concurrent)
        pending: List[asyncio.Task] = []
        fetch_limit = config.fetch_count

        async def fetch_one(url: str) -> None:
            async with semaphore:
                result = await fetch_single_async(
                    url, config.timeout,
                    config.min_content_length, config.max_content_length,
                    progress=progress, query=config.query,
                )
                await result_queue.put(result)

        while True:
            url = await fetch_queue.get()
            if url is None:
                break
            if fetch_limit == 0 or len(pending) < fetch_limit:
                pending.append(asyncio.create_task(fetch_one(url)))

        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await result_queue.put(None)

    progress.phase_start("fetch")
    asyncio.create_task(search_producer())
    asyncio.create_task(fetch_consumer())

    fetched = 0
    while True:
        result = await result_queue.get()
        if result is None:
            break
        fetched += 1
        if result.success:
            stats.urls_fetched += 1
            stats.content_chars += len(result.content)
        progress.update("fetch", fetched, stats.urls_searched or fetched)
        yield result

    progress.newline()
    progress.summary(stats.urls_fetched, stats.urls_searched, stats.content_chars)


# =============================================================================
# CROSS-PAGE DEDUPLICATION
# =============================================================================

# Common English stop words for fuzzy dedup signatures
_STOP_WORDS = frozenset(
    "a an the and or but in on at to for of is it its be by as was were are been "
    "has have had do does did will would shall should can could may might this that "
    "these those with from not no nor so if then than too also very just about above "
    "after before between each few more most other some such only own same through "
    "during until while into over under again further once here there when where why "
    "how all any both each which what who whom".split()
)


def _normalize_sentence(s: str) -> str:
    """Normalize a sentence for exact dedup: lowercase, strip punctuation, collapse whitespace."""
    s = s.lower().strip()
    s = re.sub(r'[^\w\s]', '', s)
    return RE_WHITESPACE.sub(' ', s)


def _content_signature(s: str) -> str:
    """Content-word signature for fuzzy dedup. Strips stop words, sorts remaining → key.
    Two sentences with the same content words in any order match."""
    words = sorted(w for w in s.lower().split() if w not in _STOP_WORDS and len(w) > 2)
    return " ".join(words)


@dataclass
class DedupStats:
    """Track dedup savings."""
    chars_before: int = 0
    chars_after: int = 0
    exact_dupes: int = 0
    fuzzy_dupes: int = 0
    pages_dropped: int = 0


def _dedup_results(
    results: List[FetchResult],
    seen: Optional[Set[str]] = None,
    seen_fuzzy: Optional[Set[str]] = None,
) -> Tuple[List[FetchResult], DedupStats]:
    """Remove duplicate sentences across pages (exact + fuzzy).
    Pass shared sets to dedup across multiple calls (e.g. multi-query)."""
    if seen is None:
        seen = set()
    if seen_fuzzy is None:
        seen_fuzzy = set()
    deduped: List[FetchResult] = []
    stats = DedupStats()

    for r in results:
        if not r.success:
            deduped.append(r)
            continue

        stats.chars_before += len(r.content)
        lines = r.content.split("\n")
        kept: List[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                kept.append(line)
                continue
            # Always keep headers and metadata
            if stripped.startswith("# ") or stripped.startswith("[meta"):
                kept.append(line)
                continue
            # Skip very short lines (not worth deduping)
            if len(stripped) < 40:
                kept.append(line)
                continue
            # Exact dedup (normalized)
            norm = _normalize_sentence(stripped)
            if norm in seen:
                stats.exact_dupes += 1
                continue
            seen.add(norm)
            # Fuzzy dedup (content-word signature)
            sig = _content_signature(norm)
            if sig and len(sig) > 10 and sig in seen_fuzzy:
                stats.fuzzy_dupes += 1
                continue
            if sig and len(sig) > 10:
                seen_fuzzy.add(sig)
            kept.append(line)

        new_content = "\n".join(kept).strip()
        stats.chars_after += len(new_content)
        if len(new_content) < 50:
            stats.pages_dropped += 1
            continue
        deduped.append(FetchResult(
            url=r.url,
            success=True,
            content=new_content,
            title=r.title,
            source=r.source,
        ))

    # Log dedup effectiveness to stderr (visible with -v)
    if stats.chars_before > 0 and (stats.exact_dupes or stats.fuzzy_dupes):
        saved_pct = 100 * (1 - stats.chars_after / stats.chars_before)
        logger.debug(
            f"Dedup: {stats.chars_before:,} → {stats.chars_after:,} chars "
            f"({saved_pct:.0f}% saved, {stats.exact_dupes} exact + {stats.fuzzy_dupes} fuzzy dupes, "
            f"{stats.pages_dropped} pages dropped)"
        )

    return deduped, stats


def _global_compress(
    results: List[FetchResult],
    query: str,
    budget: int,
) -> List[FetchResult]:
    """Cross-page BM25 compression: keep most relevant sentences across all pages within budget.
    Each page retains its header (title/meta) unconditionally; body sentences compete globally."""
    from rank_bm25 import BM25Okapi

    total_chars = sum(len(r.content) for r in results if r.success)
    if total_chars <= budget:
        return results

    # Parse each page into header + body sentences
    page_data: List[dict] = []
    all_sentences: List[Tuple[int, int, str]] = []  # (page_idx, sent_idx, text)
    for pi, r in enumerate(results):
        if not r.success:
            page_data.append({"header": [], "sentences": [], "result": r})
            continue
        lines = r.content.split("\n")
        header: List[str] = []
        body_lines: List[str] = []
        in_header = True
        for line in lines:
            stripped = line.strip()
            if in_header and (stripped.startswith("# ") or stripped.startswith("[meta")):
                header.append(line)
            else:
                in_header = False
                body_lines.append(line)
        sentences = _split_sentences("\n".join(body_lines))
        page_data.append({"header": header, "sentences": sentences, "result": r})
        for si, sent in enumerate(sentences):
            all_sentences.append((pi, si, sent))

    if not all_sentences:
        return results

    # BM25 score all sentences globally
    tokenized = [s.lower().split() for _, _, s in all_sentences]
    bm25 = BM25Okapi(tokenized)
    scores = bm25.get_scores(query.lower().split())

    # Rank and select within budget
    header_budget = sum(
        sum(len(h) + 1 for h in pd["header"])
        for pd in page_data
    )
    body_budget = budget - header_budget

    ranked = sorted(zip(scores, range(len(all_sentences))), reverse=True)
    selected: Set[Tuple[int, int]] = set()  # (page_idx, sent_idx)
    chars_used = 0
    for score, idx in ranked:
        if chars_used >= body_budget:
            break
        pi, si, sent = all_sentences[idx]
        selected.add((pi, si))
        chars_used += len(sent) + 1

    # Rebuild pages with only selected sentences (in original order)
    compressed: List[FetchResult] = []
    pages_trimmed = 0
    for pi, pd in enumerate(page_data):
        r = pd["result"]
        if not r.success:
            compressed.append(r)
            continue
        kept = list(pd["header"])
        page_selected = sorted(si for (p, si) in selected if p == pi)
        for si in page_selected:
            kept.append(pd["sentences"][si])
        new_content = "\n".join(kept).strip()
        if len(new_content) < 50:
            pages_trimmed += 1
            continue
        if len(new_content) < len(r.content):
            pages_trimmed += 1
        compressed.append(FetchResult(
            url=r.url, success=True, content=new_content,
            title=r.title, source=r.source,
        ))

    new_total = sum(len(r.content) for r in compressed if r.success)
    saved_pct = 100 * (1 - new_total / total_chars)
    logger.debug(
        f"Global compress: {total_chars:,} → {new_total:,} chars "
        f"({saved_pct:.0f}% saved, budget {budget:,}, {pages_trimmed} pages trimmed)"
    )
    return compressed


# =============================================================================
# BATCH OUTPUT FORMATTERS (for non-streaming mode)
# =============================================================================

def format_batch_json(results: List[FetchResult], query: str) -> str:
    """Format all results as JSON."""
    successful = [r for r in results if r.success]
    return json.dumps({
        "query": query,
        "stats": {
            "urls_fetched": len(successful),
            "content_chars": sum(len(r.content) for r in successful)
        },
        "content": [
            {"url": r.url, "title": r.title, "content": r.content, "source": r.source}
            for r in successful
        ]
    }, indent=2, ensure_ascii=False)


def format_batch_raw(results: List[FetchResult]) -> str:
    """Format all results as raw text (optimized with StringIO)."""
    buffer = StringIO()
    for r in results:
        if r.success:
            buffer.write(f"=== {r.url} ===\n")
            buffer.write(r.content)
            buffer.write("\n\n")
    return buffer.getvalue()


def format_batch_markdown(results: List[FetchResult], query: str, max_preview: int = 4000) -> str:
    """Format all results as markdown (optimized with StringIO)."""
    successful = [r for r in results if r.success]
    buffer = StringIO()

    buffer.write(f"# Research: {query}\n\n")
    buffer.write(f"**Sources Analyzed**: {len(successful)} pages\n\n")
    buffer.write("---\n\n")

    for r in successful:
        if r.content:
            title = r.title or r.url
            buffer.write(f"## {title}\n")
            buffer.write(f"*Source: {r.url}*\n\n")
            if len(r.content) > max_preview:
                buffer.write(r.content[:max_preview])
                buffer.write("...")
            else:
                buffer.write(r.content)
            buffer.write("\n\n---\n\n")

    return buffer.getvalue()


# =============================================================================
# MAIN ENTRY POINTS
# =============================================================================

def run_research(config: ResearchConfig, verbose: bool = False) -> Optional[List[FetchResult]]:
    """Execute research and output results."""
    progress = ProgressReporter(quiet=config.quiet, verbose=verbose)

    if config.stream:
        # Streaming mode: output results as they arrive
        async def stream_async():
            try:
                async for result in run_research_async(config, progress):
                    if result.success:
                        print(format_result_raw(result))
            finally:
                _shutdown_extract_pool()
        asyncio.run(stream_async())
        return None

    # Batch mode: collect all results, then format
    results: List[FetchResult] = []

    async def collect_async():
        try:
            async for result in run_research_async(config, progress):
                results.append(result)
        finally:
            _shutdown_extract_pool()
    asyncio.run(collect_async())

    return results


def main() -> None:
    """Main entry point."""
    # Force UTF-8 stdout on Windows (avoids cp1251/charmap encoding errors)
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description="Web Research Tool - Search + Fetch with TLS fingerprinting (Scrapling)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python web_research.py "Mac Studio M3 Ultra LLM performance"
  python web_research.py "AI trends 2025" --fetch 50
  python web_research.py "Python best practices" -o markdown
  python web_research.py "query" --stream  # Stream output as results arrive
  python web_research.py "query1" "query2" "query3"  # Parallel multi-query
  python web_research.py --url https://example.com   # Fetch specific URL (skip search)
  python web_research.py -u url1 url2 url3           # Fetch multiple URLs in parallel

Search: DDG primary + Brave fallback (set BRAVE_API_KEY env var or ~/.config/brave/api_key)
Fetch: Scrapling AsyncFetcher (TLS fingerprinting)
Extract: trafilatura > regex > Scrapling DOM parser (tiered fallback)
Blocked domains: facebook, youtube, tiktok, instagram, linkedin
        """
    )

    parser.add_argument("query", nargs="?", help="Search query (omit if using --url)")
    parser.add_argument("extra_queries", nargs="*", help="Additional queries (run in parallel with first)")
    parser.add_argument("-u", "--url", nargs="+", metavar="URL",
                        help="Fetch specific URLs directly (skip search)")
    parser.add_argument("-s", "--search", type=int, default=20,
                        help="Number of search results (default: 20)")
    parser.add_argument("-f", "--fetch", type=int, default=0,
                        help="Max pages to fetch (default: 0 = fetch ALL)")
    parser.add_argument("-m", "--max-length", type=int, default=8000,
                        help="Max content length per page (default: 8000)")
    parser.add_argument("-o", "--output", choices=["json", "raw", "markdown"], default="raw",
                        help="Output format (default: raw)")
    parser.add_argument("-t", "--timeout", type=int, default=5,
                        help="Fetch timeout in seconds (default: 5)")
    parser.add_argument("-c", "--concurrent", type=int, default=50,
                        help="Max concurrent connections (default: 50)")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="Suppress progress messages")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable verbose logging")
    parser.add_argument("--stream", action="store_true",
                        help="Stream output as results arrive (reduces memory usage)")
    parser.add_argument("-g", "--global-budget", type=int, default=0,
                        help="Global char budget across all pages (0 = unlimited)")
    parser.add_argument("--usage", action="store_true",
                        help="Show usage statistics (last 30 days)")
    parser.add_argument("--sci", action="store_true",
                        help="Enable scientific bonus sources (arXiv, OpenAlex)")
    parser.add_argument("--med", action="store_true",
                        help="Enable medical bonus sources (PubMed, Europe PMC, OpenAlex)")
    parser.add_argument("--tech", action="store_true",
                        help="Enable tech bonus sources (Hacker News, Stack Overflow, Dev.to, GitHub)")
    parser.add_argument("--quality", action="store_true",
                        help="Include output quality analysis (with --usage)")

    args = parser.parse_args()

    if args.usage or args.quality:
        print_usage_stats(quality=args.quality)
        sys.exit(0)

    # JSON output must not have progress messages mixed in (agents parse stdout)
    if args.output == "json":
        args.quiet = True

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    # URL-fetch mode: skip search, just fetch specific URLs
    # Use higher default for direct fetch (user wants the full page, not search snippets)
    if args.url:
        url_max = args.max_length if "--max-length" in sys.argv or "-m" in sys.argv else 50000
        async def fetch_urls():
            progress = ProgressReporter(quiet=args.quiet, verbose=args.verbose)
            tasks = [
                fetch_single_async(url, args.timeout, 100, url_max, progress=progress)
                for url in args.url
            ]
            return list(await asyncio.gather(*tasks))

        t0 = time.monotonic()
        try:
            results = asyncio.run(fetch_urls())
            ok = [r for r in results if r.success]
            failed = [r for r in results if not r.success]
            error_summary = "; ".join(dict.fromkeys(r.error for r in failed if r.error))[:200] or None
            log_usage({
                "query": "", "mode": "url-fetch", "urls_searched": 0,
                "urls_fetched": len(ok),
                "content_chars": sum(len(r.content) for r in results),
                "ok": bool(ok), "error": error_summary if not ok else None,
                "ms": int((time.monotonic() - t0) * 1000), "timeout": False,
                **_quality_fields(results),
            })
            if ok:
                if args.output == "json":
                    print(format_batch_json(ok, "url-fetch"))
                else:
                    print(format_batch_raw(ok))
            if not ok:
                print("All URLs failed to fetch", file=sys.stderr)
                sys.exit(1)
        except KeyboardInterrupt:
            sys.exit(130)
        sys.exit(0)

    if not args.query:
        parser.error("query is required (or use --url for direct fetch)")

    queries = [args.query] + (args.extra_queries or [])

    def make_config(query: str) -> ResearchConfig:
        return ResearchConfig(
            query=query,
            fetch_count=args.fetch,
            max_content_length=args.max_length,
            timeout=args.timeout,
            quiet=args.quiet,
            max_concurrent=args.concurrent,
            search_results=args.search,
            stream=args.stream,
            scientific=args.sci,
            medical=args.med,
            tech=args.tech,
        )

    # Hard wall-clock timeout: kill the entire process after 5 minutes
    import signal
    _wall_t0 = time.monotonic()
    def _timeout_handler(signum, frame):
        for q in queries:
            log_usage({
                "query": q, "mode": "multi" if len(queries) > 1 else "search",
                "urls_searched": 0, "urls_fetched": 0, "content_chars": 0,
                "ok": False, "error": "wall-clock timeout",
                "ms": int((time.monotonic() - _wall_t0) * 1000), "timeout": True,
                "short_pages": 0, "domains": [],
            })
        print(f"\nwall-clock timeout ({_WALL_TIMEOUT}s) — exiting", file=sys.stderr)
        os._exit(1)  # kills child processes (ProcessPoolExecutor workers)
    _WALL_TIMEOUT = 300
    if hasattr(signal, 'SIGALRM'):
        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(_WALL_TIMEOUT)

    try:
        if len(queries) == 1:
            # Single query: original behavior
            config = make_config(queries[0])
            t0 = time.monotonic()
            if args.stream:
                run_research(config, verbose=args.verbose)
                log_usage({
                    "query": config.query, "mode": "search",
                    "urls_fetched": 0, "content_chars": 0,
                    "ok": True, "error": None,
                    "ms": int((time.monotonic() - t0) * 1000), "timeout": False,
                    "short_pages": 0, "domains": [],
                })
            else:
                results = run_research(config, verbose=args.verbose)
                ok = [r for r in (results or []) if r.success]
                log_usage({
                    "query": config.query, "mode": "search",
                    "urls_fetched": len(ok),
                    "content_chars": sum(len(r.content) for r in (results or [])),
                    "ok": bool(results), "error": None,
                    "ms": int((time.monotonic() - t0) * 1000), "timeout": False,
                    **_quality_fields(results),
                })
                if results:
                    results, dedup_st = _dedup_results(results)
                    if args.global_budget > 0:
                        results = _global_compress(results, config.query, args.global_budget)
                    if args.output == "json":
                        print(format_batch_json(results, config.query))
                    elif args.output == "markdown":
                        print(format_batch_markdown(results, config.query, config.max_content_length))
                    else:
                        print(format_batch_raw(results))
                else:
                    print("No results found", file=sys.stderr)
                    sys.exit(1)
        else:
            # Multi-query: run all in parallel
            configs = [make_config(q) for q in queries]
            # Lower per-query concurrency to avoid resource exhaustion
            for cfg in configs:
                cfg.max_concurrent = min(cfg.max_concurrent, 20)

            t0_multi = time.monotonic()

            async def run_all():
                seen: Set[str] = set()  # cross-query URL dedup
                async def run_one(cfg: ResearchConfig) -> Tuple[str, List[FetchResult]]:
                    progress = ProgressReporter(quiet=cfg.quiet, verbose=args.verbose)
                    results: List[FetchResult] = []
                    async for result in run_research_async(cfg, progress, global_seen_urls=seen):
                        results.append(result)
                    return cfg.query, results

                return await asyncio.wait_for(
                    asyncio.gather(*(run_one(c) for c in configs)),
                    timeout=120,  # hard cap: 2 minutes for all queries
                )

            try:
                all_results = asyncio.run(run_all())
            except asyncio.TimeoutError:
                elapsed = int((time.monotonic() - t0_multi) * 1000)
                for q in queries:
                    log_usage({
                        "query": q, "mode": "multi",
                        "urls_fetched": 0, "content_chars": 0,
                        "ok": False, "error": "multi-query timeout (120s)",
                        "ms": elapsed, "timeout": True,
                    })
                print("Multi-query timed out after 120s", file=sys.stderr)
                sys.exit(1)

            elapsed = int((time.monotonic() - t0_multi) * 1000)
            dedup_seen: Set[str] = set()  # shared across queries
            dedup_fuzzy: Set[str] = set()
            for query, results in all_results:
                ok = [r for r in results if r.success]
                log_usage({
                    "query": query, "mode": "multi",
                    "urls_fetched": len(ok),
                    "content_chars": sum(len(r.content) for r in results),
                    "ok": bool(results), "error": None,
                    "ms": elapsed, "timeout": False,
                    **_quality_fields(results),
                })
                if not results:
                    continue
                results, _ = _dedup_results(results, seen=dedup_seen, seen_fuzzy=dedup_fuzzy)
                if args.global_budget > 0:
                    results = _global_compress(results, query, args.global_budget)
                if args.output == "json":
                    print(format_batch_json(results, query))
                elif args.output == "markdown":
                    print(format_batch_markdown(results, query, args.max_length))
                else:
                    if len(queries) > 1:
                        print(f"\n{'='*60}")
                        print(f"QUERY: {query}")
                        print(f"{'='*60}\n")
                    print(format_batch_raw(results))

    except KeyboardInterrupt:
        print("\nInterrupted", file=sys.stderr)
        sys.exit(130)
    except BrokenPipeError:
        # Output pipe closed (e.g. piped to head) — not a real error
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)
    except Exception as e:
        log_usage({
            "query": queries[0] if queries else "", "mode": "search",
            "urls_fetched": 0, "content_chars": 0,
            "ok": False, "error": str(e)[:200],
            "ms": int((time.monotonic() - _wall_t0) * 1000), "timeout": False,
        })
        logger.exception(f"Research failed: {e}")
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
