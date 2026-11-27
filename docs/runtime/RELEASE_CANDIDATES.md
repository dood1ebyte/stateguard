# Release candidates (Phase 2 S5.3, 2026-11-27): built and verified, NOT published

**A5 (publishing) is not approved.**
- Nothing here has been uploaded to PyPI or npm.
- The npm package stays `"private": true`, so npm refuses to publish it.
- The artifacts are kept locally in `dist/rc/`, which is ignored by git.

**Built from commit `d2801cd`,** on a clean `git worktree`:
- Python: `python -m build` (build 1.2.2.post1, hatchling);
- npm: `npm pack`, which runs `tsc -p tsconfig.build.json` first.

| Artifact | Size | SHA-256 |
|---|---|---|
| `sguard-0.2.0rc1-py3-none-any.whl` | 443,113 B | `ee0186eba7d631d258849cd8012b291d14091e1238679216f4735eacd48d6870` |
| `sguard-0.2.0rc1.tar.gz` (sdist) | 401,814 B | `b64769dc07d413d10c598306133d3bbad7a95fef8ea5ceeb41b06c5ab79623d1` |
| `stateguard-mcp-idempotency-0.2.0-rc.1.tgz` | 42,811 B | `7bc7c2e0bdd496a7ee85c75c126a12b0b69b28b51c6f92feb0f24513152a67b2` |

**Verified with `scripts/verify_rc.sh dist/rc`**, each artifact into a fresh environment:
- **The wheel and the sdist,** each in its own new venv:
  - `pip install` succeeds;
  - `stateguard --version` prints `0.2.0rc1`;
  - `stateguard relay stdio --help` and `stateguard trial --help` work;
  - the TypeScript preload is in the package (`stateguard/relay/ts/preload.ts`).
- **The npm tarball:** installed into a new npm project beside its pinned peer
  `@modelcontextprotocol/sdk@1.32.1`:
  - `import('@stateguard/mcp-idempotency')` exposes `IdempotencyStore`, `KeyedTools` and
    `Recovery`;
  - `@stateguard/mcp-idempotency/preload` resolves to the compiled `dist/preload.js`.
- **The wheel builds reproducibly:** its SHA-256 equals that of a build made earlier the same day
  from the same sources.

**Fixed for the candidates** (`d2801cd`):
- **The sdist was 45 MB.** It had swept in the benchmark evidence, `node_modules` and caches.
  It now holds only the package, the TypeScript sources the wheel force-includes, and the
  project files.
- **The npm tarball could not be imported once installed.** Node does not strip TypeScript types
  under `node_modules`. The package now ships compiled JavaScript (`dist/`), built on pack.

**Before any real release** (each needs the founder's approval):
- A5 (publishing);
- a final version number;
- a decision on the npm package's public name and scope.
