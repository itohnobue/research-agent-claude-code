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
- **Anti-Bot Bypass**: Scrapling with TLS fingerprinting — passes where httpx gets 403'd. Auto-fallback to httpx for domains where curl_cffi DNS fails
- **Smart Extraction**: Trafilatura content-area detection (article body, not nav/sidebar noise)
- **PDF Extraction**: Automatic pdftotext (poppler) extraction for PDF content
- **Token Compression**: Sentence-level BM25 + centrality scoring keeps the most relevant sentences within budget
- **Cross-Page Dedup**: Removes duplicate sentences across pages so later results only add new information
- **Bonus Sources**: Supplements web results with DDG News + Reddit discussions (searched in parallel). Scientific (`--sci`), medical (`--med`), and tech (`--tech`) flags enable domain-specific sources
- **Non-English Support**: Auto-detects query language (Japanese, Chinese, Korean, Russian, Arabic, Thai) and sets DDG region for better results
- **Observable**: Per-phase timing, failure breakdown, slow URL identification
- **Zero Setup**: Auto-installs dependencies via uv

## Requirements

- **uv**: Auto-installed by wrapper scripts
- **Python 3.11+**: Auto-installed by uv if needed
- **pdftotext** (optional): From [poppler](https://poppler.freedesktop.org/) — enables PDF text extraction. Without it, PDF pages are skipped

## Blocked Domains

Automatically filtered:

**No usable content**: facebook.com, tiktok.com, instagram.com, linkedin.com, youtube.com, msn.com

**Consistently HTTP 403** (paywalled/anti-scraper): forbes.com, nytimes.com, edmunds.com, cars.com, nejm.org, cell.com, sciencedirect.com, onlinelibrary.wiley.com, dl.acm.org, zenodo.org, percona.com, mctlaw.com, amjmed.com

## API-Routed Domains

These websites are used via APIs:

| Domain | Method | Content |
|---|---|---|
| twitter.com, x.com | FxTwitter API | Tweet text, author, metrics |
| reddit.com | DDG snippet injection | Discussion titles + summaries |
| en.wikipedia.org | MediaWiki API | Article text (no citation noise) |
| github.com | GitHub REST API | README rendered to text |
| arxiv.org | ArXiv Atom API | Paper metadata + abstract |
| semanticscholar.org | Semantic Scholar API | Paper metadata + abstract + citations |
| europepmc.org | Europe PMC REST API | Paper metadata + OA full-text links |

## Domain-Specific Search

Use `--sci`, `--med`, and/or `--tech` flags to enable domain-specific bonus sources:

| Flag | Sources | Best for |
|------|---------|----------|
| `--sci` | arXiv + OpenAlex | CS, physics, math, engineering, materials science |
| `--med` | PubMed + Europe PMC + OpenAlex | Medicine, clinical trials, pharmacology, biomedical |
| `--tech` | Hacker News + Stack Overflow + Dev.to + GitHub | Software development, DevOps, IT, startups |
| `--sci --med` | All academic sources | Interdisciplinary (bioinformatics, medical imaging AI) |

**Academic Sources (`--sci`, `--med`):**

- **arXiv API**: Preprint papers (5 results, sorted by relevance). Falls back to Semantic Scholar if arXiv returns nothing
- **OpenAlex**: 250M+ works across all disciplines — covers IEEE, ACM, Springer, Elsevier (5 results, prefers OA URLs)
- **PubMed**: NCBI E-utilities for biomedical literature (5 results)
- **Europe PMC**: European PubMed Central — broader OA full-text coverage than PubMed (5 results, sorted by citations)

**Tech Sources (`--tech`):**

- **Hacker News**: Algolia search API — top stories by relevance (5 results)
- **Stack Overflow**: Stack Exchange API — Q&A with code snippets (5 results, sorted by relevance)
- **Dev.to**: Forem API — developer blog articles and tutorials (5 results)
- **GitHub**: Repository search API — top repos by stars (5 results)

All APIs are free with no keys required.

Paywalled pages automatically fall back to Wayback Machine cached versions.

## License

MIT
