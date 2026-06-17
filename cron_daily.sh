#!/usr/bin/env bash
# Cron runner for the Tubi scraper.
# Schedule daily at 7am PT by adding to crontab (crontab -e):
#   0 14 * * * /full/path/to/tubi-agent/cron_daily.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"

LOGFILE="$LOG_DIR/scrape_$(date +%Y%m%d).log"

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Starting scrape" | tee -a "$LOGFILE"

uv run --no-project python "$SCRIPT_DIR/src/scrape.py" 2>&1 | tee -a "$LOGFILE"

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Done" | tee -a "$LOGFILE"
