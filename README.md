# 🛡️ SafeEyes

**AI-powered content safety scanner** — 20 providers in parallel, REST API, webhooks, batch processing, and a React dashboard.

> **הקב"ה ציווה לשמור את העיניים ממראות אסורות. SafeEyes עוזרת.**
> 
> 🇮🇱 Israeli open-source project. Proudly made in Israel.

[![Demo](https://img.shields.io/badge/Demo-lhflow.com%2FSafeEye-C9A962?style=for-the-badge)](https://lhflow.com/SafeEye/)
[![Docs](https://img.shields.io/badge/Docs-API%20Reference-1A1A1A?style=for-the-badge)](https://lhflow.com/SafeEye/docs.html)

## What It Does

SafeEyes scans images and videos for **pornography, violence, weapons, drugs, and offensive content** — before they reach your users.

- **20 AI providers** scan simultaneously — local models, pip-installable, and cloud APIs
- **Smart voting** — majority rules, weighted by provider accuracy
- **722K+ domain blocklist** — blocks known porn/violence sites before download
- **Video scanning** — extracts up to 30 random frames with jitter
- **Webhooks** — get results via HTTP callback, no polling needed
- **Batch processing** — scan up to 100 URLs in one request
- **Hebrew & English** dashboard with warm, premium design

## Available Providers (20)

Install only what you need — each activates automatically.

### Always Active (no setup)
| Provider | Detects | Size |
|----------|---------|------|
| NudeNet | Nudity (exposed body parts) | 12MB |
| Deepfake Check | Face consistency in video | 0 (OpenCV) |
| Audio Check | Audio metadata | 0 (ffprobe) |

### Install to Activate (pip install)
| Provider | Detects | Install | Size |
|----------|---------|---------|------|
| Marqo NSFW | Nudity (fastest, 98.56%) | `pip install timm torch` | 22MB |
| Falconsai NSFW | Nudity (ViT, 98%) | `pip install transformers torch` | 330MB |
| Freepik NSFW | 4-level: neutral/low/medium/high | `pip install transformers torch` | 330MB |
| SigLIP2 | 5-class incl. hentai/anime | `pip install transformers torch` | 400MB |
| Bumble Private | Lewd content (production-proven) | `pip install tensorflow` | 21MB |
| NSFWJS | 5-class: porn/sexy/hentai/drawing | `pip install onnxruntime` + model file | 10MB |
| Deepfake v2 | Real deepfake detection (92%) | `pip install transformers torch` | 330MB |
| YOLOv8 Weapons | Guns, knives | `pip install ultralytics` + model | 6MB |
| Detoxify | Text toxicity (6 categories) | `pip install detoxify` | 65MB |
| Hate Speech | Hate speech in text | `pip install transformers torch` | 440MB |

### API Providers (need API key)
| Provider | Detects | Config |
|----------|---------|--------|
| Sightengine | Nudity, violence, drugs, weapons | `SIGHTENGINE_API_USER/SECRET` |
| Google Vision | SafeSearch | `GOOGLE_VISION_CREDENTIALS` |
| Amazon Rekognition | Nudity, violence | `AWS_ACCESS_KEY_ID/SECRET` |
| Azure Content Safety | Hate, self-harm, sexual, violence | `AZURE_CONTENT_SAFETY_KEY` |
| PicPurify | Nudity, gore, drugs, weapons | `PICPURIFY_API_KEY` |
| ModerateContent | Adult, violence | `MODERATECONTENT_API_KEY` |
| CLIP Search | Zero-shot classification | `HF_API_TOKEN` |

## Quick Start

```bash
git clone https://github.com/Dandona100/SafeEye.git
cd SafeEye
docker compose up -d
```

That's it. A master token is auto-generated on first run — check the logs with `docker compose logs` to see it.

Dashboard: http://localhost:1985/dashboard
Health: http://localhost:1985/health

### Create a token

```bash
curl -X POST http://localhost:1985/api/v1/admin/tokens \
  -H "Authorization: Bearer YOUR_MASTER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"my-bot"}'
```

### Scan a file

```bash
curl -X POST http://localhost:1985/api/v1/scan/file \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -F "file=@photo.jpg"
```

### Async scan with webhook

```bash
curl -X POST "http://localhost:1985/api/v1/scan/async?url=https://example.com/image.jpg&webhook_url=https://myserver.com/callback" \
  -H "Authorization: Bearer YOUR_TOKEN"
# Returns immediately: {"job_id": "abc123", "status": "pending"}
# SafeEyes POSTs result to your webhook when done
```

### Batch scan

```bash
curl -X POST http://localhost:1985/api/v1/scan/batch \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"urls": ["url1.jpg", "url2.jpg", "url3.jpg"], "webhook_url": "https://myserver.com/callback"}'
# Returns: {"batch_id": "batch_xyz", "total": 3}
```

## API Reference

### Scanning

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/api/v1/scan/file` | Token | Upload and scan a file (sync) |
| POST | `/api/v1/scan/url` | Token | Scan a URL (sync) |
| POST | `/api/v1/scan/async` | Token | Async scan — returns job_id immediately |
| POST | `/api/v1/scan/batch` | Token | Batch scan — up to 100 URLs |
| GET | `/api/v1/job/{id}` | Token | Poll async job status |
| GET | `/api/v1/batch/{id}` | Token | Batch progress + results |
| POST | `/api/v1/demo/scan` | Public | Demo endpoint (no auth, no persist) |

### Stats & Feedback

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/v1/stats` | Token | Overview statistics |
| GET | `/api/v1/stats/providers` | Token | Per-provider metrics |
| GET | `/api/v1/stats/history` | Token | Scan history |
| POST | `/api/v1/feedback/{id}` | Token | Submit accuracy feedback |

### Admin

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/api/v1/admin/tokens` | Master | Create API token |
| DELETE | `/api/v1/admin/tokens/{name}` | Master | Revoke token |
| GET | `/api/v1/admin/tokens` | Master | List all tokens |
| GET | `/api/v1/admin/check-update` | Master | Check for new version |

### Community

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/v1/community` | Public | List bug reports & feature suggestions |
| POST | `/api/v1/community` | Public | Submit report (UUID tracked) |
| POST | `/api/v1/community/{id}/vote` | Public | Vote (one per device) |

## How Voting Works

All configured providers scan **in parallel**. Results are aggregated:

- **Any provider** flags with confidence ≥ 75% → **NSFW**
- **2+ providers** flag (any confidence) → **NSFW** (majority vote)
- **1 provider** flags with < 75% → **Borderline** (caller decides)
- Confidence = weighted average (NudeNet 1.0, Sightengine 1.2, Google 1.1, Amazon 1.2, Azure 1.2, PicPurify 1.1)

## What It Detects

| Category | Labels | Providers |
|----------|--------|-----------|
| 🔞 Nudity & Pornography | sexual_activity, sexual_display, erotica | NudeNet, Sightengine, Google, Azure |
| 🔪 Violence & Gore | gore, serious_injury, corpse | Sightengine, Google, Azure |
| 🔫 Weapons | weapon_firearm (99%), weapon_knife | Sightengine, PicPurify |
| 💊 Drugs | recreational_drug, cannabis | Sightengine, PicPurify |
| 🚫 Offensive | nazi, confederate, hate symbols | Sightengine |
| 🌐 Domain Blocklist | 722,000+ domains | Built-in |

## Dashboard

Access at `http://localhost:1985/dashboard`

9 tabs: Dashboard, History, Providers, Tokens, Integrations, API Docs, Domain, Report, About

- **Skeleton loaders** — no more "Loading..." text
- **Stagger animations** — elements fade in sequentially
- **Progress bars** — see scan status in real-time
- **Interactive terminal** — test API calls from the browser
- **Hebrew & English** — full RTL support

## System Requirements

| Resource | Minimum | Recommended |
|----------|---------|-------------|
| CPU | 2 cores | 4 cores |
| RAM | 512MB | 2GB |
| Storage | 1GB | 2GB |
| GPU | Not required | Not required |
| Docker | Required | Required |
| Port | 1985 (configurable) | — |

NudeNet runs on CPU. Works offline after first model download.

## Configuration

See [.env.example](.env.example) for all options:

- `SCAN_API_MASTER_TOKEN` — Admin token (required)
- `SCAN_PORT` — Server port (default: 1985)
- `SIGHTENGINE_API_USER/SECRET` — Enable Sightengine
- `GOOGLE_VISION_CREDENTIALS` — Enable Google Vision
- `AWS_ACCESS_KEY_ID/SECRET` — Enable Amazon Rekognition
- `AZURE_CONTENT_SAFETY_KEY/ENDPOINT` — Enable Azure
- `PICPURIFY_API_KEY` — Enable PicPurify
- `NSFW_BLOCKLIST_AUTO_UPDATE` — Auto-update domain list
- `LOG_LEVEL` — Debug verbosity (default: info)

## Contributing

We welcome contributions! Here's how:

- 🐛 **Report bugs** — [Open an issue](https://github.com/Dandona100/SafeEye/issues/new?labels=bug)
- 💡 **Suggest features** — [Open an issue](https://github.com/Dandona100/SafeEye/issues/new?labels=enhancement)
- 🔀 **Submit PRs** — [Pull requests](https://github.com/Dandona100/SafeEye/pulls)
- ⭐ **Star us** — It helps!

### UI Contributions Welcome! 🎨

> *The UI is functional and warm-themed, but we'd love to see what the community can do with it. PRs for design improvements, animations, mobile optimizations, and accessibility are especially welcome!*

Areas that could use love:
- Mobile-first responsive improvements
- Advanced data visualizations (D3.js, recharts)
- Accessibility (ARIA, keyboard navigation)
- Dark mode toggle
- Framer Motion / GSAP animations

## Architecture Note

**SafeEyes does NOT store scanned files.** Files are processed in memory and deleted immediately after scanning. Only scan results (JSON metadata) are persisted in SQLite. This is by design — privacy first.

## New Features

### 🔍 Perceptual Hashing (pHash)
Every scan now computes a perceptual hash of the image. This enables detecting edited, cropped, or watermarked versions of the same content.

```bash
# Scan returns pHash
curl -X POST .../api/v1/scan/file -F "file=@photo.jpg" -H "Authorization: Bearer TOKEN"
# Response includes: "phash": "ff8fc3e07018040707"

# Find similar images (hamming distance < 10)
curl ".../api/v1/scan/similar?phash=ff8fc3e07018040707" -H "Authorization: Bearer TOKEN"
```

### 📡 Live Stream Monitoring
Monitor RTMP/HLS streams in real-time. SafeEyes extracts frames periodically and scans them.

```bash
# Start monitoring
curl -X POST .../api/v1/stream/start -H "Authorization: Bearer MASTER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://stream.example.com/live.m3u8", "interval": 10, "webhook_url": "https://myserver.com/alert"}'

# Check status
curl .../api/v1/stream/status -H "Authorization: Bearer TOKEN"

# Stop
curl -X POST .../api/v1/stream/stop -H "Authorization: Bearer MASTER_TOKEN" \
  -d '{"url": "https://stream.example.com/live.m3u8"}'
```

### 🧩 Browser Extension
Right-click any image on the web → "Scan with SafeEyes". Chrome extension included.

**Install:** `chrome://extensions/` → Developer Mode → Load unpacked → select `nsfw_scanner/extension/`

**Features:**
- Context menu on all images
- Badge shows result (green ✓ / red ✗)
- Toast notification on the page
- Settings: configure server URL + API token
- Popup with detailed results + per-provider breakdown

## Roadmap

### Near-term
- [x] ~~🧩 Browser extension~~ ✅
- [x] ~~🔍 Perceptual hashing (pHash)~~ ✅
- [x] ~~📡 Live stream monitoring~~ ✅
- [ ] 🎭 **Privacy masking** — auto-blur faces and PII before analysis
- [ ] 📊 **Advanced dashboard analytics** — D3.js visualizations

### Medium-term
- [ ] 🤖 **Deepfake detection** — pixel consistency analysis between frames
- [ ] 🔎 **CLIP-based search** — find content by text description
- [ ] 🔄 **Delta detection** — what changed between two versions

### Long-term
- [ ] 🧠 **Vector database (Pinecone/Milvus)** — visual similarity search at scale
- [ ] ⚡ **WASM client-side preprocessing** — hash/analyze in browser before upload
- [ ] 🗺️ **Command Center UI** — graph view of content propagation

> Want to work on any of these? [Open a PR!](https://github.com/Dandona100/SafeEye/pulls)

## License

MIT

## Author

**DVS Technology** — [@DVS20](https://t.me/DVS20)

🇮🇱 Proudly made in Israel.
