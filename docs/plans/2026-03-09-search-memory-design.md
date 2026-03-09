# Search Memory + Source Authority

## Problem
Every web search starts from scratch. We re-fetch the same authoritative pages, pay the same latency, and have no recall of prior research. Meanwhile, junk domains pollute results while well-known sources get equal weight.

## Solution
SQLite cache at `~/.web-research/cache.db` with vector embeddings (nomic-embed-text via Ollama). Two subsystems:

1. **Search Memory** — cache fetched pages with embeddings. On new query, check cache first. Return cached results that are semantically similar, fresh enough, and authoritative.
2. **Source Authority** — hardcoded domain tier map (~100 domains, 0.0-1.0 scores). Used in both cache ranking and fresh result ordering.

## Schema
```sql
CREATE TABLE pages (
    url TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    title TEXT,
    content TEXT NOT NULL,
    query TEXT NOT NULL,        -- original query that fetched this
    embedding BLOB,             -- 768-dim float32 from nomic-embed-text
    authority REAL DEFAULT 0.5, -- from domain tier map
    fetched_at REAL NOT NULL,   -- time.time()
    content_hash TEXT NOT NULL   -- sha256 of content, for dedup
);
CREATE INDEX idx_domain ON pages(domain);
CREATE INDEX idx_fetched ON pages(fetched_at);
```

## Domain Authority Map (~100 domains)

Tiers:
- **1.0** (gold standard): Official docs, specs, standards bodies
- **0.9** (very high): Top-tier tech news, research, major platforms
- **0.8** (high): Well-known tech blogs, established companies
- **0.7** (good): Quality community sources, niche experts
- **0.6** (decent): Forums, aggregators, general news
- **0.5** (neutral): Unknown domains (default)
- **0.3** (low): Content farms, SEO-heavy sites
- **0.1** (very low): Known spam/clickbait

Domains:
```
# 1.0 — Official docs & standards
docs.python.org, docs.rs, doc.rust-lang.org, go.dev, developer.mozilla.org,
developer.apple.com, developer.android.com, learn.microsoft.com, cloud.google.com,
docs.aws.amazon.com, kubernetes.io, nodejs.org, react.dev, nextjs.org,
w3.org, rfc-editor.org, ietf.org, tc39.es, ecma-international.org

# 0.9 — Research & top-tier
arxiv.org, github.com, stackoverflow.com, huggingface.co,
arstechnica.com, lwn.net, acm.org, ieee.org, nature.com, science.org,
openai.com, anthropic.com, deepmind.google, ai.meta.com,
blog.google, engineering.fb.com, netflixtechblog.com,
aws.amazon.com/blogs, azure.microsoft.com/blog

# 0.8 — High quality tech
hackernews (news.ycombinator.com), lobste.rs,
martinfowler.com, joelonsoftware.com, paulgraham.com,
simonwillison.net, julia-evans.com, jvns.ca, danluu.com,
brooker.co.za, brandur.org, fasterthanli.me,
web.dev, css-tricks.com, smashingmagazine.com,
infoq.com, thenewstack.io, theregister.com,
postgres.org, sqlite.org, redis.io,
pypi.org, npmjs.com, crates.io, pkg.go.dev,
vercel.com, cloudflare.com, fastly.com, fly.io,
stripe.com, twilio.com, auth0.com,
hashicorp.com, terraform.io, docker.com

# 0.7 — Good quality
dev.to, medium.com, substack.com,
realpython.com, learnxinyminutes.com, freecodecamp.org,
digitalocean.com/community, linode.com/docs,
baeldung.com, geeksforgeeks.org,
techcrunch.com, wired.com, theverge.com,
bloomberg.com/technology, reuters.com/technology,
reddit.com, wikipedia.org

# 0.6 — Decent / forums
superuser.com, askubuntu.com, serverfault.com,
unix.stackexchange.com, apple.stackexchange.com,
discourse.org hosted forums,
news.ycombinator.com (comments),
slashdot.org, lobsters comments

# 0.3 — Low quality
w3schools.com, tutorialspoint.com, javatpoint.com,
programiz.com, guru99.com,
makeuseof.com, howtogeek.com (non-tech),
about.com, ehow.com, wikihow.com (tech)

# 0.1 — Spam / clickbait
contentfarms, SEO doorway pages
```

## Cache Lookup Flow
1. Embed query via Ollama nomic-embed-text (768-dim)
2. Load all page embeddings from SQLite
3. Compute cosine similarity
4. Filter: similarity > 0.75 AND age < 7 days
5. Rank by: `similarity * 0.6 + authority * 0.3 + freshness * 0.1`
   - freshness = max(0, 1 - age_hours / 168)
6. Return top-N cached results as FetchResult objects

## Cache Write Path
After fetch results return, async-write each successful page:
- Embed content title+snippet via Ollama
- Compute content_hash (sha256)
- INSERT OR REPLACE into pages table
- Look up domain authority from tier map

## CLI Integration
- `--no-cache` — skip cache lookup and write
- `--cache-only` — only return cached results, no web fetch
- `--cache-stats` — print cache statistics

## Freshness Rules
- Default max age: 7 days
- `--max-age N` flag for custom (hours)
- Cache writes always happen (even if cache lookup returned hits — fresher data for next time)

## Performance
- Embedding: ~50ms per query via Ollama local
- SQLite lookup: <10ms for thousands of rows
- Total cache overhead: <100ms (negligible vs 15-20s web search)

## Outcome
- Repeated/similar queries return instantly from cache
- Authoritative sources rank higher in both cached and fresh results
- Cache grows organically — no maintenance needed
- Domain authority is transparent and auditable (hardcoded, not ML)
