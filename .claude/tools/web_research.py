#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["scrapling[fetchers]", "ddgs"]
# ///
# -*- coding: utf-8 -*-
"""
Web Research Tool - Autonomous Search + Fetch + Report

Unified tool combining search and fetch into a single optimized workflow:
1. Search via DuckDuckGo + Brave (fallback) for maximum coverage
2. Filter and deduplicate URLs during search (early filtering)
3. Fetch content in parallel via Scrapling (TLS fingerprinting, anti-bot bypass)
4. Stealth browser retry for blocked pages (403/CAPTCHA)
5. Scrapling text extraction fallback for "Too short" pages
6. Output combined results (streaming or batched)

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
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
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
    "reddit.com", "twitter.com", "x.com", "facebook.com",
    "youtube.com", "tiktok.com", "instagram.com",
    "linkedin.com", "medium.com",
)

SKIP_URL_PATTERNS: Tuple[str, ...] = (
    r"\.pdf$", r"\.jpg$", r"\.png$", r"\.gif$",
    r"/login", r"/signin", r"/signup", r"/cart", r"/checkout",
    r"amazon\.com/.*/(dp|gp)/", r"ebay\.com/itm/",
    r"/tag/", r"/tags/", r"/category/", r"/categories/",
    r"/topic/", r"/topics/", r"/archive/", r"/page/\d+",
    r"/shop/", r"/store/", r"/buy/", r"/product/", r"/products/",
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

# Navigation text patterns to skip (checked with startswith after lowercasing)
NAVIGATION_PATTERNS: Tuple[str, ...] = (
    "skip to",
    "jump to",
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
RE_STRIP_TAGS = re.compile(r"<(script|style|nav|footer|header|aside|noscript)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
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

# w3m availability (checked once at import)
W3M_PATH = shutil.which("w3m")

# =============================================================================
# REQUIRED DEPENDENCIES (managed by uv)
# =============================================================================

from scrapling.fetchers import AsyncFetcher, StealthyFetcher
from ddgs import DDGS

# Scrapling adds its own StreamHandler at INFO — remove it post-import
_scrapling_logger = logging.getLogger("scrapling")
_scrapling_logger.handlers.clear()
_scrapling_logger.setLevel(logging.CRITICAL)

# Errors that warrant a stealth retry (browser-based fetch)
STEALTH_RETRY_ERRORS = {"HTTP 403", "HTTP 429", "CAPTCHA/blocked"}

# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class ResearchConfig:
    """Configuration for research workflow."""
    query: str
    fetch_count: int = 0
    max_content_length: int = 5000
    timeout: int = 20
    quiet: bool = False
    min_content_length: int = 600
    max_concurrent: int = 50  # Match default search count
    search_results: int = 50
    stream: bool = False
    no_stealth: bool = False


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


def is_navigation_line(line: str) -> bool:
    """Check if line is navigation text that should be skipped."""
    line_lower = line.lower()
    return any(line_lower.startswith(pattern) for pattern in NAVIGATION_PATTERNS)


_RE_BOILERPLATE = re.compile(
    r"<(script|style|nav|footer|header|aside|noscript|iframe|svg|form)[^>]*>.*?</\1>",
    re.DOTALL | re.IGNORECASE
)
# Common sidebar/menu class patterns
_RE_NAV_DIVS = re.compile(
    r'<div[^>]+(?:class|id)\s*=\s*"[^"]*(?:sidebar|sidenav|menu|toc|breadcrumb|nav-|topbar|cookie|banner|popup|modal)[^"]*"[^>]*>.*?</div>',
    re.DOTALL | re.IGNORECASE
)
# Navigation lists: <ul> containing many <li><a> (menu/sidebar pattern)
_RE_NAV_LISTS = re.compile(
    r'<ul[^>]*>((?:\s*<li[^>]*>\s*<a[^>]*>.*?</a>\s*</li>\s*){5,})</ul>',
    re.DOTALL | re.IGNORECASE
)


def _strip_boilerplate(html: str) -> Tuple[str, str]:
    """Strip boilerplate tags and extract title. Returns (cleaned_html, title)."""
    html = _RE_BOILERPLATE.sub("", html)
    html = _RE_NAV_DIVS.sub("", html)
    html = _RE_NAV_LISTS.sub("", html)
    html = RE_COMMENTS.sub("", html)

    title_match = RE_TITLE.search(html)
    raw_title = unescape(title_match.group(1).strip()) if title_match else ""
    title = re.sub(r'\s*[\|\-\u2013\u2014]\s*[^|\-\u2013\u2014]{3,50}$', '', raw_title) if raw_title else ""

    return html, title


def _extract_with_w3m(html: str) -> str:
    """Render HTML to text using w3m."""
    try:
        result = subprocess.run(
            [W3M_PATH, "-dump", "-T", "text/html", "-cols", "120", "-O", "utf-8"],
            input=html.encode("utf-8", errors="replace"),
            capture_output=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.decode("utf-8", errors="replace")
    except (subprocess.TimeoutExpired, OSError):
        pass
    return ""


def _extract_with_regex(html: str) -> str:
    """Fallback: extract text from HTML using regex."""
    html = RE_BR.sub("\n", html)
    html = RE_BLOCK_END.sub("\n\n", html)
    html = RE_LI.sub("\u2022 ", html)

    text = RE_ALL_TAGS.sub(" ", html)
    text = unescape(text)
    text = RE_SPACES.sub(" ", text)
    text = RE_LEADING_SPACE.sub("\n", text)
    return RE_MULTI_NEWLINE.sub("\n\n", text)


def extract_text(html: str) -> str:
    """Extract readable text from HTML. Uses w3m if available, regex fallback."""
    cleaned_html, title = _strip_boilerplate(html)

    if W3M_PATH:
        text = _extract_with_w3m(cleaned_html)
        if not text:
            text = _extract_with_regex(cleaned_html)
    else:
        text = _extract_with_regex(cleaned_html)

    # Filter noise from extracted text
    lines = []
    prev_line = ""
    title_seen = False

    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if is_navigation_line(line):
            continue
        # Skip lines that are mostly symbols (nav remnants)
        alnum_count = sum(1 for c in line if c.isalnum())
        if len(line) > 3 and alnum_count / len(line) < 0.3:
            continue
        if line == prev_line:
            continue
        # Skip duplicate title
        if title and not title_seen:
            line_normalized = re.sub(r'\s*[\|\-\u2013\u2014]\s*[^|\-\u2013\u2014]{3,50}$', '', line)
            if line_normalized == title:
                title_seen = True
                continue
        lines.append(line)
        prev_line = line

    text = "\n".join(lines)
    text = RE_MULTI_NEWLINE.sub("\n\n", text)
    text = text.strip()

    if title:
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
            for entity in data.get("mainEntity", [])[:5]:
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

def _create_fetch_result(
    url: str,
    content: Optional[str],
    min_length: int,
    max_length: int
) -> FetchResult:
    """Create FetchResult from content, applying length checks and truncation."""
    if content and len(content) >= min_length:
        if len(content) > max_length:
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


async def fetch_single_async(
    url: str,
    timeout: int,
    min_content_length: int,
    max_content_length: int,
    progress: Optional[ProgressReporter] = None
) -> FetchResult:
    """Fetch single URL using Scrapling's AsyncFetcher (TLS fingerprinting)."""
    t0 = time.monotonic()
    try:
        page = await AsyncFetcher.get(url, timeout=timeout, stealthy_headers=True)
        elapsed = time.monotonic() - t0

        if page.status != 200:
            if progress:
                progress.url_result(url, False, elapsed, f"HTTP {page.status}")
            return FetchResult(url=url, success=False, error=f"HTTP {page.status}")

        raw_html = page.html_content
        if len(raw_html) > MAX_CONTENT_BYTES:
            if progress:
                progress.url_result(url, False, elapsed, "Too large")
            return FetchResult(url=url, success=False, error="Too large")

        if is_blocked_content(raw_html):
            if progress:
                progress.url_result(url, False, elapsed, "CAPTCHA/blocked")
            return FetchResult(url=url, success=False, error="CAPTCHA/blocked")

        # Extract structured data (JSON-LD) — prepended to content
        structured = extract_jsonld_metadata(raw_html)

        # Primary extraction: w3m/regex
        content = extract_text(raw_html)

        # Fallback: Scrapling's DOM parser when primary extraction is too short
        if len(content) < min_content_length:
            scrapling_content = _extract_with_scrapling_fallback(page, min_content_length)
            if scrapling_content:
                content = scrapling_content

        # Prepend structured data to content
        if structured:
            content = structured + content

        result = _create_fetch_result(url, content, min_content_length, max_content_length)
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
# STEALTH FETCHER (browser-based retry for blocked pages)
# =============================================================================

