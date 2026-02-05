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
import random
import os
import re
import ssl
import sys
import time

# Force UTF-8 stdout/stderr on Windows to avoid cp1251/charmap encoding errors
if sys.platform == "win32" and not os.environ.get("PYTHONIOENCODING"):
    sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', errors='replace', closefd=False)
    sys.stderr = open(sys.stderr.fileno(), mode='w', encoding='utf-8', errors='replace', closefd=False)
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
    max_concurrent: int = 20  # Increased for HTTP/2 multiplexing
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
    """Unified progress reporting."""

    def __init__(self, quiet: bool = False):
        self.quiet = quiet
        self._last_line_len = 0

    def message(self, msg: str) -> None:
        """Print a message line."""
        if not self.quiet:
            print(msg, file=sys.stderr)

    def update(self, phase: str, current: int, total: int) -> None:
        """Update progress on same line."""
        if not self.quiet:
            line = f"\r    {phase.capitalize()}: {current}/{total}"
            # Clear previous content if shorter
            padding = max(0, self._last_line_len - len(line))
            print(f"{line}{' ' * padding}", end="", file=sys.stderr)
            self._last_line_len = len(line)

    def newline(self) -> None:
        """Print newline after progress updates."""
        if not self.quiet:
            print(file=sys.stderr)
            self._last_line_len = 0


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


