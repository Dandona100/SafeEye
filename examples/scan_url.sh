#!/bin/bash
# Quick scan a URL from command line
# Usage: ./scan_url.sh https://example.com/image.jpg

URL="${1:?Usage: $0 <image_url>}"
SAFEEYE="${SAFEEYE_URL:-http://localhost:1985}"
TOKEN="${SAFEEYE_TOKEN:?Set SAFEEYE_TOKEN env var}"

echo "Scanning: $URL"

RESULT=$(curl -s -X POST "${SAFEEYE}/api/v1/scan/url?url=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$URL'))")" \
  -H "Authorization: Bearer $TOKEN")

IS_NSFW=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['is_nsfw'])" 2>/dev/null)
LABELS=$(echo "$RESULT" | python3 -c "import sys,json; print(', '.join(json.load(sys.stdin)['result']['labels']))" 2>/dev/null)
CONF=$(echo "$RESULT" | python3 -c "import sys,json; print(f\"{json.load(sys.stdin)['result']['confidence']*100:.0f}%\")" 2>/dev/null)

if [ "$IS_NSFW" = "True" ]; then
    echo "🚨 NSFW detected! ($CONF)"
    echo "   Labels: $LABELS"
    exit 1
else
    echo "✅ Safe"
    exit 0
fi