MAX_STEALTH_RETRIES = 5  # Cap to avoid slowing down the whole search

async def fetch_stealth_async(
    url: str,
    min_content_length: int,
    max_content_length: int,
    progress: Optional[ProgressReporter] = None
) -> FetchResult:
    """Fetch a single URL using StealthyFetcher (headless browser with anti-bot bypass)."""
    t0 = time.monotonic()
    try:
        page = await StealthyFetcher.async_fetch(url, headless=True, network_idle=True)
        elapsed = time.monotonic() - t0

        if page.status != 200:
            if progress:
                progress.url_result(url, False, elapsed, f"Stealth HTTP {page.status}")
            return FetchResult(url=url, success=False, error=f"Stealth HTTP {page.status}", source="stealth")

        raw_html = page.html_content
        if is_blocked_content(raw_html):
            if progress:
                progress.url_result(url, False, elapsed, "Stealth still blocked")
            return FetchResult(url=url, success=False, error="Stealth still blocked", source="stealth")

        structured = extract_jsonld_metadata(raw_html)
        content = extract_text(raw_html)
        if len(content) < min_content_length:
            scrapling_content = _extract_with_scrapling_fallback(page, min_content_length)
            if scrapling_content:
                content = scrapling_content
        if structured:
            content = structured + content

        result = _create_fetch_result(url, content, min_content_length, max_content_length)
        result.source = "stealth"
        if progress:
            progress.url_result(url, result.success, elapsed, result.error or "")
        return result

    except Exception as e:
        elapsed = time.monotonic() - t0
        error_msg = str(e)[:50] if str(e) else type(e).__name__
        if progress:
            progress.url_result(url, False, elapsed, f"Stealth: {error_msg}")
        return FetchResult(url=url, success=False, error=f"Stealth: {error_msg}", source="stealth")


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


