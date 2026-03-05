#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx[http2]", "ddgs"]
# ///
# -*- coding: utf-8 -*-
"""
Web Research Tool - Autonomous Search + Fetch + Report

Unified tool combining search and fetch into a single optimized workflow:
1. Search via DuckDuckGo (50 results by default)
2. Filter and deduplicate URLs during search (early filtering)
3. Fetch content in parallel with HTTP/2 connection reuse
4. Output combined results (streaming or batched)

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
import random
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from html import unescape
from io import StringIO
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

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s: %(message)s",
    stream=sys.stderr
)
logger = logging.getLogger(__name__)

# =============================================================================
# CONSTANTS
# =============================================================================

USER_AGENTS: Tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15",
)

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

import httpx
from ddgs import DDGS

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


@dataclass
class FetchResult:
    """Single fetch result."""
    url: str
    success: bool
    content: str = ""
    title: str = ""
    error: Optional[str] = None
    source: str = "direct"


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
            rate_indicator = " ⚠ LOW"
        elif rate < 70:
            rate_indicator = " ↓"
        print(f"  Done: {fetched_ok}/{total} ok ({rate:.0f}%{rate_indicator}) — {chars:,} chars in {total_elapsed:.1f}s", file=sys.stderr)

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
# SSL CONTEXT
# =============================================================================

_SSL_CONTEXT: Optional[ssl.SSLContext] = None

def get_ssl_context() -> ssl.SSLContext:
    """Get or create reusable SSL context (verification disabled for reliability)."""
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        _SSL_CONTEXT = ssl.create_default_context()
        _SSL_CONTEXT.check_hostname = False
        _SSL_CONTEXT.verify_mode = ssl.CERT_NONE
    return _SSL_CONTEXT


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
    title = re.sub(r'\s*[\|\-–—]\s*[^|\-–—]{3,50}$', '', raw_title) if raw_title else ""

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
    html = RE_LI.sub("• ", html)

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
            line_normalized = re.sub(r'\s*[\|\-–—]\s*[^|\-–—]{3,50}$', '', line)
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


def get_random_user_agent() -> str:
    """Get a random user agent string."""
    return random.choice(USER_AGENTS)


# =============================================================================
# URL FETCHER
# =============================================================================

def _create_fetch_result(
    url: str,
    content: Optional[str],
    source: str,
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
            source=source
        )
    return FetchResult(url=url, success=False, error="Content too short or empty")


MAX_CONTENT_BYTES = 2_000_000  # 2MB max content size


async def fetch_single_async(
    client: httpx.AsyncClient,
    url: str,
    timeout: int,
    min_content_length: int,
    max_content_length: int,
    user_agent: str = "",
    progress: Optional[ProgressReporter] = None
) -> FetchResult:
    """Fetch single URL (async)."""
    t0 = time.monotonic()
    try:
        resp = await client.get(
            url,
            headers={
                "User-Agent": user_agent or get_random_user_agent(),
                "Accept": "text/html,application/xhtml+xml",
            },
            timeout=timeout,
            follow_redirects=True
        )
        elapsed = time.monotonic() - t0
        if resp.status_code == 200:
            content_length = resp.headers.get('content-length')
            if content_length and int(content_length) > MAX_CONTENT_BYTES:
                if progress:
                    progress.url_result(url, False, elapsed, "Too large")
                return FetchResult(url=url, success=False, error="Content too large")

            raw_text = resp.text
            if is_blocked_content(raw_text):
                if progress:
                    progress.url_result(url, False, elapsed, "CAPTCHA/blocked")
                return FetchResult(url=url, success=False, error="CAPTCHA/blocked")

            content = extract_text(raw_text)
            if len(content) >= min_content_length:
                result = _create_fetch_result(url, content, "direct", min_content_length, max_content_length)
                if progress:
                    progress.url_result(url, True, elapsed)
                return result
            if progress:
                progress.url_result(url, False, elapsed, "Too short")
            return FetchResult(url=url, success=False, error="Content too short")
        else:
            if progress:
                progress.url_result(url, False, elapsed, f"HTTP {resp.status_code}")
            return FetchResult(url=url, success=False, error=f"HTTP {resp.status_code}")
    except httpx.TimeoutException:
        elapsed = time.monotonic() - t0
        if progress:
            progress.url_result(url, False, elapsed, "Timeout")
        return FetchResult(url=url, success=False, error="Timeout")
    except httpx.RequestError as e:
        elapsed = time.monotonic() - t0
        logger.debug(f"Request error for {url}: {e}")
        if progress:
            progress.url_result(url, False, elapsed, "Request error")
        return FetchResult(url=url, success=False, error="Request error")
    except httpx.HTTPStatusError as e:
        elapsed = time.monotonic() - t0
        logger.debug(f"HTTP status error for {url}: {e}")
        if progress:
            progress.url_result(url, False, elapsed, f"HTTP {e.response.status_code}")
        return FetchResult(url=url, success=False, error=f"HTTP {e.response.status_code}")


# =============================================================================
# DUCKDUCKGO SEARCH
# =============================================================================

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
    search_elapsed: float = 0

    async def search_producer() -> None:
        nonlocal search_elapsed
        loop = asyncio.get_event_loop()
        ddg = DuckDuckGoSearch()
        t0 = time.monotonic()

        def search_and_stream():
            for url, title in ddg.search(config.query, config.search_results):
                urls.append(url)
                stats.urls_searched = len(urls)
                loop.call_soon_threadsafe(fetch_queue.put_nowait, url)

        with ThreadPoolExecutor(max_workers=1) as executor:
            await loop.run_in_executor(executor, search_and_stream)

        search_elapsed = time.monotonic() - t0
        progress.message(f"  [search] {stats.urls_searched} URLs in {search_elapsed:.1f}s")
        await fetch_queue.put(None)

    async def fetch_consumer(client: httpx.AsyncClient) -> None:
        semaphore = asyncio.Semaphore(config.max_concurrent)
        pending: List[asyncio.Task] = []
        fetch_limit = config.fetch_count
        session_ua = get_random_user_agent()

        async def fetch_one(url: str) -> None:
            async with semaphore:
                result = await fetch_single_async(
                    client, url, config.timeout,
                    config.min_content_length, config.max_content_length,
                    user_agent=session_ua,
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
    async with httpx.AsyncClient(
        verify=False,
        http2=True,
        limits=httpx.Limits(
            max_connections=config.max_concurrent,
            max_keepalive_connections=config.max_concurrent,
            keepalive_expiry=30.0
        ),
        timeout=httpx.Timeout(config.timeout, connect=5.0)
    ) as client:
        asyncio.create_task(search_producer())
        asyncio.create_task(fetch_consumer(client))

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
        description="Web Research Tool - Autonomous Search + Fetch",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python web_research.py "Mac Studio M3 Ultra LLM performance"
  python web_research.py "AI trends 2025" --fetch 50
  python web_research.py "Python best practices" -o markdown
  python web_research.py "query" --stream  # Stream output as results arrive

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
