"""
``--keys auto``: start the server with StateGuard's idempotency keys, when the command
says how.

The relay cannot add keys to a server by itself: keys are honoured by the server, and an
unmodified server is adopted at process start, the operator's way, by one of two adapters
configured by the same environment variables (``STATEGUARD_IDEMPOTENCY_DB``,
``STATEGUARD_IDEMPOTENCY_SCOPE``, ``STATEGUARD_KEYED_TOOLS``):

* a **Python** server (``mcp`` 1.x or 2.x) runs under the injector,
  ``python -m stateguard.adapters.mcp.inject <script | -m module> ...``;
* a **Node** server (TypeScript SDK 1.x) runs with the preload,
  ``node --import <preload.ts> <entry.js> ...`` (Node 22.18 or later: type stripping).

:func:`plan` reads the server command and returns either a :class:`KeyedLaunch` (the
command and the environment to start it with) or :class:`Unkeyed` with the reason. It never
guesses: a command it cannot read with certainty runs unkeyed, and the relay logs why.
Running unkeyed is the relay's behaviour without ``--keys`` -- nothing is claimed for it.

What is detected:

* ``python[3[.N]] [-m module | script] ...`` -- the interpreter is checked first (Python 3.11
  or later, with ``anyio`` and ``mcp`` importable, which the injector needs);
* a console script or script whose ``#!`` line names a Python interpreter (directly, or
  ``/usr/bin/env python3``), as ``pip`` writes them;
* ``node [node options] entry.js ...`` -- Node's version is checked first.

Everything else (``npx``, ``uvx``, ``docker``, a shell wrapper, a compiled binary, a Python
interpreter given other flags before the script) runs unkeyed, with the reason.

A server that starts under an adapter but never registers its tools through the SDK the
adapter wraps advertises no keys; the relay then sends its calls unkeyed, as for any server
that does not advertise keys, and says so after the listing (``keys_not_advertised``).

The injected Python server imports stateguard from a directory that holds **only** the
stateguard package (a symlink, next to the key store), prepended to its ``PYTHONPATH``: the
relay's own environment (its ``mcp`` version, say) never shadows the server's.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import stateguard

#: The environment variables both adapters read.
DB_VAR = "STATEGUARD_IDEMPOTENCY_DB"
SCOPE_VAR = "STATEGUARD_IDEMPOTENCY_SCOPE"
KEYED_VAR = "STATEGUARD_KEYED_TOOLS"
#: An operator's own preload path (a checkout of ``adapters/typescript``, say).
PRELOAD_VAR = "STATEGUARD_PRELOAD"

DEFAULT_DB = Path.home() / ".stateguard" / "keys.db"
#: Node's first release that strips TypeScript types without a flag.
NODE_MINIMUM = (22, 18)
PYTHON_MINIMUM = (3, 11)

_PYTHON = re.compile(r"^python(\d+(\.\d+)?)?(\.exe)?$")
_NODE = re.compile(r"^(node|nodejs)(\.exe)?$")
_PROBE_TIMEOUT = 30.0
#: Commands that start the server somewhere else (a cache, a container): never wrapped.
_LAUNCHERS = frozenset({"npx", "uvx", "pipx", "docker", "podman", "bunx", "deno", "bun"})


@dataclass(frozen=True)
class KeyedLaunch:
    """Start the server as *command*, with *env* (the whole environment)."""

    command: list[str]
    env: dict[str, str]
    adapter: str  # "python-injector" | "node-preload"
    keyed_tools: tuple[str, ...]
    db: str
    scope: str
    notes: tuple[str, ...] = field(default=())

    def describe(self) -> dict[str, object]:
        return {
            "keys": "auto",
            "adapter": self.adapter,
            "keyed_tools": list(self.keyed_tools),
            "db": self.db,
            "scope": self.scope,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class Unkeyed:
    """The server runs as given, without keys, for *reason*."""

    reason: str

    def describe(self) -> dict[str, object]:
        return {"keys": "auto", "adapter": None, "unkeyed_reason": self.reason}


Probe = Callable[[Sequence[str], Mapping[str, str]], "subprocess.CompletedProcess[str]"]


def _probe(argv: Sequence[str], env: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        env=dict(env),
        capture_output=True,
        text=True,
        timeout=_PROBE_TIMEOUT,
        check=False,
        stdin=subprocess.DEVNULL,
    )


def default_scope(command: Sequence[str]) -> str:
    """A key scope of its own for each server command (keys never cross servers)."""
    digest = hashlib.sha256("\0".join(command).encode()).hexdigest()
    return f"sg-{digest[:12]}"


def preload_path() -> Path | None:
    """The TypeScript preload: ``$STATEGUARD_PRELOAD``, the wheel's copy, or a checkout's."""
    configured = os.environ.get(PRELOAD_VAR)
    if configured:
        return Path(configured)
    here = Path(__file__).resolve().parent
    for candidate in (
        here / "ts" / "preload.ts",  # shipped in the wheel
        here.parents[2] / "adapters" / "typescript" / "src" / "preload.ts",  # a checkout
    ):
        if candidate.is_file():
            return candidate
    return None


def _which(program: str, env: Mapping[str, str]) -> str | None:
    if os.sep in program or (os.altsep and os.altsep in program):
        return program if Path(program).is_file() else None
    return shutil.which(program, path=env.get("PATH"))


def _shebang(path: str) -> list[str] | None:
    """The ``#!`` line of *path*, split, or None (not a script, unreadable)."""
    try:
        with open(path, "rb") as handle:
            first = handle.readline(512)
    except OSError:
        return None
    if not first.startswith(b"#!"):
        return None
    try:
        return shlex.split(first[2:].decode("utf-8").strip())
    except (UnicodeDecodeError, ValueError):
        return None