class BraveSearch:
    """Brave Search API backend."""

    def __init__(self, api_key: str):
        self.api_key = api_key

    def search(
        self,
        query: str,
        num_results: int = 20,
    ) -> Iterator[Tuple[str, str]]:
        """Search Brave and yield (url, title) tuples."""
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
                data = json.loads(resp.read().decode())
            for r in data.get("web", {}).get("results", []):
                result_url = r.get("url", "")
                if result_url and result_url not in seen_urls and is_valid_url(result_url) and not is_blocked_url(result_url):
                    seen_urls.add(result_url)
                    yield result_url, r.get("title", "")
                    count += 1
                    if count >= num_results:
                        return
        except Exception as e:
            logger.debug(f"Brave search failed: {e}")
            return


class DuckDuckGoSearch:
    """DuckDuckGo search with early URL filtering."""

    def search(
        self,
        query: str,
        num_results: int = 50,
    ) -> Iterator[Tuple[str, str]]:
        """
        Search DuckDuckGo and yield (url, title) tuples.
        Filters blocked URLs during iteration.
        """
        seen_urls: Set[str] = set()
        count = 0

        ddg = DDGS(verify=False)
        for r in ddg.text(query, max_results=num_results * 2):
            url = r.get("href", "")
            if url and url not in seen_urls and is_valid_url(url) and not is_blocked_url(url):
                seen_urls.add(url)
                yield url, r.get("title", "")
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
    ) -> Iterator[Tuple[str, str]]:
        """Search DDG first. If under target, supplement with Brave."""
        seen_urls: Set[str] = set()
        count = 0

        # Phase 1: DuckDuckGo (primary)
        ddg = DuckDuckGoSearch()
        for url, title in ddg.search(query, num_results):
            if url not in seen_urls:
                seen_urls.add(url)
                yield url, title
                count += 1

        # Phase 2: Brave (supplement if DDG fell short)
        shortfall = num_results - count
        if shortfall > 0 and self._brave_key:
            brave = BraveSearch(self._brave_key)
            for url, title in brave.search(query, shortfall + 5):  # request extra to account for dupes
                if url not in seen_urls:
                    seen_urls.add(url)
                    yield url, title
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
    progress: ProgressReporter
) -> AsyncIterator[FetchResult]:
    """
    Async streaming research workflow.
    Yields FetchResult objects as they complete.
    """
    progress.message(f'Researching: "{config.query}"')

    urls: List[str] = []
    fetch_queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
    result_queue: asyncio.Queue[Optional[FetchResult]] = asyncio.Queue()
    stats = ResearchStats(query=config.query)
    search_source = ""

    async def search_producer() -> None:
        nonlocal search_source
        loop = asyncio.get_event_loop()
        searcher = MultiSearch()
        t0 = time.monotonic()

        ddg_count = 0
        brave_count = 0

        def search_and_stream():
            nonlocal ddg_count, brave_count
            prev_count = 0
            for url, title in searcher.search(config.query, config.search_results):
                urls.append(url)
                stats.urls_searched = len(urls)
                loop.call_soon_threadsafe(fetch_queue.put_nowait, url)

            # Count sources (DDG fills first, Brave supplements)
            # We can't easily distinguish here, but MultiSearch logs internally
            ddg_count = len(urls)

        with ThreadPoolExecutor(max_workers=1) as executor:
            await loop.run_in_executor(executor, search_and_stream)

        search_elapsed = time.monotonic() - t0
        source_info = f"{stats.urls_searched} URLs"
        if searcher._brave_key:
            source_info += " (DDG+Brave)"
        else:
            source_info += " (DDG)"
        progress.message(f"  [search] {source_info} in {search_elapsed:.1f}s")
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
                    progress=progress
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
    stealth_candidates: List[FetchResult] = []
    while True:
        result = await result_queue.get()
        if result is None:
            break
        fetched += 1
        if result.success:
            stats.urls_fetched += 1
            stats.content_chars += len(result.content)
        elif result.error in STEALTH_RETRY_ERRORS:
            stealth_candidates.append(result)
        progress.update("fetch", fetched, stats.urls_searched or fetched)
        yield result

    progress.newline()
    progress.summary(stats.urls_fetched, stats.urls_searched, stats.content_chars)

    # Phase 2: Stealth retry for blocked/403/CAPTCHA pages
    if stealth_candidates and not config.no_stealth:
        retry_urls = [r.url for r in stealth_candidates[:MAX_STEALTH_RETRIES]]
        progress.message(f"  [stealth] retrying {len(retry_urls)} blocked URLs...")
        # Reset counters for stealth phase
        progress._ok_count = 0
        progress._failures = []
        progress.phase_start("stealth")

        stealth_ok = 0
        for i, url in enumerate(retry_urls):
            result = await fetch_stealth_async(
                url, config.min_content_length, config.max_content_length,
                progress=progress
            )
            progress.update("stealth", i + 1, len(retry_urls))
            if result.success:
                stealth_ok += 1
                stats.urls_fetched += 1
                stats.content_chars += len(result.content)
            yield result

        progress.newline()
        progress.message(f"  [stealth] {stealth_ok}/{len(retry_urls)} recovered")


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
            async for result in run_research_async(config, progress):
                if result.success:
                    print(format_result_raw(result))
        asyncio.run(stream_async())
        return None

    # Batch mode: collect all results, then format
    results: List[FetchResult] = []

    async def collect_async():
        async for result in run_research_async(config, progress):
            results.append(result)
    asyncio.run(collect_async())

    return results


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Web Research Tool - Search + Fetch with TLS fingerprinting (Scrapling)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python web_research.py "Mac Studio M3 Ultra LLM performance"
  python web_research.py "AI trends 2025" --fetch 50
  python web_research.py "Python best practices" -o markdown
  python web_research.py "query" --stream  # Stream output as results arrive

