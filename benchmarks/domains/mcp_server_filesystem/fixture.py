"""
Sandbox fixture for the mcp-server-filesystem domain: build, reset, snapshot, oracle.

Everything the workflow is allowed to touch lives under one sandbox directory
passed to the server as its only allowed directory. A sentinel file is placed
*next to* the sandbox (outside it) so an escape would be visible.

Ground truth never comes from the server or from StateGuard: ``snapshot`` hashes
the files on disk directly, and ``expected_after`` computes the intended final
state from the fixture's own content with plain Python string operations.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path

#: Synthetic content only. The order file is what the workflow processes.
FILES: dict[str, str] = {
    "inbox/order-001.txt": "order: 001\nstatus: pending\nitems: 2\n",
    "ledger/log.txt": "# workflow log\nEND\n",
    "done/.keep": "",
    "config/settings.ini": "[a]\n  mode = draft\n[b]\n  mode = draft\n",
}
SENTINEL = "outside-sentinel.txt"
SENTINEL_TEXT = "must never change\n"


def build(sandbox: Path) -> None:
    """Create a fresh sandbox (refuses to reuse a non-empty directory) and the sentinel."""
    if sandbox.exists() and any(sandbox.iterdir()):
        raise FileExistsError(f"{sandbox} is not empty; use reset()")
    for rel, text in FILES.items():
        path = sandbox / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (sandbox.parent / SENTINEL).write_text(SENTINEL_TEXT, encoding="utf-8")


def reset(sandbox: Path) -> None:
    """Delete the sandbox and rebuild it from ``FILES``. Only ever touches ``sandbox``."""
    if sandbox.exists():
        shutil.rmtree(sandbox)
    sandbox.mkdir(parents=True)
    build(sandbox)


DIR = "<dir>"


def snapshot(sandbox: Path) -> dict[str, str]:
    """
    ``{relative path: sha256}`` for every file, ``{relative dir + "/": "<dir>"}``
    for every directory, plus the sentinel's hash. Symlinks are recorded as such
    and never followed.
    """
    out: dict[str, str] = {}
    for p in sorted(sandbox.rglob("*")):
        rel = str(p.relative_to(sandbox))
        if p.is_symlink():
            out[rel] = "<symlink>"
        elif p.is_dir():
            out[rel + "/"] = DIR
        elif p.is_file():
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    sentinel = sandbox.parent / SENTINEL
    out["<sentinel>"] = (
        hashlib.sha256(sentinel.read_bytes()).hexdigest() if sentinel.exists() else "missing"
    )
    return out


def read(sandbox: Path, rel: str) -> str | None:
    path = sandbox / rel
    return path.read_text(encoding="utf-8") if path.is_file() else None


def redact(text: str) -> str:
    """Replace every absolute path with ``<path>`` (server messages echo full paths)."""
    return re.sub(r"/[^\s:,'\"]+", "<path>", text)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Oracle: the intended outcome, computed independently of server and StateGuard
# ---------------------------------------------------------------------------

ORDER = "inbox/order-001.txt"
DONE = "done/order-001.txt"
LOG = "ledger/log.txt"
LOG_LINE = "approved order 001"


APPROVED_ORDER = FILES[ORDER].replace("status: pending", "status: approved", 1)
LOGGED = FILES[LOG].replace("END", f"{LOG_LINE}\nEND", 1)


def _state(files: dict[str, str]) -> dict[str, str]:
    """
    The snapshot a sandbox holding exactly *files* would have. Directories are
    those of the fixture (no workflow step removes one) plus any file's parents.
    """
    state = {rel: digest(text) for rel, text in files.items()}
    for rel in [*FILES, *files]:
        parent = Path(rel).parent
        while str(parent) != ".":
            state[f"{parent}/"] = DIR
            parent = parent.parent
    state["<sentinel>"] = digest(SENTINEL_TEXT)
    return dict(sorted(state.items()))


def checkpoints() -> list[dict[str, str]]:
    """
    The intended sandbox state after 0, 1, 2, 3 and 4 completed workflow steps:
    inspect (no change), approve, file, log. Index *k* is the state a correct
    run has reached once exactly *k* steps have taken effect.
    """
    approved = {**FILES, ORDER: APPROVED_ORDER}
    filed = {k: v for k, v in approved.items() if k != ORDER} | {DONE: APPROVED_ORDER}
    logged = {**filed, LOG: LOGGED}
    return [_state(FILES), _state(FILES), _state(approved), _state(filed), _state(logged)]


def initial() -> dict[str, str]:
    return checkpoints()[0]


def expected_after() -> dict[str, str]:
    """Hashes of the sandbox after one correct run of the approval workflow."""
    return checkpoints()[-1]


def matching_checkpoint(state: dict[str, str]) -> int | None:
    """The index of the checkpoint *state* equals; ``None`` when it matches none."""
    found = [i for i, cp in enumerate(checkpoints()) if cp == state]
    return max(found) if found else None
