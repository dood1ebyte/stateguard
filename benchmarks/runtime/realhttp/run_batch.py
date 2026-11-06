"""
Run one recorded realhttp batch (TEST-ONLY harness).

    python benchmarks/runtime/realhttp/run_batch.py BATCH COMMIT [--part NAME] \
        -- SERVER N [cells] [arms] [workers]

* ``benchmarks/evidence/realhttp/BATCH/PLAN.md`` must already exist: the plan is written before
  any row. Rows are never overwritten: a batch with ``rows*.jsonl`` already present is refused
  unless ``--part NAME`` names a new part (``rows_NAME.jsonl``).
* The code under test is a clean ``git worktree`` of COMMIT under the system temp directory, so
  working-tree edits never reach a run. The TypeScript adapter's ``node_modules`` is linked
  from this checkout; its lockfile hash is recorded.
* ``codestate.json`` records the commit, the versions (Python, mcp, Node, TypeScript SDK,
  server-everything and its package-tree hash) and the exact command.
* Rows go to ``rows[_PART].jsonl``; failing runs, compressed, to ``failing[_PART].tar.gz``.
  ``summarize.py`` writes ``summary[_PART].txt`` / ``.json`` beside them.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
EVIDENCE_ROOT = REPO / "benchmarks" / "evidence"
EVERYTHING_DIR = Path(
    os.environ.get(
        "EVERYTHING_DIR", "/opt/sg-servers/everything/node_modules/@modelcontextprotocol"
    )
)


def _out(cmd: list[str], cwd: Path | None = None) -> str:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False).stdout.strip()


def _tree_hash(root: Path) -> str | None:
    if not root.exists():
        return None
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def main(argv: list[str]) -> int:
    if "--" not in argv:
        raise SystemExit(
            "usage: run_batch.py BATCH COMMIT [--part NAME] -- SERVER N [cells] [arms] [workers]"
        )
    split = argv.index("--")
    parser = argparse.ArgumentParser()
    parser.add_argument("batch")
    parser.add_argument("commit")
    parser.add_argument("--part", default="")
    parser.add_argument(
        "--driver",
        default="driver.py",
        help="driver.py, stale_pool.py, or ../realstdio/driver.py (relative to this directory)",
    )
    parser.add_argument(
        "--suite", default="realhttp", help="evidence directory: realhttp, realstdio"
    )
    args = parser.parse_args(argv[:split])
    driver_args = argv[split + 1 :]
    out = EVIDENCE_ROOT / args.suite / args.batch
    if not (out / "PLAN.md").exists():
        raise SystemExit(f"{out / 'PLAN.md'} does not exist: write the plan before any row")
    suffix = f"_{args.part}" if args.part else ""
    rows_path = out / f"rows{suffix}.jsonl"
    if rows_path.exists():
        raise SystemExit(f"refusing to overwrite {rows_path}")
    commit = _out(["git", "rev-parse", args.commit], REPO)
    worktree = Path(tempfile.gettempdir()) / f"sg-code-{commit[:12]}"
    # Parts run in parallel share the worktree: one creates it, the others wait for it.
    with (Path(tempfile.gettempdir()) / f"sg-code-{commit[:12]}.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not worktree.exists():
            subprocess.run(
                ["git", "worktree", "add", "--detach", str(worktree), commit], cwd=REPO, check=True
            )
        modules = REPO / "adapters" / "typescript" / "node_modules"
        link = worktree / "adapters" / "typescript" / "node_modules"
        if modules.exists() and not link.exists():
            link.symlink_to(modules)
    # The node_modules link (added above) is the one entry allowed: the repository ignores
    # "node_modules/", which matches a directory but not a symlink.
    dirty = _out(
        ["git", "status", "--porcelain", "--", ".", ":(exclude)adapters/typescript/node_modules"],
        worktree,
    )
    if dirty:
        raise SystemExit(f"{worktree} is not clean:\n{dirty}")
    python = os.environ.get("STATEGUARD_PYTHON", sys.executable)
    node = os.environ.get("NODE") or shutil.which("node") or "/opt/node22/bin/node"
    lock = worktree / "adapters" / "typescript" / "package-lock.json"
    state = {
        "batch": args.batch,
        "part": args.part or None,
        "started": datetime.now(UTC).isoformat(),
        "commit": commit,
        "harness_commit": _out(["git", "rev-parse", "HEAD"], REPO),
        "harness_dirty": bool(
            _out(["git", "status", "--porcelain", "--", "benchmarks/runtime"], REPO)
        ),
        "driver": args.driver,
        "driver_args": driver_args,
        "trial_offset": os.environ.get("TRIAL_OFFSET", "0"),
        "python": _out([python, "-c", "import sys; print(sys.version.split()[0])"]),
        "mcp": _out([python, "-c", "import importlib.metadata as m; print(m.version('mcp'))"]),
        "node": _out([node, "--version"]),
        "typescript_sdk": _out(
            [node, "-p", f"require('{modules}/@modelcontextprotocol/sdk/package.json').version"]
        ),
        "typescript_lockfile_sha256": hashlib.sha256(lock.read_bytes()).hexdigest()
        if lock.exists()
        else None,
        "everything_tree_sha256": _tree_hash(EVERYTHING_DIR),
        "platform": platform.platform(),
        "cpus": os.cpu_count(),
    }
    out.mkdir(parents=True, exist_ok=True)
    runs = Path(tempfile.mkdtemp(prefix=f"realhttp-{args.batch}-"))
    failing = runs / "failing"
    env = {
        **os.environ,
        "STATEGUARD_CODE": str(worktree),
        "RUNS_DIR": str(runs / "runs"),
        "FAILING_DIR": str(failing),
    }
    with rows_path.open("w") as rows, (out / f"rows{suffix}.err").open("w") as err:
        code = subprocess.run(
            [python, str(HERE / args.driver), *driver_args],
            stdout=rows,
            stderr=err,
            env=env,
            check=False,
        ).returncode
    state["driver_exit"] = code
    state["finished"] = datetime.now(UTC).isoformat()
    state["everything_tree_sha256_after"] = _tree_hash(EVERYTHING_DIR)
    (out / f"codestate{suffix}.json").write_text(json.dumps(state, indent=1) + "\n")
    if failing.exists():
        with tarfile.open(out / f"failing{suffix}.tar.gz", "w:gz") as tar:
            tar.add(failing, arcname=f"failing{suffix}")
    shutil.rmtree(runs, ignore_errors=True)
    scorer = "stale_pool_summary.py" if args.driver == "stale_pool.py" else "summarize.py"
    with (out / f"summary{suffix}.txt").open("w") as summary:
        subprocess.run(
            [
                python,
                str(HERE / scorer),
                str(rows_path),
                "--json",
                str(out / f"summary{suffix}.json"),
            ],
            stdout=summary,
            check=False,
        )
    print(f"{rows_path}: {sum(1 for _ in rows_path.open())} rows, driver exit {code}")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
