# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 6.x     | :white_check_mark: |
| < 6.0   | :x:                |

## Reporting a Vulnerability

If you discover a security vulnerability in SafeEyes, please report it responsibly:

1. **Do NOT open a public issue**
2. **Contact directly**: [Telegram @DVS20](https://t.me/DVS20)
3. **Include**: Description, steps to reproduce, and potential impact.

We respond within 48 hours and aim to release a fix within 7 days for critical issues.

## Security Design & Privacy

SafeEyes is built on a **Privacy-First, Local-First** architecture:

- **Memory-Only Processing** — Scanned files are processed in-memory and never written to disk. They are destroyed immediately after analysis.
- **Metadata Persistence** — Only scan results (JSON) and perceptual hashes (pHash) are stored in the local SQLite database.
- **Air-Gapped Ready** — Local providers (NudeNet, Deepfake Check) run fully offline. No data leaves your network unless you explicitly enable Cloud APIs (Google/AWS/Azure).
- **Identity Isolation** — Uses Bearer Token authentication. Each token is isolated to its own scan history.
- **Admin Lockdown** — Sensitive operations (token creation, updates) require a dedicated `SCAN_API_MASTER_TOKEN`.

## Webhooks & Local-Network Compatibility

To ensure SafeEyes works "out-of-the-box" in local environments, home labs, and internal networks without requiring public domains or SSL certificates, we use a **Zero-Config Webhook** approach:

- **Simple HTTP Delivery** — Results are POSTed directly to your specified `webhook_url`. 
- **Internal Security** — Since SafeEyes is often deployed behind a firewall or on `localhost`, we do not enforce HMAC signatures by default to avoid integration complexity.
- **Recommendation** — For production use, we recommend securing your webhook endpoints using internal IP whitelisting or verifying custom headers if passed through a reverse proxy.

## Known Considerations

- **Public Demo Endpoint**: The `/api/v1/demo/scan` is open for testing and does not persist any data. Use it only for ephemeral testing.
- **GitHub Auto-Update**: The `/api/v1/webhook/github` endpoint triggers a pull/rebuild. For enhanced security, restrict access to this endpoint to GitHub's official IP ranges via your firewall or Nginx configuration.
- **Cloud Providers**: When enabling Cloud APIs (e.g., Sightengine, AWS), images are temporarily transmitted to their servers. Refer to their respective privacy policies for data handling.

---
**SafeEyes** — Dedicated to digital purity and technical excellence. 
🇮🇱 Proudly made in Israel.
