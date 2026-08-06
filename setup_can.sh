#!/bin/bash
# Backwards-compatible entry point. The v2 supervisor remains running and
# recreates can0 after USB disconnects or interface loss.
set -e
exec "$(cd "$(dirname "$0")" && pwd)/can_supervisor.sh"