def _stateguard_path(db: Path) -> Path:
    """A directory holding only the stateguard package (a symlink), for ``PYTHONPATH``."""
    package = Path(stateguard.__file__).resolve().parent
    tag = hashlib.sha256(str(package).encode()).hexdigest()[:12]
    directory = db.parent / "pythonpath" / tag
    link = directory / "stateguard"
    if link.is_symlink() and link.resolve() == package:
        return directory
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f".stateguard.{os.getpid()}"
    with contextlib.suppress(FileNotFoundError):
        temporary.unlink()
    temporary.symlink_to(package, target_is_directory=True)
    os.replace(temporary, link)  # atomic: a concurrent relay sees the old link or the new one
    return directory


def _python_ready(python: str, env: Mapping[str, str], probe: Probe) -> str | None:
    """None if *python*, in *env* (the server's), can run the injector; else why not."""
    code = (
        "import sys\n"
        f"assert sys.version_info >= {PYTHON_MINIMUM!r}, 'Python %d.%d' % sys.version_info[:2]\n"
        "import mcp\n"
        "import stateguard.adapters.mcp.inject\n"
    )
    try:
        done = probe([python, "-c", code], env)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"could not run {python}: {exc}"
    if done.returncode != 0:
        lines = (done.stderr or "").strip().splitlines()
        last = lines[-1] if lines else f"exit {done.returncode}"
        return (
            f"{python} cannot run the injector (it needs Python "
            f"{PYTHON_MINIMUM[0]}.{PYTHON_MINIMUM[1]}+ with the server's mcp): {last}"
        )
    return None


def _node_ready(node: str, env: Mapping[str, str], probe: Probe) -> str | None:
    try:
        done = probe([node, "--version"], env)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"could not run {node}: {exc}"
    match = re.match(r"v(\d+)\.(\d+)", (done.stdout or "").strip())
    if done.returncode != 0 or match is None:
        return f"could not read the version of {node}"
    version = (int(match.group(1)), int(match.group(2)))
    if version < NODE_MINIMUM:
        return (
            f"{node} is v{version[0]}.{version[1]}; the preload needs Node "
            f"{NODE_MINIMUM[0]}.{NODE_MINIMUM[1]}+ (TypeScript type stripping)"
        )
    return None


def plan(
    command: Sequence[str],
    keyed_tools: Sequence[str],
    *,
    db: str | os.PathLike[str] | None = None,
    scope: str | None = None,
    environ: Mapping[str, str] | None = None,
    probe: Probe = _probe,
) -> KeyedLaunch | Unkeyed:
    """How to start *command* with keys for *keyed_tools*, or why it runs unkeyed."""
    env = dict(os.environ if environ is None else environ)
    tools = tuple(dict.fromkeys(t for t in keyed_tools if t))
    if not tools:
        return Unkeyed(
            "no tools to key: name them with --keyed-tools, or declare them non_idempotent "
            "or idempotent in --effects"
        )
    if not command:
        return Unkeyed("no server command")
    for adapter_var in (DB_VAR, KEYED_VAR):
        if adapter_var in env:
            return Unkeyed(
                f"{adapter_var} is already set in the relay's environment: the server is "
                "configured by hand; --keys auto changes nothing"
            )
    store = Path(db) if db is not None else DEFAULT_DB
    store = store.expanduser().resolve()
    try:
        store.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return Unkeyed(f"could not create the key store's directory {store.parent}: {exc}")
    key_scope = scope or default_scope(command)
    adapter_env = {DB_VAR: str(store), SCOPE_VAR: key_scope, KEYED_VAR: ",".join(tools)}

    program = command[0]
    resolved = _which(program, env)
    if resolved is None:
        return Unkeyed(f"the server program {program!r} was not found")
    name = Path(resolved).name

    if _PYTHON.match(name):
        rest = list(command[1:])
        if not rest:
            return Unkeyed(f"{program} was given no script or -m module")
        if rest[0].startswith("-") and rest[0] != "-m":
            return Unkeyed(
                f"{program} {rest[0]} ...: only 'python SCRIPT' and 'python -m MODULE' can be "
                "wrapped (an interpreter option before the script would be lost)"
            )
        return _python_launch(program, rest, env, adapter_env, store, tools, key_scope, probe)

    if name in _LAUNCHERS:
        return Unkeyed(
            f"{program} starts the server in an environment the relay cannot reach into; run "
            "the server's own command (its python or node entry point) instead"
        )

    if _NODE.match(name):
        return _node_launch(
            program, list(command[1:]), env, adapter_env, store, tools, key_scope, probe
        )

    shebang = _shebang(resolved)
    if shebang:
        interpreter = shebang
        if Path(shebang[0]).name == "env" and len(shebang) == 2:
            interpreter = shebang[1:]
        kind = Path(interpreter[0]).name if len(interpreter) == 1 else ""
        if _PYTHON.match(kind) or _NODE.match(kind):
            found = _which(interpreter[0], env)
            if found is None:
                return Unkeyed(f"{resolved}: its interpreter {interpreter[0]!r} was not found")
            rest = [resolved, *command[1:]]
            if _NODE.match(kind):  # an npm-installed bin: #!/usr/bin/env node
                return _node_launch(found, rest, env, adapter_env, store, tools, key_scope, probe)
            return _python_launch(found, rest, env, adapter_env, store, tools, key_scope, probe)
        return Unkeyed(
            f"{program} is a script for {' '.join(shebang)!r}: only Python and Node servers "
            "can be wrapped"
        )
    return Unkeyed(f"{program} is not a Python or Node server command")


