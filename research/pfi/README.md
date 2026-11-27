# Public failure intelligence (Track B)

What developers publicly report about MCP failures, collected for **problem discovery,
prioritisation and test-case generation** (PHASE2_PLAN.md sections 10 and 11).

**Never use any of this:**
- to compute a reliability rate, a failure-reduction percentage or a sub-1% claim;
- as a denominator;
- to change the S6 protocol.
Track A (controlled experiments) and Track B meet only through a named controlled reproduction.

| File | What |
|---|---|
| `corpus.jsonl` | One record per signal (the schema in `tools/schema.py`). |
| `queries.md` | The documented search queries, by id. |
| `access_log.jsonl` | What was accessible, and what was not and why. |
| `passes/` | One log per collection pass: queries, date range, items reviewed, counts kept and rejected. |
| `tools/schema.py` | The record schema and validator: `python -m research.pfi.tools.schema corpus.jsonl`. |
| `tools/dedup.py` | Deduplication into root issues: `python -m research.pfi.tools.dedup corpus.jsonl [--write]`. |
| `tools/counts.py` | Counts of independent records (never rates). |

**Rules:**
- **Public information only.** No login. Never bypass authentication, access controls, paywalls
  or bot protection. Respect terms and rate limits. A blocked source is logged as "not
  accessible".
- **Never post, comment, react, message, or open issues or pull requests anywhere.**
- **This repository is public.** Records hold URLs, our own short paraphrases and
  classifications. No copied bulk content, and no personal data: no usernames, emails or
  profiles.
- **Unknown fields.** A field we could not establish is "not available", never a guess.
- **Classification:**
  - **confirmed technical failure:** the project labelled it a bug, and it states a concrete
    failure;
  - **likely technical failure:** a concrete failure, not labelled;
  - **feature request, speculation, opinion, unrelated:** rejected and counted, not kept.
- **Capability.** StateGuard's capability is claimed only where a Track A mechanism and its
  controlled evidence exist. Otherwise it is "cannot handle" or "unclear".
- **Not the private file.** This corpus is not the private `PUBLIC_FAILURE_EVIDENCE_RESEARCH.md`
  from before Phase 1, which stays uncommitted.
