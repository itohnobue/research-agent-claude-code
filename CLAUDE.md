## Web Research

For any internet search:

1. Run `./.claude/tools/web_search.sh "query"` for deep coverage (50+ results)
2. Use `-s N` for result count, `-f N` for fetch limit, `-v` for per-URL timing
3. Synthesize results into a report

**Note**: Always use forward slashes (`/`) in paths, even on Windows.
Dependencies handled automatically via uv.

### Search tiers
- **DDG** primary + **Brave** fallback (set `BRAVE_API_KEY` env var or `~/.config/brave/api_key`)
- **Scrapling AsyncFetcher** for fast TLS-fingerprinted fetching (bypasses 403s)
- **StealthyFetcher** auto-retry for blocked/CAPTCHA pages (disable with `--no-stealth`)
- **Text extraction**: w3m > regex > Scrapling DOM parser (tiered fallback)
