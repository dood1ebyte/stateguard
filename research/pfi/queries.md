# Track B: documented search queries

Each query is a public GitHub issue search on one repository's issue list, read through public
web access (the page itself, no login). Only the first result page (up to 25 issues) is reviewed
in a pass unless the pass log says otherwise. Results depend on the date they are run; the pass
log records the date.

| Id | Source | Repository | Query (GitHub issue search) | Why |
|---|---|---|---|---|
| GH-1 | GitHub | modelcontextprotocol/python-sdk | `is:issue timeout created:>=2026-09-01` | hangs, deadlines, lost answers (categories F, I) |
| GH-2 | GitHub | modelcontextprotocol/python-sdk | `is:issue "connection closed" created:>=2026-09-01` | transport loss (E, I) |
| GH-3 | GitHub | modelcontextprotocol/typescript-sdk | `is:issue timeout created:>=2026-09-01` | as GH-1 |
| GH-4 | GitHub | modelcontextprotocol/typescript-sdk | `is:issue session created:>=2026-09-01` | session loss and lifecycle (D) |
| GH-5 | GitHub | modelcontextprotocol/servers | `is:issue "connection closed" created:>=2026-09-01` | as GH-2, reference servers |
| GH-6 | GitHub | modelcontextprotocol/servers | `is:issue memory created:>=2026-09-01` | server-memory (SER1's concurrency) |

URL form: `https://github.com/<repository>/issues?q=<query, URL-encoded>`.

## Other sources (written 2026-11-27 at about 10:54 IST, before any of them was run)

Each source's terms and `robots.txt` are read first, and the outcome is written to
`access_log.jsonl`. A query runs only through an official public endpoint, or a public page the
site's `robots.txt` allows, with no login, once, at a low rate. A source that cannot be read that
way is logged "not accessible", and its queries are left unrun. The date range is the same as
GitHub's: created on or after 2026-09-01 (epoch 1788220800). Only the first result page (up to
25 items) is reviewed.

| Id | Source | Where | Query | Why |
|---|---|---|---|---|
| HN-1 | Hacker News | Algolia HN Search API, stories and comments | `MCP timeout` | as GH-1 |
| HN-2 | Hacker News | as HN-1 | `MCP "connection closed"` | as GH-2 |
| HN-3 | Hacker News | as HN-1 | `MCP duplicate` | a call that ran twice (category C) |
| RD-1 | Reddit | r/mcp search, newest first | `timeout` | as GH-1 |
| RD-2 | Reddit | r/mcp search, newest first | `"connection closed"` | as GH-2 |
| RD-3 | Reddit | r/ClaudeAI search, newest first | `MCP timeout` | client-side reports |
| IH-1 | Indie Hackers | site search | `MCP` | builder reports |

URL forms:
- HN: `https://hn.algolia.com/api/v1/search_by_date?query=<query>&tags=(story,comment)&numericFilters=created_at_i>=1788220800&hitsPerPage=25`
  (the public, unauthenticated API that Hacker News links to). Item URL:
  `https://news.ycombinator.com/item?id=<id>`.
- Reddit: `https://www.reddit.com/r/<sub>/search/?q=<query>&restrict_sr=1&sort=new`.
- Indie Hackers: `https://www.indiehackers.com/search?q=<query>`.

**Next passes (S7.1):** a GitHub refresh since S5; MCP clients and agent frameworks on GitHub;
then each source above again (PHASE2_PLAN.md section 10). Each new query gets an id here before
it is run.
