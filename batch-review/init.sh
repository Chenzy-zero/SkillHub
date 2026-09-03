#!/usr/bin/env bash
set -euo pipefail

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
python_command="${SKILL_REVIEW_PYTHON:-python3.12}"

exec "$python_command" "$script_dir/tools/init_project.py" "$@"
