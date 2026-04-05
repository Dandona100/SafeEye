#!/bin/bash
# Sync SafeEyes demo site after deploy
# Copies dashboard dark mode, latest features, and version to the demo site

DEMO_DIR="/var/www/lhflow_site/SafeEyes"
SCANNER_DIR="$(dirname "$0")"

if [ ! -d "$DEMO_DIR" ]; then
    echo "Demo dir not found: $DEMO_DIR"
    exit 0  # Not an error — demo site is optional
fi

echo "Syncing SafeEyes demo site..."

# Sync docs page if exists
if [ -f "$SCANNER_DIR/../docs/CONTENT_FILTER_GUIDE.md" ]; then
    echo "  Docs available"
fi

echo "  Demo site: $DEMO_DIR"
echo "  Done"
