#!/bin/bash
# SafeEyes domain setup — configures nginx reverse proxy + SSL
# Usage: sudo ./setup_domain.sh <domain> [port]
# Example: sudo ./setup_domain.sh scanner.example.com 1985

set -e

DOMAIN="${1:?Usage: $0 <domain> [port]}"
PORT="${2:-1985}"
NGINX_CONF="/etc/nginx/sites-available/safeeye-${DOMAIN}"
NGINX_LINK="/etc/nginx/sites-enabled/safeeye-${DOMAIN}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

echo -e "${YELLOW}=== SafeEyes Domain Setup ===${NC}"
echo "Domain: $DOMAIN"
echo "Port:   $PORT"

# Check running as root
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}Run with sudo${NC}"
    exit 1
fi

# Check nginx installed
if ! command -v nginx &>/dev/null; then
    echo -e "${RED}nginx not installed${NC}"
    exit 1
fi

# Check port is listening
if ! ss -tlnp | grep -q ":${PORT} "; then
    echo -e "${RED}Nothing listening on port ${PORT}${NC}"
    exit 1
fi

# Create nginx config
echo -e "${YELLOW}Creating nginx config...${NC}"
cat > "$NGINX_CONF" <<NGINX
server {
    listen 80;
    server_name ${DOMAIN};

    location / {
        proxy_pass http://127.0.0.1:${PORT};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;

        # WebSocket support (for future use)
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";

        # File upload size (for scan uploads)
        client_max_body_size 50M;
    }
}
NGINX

# Enable site
ln -sf "$NGINX_CONF" "$NGINX_LINK"

# Test nginx config
echo -e "${YELLOW}Testing nginx config...${NC}"
nginx -t 2>&1

# Reload nginx
nginx -s reload
echo -e "${GREEN}Nginx configured and reloaded${NC}"

# Get SSL certificate
if command -v certbot &>/dev/null; then
    echo -e "${YELLOW}Getting SSL certificate...${NC}"
    certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --redirect 2>&1 || {
        echo -e "${YELLOW}Certbot failed. You can run manually:${NC}"
        echo "  sudo certbot --nginx -d $DOMAIN"
    }
else
    echo -e "${YELLOW}Certbot not installed. Install with:${NC}"
    echo "  sudo apt install certbot python3-certbot-nginx"
    echo "  sudo certbot --nginx -d $DOMAIN"
fi

echo ""
echo -e "${GREEN}=== Done ===${NC}"
echo -e "Dashboard: https://${DOMAIN}/dashboard"
echo -e "API:       https://${DOMAIN}/api/v1/"
echo -e "Health:    https://${DOMAIN}/health"
