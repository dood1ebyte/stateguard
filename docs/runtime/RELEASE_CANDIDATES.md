# Release candidates (Phase 2 S5.3, 2026-11-27): built and verified, NOT published

**A5 (publishing) is not approved.**
- Nothing here has been uploaded to PyPI or npm.
- The npm package stays `"private": true`, so npm refuses to publish it.
- The artifacts are kept locally in `dist/rc/`, which is ignored by git.

**Built from commit `d49df75`** (first built from `d2801cd`, then rebuilt once `stateguard doctor` existed), on a clean `git worktree`:
- Python: `python -m build` (build 1.2.2.post1, hatchling);
- npm: `npm pack`, which runs `tsc -p tsconfig.build.json` first.

| Artifact | Size | SHA-256 |
|---|---|---|
| `sguard-0.2.0rc1-py3-none-any.whl` | 445,554 B | `8b3f7b4d1d1d2a183a4f58b73177f7a4e672b9948dd4d49972788bf72c58f0eb` |
| `sguard-0.2.0rc1.tar.gz` (sdist) | 403,695 B | `6d6fd096f35e060f771432b6f84481582563d977b244d31710335bfdbf3456ac` |
| `stateguard-mcp-idempotency-0.2.0-rc.1.tgz` | 42,811 B | `7bc7c2e0bdd496a7ee85c75c126a12b0b69b28b51c6f92feb0f24513152a67b2` |

**Verified with `scripts/verify_rc.sh dist/rc`**, each artifact into a fresh environment:
- **The wheel and the sdist,** each in its own new venv:
  - `pip install` succeeds;
  - `stateguard --version` prints `0.2.0rc1`, and `stateguard doctor` reports "ready";
  - `stateguard relay stdio --help` and `stateguard trial --help` work;
  - the TypeScript preload is in the package (`stateguard/relay/ts/preload.ts`).
- **The npm tarball:** installed into a new npm project beside its pinned peer
  `@modelcontextprotocol/sdk@1.32.1`:
  - `import('@stateguard/mcp-idempotency')` exposes `IdempotencyStore`, `KeyedTools` and
    `Recovery`;
  - `@stateguard/mcp-idempotency/preload` resolves to the compiled `dist/preload.js`.
- **The wheel builds reproducibly:** at `d2801cd`, two builds from the same sources had the same
  SHA-256.
- **The npm tarball** is unchanged between the two commits (same SHA-256).

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
