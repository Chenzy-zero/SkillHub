#!/usr/bin/env bash
# Install the SkillHub synchronous submit hook into a Gerrit site.

set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <GERRIT_SITE> [POC_HOME]"
  echo "Example: $0 /var/gerrit/review_site /opt/skillhub/gerrit-change-discovery"
  exit 1
fi

GERRIT_SITE="$1"
POC_HOME="${2:-/opt/skillhub/gerrit-change-discovery}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK_DIR="${GERRIT_SITE}/hooks"
TARGET="${HOOK_DIR}/submit"

mkdir -p "$HOOK_DIR"
cp "${SCRIPT_DIR}/submit" "$TARGET"
chmod 0755 "$TARGET"

cat <<EOF
Submit hook installed:
  $TARGET

POC expected at:
  $POC_HOME

Before testing, ensure the Gerrit service user can:
  1. execute Python and Git
  2. read $POC_HOME/config.json
  3. write $POC_HOME/output and workspace
  4. access Gerrit REST/SSH and MySQL

Hooks Plugin configuration should include:

[hooks]
    path = hooks
    submitHook = submit
    syncHookTimeout = 180

The timeout value is only a POC example. Set it according to your real scan duration.
EOF
