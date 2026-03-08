# Web Search Agent

A web search agent for Claude Code (or any LLM) that processes 50+ results per search — far beyond the typical 10-20 limit.

## Quick Start

1. **Copy files**: Put `.claude/` folder into your Claude Code working directory
2. **Add instructions**: Copy `CLAUDE.md` contents into your project's instruction file
3. **Test it**: Ask Claude to search the web, e.g., *"Search for most beautiful Hokusai paintings and explain why they're great"*

Wrapper scripts: `web_search.sh` (Linux/macOS) and `web_search.bat` (Windows). They auto-install **uv**, which handles Python and dependencies.

## Why You Need This

Most LLM tools (including Claude Code) only use 10-20 search results, limiting research depth.

This agent uses DuckDuckGo + Brave to fetch and process 50+ pages per query — similar to Qwen's Search function but works with any LLM.

**Best for**: Solving tricky bugs, tech research, any task where more information means better answers.

## Features

- **Deep Search**: 50+ results via [DuckDuckGo Search](https://github.com/deedy5/duckduckgo_search) + Brave Search fallback
- **Anti-Bot Bypass**: [Scrapling](https://github.com/D4Vinci/Scrapling) with TLS fingerprinting ([curl-cffi](https://github.com/lexiforest/curl_cffi)) — passes where httpx gets 403'd
- **Stealth Retry**: Blocked pages auto-retry via headless browser ([Camoufox](https://github.com/daijro/camoufox), max 5 retries)
- **Smart Extraction**: [Trafilatura](https://github.com/adbar/trafilatura) content-area detection (article body, not nav/sidebar noise)
- **Token-Efficient Compression**: Sentence-level BM25 ([rank-bm25](https://github.com/dorianbrown/rank_bm25)) + centrality scoring keeps the most relevant and important sentences within budget
- **Cross-Page Dedup**: Removes duplicate sentences across pages so later results only add new information
- **Bonus Sources**: Supplements web results with DDG News + Reddit discussions (searched in parallel)
- **Snippet Pre-Filter**: Scores search snippets by query relevance, skips irrelevant URLs before fetching
- **Observable**: Per-phase timing, failure breakdown, slow URL identification
- **Zero Setup**: Auto-installs dependencies via uv

## Requirements

- **[uv](https://github.com/astral-sh/uv)**: Auto-installed by wrapper scripts
- **Python 3.11+**: Auto-installed by uv if needed

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

Default output (stderr) shows a live progress line and summary:

```
Researching: "Python asyncio tutorial"
  [search] 10 URLs (DDG+Brave) in 1.7s
    fetch: 10/10 (9 ok, 2s)
  Done: 9/10 ok (90%) -- 35,567 chars in 2.6s
  Skipped: 1 HTTP 403
```

With `-v` (verbose), each URL prints its own status line instead of the progress counter:

```
Researching: "Python asyncio tutorial"
  [search] 10 URLs (DDG+Brave) in 1.7s
    --   0.2s  python.plainenglish.io (HTTP 403)
    OK   0.4s  blog.apify.com
    OK   0.5s  docs.python.org
    OK   1.6s  www.lambdatest.com
  Done: 9/10 ok (90%) -- 35,567 chars in 2.6s
  Skipped: 1 HTTP 403
```

Slow URLs (>5s) are always listed in the summary, even without `-v`. Low success rates get flagged (`!` below 70%, `!! LOW` below 50%).

## Options

```
-s N          Number of search results (default: 20)
-f N          Max pages to fetch (default: 0 = all)
-m N          Max chars per page (default: 8000)
-o FORMAT     Output format: raw (default), json, markdown
-v            Verbose: show per-URL timing
-q            Quiet: suppress progress messages
-t N          Fetch timeout in seconds (default: 5)
-c N          Max concurrent connections (default: 50)
-u URL ...    Direct URL fetch (skip search)
--stream      Stream results as they arrive
--no-stealth  Disable headless browser retry for blocked pages
--usage       Show usage statistics (last 30 days)
--quality     Show usage stats with output quality analysis
```

## Multi-Query

Run multiple searches in parallel with cross-query URL deduplication:

```bash
web_search.sh "query1" "query2" "query3" -s 10
```

## Direct URL Fetch

Fetch specific URLs without searching:

```bash
web_search.sh -u https://example.com https://other.com
```

## Compression Pipeline

Each fetched page goes through a multi-stage compression pipeline before being returned to the LLM:

```
HTML → Trafilatura extraction → BM25 sentence selection → Cross-page dedup → Output
```

### Stage 1: Text Extraction (Trafilatura)

Trafilatura detects the article body and strips navigation, sidebars, ads, and boilerplate. This is the biggest compression step — raw HTML shrinks to clean article text (typically 3-7K chars per page). Falls back to regex extraction, then Scrapling DOM parser.

### Stage 2: BM25 Sentence Compression

When a page exceeds the per-page budget (`-m`, default 8K), the content is compressed using query-focused sentence selection:

1. **Sentence splitting**: Text is split into individual sentences (line breaks + sentence boundary regex)
2. **BM25 scoring** (70% weight): Each sentence is scored against the search query using Okapi BM25
3. **Centrality scoring** (30% weight): Each sentence's average Jaccard similarity to all other sentences — surfaces "hub" sentences that explain key concepts even without query terms
4. **Selection**: Top-scoring sentences are kept within the char budget, in original order

**Measured impact**: Only triggers on pages exceeding 8K chars (~20% of pages). When it does trigger, the quality improvement over paragraph-level BM25 or head truncation is real but hard to quantify numerically — it selects more relevant sentences rather than fewer chars.

### Stage 3: Cross-Page Deduplication

After all pages are compressed, duplicate sentences are removed across pages:

1. **Exact dedup**: Sentences are normalized (lowercase, strip punctuation) and hashed. Duplicates across pages are removed (earlier pages take priority)
2. **Fuzzy dedup**: Content-word signatures (stop words removed, remaining words sorted) catch paraphrased duplicates like "M4 chip features" vs "M4 processor features"

**Measured impact**: 0-12% token savings depending on topic overlap. Technical documentation (Kubernetes, AWS) shows highest savings (~12%, 80+ duplicates) because many docs copy from each other. Diverse opinion content (reviews, comparisons) shows minimal savings (~1%).

### Stage 4: Snippet Pre-Filter

Before fetching, search result snippets from DDG/Brave are scored by query word overlap. URLs with zero overlap in their snippet+title are skipped (minimum 5 URLs always fetched as safety net).

**Measured impact**: Speed optimization, not token savings. Skips 0-5 irrelevant fetches per query, reducing latency by 20-40% on queries with noisy results.

### Ablation Summary

| Feature | Token savings | Quality | Speed | Notes |
|---|---|---|---|---|
| Trafilatura extraction | ~90% vs raw HTML | High | Fast | The heavy lifter |
| 8K per-page budget | ~60% vs uncompressed | Good | Same | Most pages are already under 8K |
| Sentence BM25 + centrality | ~0% additional | Potentially better selection | Same | Only triggers on ~20% of pages |
| Cross-page exact dedup | 0-12% | Same | Same | Highest on docs/specs topics |
| Fuzzy dedup | ~0.5% on top of exact | Same | Same | Marginal |
| Snippet pre-filter | 0% | Same | 20-40% faster | Speed only |

## Blocked Domains

Automatically filtered (no usable text content):
facebook.com, tiktok.com, instagram.com, linkedin.com, youtube.com

## API-Routed Domains

These previously-blocked domains now return clean content via API extraction:

| Domain | Method | Content |
|---|---|---|
| twitter.com, x.com | [FxTwitter](https://github.com/FixTweet/FxTwitter) API | Tweet text, author, metrics |
| reddit.com | [Reddit JSON](https://www.reddit.com/dev/api/) API | Post + top comments |
| en.wikipedia.org | [MediaWiki](https://www.mediawiki.org/wiki/API:Main_page) API | Article text (no citation noise) |
| github.com | [GitHub REST](https://docs.github.com/en/rest) API | README rendered to text |
| arxiv.org | [ArXiv Atom](https://info.arxiv.org/help/api/index.html) API | Paper metadata + abstract |

Paywalled pages automatically fall back to [Wayback Machine](https://web.archive.org/) cached versions.

## Credits

Built on these excellent open-source projects:

- [Scrapling](https://github.com/D4Vinci/Scrapling) — TLS-fingerprinted fetching with anti-bot bypass
- [Trafilatura](https://github.com/adbar/trafilatura) — Content extraction and boilerplate removal
- [Camoufox](https://github.com/daijro/camoufox) — Stealth headless browser for blocked pages
- [curl-cffi](https://github.com/lexiforest/curl_cffi) — TLS fingerprinting (used by Scrapling)
- [rank-bm25](https://github.com/dorianbrown/rank_bm25) — BM25 scoring for sentence selection
- [DuckDuckGo Search](https://github.com/deedy5/duckduckgo_search) — Primary search backend
- [uv](https://github.com/astral-sh/uv) — Python package and dependency management

## License

MIT
