## Web Research

For any internet search:

1. Run `./.claude/tools/web_search.sh "query" -g 30000` for deep coverage with compression
2. Use `-s N` for result count, `-f N` for fetch limit, `-g N` for global char budget, `-v` for per-URL timing
3. Synthesize results into a report

**Note**: Always use forward slashes (`/`) in paths, even on Windows.
Dependencies handled automatically via uv.

### Search tiers
- **DDG** primary + **Brave** fallback (set `BRAVE_API_KEY` env var or `~/.config/brave/api_key`)
- **Snippet pre-filter**: skips URLs with zero query word overlap in snippet+title
- **Scrapling AsyncFetcher** for fast TLS-fingerprinted fetching (bypasses 403s)
- **StealthyFetcher** auto-retry for blocked/CAPTCHA pages (disable with `--no-stealth`)
- **Text extraction**: Trafilatura (content-area detection, boilerplate removal) > regex fallback
- **Compression**: Sentence-level BM25 (70%) + centrality scoring (30%), cross-page sentence dedup
- **Global compression** (`-g 30000`): Cross-page BM25 keeps only the most relevant sentences within budget. Use `-g 30000` for ~2.5x compression with high signal retention
