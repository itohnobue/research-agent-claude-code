# Web Search Agent

A web search agent for Claude Code (or any LLM) that processes 50+ results per search — far beyond the typical 10-20 limit.

## Quick Start

1. **Copy files**: Put `.claude/` folder into your Claude Code working directory
2. **Add instructions**: Copy `CLAUDE.md` contents into your project's instruction file
3. **Test it**: Ask Claude to search the web, e.g., *"Search for most beautiful Hokusai paintings and explain why they're great"*

The wrapper scripts auto-install **uv**, which handles Python and dependencies.

## Why You Need This

Most LLM tools (including Claude Code) only use 10-20 search results, limiting research depth.

This agent uses DuckDuckGo to fetch and process 50+ pages per query — similar to Qwen's Search function but works with any LLM.

**Best for**: Solving tricky bugs, tech research, any task where more information means better answers.

## Features

- **Deep Search**: 50+ results via DuckDuckGo + Brave Search fallback
- **Anti-Bot Bypass**: Scrapling with TLS fingerprinting — passes where httpx gets 403'd
- **Smart Extraction**: Trafilatura content-area detection (article body, not nav/sidebar noise)
- **Token Compression**: Sentence-level BM25 + centrality scoring keeps the most relevant sentences within budget
- **Cross-Page Dedup**: Removes duplicate sentences across pages so later results only add new information
- **Bonus Sources**: Supplements web results with DDG News + Reddit discussions (searched in parallel)
- **Observable**: Per-phase timing, failure breakdown, slow URL identification
- **Zero Setup**: Auto-installs dependencies via uv

## Requirements

- **uv**: Auto-installed by wrapper scripts
- **Python 3.11+**: Auto-installed by uv if needed

## Blocked Domains

Automatically filtered (no usable text content):
facebook.com, tiktok.com, instagram.com, linkedin.com, youtube.com

## API-Routed Domains

These websites are used via APIs:

| Domain | Method | Content |
|---|---|---|
| twitter.com, x.com | FxTwitter API | Tweet text, author, metrics |
| reddit.com | Reddit JSON API | Post + top comments |
| en.wikipedia.org | MediaWiki API | Article text (no citation noise) |
| github.com | GitHub REST API | README rendered to text |
| arxiv.org | ArXiv Atom API | Paper metadata + abstract |

Paywalled pages automatically fall back to Wayback Machine cached versions.

## License

MIT
