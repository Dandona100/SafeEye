# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 6.x     | :white_check_mark: |
| < 6.0   | :x:                |

## Reporting a Vulnerability

If you discover a security vulnerability in SafeEye, please report it responsibly:

1. **Do NOT open a public issue**
2. **Contact directly**: [Telegram @DVS20](https://t.me/DVS20)
3. **Include**: description, steps to reproduce, potential impact

We will respond within 48 hours and aim to release a fix within 7 days for critical issues.

## Security Design

SafeEye is designed with privacy in mind:

- **No file storage** — scanned files are processed in memory and deleted immediately
- **Only metadata persisted** — scan results (JSON) are stored in SQLite, never the original files
- **Token-based auth** — all API endpoints require Bearer tokens
- **Master token isolation** — admin operations require a separate master token
- **Multi-tenant** — each token sees only its own scan history
- **No outbound data** — local providers (NudeNet, etc.) run fully offline

## Known Considerations

- The `/api/v1/demo/scan` endpoint is public (no auth). It does not persist results.
- Webhook delivery sends scan results to user-specified URLs — ensure your webhook endpoints are secured.
- The GitHub auto-deploy webhook (`/api/v1/webhook/github`) does not verify GitHub signatures. Use network-level restrictions if needed.
