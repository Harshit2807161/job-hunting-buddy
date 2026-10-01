#!/bin/bash
# Runs one poll cycle. Called by launchd every 15 minutes (see register_launchd.sh).
# Calls the env's python directly -- no conda activation needed.
cd "$(dirname "$0")" || exit 1
mkdir -p data
exec "$HOME/miniforge3/envs/jhb/bin/python" -m jhb.poll >> data/poll.log 2>&1
