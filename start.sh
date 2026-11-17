#!/usr/bin/env bash
cd "$(dirname "$0")"
PY=python3; [ -x .venv/bin/python ] && PY=.venv/bin/python
# The API key lives in ~/.zshrc; if this shell doesn't have it (e.g. started from Finder), go through zsh.
if [ -z "$JOBPILOT_API_KEY" ] && command -v zsh >/dev/null; then
  exec zsh -ic "cd '$PWD' && $PY -m jobpilot serve"
fi
$PY -m jobpilot serve
