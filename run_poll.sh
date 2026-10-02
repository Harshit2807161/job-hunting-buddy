#!/bin/bash
# Runs one poll cycle. Called by launchd every 15 minutes (see register_launchd.sh).
# Calls the env's python directly -- no conda activation needed.
cd "$(dirname "$0")" || exit 1
mkdir -p data
"$HOME/miniforge3/envs/jhb/bin/python" -m jhb.poll >> data/poll.log 2>&1
jhb_poll_exit=$?
jhb_pipeline_exit=0
if [ -x .venv/bin/python ]; then
    export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
    export PLAYWRIGHT_BROWSERS_PATH="$PWD/.local-browsers"
    .venv/bin/python -m jhb.applications.cli pipeline --if-enabled >> data/applications.log 2>&1
    jhb_pipeline_exit=$?
fi
if [ "$jhb_poll_exit" -ne 0 ]; then
    exit "$jhb_poll_exit"
fi
exit "$jhb_pipeline_exit"
