## Web Research

**Search freely.** With Gemini summarization enabled, each search costs ~500 tokens (~2K chars). Do 10-20+ searches per session without hesitation. Search whenever curious, don't ration.

For any internet search:

1. Run `./.claude/tools/web_search.sh "query"` — searches, fetches, and summarizes via Gemini Flash (default)
2. Use `-s N` for result count, `-f N` for fetch limit, `-v` for per-URL timing, `--no-summarize` for raw output
3. Summarization is on by default (~10-20x compression, preserves all technical details). Requires `GEMINI_API_KEY` env var; falls back to raw output if unset

**Note**: Always use forward slashes (`/`) in paths, even on Windows.
Dependencies handled automatically via uv.

### Search tiers
- **DDG** primary + **Brave** fallback (set `BRAVE_API_KEY` env var or `~/.config/brave/api_key`)
- **Snippet pre-filter**: skips URLs with zero query word overlap in snippet+title
- **Scrapling AsyncFetcher** for fast TLS-fingerprinted fetching (bypasses 403s)
- **StealthyFetcher** auto-retry for blocked/CAPTCHA pages (disable with `--no-stealth`)
- **Text extraction**: Trafilatura (content-area detection, boilerplate removal) > regex fallback
- **Summarization** (default): Gemini Flash API summarizes results (~10x compression). Disable with `--no-summarize`
- **BM25 compression** (legacy, not recommended): `-g N` enables sentence-level compression. Hurts factual accuracy