def _node_launch(
    node: str,
    rest: list[str],
    env: dict[str, str],
    adapter_env: dict[str, str],
    store: Path,
    tools: tuple[str, ...],
    key_scope: str,
    probe: Probe,
) -> KeyedLaunch | Unkeyed:
    why = _node_ready(node, env, probe)
    if why is not None:
        return Unkeyed(why)
    why = _sdk_one_above(rest)
    if why is not None:
        return Unkeyed(why)
    preload = preload_path()
    if preload is None:
        return Unkeyed(
            "the TypeScript preload was not found (set STATEGUARD_PRELOAD to "
            "adapters/typescript/src/preload.ts)"
        )
    return KeyedLaunch(
        command=[node, "--import", str(preload), *rest],
        env={**env, **adapter_env},
        adapter="node-preload",
        keyed_tools=tools,
        db=str(store),
        scope=key_scope,
    )


#: Node options that take the next argument as their value (``node --import X entry.js``).
_NODE_VALUE_OPTIONS = frozenset({"--import", "--require", "-r", "--loader", "--env-file", "-e"})


def _node_entry(rest: Sequence[str]) -> str | None:
    """The entry script in ``node [options] ENTRY [args]``, or None."""
    skip = False
    for arg in rest:
        if skip:
            skip = False
            continue
        if arg in _NODE_VALUE_OPTIONS:
            skip = True
            continue
        if arg.startswith("-"):
            continue
        return arg
    return None


def _sdk_one_above(rest: Sequence[str]) -> str | None:
    """
    Why the preload cannot wrap this server, or None. The preload wraps the TypeScript SDK
    1.x (``@modelcontextprotocol/sdk``) that the server's own entry resolves; a server built on
    the SDK v2 (``@modelcontextprotocol/server``) has none, and the preload would stop it from
    starting (M2, 2026-11-27). So the plan looks where the preload will, and runs it unkeyed
    when it is not there.
    """
    entry = _node_entry(rest)
    if entry is None:
        return "no entry script found in the node command"
    path = Path(entry)
    if not path.is_file():
        return f"{entry}: the entry script was not found, so its TypeScript SDK cannot be checked"
    directory = path.resolve().parent
    while True:
        package = directory / "node_modules" / "@modelcontextprotocol" / "sdk" / "package.json"
        if package.is_file():
            try:
                version = str(json.loads(package.read_text(encoding="utf-8")).get("version", ""))
            except (OSError, ValueError):
                return f"{package}: unreadable"
            if version.split(".")[0] != "1":
                return (
                    f"the server uses @modelcontextprotocol/sdk {version}; the preload wraps "
                    "the TypeScript SDK 1.x only"
                )
            return None
        if directory.parent == directory:
            return (
                f"no @modelcontextprotocol/sdk (1.x) above {entry}: the server is not built on "
                "the TypeScript SDK 1.x the preload wraps (an SDK v2 server, perhaps)"
            )
        directory = directory.parent


def _python_launch(
    python: str,
    rest: list[str],
    env: dict[str, str],
    adapter_env: dict[str, str],
    store: Path,
    tools: tuple[str, ...],
    key_scope: str,
    probe: Probe,
) -> KeyedLaunch | Unkeyed:
    try:
        importable = _stateguard_path(store)
    except OSError as exc:
        return Unkeyed(f"could not make stateguard importable for the server: {exc}")
    existing = env.get("PYTHONPATH")
    pythonpath = str(importable) + (os.pathsep + existing if existing else "")
    launch_env = {**env, **adapter_env, "PYTHONPATH": pythonpath}
    why = _python_ready(python, launch_env, probe)
    if why is not None:
        return Unkeyed(why)
    return KeyedLaunch(
        command=[python, "-m", "stateguard.adapters.mcp.inject", *rest],
        env=launch_env,
        adapter="python-injector",
        keyed_tools=tools,
        db=str(store),
        scope=key_scope,
    )
