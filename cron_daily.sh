#!/usr/bin/env bash
# Daily runner for the Tubi scraper.
# Invoked by launchd via com.tubiagent.daily.plist — do not rely on user PATH.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"
LOGFILE="$LOG_DIR/scrape_$(date +%Y%m%d).log"

# launchd strips PATH; resolve uv explicitly
UV="$(command -v uv 2>/dev/null \
    || echo "/Users/ruichenyang/miniconda3/bin/uv")"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOGFILE"; }

log "Starting scrape (uv: $UV)"

EXIT_CODE=0
"$UV" run --no-project python "$SCRIPT_DIR/src/scrape_carousels.py" \
    2>&1 | tee -a "$LOGFILE" || EXIT_CODE=$?

if [ "$EXIT_CODE" -ne 0 ]; then
    log "FAILED with exit code $EXIT_CODE"
else
    log "Done"
fi

exit "$EXIT_CODE"
