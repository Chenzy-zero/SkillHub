#!/usr/bin/env bash
set -euo pipefail

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

resolve_python() {
  if [[ -n "${SKILL_REVIEW_PYTHON:-}" ]]; then
    printf '%s\n' "$SKILL_REVIEW_PYTHON"
    return 0
  fi
  local candidate
  for candidate in python3.14 python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 &&
      "$candidate" -c 'import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] < (3, 15) else 1)' >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

if ! python_command="$(resolve_python)"; then
  echo "Error: Python 3.11-3.14 was not found." >&2
  exit 2
fi

exec "$python_command" "$script_dir/tools/project_status.py" "$@"