def extract_text(html: str) -> str:
    """Extract readable text from HTML (fast version with noise filtering)."""
    html = RE_STRIP_TAGS.sub("", html)
    html = RE_COMMENTS.sub("", html)

    title_match = RE_TITLE.search(html)
    raw_title = unescape(title_match.group(1).strip()) if title_match else ""
    # Clean title: remove site name suffix (e.g., " | Site Name" or " - Site Name")
    title = re.sub(r'\s*[\|\-–—]\s*[^|\-–—]{3,50}$', '', raw_title) if raw_title else ""

    html = RE_BR.sub("\n", html)
    html = RE_BLOCK_END.sub("\n\n", html)
    html = RE_LI.sub("• ", html)

    text = RE_ALL_TAGS.sub(" ", html)
    text = unescape(text)
    text = RE_SPACES.sub(" ", text)
    text = RE_LEADING_SPACE.sub("\n", text)
    text = RE_MULTI_NEWLINE.sub("\n\n", text)

    # General noise filtering - pattern-based, not site-specific
    lines = []
    short_buffer: List[str] = []  # Buffer for consecutive short lines
    prev_line = ""
    title_seen = False  # Track if we've seen the title to remove duplicates

    for line in text.split("\n"):
        line = line.strip()

        # Skip empty lines
        if not line:
            continue

        # Skip navigation lines (Skip to content, Jump to, etc.)
        if is_navigation_line(line):
            continue

        # Skip lines that are mostly repeated punctuation/symbols (nav remnants)
        alnum_count = sum(1 for c in line if c.isalnum())
        if len(line) > 3 and alnum_count / len(line) < 0.3:
            continue

        # Skip lines with excessive repeated bullets/symbols anywhere
        bullet_count = sum(1 for c in line if c in "•·●○◦‣⁃")
        if bullet_count >= 4:
            continue

        # Skip lines that are just bullet/list markers
        stripped = line.strip("•-*·►▸▹→‣⁃● ")
        if not stripped or len(stripped) < 2:
            continue

        # Skip duplicate of previous line (common with titles)
        if line == prev_line:
            continue

        # Skip duplicate title line (appears after # Title heading)
        if title and not title_seen:
            # Check if line matches title (exact or without site suffix)
            line_normalized = re.sub(r'\s*[\|\-–—]\s*[^|\-–—]{3,50}$', '', line)
            if line_normalized == title or line == raw_title:
                title_seen = True
                continue

        # Skip very short lines that look like UI elements (1-2 words, no sentence structure)
        words = line.split()
        if len(line) < 15 and len(words) <= 2 and not line.startswith("#"):
            # Check if it's a meaningful short phrase (has lowercase = likely sentence)
            if not any(c.islower() for c in line):
                continue

        # Handle short lines - collapse consecutive ones
        if len(line) < 25 and not line.startswith("#"):
            short_buffer.append(line)
            if len(short_buffer) >= 5:
                # Join consecutive short lines
                joined = " | ".join(short_buffer)
                if len(joined) < 300:
                    lines.append(joined)
                short_buffer = []
        else:
            # Flush short buffer before adding long line
            if short_buffer:
                if len(short_buffer) <= 2:
                    lines.extend(short_buffer)
                else:
                    lines.append(" | ".join(short_buffer))
                short_buffer = []
            lines.append(line)
            prev_line = line

    # Flush remaining short buffer
    if short_buffer:
        if len(short_buffer) <= 2:
            lines.extend(short_buffer)
        else:
            lines.append(" | ".join(short_buffer))

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
    user_agent: str = ""
) -> FetchResult:
    """Fetch single URL (async)."""
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
        if resp.status_code == 200:
            # Early content-length check to skip huge pages
            content_length = resp.headers.get('content-length')
            if content_length and int(content_length) > MAX_CONTENT_BYTES:
                return FetchResult(url=url, success=False, error="Content too large")

            # Check for CAPTCHA/blocked page before extraction
            raw_text = resp.text
            if is_blocked_content(raw_text):
                return FetchResult(url=url, success=False, error="CAPTCHA/blocked")

            content = extract_text(raw_text)
            if len(content) >= min_content_length:
                return _create_fetch_result(url, content, "direct", min_content_length, max_content_length)
            return FetchResult(url=url, success=False, error="Content too short")
        else:
            return FetchResult(url=url, success=False, error=f"HTTP {resp.status_code}")
    except httpx.TimeoutException:
        return FetchResult(url=url, success=False, error="Timeout")
    except httpx.RequestError as e:
        logger.debug(f"Request error for {url}: {e}")
        return FetchResult(url=url, success=False, error="Request error")
    except httpx.HTTPStatusError as e:
        logger.debug(f"HTTP status error for {url}: {e}")
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
    progress.message("  Mode: streaming pipeline (search + fetch in parallel)")

    urls: List[str] = []
    fetch_queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
    result_queue: asyncio.Queue[Optional[FetchResult]] = asyncio.Queue()
    stats = ResearchStats(query=config.query)

    async def search_producer() -> None:
        """Search and queue URLs for fetching (streams results as they arrive)."""
        loop = asyncio.get_event_loop()
        ddg = DuckDuckGoSearch()

        def search_and_stream():
            """Run search and queue URLs immediately as found."""
            for url, title in ddg.search(config.query, config.search_results):
                urls.append(url)
                stats.urls_searched = len(urls)
                # Queue URL immediately for fetching (thread-safe)
                loop.call_soon_threadsafe(fetch_queue.put_nowait, url)

        with ThreadPoolExecutor(max_workers=1) as executor:
            await loop.run_in_executor(executor, search_and_stream)

        # Signal end of search
        await fetch_queue.put(None)

    async def fetch_consumer(client: httpx.AsyncClient) -> None:
        """Fetch URLs and queue results."""
        semaphore = asyncio.Semaphore(config.max_concurrent)
        pending: List[asyncio.Task] = []
        fetch_limit = config.fetch_count  # 0 means unlimited
        session_ua = get_random_user_agent()  # Single UA per session

        async def fetch_one(url: str) -> None:
            async with semaphore:
                result = await fetch_single_async(
                    client, url, config.timeout,
                    config.min_content_length, config.max_content_length,
                    user_agent=session_ua
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

    # HTTP/2 enabled with optimized connection pool
    async with httpx.AsyncClient(
        verify=False,
        http2=True,  # Enable HTTP/2 for multiplexing
        limits=httpx.Limits(
            max_connections=config.max_concurrent,
            max_keepalive_connections=config.max_concurrent,
            keepalive_expiry=30.0
        ),
        timeout=httpx.Timeout(config.timeout, connect=5.0)
    ) as client:
        # Start search and fetch concurrently
        asyncio.create_task(search_producer())
        asyncio.create_task(fetch_consumer(client))

        # Yield results as they arrive
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
    progress.message(f"  Done: {stats.urls_fetched}/{stats.urls_searched} pages ({stats.content_chars:,} chars)")


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

def run_research(config: ResearchConfig) -> Optional[List[FetchResult]]:
    """Execute research and output results."""
    progress = ProgressReporter(quiet=config.quiet)

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
    parser.add_argument("-s", "--search", type=int, default=50,
                        help="Number of search results (default: 50)")
    parser.add_argument("-f", "--fetch", type=int, default=0,
                        help="Max pages to fetch (default: 0 = fetch ALL)")
    parser.add_argument("-m", "--max-length", type=int, default=4000,
                        help="Max content length per page (default: 4000)")
    parser.add_argument("-o", "--output", choices=["json", "raw", "markdown"], default="raw",
                        help="Output format (default: raw)")
    parser.add_argument("-t", "--timeout", type=int, default=20,
                        help="Fetch timeout in seconds (default: 20)")
    parser.add_argument("-c", "--concurrent", type=int, default=10,
                        help="Max concurrent connections (default: 10)")
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
            # Streaming mode outputs directly
            run_research(config)
        else:
            # Batch mode
            results = run_research(config)
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
