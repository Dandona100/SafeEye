#!/bin/bash
# SafeEyes auto-deploy: pull latest from GitHub, rebuild, sync demo site
# Can be triggered by webhook or cron

set -e
cd "$(dirname "$0")/.."

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
LOG="/tmp/safeeye_deploy.log"

echo "$(date) — SafeEyes auto-deploy started" | tee -a "$LOG"

# 1. Pull latest
echo -e "${YELLOW}Pulling from GitHub...${NC}" | tee -a "$LOG"
git pull origin main 2>&1 | tee -a "$LOG"

# 2. Rebuild scanner
echo -e "${YELLOW}Rebuilding scanner...${NC}" | tee -a "$LOG"
docker compose build safeeye 2>&1 | tail -3 | tee -a "$LOG"
docker compose up -d safeeye 2>&1 | tee -a "$LOG"

# 3. Sync demo site
DEMO_DIR="/var/www/lhflow_site/SafeEyes"
if [ -d "$DEMO_DIR" ]; then
    echo -e "${YELLOW}Syncing demo site...${NC}" | tee -a "$LOG"
    # Copy static dashboard as fallback
    cp nsfw_scanner/static/dashboard.html "$DEMO_DIR/dashboard.html" 2>/dev/null
    # Copy docs
    cp docs/CONTENT_FILTER_GUIDE.md "$DEMO_DIR/SETUP_GUIDE.md" 2>/dev/null
    echo -e "${GREEN}Demo site synced${NC}" | tee -a "$LOG"
fi

# 4. Health check
sleep 10
if curl -sf http://localhost:1985/health > /dev/null 2>&1; then
    echo -e "${GREEN}$(date) — Deploy successful!${NC}" | tee -a "$LOG"
else
    echo -e "${RED}$(date) — Health check failed!${NC}" | tee -a "$LOG"
    exit 1
fi
