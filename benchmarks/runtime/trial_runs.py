"""
T1 (TEST-ONLY): ``stateguard trial`` on a real pinned server with a scripted workload.
Plan: ``benchmarks/evidence/trial/T1/PLAN.md``.

    trial_runs.py SERVER RUN -> one JSON row (the trial report plus ground truth) on stdout
    trial_runs.py ROWS.jsonl --json OUT -> a summary (run_batch.py's scorer call)

SERVER: filesystem, sqlite or git. RUN: plain, or fault_read (lost_answer on the main read).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
CODE = Path(os.environ.get("STATEGUARD_CODE", str(HERE.parents[1]))).resolve()
PY = os.environ.get("STATEGUARD_PYTHON", sys.executable)
NODE = os.environ.get("NODE") or shutil.which("node") or "/opt/node22/bin/node"
FS_JS = "/opt/sg-servers/filesystem/node_modules/@modelcontextprotocol/server-filesystem/dist/index.js"
SQLITE = os.environ.get("MCP1_SQLITE", "/tmp/mcp1/bin/mcp-server-sqlite")
GIT = os.environ.get("SGPY_GIT", "/tmp/sgpy/bin/mcp-server-git")
RUNS = Path(os.environ.get("RUNS_DIR", tempfile.gettempdir() + "/trial-runs"))
READ = {"filesystem": "read_text_file", "sqlite": "read_query", "git": "git_status"}
GENV = {"GIT_AUTHOR_NAME": "trial", "GIT_AUTHOR_EMAIL": "trial@invalid",
        "GIT_COMMITTER_NAME": "trial", "GIT_COMMITTER_EMAIL": "trial@invalid"}


def setup(server: str, base: Path) -> tuple[list[str], Path, list[dict[str, Any]], list[dict]]:
    """(server command, sandbox, calls, effects)."""
    if server == "filesystem":
        root = base / "root"
        (root / "notes").mkdir(parents=True)
        (root / "notes" / "a.md").write_text("alpha tok-a\n")
        r = str(root)
        calls = [
            {"tool": "read_text_file", "arguments": {"path": f"{r}/notes/a.md"}},
            {"tool": "list_directory", "arguments": {"path": f"{r}/notes"}},
            {"tool": "write_file", "arguments": {"path": f"{r}/notes/b.md", "content": "beta\n"}},
            {"tool": "read_text_file", "arguments": {"path": f"{r}/notes/b.md"}},
            {"tool": "get_file_info", "arguments": {"path": f"{r}/notes/a.md"}},
            {"tool": "edit_file", "arguments": {"path": f"{r}/notes/a.md",
                                                "edits": [{"oldText": "tok-a", "newText": "tok-z"}]}},
            {"tool": "search_files", "arguments": {"path": r, "pattern": "*.md"}},
            {"tool": "move_file", "arguments": {"source": f"{r}/notes/b.md",
                                                "destination": f"{r}/notes/c.md"}},
            {"tool": "read_text_file", "arguments": {"path": f"{r}/notes/missing.md"}},
            {"tool": "directory_tree", "arguments": {"path": r}},
        ]
        effects = [{"name": n, "effect": "read_only"} for n in
                   ("read_text_file", "list_directory", "get_file_info", "search_files",
                    "directory_tree")]
        effects += [{"name": n, "effect": "non_idempotent"} for n in
                    ("write_file", "edit_file", "move_file")]
        return [NODE, "--no-warnings", FS_JS, r], root, calls, effects
    if server == "sqlite":
        box = base / "db"
        box.mkdir()
        with sqlite3.connect(box / "app.db") as conn:
            conn.execute("CREATE TABLE notes (text TEXT)")
            conn.execute("INSERT INTO notes VALUES ('seed')")
        calls = [{"tool": "list_tables", "arguments": {}},
                 {"tool": "read_query", "arguments": {"query": "SELECT text FROM notes"}},
                 {"tool": "describe_table", "arguments": {"table_name": "notes"}}]
        calls += [{"tool": "write_query", "arguments": {"query": f"INSERT INTO notes VALUES ('t-{i}')"}}
                  for i in range(3)]
        calls += [{"tool": "read_query", "arguments": {"query": "SELECT COUNT(*) AS n FROM notes"}},
                  {"tool": "create_table", "arguments": {"query": "CREATE TABLE tags (name TEXT)"}},
                  {"tool": "read_query", "arguments": {"query": "SELECT nope FROM missing"}},
                  {"tool": "list_tables", "arguments": {}}]
        effects = [{"name": n, "effect": "read_only"} for n in ("list_tables", "read_query",
                                                                 "describe_table")]
        effects += [{"name": n, "effect": "non_idempotent"} for n in ("write_query", "create_table")]
        return [SQLITE, "--db-path", str(box / "app.db")], box, calls, effects
    repo = base / "repo"
    repo.mkdir()
    env = {**os.environ, **GENV}

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    (repo / "README.md").write_text("# trial\n")
    git("add", "README.md")
    git("commit", "-q", "-m", "initial")
    (repo / "change.txt").write_text("a change\n")
    r = str(repo)
    calls = [{"tool": "git_status", "arguments": {"repo_path": r}},
             {"tool": "git_log", "arguments": {"repo_path": r, "max_count": 3}},
             {"tool": "git_diff_unstaged", "arguments": {"repo_path": r}},
             {"tool": "git_add", "arguments": {"repo_path": r, "files": ["change.txt"]}},
             {"tool": "git_diff_staged", "arguments": {"repo_path": r}},
             {"tool": "git_commit", "arguments": {"repo_path": r, "message": "trial change"}},
             {"tool": "git_show", "arguments": {"repo_path": r, "revision": "HEAD"}},
             {"tool": "git_branch", "arguments": {"repo_path": r, "branch_type": "local"}},
             {"tool": "git_show", "arguments": {"repo_path": r, "revision": "no-such-rev"}},
             {"tool": "git_status", "arguments": {"repo_path": r}}]
    effects = [{"name": n, "effect": "read_only"} for n in
               ("git_status", "git_log", "git_diff_unstaged", "git_diff_staged", "git_show",
                "git_branch")]
    effects += [{"name": "git_add", "effect": "idempotent"},
                {"name": "git_commit", "effect": "non_idempotent"}]
    return [GIT, "--repository", r], repo, calls, effects


def truth(server: str, copy: Path) -> dict[str, Any]:
    """Each write's effect in a run's sandbox copy, counted (a duplicate would show here)."""
    if server == "filesystem":
        notes = copy / "notes"
        return {"files": sorted(p.name for p in notes.iterdir()),
                "a_edited": "tok-z" in (notes / "a.md").read_text()}
    if server == "sqlite":
        with sqlite3.connect(copy / "app.db") as conn:
            rows = [r[0] for r in conn.execute("SELECT text FROM notes")]
            tables = sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"))
        return {"rows": rows, "tables": tables}
    log = subprocess.run(["git", "log", "--format=%s"], cwd=copy, capture_output=True, text=True)
    return {"commits": log.stdout.split("\n")[:-1]}


def run(server: str, kind: str) -> dict[str, Any]:
    base = Path(tempfile.mkdtemp(prefix=f"{server}-{kind}-", dir=RUNS if RUNS.exists() else None))
    command, sandbox, calls, effects = setup(server, base)
    calls_file, effects_file = base / "calls.jsonl", base / "effects.json"
    calls_file.write_text("".join(json.dumps(c) + "\n" for c in calls))
    effects_file.write_text(json.dumps({"tools": effects}))
    out = base / "trial"
    fault = ["--fault", "lost_answer", "--fault-tool", READ[server]] if kind == "fault_read" else []
    env = {**os.environ, **GENV, "PYTHONPATH": str(CODE / "src")}
    done = subprocess.run(
        [PY, "-m", "stateguard", "trial", "--dir", str(out), "--calls", str(calls_file),
         "--effects", str(effects_file), "--sandbox", str(sandbox), "--label",
         f"T1 {server} {kind}: real server, scripted workload", *fault, "--", *command],
        capture_output=True, text=True, env=env, timeout=600, check=False)
    report = json.loads((out / "trial_report.json").read_text()) if (out / "trial_report.json").exists() else None
    row = {"server": server, "run": kind, "exit": done.returncode, "report": report,
           "stderr_tail": done.stderr.splitlines()[-3:] if done.returncode else []}
    if report is not None:
        row["truth"] = {mode: truth(server, out / mode / "sandbox" / sandbox.name)
                        for mode in ("shadow", "auto")}
    shutil.rmtree(base, ignore_errors=True)
    return row


def summarize(rows_path: str, out: str | None) -> None:
    rows = [json.loads(x) for x in Path(rows_path).read_text().splitlines() if x.strip()]
    summary = []
    for r in rows:
        rep = r.get("report") or {}
        s, a = rep.get("shadow", {}), rep.get("auto", {})
        summary.append({"server": r["server"], "run": r["run"], "exit": r["exit"],
                        "shadow": {k: s.get(k) for k in ("client", "unknown", "would_retry", "denominators_hold", "bundle_rebuilds", "fault_fired")},
                        "auto": {k: a.get(k) for k in ("client", "unknown", "calls_resent", "denominators_hold", "bundle_rebuilds", "fault_fired")},
                        "changed_calls": [c["call"] for c in rep.get("changed_calls", [])],
                        "truth": r.get("truth")})
    if out:
        Path(out).write_text(json.dumps(summary, indent=1) + "\n")
    for s in summary:
        print(json.dumps(s))


def main() -> None:
    if sys.argv[1].endswith(".jsonl"):
        summarize(sys.argv[1], sys.argv[3] if len(sys.argv) > 3 else None)
        return
    print(json.dumps(run(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "plain")), flush=True)


if __name__ == "__main__":
    main()
