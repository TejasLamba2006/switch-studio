#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

export PYTHONPATH="$ROOT_DIR/src:${PYTHONPATH:-}"

if command -v uv >/dev/null 2>&1 && [ -d "/home/tejaslamba/switch-wt-771/core" ]; then
  echo "=== [0/5] Validating studio.template.yaml against switch_core (version 2 compiler + linter) ==="
  uv run --directory /home/tejaslamba/switch-wt-771/core python -c '
from pathlib import Path
from switch_core.template_lint import lint_template
from switch_core.template_v2 import compile_v2
text = Path("'"$ROOT_DIR"'/studio.template.yaml").read_text()
res = lint_template(text)
assert not res.errors and not res.warnings, f"Lint issues: {res}"
compiled = compile_v2(
    text,
    inputs={"studio_name": "pulse", "bridge": "Mattermost", "provider": "claude", "machine": "local", "directory": "/tmp/pulse"},
    builtins={"$creator": "tejas", "$agents_dir": "/tmp/agents", "$date": "2026-11-06", "$timestamp": "2026-11-06T00:00:00Z"},
)
print("  [OK] studio.template.yaml compiled cleanly ->", compiled.names)
'
  uv run --directory /home/tejaslamba/switch-wt-771/core python -m switch_studio.cli --prove-switch-central --artifacts-dir "$ROOT_DIR/artifacts" "$@"
else
  python3 -m switch_studio.cli --prove-switch-central --artifacts-dir "$ROOT_DIR/artifacts" "$@"
fi
