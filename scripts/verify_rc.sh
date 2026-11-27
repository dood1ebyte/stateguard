#!/bin/bash
# Install each release candidate in DIR into a fresh environment and smoke-test it (S5.3).
# Nothing is published. usage: scripts/verify_rc.sh DIR
set -euo pipefail
DIR=$(cd "$1" && pwd)
WORK=$(mktemp -d)
cd "$WORK"
for kind in whl tar.gz; do
  python3 -m venv "v_$kind"
  "v_$kind/bin/pip" install -q "$DIR"/sguard-*."$kind"
  echo "== $kind"
  "v_$kind/bin/stateguard" --version
  "v_$kind/bin/stateguard" relay stdio --help | head -1
  "v_$kind/bin/stateguard" trial --help | head -1
  "v_$kind/bin/python" -c "import stateguard, pathlib; p = pathlib.Path(stateguard.__file__).parent / 'relay' / 'ts' / 'preload.ts'; print('preload in package:', p.exists())"
done
mkdir npm && cd npm && npm init -y >/dev/null
npm install --no-audit --no-fund -q "$DIR"/stateguard-mcp-idempotency-*.tgz @modelcontextprotocol/sdk@1.32.1
echo "== npm"
node --no-warnings --input-type=module -e "const m = await import('@stateguard/mcp-idempotency'); console.log(['IdempotencyStore', 'KeyedTools', 'Recovery'].map((k) => k + ': ' + typeof m[k]).join(', ')); console.log('preload:', import.meta.resolve('@stateguard/mcp-idempotency/preload').endsWith('dist/preload.js'))"
rm -rf "$WORK"
echo "OK"
