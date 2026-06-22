#!/usr/bin/env bash
# Monthly runner for the Sony Pictures catalog scraper.
# Invoked by launchd via com.tubiagent.monthly.plist — do not rely on user PATH.
#
# Behavior: fresh full crawl into data/catalog_YYYYMM.jsonl for the current
# month, then one auto-retry pass on errors, then Sony CSV export. Per-title
# metadata (availability windows, ratings, posters) changes month over month
# so each month must be a fresh capture. The script's resume mechanism only
# kicks in if a single month's run gets interrupted partway. Total runtime:
# ~2 hours full + ~6 min retry.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"
LOGFILE="$LOG_DIR/sony_$(date +%Y%m).log"

UV="$(command -v uv 2>/dev/null \
    || ls "$HOME/.local/bin/uv" 2>/dev/null \
    || ls /opt/homebrew/bin/uv 2>/dev/null \
    || ls /usr/local/bin/uv 2>/dev/null)"

if [ -z "$UV" ]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] ERROR: uv not found" | tee -a "$LOGFILE"
    exit 127
fi

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOGFILE"; }

log "Starting Sony catalog scrape (uv: $UV)"

EXIT_CODE=0
"$UV" run --project "$SCRIPT_DIR" python "$SCRIPT_DIR/src/scrape_sony_catalog.py" \
    --workers 8 \
    2>&1 | tee -a "$LOGFILE" || EXIT_CODE=$?

if [ "$EXIT_CODE" -ne 0 ]; then
    log "FAILED with exit code $EXIT_CODE"
else
    log "Done"
fi

exit "$EXIT_CODE"
