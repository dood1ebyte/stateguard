#!/bin/bash
# Reproduce a Phase 1/2 matrix part from a clean checkout (Phase 2 S5.4). TEST-ONLY harness.
#
#   scripts/reproduce.sh setup                  # build the environment into $SG_PREFIX
#   scripts/reproduce.sh run SUITE PART REF     # re-run one part, compare it with REF cell for cell
#   scripts/reproduce.sh env                    # print the variables the drivers read
#
# SG_PREFIX (default: ./.repro) holds every installed piece: a venv for the repository, the
# mcp 1.x venv (mcp-server-sqlite), the git/fetch venv, and the Node servers, all at the pinned
# versions of the evidence. Nothing outside SG_PREFIX and the checkout is written.
# Requirements: git, python3.11+, Node 22.18+ (npm), network access to PyPI and npm.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PREFIX=$(mkdir -p "${SG_PREFIX:-$ROOT/.repro}" && cd "${SG_PREFIX:-$ROOT/.repro}" && pwd)
PY=${PYTHON:-python3}

env_vars() {
  cat <<VARS
export STATEGUARD_PYTHON=$PREFIX/venv/bin/python
export MCP1_PYTHON=$PREFIX/mcp1/bin/python
export SGPY=$PREFIX/sgpy/bin
export MEMORY_JS=$PREFIX/servers/memory/node_modules/@modelcontextprotocol/server-memory/dist/index.js
export FS_JS=$PREFIX/servers/filesystem/node_modules/@modelcontextprotocol/server-filesystem/dist/index.js
export EVERYTHING_JS=$PREFIX/servers/everything/node_modules/@modelcontextprotocol/server-everything/dist/index.js
export EVERYTHING_DIR=$PREFIX/servers/everything/node_modules/@modelcontextprotocol
VARS
}

setup() {
  echo "== the repository's venv"
  "$PY" -m venv "$PREFIX/venv"
  "$PREFIX/venv/bin/pip" install -q -e "$ROOT[pydantic,mcp,dev]"
  echo "== mcp 1.x: mcp-server-sqlite (pinned)"
  "$PY" -m venv "$PREFIX/mcp1"
  "$PREFIX/mcp1/bin/pip" install -q "mcp==1.9.4" "mcp-server-sqlite==2025.4.25" "pydantic==2.11.7"
  echo "== mcp-server-git and mcp-server-fetch (pinned)"
  "$PY" -m venv "$PREFIX/sgpy"
  "$PREFIX/sgpy/bin/pip" install -q "mcp-server-git==2026.8.18" "mcp-server-fetch==2026.10.10"
  echo "== the Node servers (pinned)"
  for s in memory filesystem everything; do
    mkdir -p "$PREFIX/servers/$s"
    (cd "$PREFIX/servers/$s" && npm init -y >/dev/null && \
      npm install --no-audit --no-fund -q "@modelcontextprotocol/server-$s@2026.8.31")
  done
  echo "== the TypeScript adapter's dependencies (lockfile)"
  (cd "$ROOT/adapters/typescript" && npm ci --no-audit --no-fund -q)
  echo "setup done: $PREFIX"
}

run() {
  local suite=$1 part=$2 ref=$3
  eval "$(env_vars)"
  local batch="REPRO-$(date -u +%Y%m%d)"
  local dir="$ROOT/benchmarks/evidence/$suite/$batch"
  local refdir="$ROOT/benchmarks/evidence/$suite/$ref"
  [ -f "$refdir/codestate_$part.json" ] || { echo "no $refdir/codestate_$part.json" >&2; exit 2; }
  # The reference part's own command: its driver, its arguments and its trial offset.
  read -r driver offset args < <("$STATEGUARD_PYTHON" -c "import json,sys; s=json.load(open(sys.argv[1])); print(s['driver'], s['trial_offset'], ' '.join(s['driver_args']))" "$refdir/codestate_$part.json")
  mkdir -p "$dir"
  if [ ! -f "$dir/PLAN.md" ]; then
    cat > "$dir/PLAN.md" <<PLAN
# $batch: reproduce $ref's part $part from a clean checkout (scripts/reproduce.sh)

The same driver ($driver), arguments ($args) and trial offset ($offset) as $ref's
\`codestate_$part.json\`, on this checkout's HEAD. Prediction: $ref, cell for cell, in every
StateGuard arm. Synthetic fault injection; not production traffic.
PLAN
  fi
  local suite_flag=()
  [ "$suite" != realhttp ] && suite_flag=(--suite "$suite")
  (cd "$ROOT/benchmarks/runtime/realhttp" && TRIAL_OFFSET=$offset "$STATEGUARD_PYTHON" run_batch.py \
     "$batch" HEAD "${suite_flag[@]}" --driver "$driver" --part "$part" -- $args)
  "$STATEGUARD_PYTHON" "$ROOT/benchmarks/runtime/compare_cells.py" "$dir/summary_$part.json" \
     "$refdir/summary_$part.json" | tee "$dir/summary_$part.compare.txt"
}

case "${1:-}" in
  setup) setup ;;
  run) shift; run "$@" ;;
  env) env_vars ;;
  *) sed -n 2,13p "$0"; exit 2 ;;
esac
