#!/usr/bin/env bash
# Copy JobMan to the server and keep it in sync. Run on the Mac:
#   ~/jobpilot/deploy/push.sh ubuntu@<server-ip>          (first time: add --setup)
# Copies the code, your config, CV, database, sign-in sessions and application files; never .venv.
# Keys and passwords go into ~/.jobman.env on the server (readable only by you there).
set -euo pipefail
HOST="$1"; SETUP="${2:-}"
KEY="$HOME/.ssh/jobman_oracle"
SSH="ssh -i $KEY -o StrictHostKeyChecking=accept-new"
cd "$(dirname "$0")/.."
rsync -az --delete -e "$SSH" --exclude .venv --exclude __pycache__ --exclude '*.db-wal' --exclude '*.db-shm' \
      --exclude 'config.toml.bak-*' ./ "$HOST:jobpilot/"
# secrets: from this Mac's environment or Keychain straight into the server file, never printed
PY=.venv/bin/python
$PY - "$HOST" "$KEY" <<'PYEOF'
import subprocess, sys
from jobpilot.core import SECRETS, secret
host, key = sys.argv[1], sys.argv[2]
body = "".join(f"{n}={secret(n)}\n" for n in SECRETS if secret(n))
subprocess.run(["ssh", "-i", key, host, "umask 077; cat > ~/.jobman.env"], input=body, text=True, check=True)
PYEOF
if [ "$SETUP" = "--setup" ]; then
  $SSH "$HOST" "bash ~/jobpilot/deploy/server_setup.sh"
fi
echo "Synced to $HOST."
