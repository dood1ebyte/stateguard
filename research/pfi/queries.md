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

**Next passes (S7):** MCP clients and agent frameworks on GitHub, then Reddit, Hacker News and
Indie Hackers, one search pass each (PHASE2_PLAN.md section 10). Each new query gets an id here
before it is run.
