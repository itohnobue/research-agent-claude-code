# Web Search Agent

A web search agent for Claude Code (or any LLM) that processes 50+ results per search — far beyond the typical 10-20 limit.

## Quick Start

1. **Copy files**: Put `.claude/` folder into your Claude Code working directory
2. **Add instructions**: Copy `CLAUDE.md` contents into your project's instruction file
3. **Test it**: Ask Claude to search the web, e.g., *"Search for most beautiful Hokusai paintings and explain why they're great"*

The wrapper scripts auto-install **uv**, which handles Python and dependencies.

## Why You Need This

Most LLM tools (including Claude Code) only use 10-20 search results, limiting research depth.

This agent uses DuckDuckGo + Brave to fetch and process 50+ pages per query — similar to Qwen's Search function but works with any LLM.

**Best for**: Solving tricky bugs, tech research, any task where more information means better answers.

## Features

- **Deep Search**: 50+ results via DDG + Brave Search fallback
- **Anti-Bot Bypass**: Scrapling with TLS fingerprinting (curl-cffi) — passes where httpx gets 403'd
- **Stealth Retry**: Blocked pages auto-retry via headless browser (Camoufox, max 5 retries)
- **Smart Extraction**: w3m > regex > Scrapling DOM parser (tiered fallback, fixes "Too short" pages)
- **Observable**: Per-phase timing, failure breakdown, slow URL identification
- **Zero Setup**: Auto-installs dependencies via uv

## Requirements

- **uv**: Auto-installed by wrapper scripts
- **Python 3.11+**: Auto-installed by uv if needed
- **w3m** (optional): Better HTML rendering (tables, lists). Falls back to regex if not installed

## Brave Search (Optional)

For better search coverage, add a [Brave Search API](https://brave.com/search/api/) key:

```bash
# Option 1: environment variable
export BRAVE_API_KEY="your-key-here"

# Option 2: config file
mkdir -p ~/.config/brave
echo "your-key-here" > ~/.config/brave/api_key
```

Without Brave, DDG is used exclusively (still works well).

## Diagnostics

Default output (stderr) shows timing at each phase:

```
Researching: "Python asyncio tutorial"
  [search] 10 URLs (DDG+Brave) in 1.7s
    fetch: 10/10 (9 ok, 2s)
  Done: 9/10 ok (35,567 chars) in 2.6s
  Skipped: 1 HTTP 403
```

With `-v` (verbose), you see every URL individually:

```
Researching: "Python asyncio tutorial"
  [search] 10 URLs (DDG+Brave) in 1.7s
    --   0.2s  python.plainenglish.io (HTTP 403)
    OK   0.4s  blog.apify.com
    OK   0.5s  docs.python.org
    OK   1.6s  www.lambdatest.com
  Done: 9/10 ok (35,567 chars) in 2.6s
  Skipped: 1 HTTP 403
```

Slow URLs (>5s) are always listed in the summary, even without `-v`.

## Options

```
-s N          Number of search results (default: 20)
-f N          Max pages to fetch (default: 10)
-m N          Max chars per page (default: 15000)
-o FORMAT     Output format: text (default), json, raw, markdown
-v            Verbose: show per-URL timing
--stream      Stream results as they arrive
--no-stealth  Disable headless browser retry for blocked pages
```

## Blocked Domains

Automatically filtered (require login or block scraping):
reddit.com, twitter.com, x.com, facebook.com, youtube.com, tiktok.com, instagram.com, linkedin.com, medium.com

## License

MIT
