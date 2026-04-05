# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

SafeEye is an AI-powered content safety scanner. It runs up to 20 AI providers in parallel to detect NSFW content, violence, weapons, and offensive material in images and videos. Results are aggregated via weighted voting.

- **Backend**: Python 3.11+ / FastAPI / Uvicorn / SQLite (aiosqlite, WAL mode)
- **Frontend**: Single-file React 18 app (`nsfw_scanner/static/dashboard.html`) — no build step, Babel compiles JSX in-browser, Tailwind CSS 4 via CDN
- **Browser Extension**: Chrome MV3 (`extension/`) — vanilla JS
- **Deployment**: Docker Compose

## Commands

```bash
# Run server locally
python -m nsfw_scanner

# Run tests
pip install pytest
pytest nsfw_scanner/tests/ -v

# Run a single test
pytest nsfw_scanner/tests/test_scanner.py -v -k "test_name"

# Docker
docker build -t safeeye .
docker compose up -d
docker compose logs -f safeeye

# Health check
curl http://localhost:1985/health
```

## Architecture

### Backend (`nsfw_scanner/`)

- **Entry point**: `__main__.py` — port detection, token generation, system checks, launches Uvicorn
- **API**: `app.py` (~2000 lines) — all FastAPI routes. Key endpoint groups:
  - Scanning: `POST /api/v1/scan/file`, `/scan/url`, `/scan/async`, `/scan/batch`
  - Stats: `GET /api/v1/stats`, `/stats/providers`, `/stats/history`
  - Admin: `POST /api/v1/admin/tokens` (master token only)
  - Stream: `POST /api/v1/stream/start|stop`, `GET /api/v1/stream/status`
  - Community: `GET|POST /api/v1/community`
- **Scanner**: `scanner.py` — orchestrates parallel provider execution with `asyncio`, applies timeout per provider (default 15s), aggregates results via weighted voting
- **Database**: `db.py` — SQLite schema with tables: `scan_history`, `provider_results`, `accuracy_feedback`, `api_tokens`, `provider_config`, `jobs`, `community_reports`
- **Auth**: `auth.py` — Bearer token auth. Master token from `SCAN_API_MASTER_TOKEN` env var; API tokens are SHA256-hashed in DB
- **Models**: `models.py` — Pydantic models (`ProviderResult`, `AggregatedResult`, etc.)

### Provider System (`nsfw_scanner/providers/`)

All 20 providers extend `BaseProvider` (in `base.py`) with two methods:
- `is_configured() -> bool` — checks if dependencies/API keys are available
- `async scan(file_path: str) -> ProviderResult` — performs the scan

Providers activate automatically based on installed packages or configured API keys. NudeNet is always active (included in requirements.txt).

**Voting logic** in `scanner.py`: any provider at >=75% confidence → NSFW; 2+ providers flagging at any confidence → NSFW; single provider <75% → Borderline. Confidence is weighted average using `_WEIGHTS` dict.

### Frontend (`nsfw_scanner/static/dashboard.html`)

Single ~3500-line HTML file containing React components, Tailwind styles, and Chart.js visualizations. 9 tabs. Built-in Hebrew (RTL) + English localization with 170+ translation keys in `LANGS` object. Dark/light mode via localStorage.

### Chrome Extension (`extension/`)

MV3 service worker architecture. Right-click image → scan via `/api/v1/scan/file`. Settings stored in `chrome.storage.sync`.

## Key Patterns

- **Async throughout**: all scanning, DB access, and HTTP calls are async
- **No file storage**: scanned files are processed in memory, only JSON metadata is persisted
- **Rate limiting**: 30 scans/minute per token (configurable via `RATE_LIMIT_PER_MINUTE`)
- **Perceptual hashing**: dHash on 8x8 grid for duplicate/similar image detection
- **Token isolation**: each API token only sees its own scan history

## Configuration

Primary config is via environment variables (see `.env.example`). Key vars:
- `SCAN_API_MASTER_TOKEN` — admin token (auto-generated if not set)
- `SCAN_PORT` — server port (default 1985)
- `PROVIDER_TIMEOUT_SECONDS` — per-provider timeout (default 15)
- Cloud provider API keys: `SIGHTENGINE_API_USER/SECRET`, `GOOGLE_VISION_CREDENTIALS`, `AWS_ACCESS_KEY_ID/SECRET`, `AZURE_CONTENT_SAFETY_KEY/ENDPOINT`, `PICPURIFY_API_KEY`, `MODERATECONTENT_API_KEY`, `HF_API_TOKEN`

## CI

GitHub Actions (`.github/workflows/test.yml`): runs pytest on Python 3.11 and builds+health-checks Docker image on push/PR to main.
