## Web Research

For any internet search:

1. Read agent instructions: `.claude/agents/web-searcher.md`
2. Use internal web search for quick results
3. Run `./.claude/tools/web_search.sh "query"` (or `.claude/tools/web_search.bat` on Windows) for deep coverage
4. Synthesize results into a report

**Note**: Always use forward slashes (`/`) in paths for agent tool run, even on Windows.
Dependencies handled automatically via uv.