Search: DDG primary + Brave fallback (set BRAVE_API_KEY env var or ~/.config/brave/api_key)
Fetch: Scrapling AsyncFetcher (TLS fingerprinting) + StealthyFetcher retry
Extract: w3m > regex > Scrapling DOM parser (tiered fallback)
Blocked domains: reddit, twitter, facebook, youtube, tiktok, instagram, linkedin, medium
        """
    )

    parser.add_argument("query", help="Search query")
    parser.add_argument("-s", "--search", type=int, default=20,
                        help="Number of search results (default: 20)")
    parser.add_argument("-f", "--fetch", type=int, default=0,
                        help="Max pages to fetch (default: 0 = fetch ALL)")
    parser.add_argument("-m", "--max-length", type=int, default=4000,
                        help="Max content length per page (default: 4000)")
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
    parser.add_argument("--no-stealth", action="store_true",
                        help="Disable stealth browser retry for blocked pages")

    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    config = ResearchConfig(
        query=args.query,
        fetch_count=args.fetch,
        max_content_length=args.max_length,
        timeout=args.timeout,
        quiet=args.quiet,
        max_concurrent=args.concurrent,
        search_results=args.search,
        stream=args.stream,
        no_stealth=args.no_stealth,
    )

    try:
        if args.stream:
            run_research(config, verbose=args.verbose)
        else:
            results = run_research(config, verbose=args.verbose)
            if results:
                if args.output == "json":
                    print(format_batch_json(results, config.query))
                elif args.output == "markdown":
                    print(format_batch_markdown(results, config.query, config.max_content_length))
                else:
                    print(format_batch_raw(results))

    except KeyboardInterrupt:
        print("\nInterrupted", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        logger.exception(f"Research failed: {e}")
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
