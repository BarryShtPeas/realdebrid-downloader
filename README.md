# Real-Debrid Downloader

Small self-hosted web app for submitting Real-Debrid-supported hoster links and handing the unrestricted download URL to an internal aria2 downloader.

## Purpose

This app provides a simple web form for Rapidgator and other Real-Debrid-supported hoster URLs. Paste a single link or free text containing multiple links, and the app extracts URLs, unrestricts them through Real-Debrid, and hands the direct downloads to an internal aria2 worker. Multipart archive submissions are tracked as one group and extracted automatically after all parts complete.

The runtime model is:

- `rd-downloader`: web app container and Real-Debrid API client.
- `rd-aria2`: internal aria2 JSON-RPC downloader.
- Shared `/downloads` mount where aria2 writes completed files.
- Persistent `/config` mount for app state.

## Download Flow

1. The operator submits a hoster URL or free text containing multiple hoster URLs.
2. The app checks Real-Debrid supported hosts through `/hosts/domains` when that metadata is available.
3. The app calls `/unrestrict/check` to check whether Real-Debrid currently has a downloadable file for that link.
4. The app calls `/unrestrict/link` with the submitted URL.
5. If Real-Debrid returns a generated direct download URL, the app submits it to aria2 using JSON-RPC.
6. The web UI shows the filename, aria2 id, and generated Real-Debrid direct download URL.
7. aria2 downloads the file to `/downloads`.

When multiple URLs are pasted, the app preserves the first occurrence of each exact URL and submits each URL independently. Submitted multipart parts are stored as one group in `/config/download-groups.json` by default. The group state stores original hostnames, aria2 ids, filenames, local paths, status, and extraction state; it does not store full submitted URLs.

The app polls aria2 for tracked groups every 30 seconds by default. After every part in a group reaches `complete`, it looks for a supported archive start file, preferring `.part1.rar`, then `.rar`, `.zip`, and `.7z`. Extraction runs with 7-Zip into `/downloads/<group-name>/`. Archive part files are deleted only after extraction succeeds; failed extraction leaves archive files in place and marks the group failed. Completed non-archive downloads, such as `.dmg` files, are marked `skipped` for extraction and are left in place.

The queue page also opens a browser-local Server-Sent Events stream to refresh queue and multipart group progress every second by default. The stream contains only rendered queue HTML from sanitized aria2 and app metadata; it does not expose the aria2 RPC URL, aria2 RPC secret, submitted URLs, or Real-Debrid API token.

If supported-host data is unavailable or inconclusive, the app still attempts the unrestrict step. When Real-Debrid cannot produce a usable download URL, the web UI shows this user-facing message:

```text
No download available from Real-Debrid for this link.
```

## Real-Debrid API Notes

The Real-Debrid REST API base URL is:

```text
https://api.real-debrid.com/rest/1.0/
```

Relevant endpoints for the initial implementation:

| Endpoint | Use |
| --- | --- |
| `GET /hosts/domains` | Fetch supported hoster domains without authentication. |
| `GET /hosts`, `GET /hosts/status`, `GET /hosts/regex` | Optional supported-host and matching metadata. |
| `POST /unrestrict/check` | Check whether a hoster link is currently downloadable. |
| `POST /unrestrict/link` | Generate an unrestricted direct download URL. |

Most Real-Debrid endpoints require authentication. Do not include `REALDEBRID_API_TOKEN` in logs, rendered pages, screenshots, examples, or issue text.

## Environment

Copy `.env.example` to `.env` for local development and fill in only local secrets.

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `REALDEBRID_API_TOKEN` | yes | none | Real-Debrid API token. Never commit a real value. |
| `REALDEBRID_API_BASE_URL` | no | `https://api.real-debrid.com/rest/1.0` | Override only for tests or API-compatible mocks. |
| `APP_HOST` | no | `0.0.0.0` | Uvicorn bind host. |
| `APP_PORT` | no | `8080` | Container listen port. |
| `APP_LOG_LEVEL` | no | `info` | Application log level. |
| `APP_DOWNLOAD_DIR` | no | `/downloads` | Container path shared with aria2. |
| `APP_CONFIG_DIR` | no | `/config` | Persistent state/config path. |
| `APP_SUBMITTED_URL_LOGGING` | no | `false` | Keep false unless debugging with redaction. |
| `APP_GROUP_STATE_FILE` | no | `/config/download-groups.json` | Persistent multipart group state file. |
| `APP_EXTRACT_TIMEOUT_SECONDS` | no | `7200` | Maximum 7-Zip extraction time per group. |
| `APP_GROUP_POLL_SECONDS` | no | `30` | aria2 polling interval for tracked multipart groups. Set to `0` to disable the background poller. |
| `APP_QUEUE_STREAM_INTERVAL_SECONDS` | no | `1` | Server-Sent Events refresh interval for the `/queue` page. |
| `ARIA2_RPC_URL` | yes | `http://rd-aria2:6800/jsonrpc` | Internal aria2 JSON-RPC URL. |
| `ARIA2_RPC_SECRET` | recommended | none | aria2 RPC token. |
| `ARIA2_DOWNLOAD_DIR` | no | `/downloads` | Directory passed to aria2. |
| `ARIA2_MAX_CONNECTION_PER_SERVER` | no | `8` | aria2 connection tuning. |
| `ARIA2_SPLIT` | no | `8` | aria2 split tuning. |

## Queue Management

Open `/queue` to view and manage the internal aria2 queue. The page is server-rendered for initial load and no-JavaScript fallback. When JavaScript is available, it connects to `/queue/events` with Server-Sent Events and refreshes the queue sections without a manual page reload. All aria2 JSON-RPC calls still happen from the app container, so the aria2 RPC endpoint and RPC secret are not exposed to the browser.

The queue page shows:

- Multipart groups, including aggregate progress, part status, and extraction status.
- Active downloads.
- Waiting or paused downloads.
- Recently stopped, completed, removed, or failed downloads.

Supported controls:

- Pause, resume, and remove active/waiting downloads.
- Move waiting or paused downloads to the top, up, down, or bottom.
- Clear individual stopped history entries.
- Clear all stopped history entries.
- Clear eligible multipart group history entries individually.
- Clear all eligible multipart group history entries.

Multipart group clearing removes only persisted app metadata from `/config/download-groups.json`; it never deletes archive files, extracted files, or `/downloads` contents. A group can be cleared only after all tracked parts are in terminal aria2 states such as `complete`, `error`, or `removed`, or after extraction is marked `complete`, `failed`, or `skipped`. Active, waiting, paused, submitted, pending, or extracting groups remain visible and cannot be cleared.

Queue actions redirect back to `/queue`. Live updates resume after the redirect.

## API and Swagger

RDD exposes a JSON API for integrations such as browser extensions:

- `GET /api/version` returns the app name and semantic version.
- `POST /api/submit` accepts `{"url": "..."}` where the value can be one URL or free text containing multiple URLs.
- `GET /api/queue` returns sanitized queue and multipart group state.
- `POST /api/queue/{gid}/pause`, `/resume`, `/remove`, and `/clear` control aria2 queue entries.
- `POST /api/queue/clear-stopped` clears aria2 stopped history.
- `POST /api/queue/groups/{group_id}/clear` and `/api/queue/groups/clear` clear eligible multipart group metadata.

Swagger UI is available at `/docs`, and the OpenAPI definition is available at `/openapi.json`. UI-only routes such as `/submit`, `/queue/events`, and redirect-based queue controls are intentionally excluded from the API schema.

API responses are sanitized for clients. They include useful status, filename, progress, group, and aria2 id fields, but they do not include Real-Debrid tokens, aria2 RPC details, submitted full URLs, generated direct Real-Debrid URLs, or local download paths.

## Firefox Extension

An unpacked local Firefox WebExtension is available in `extensions/firefox-rdd`.
It adds context-menu actions for sending links or selected text to an RDD
instance through `POST /api/submit`, and it tests connectivity with
`GET /api/version`.

The extension stores only the configured RDD base URL in Firefox extension
storage. It does not store Real-Debrid tokens, aria2 RPC secrets, submitted
URLs, or cookies.

## Local Development

Required local tooling:

- Python 3.12.
- `pip`, or `uv` when Python is not installed in the development container.
- Docker with the Compose plugin for running the full app plus aria2 stack.
- 7-Zip (`7z`) only when testing archive extraction outside the app Docker image.

The production app image installs Python dependencies and 7-Zip through the
`Dockerfile`. Local Docker/Compose development still requires access to a Docker
daemon; a locked-down Codex or CI helper container must either expose a Docker
socket or bake Docker tooling into that helper image.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

Run tests:

```bash
pytest
```

When using `uv` in a restricted container, keep the cache in a writable path:

```bash
UV_CACHE_DIR="$PWD/.uv-cache" uv venv --python 3.12
UV_CACHE_DIR="$PWD/.uv-cache" uv pip install -r requirements-dev.txt --python .venv/bin/python
.venv/bin/python -m pytest
```

Run the app with aria2:

```bash
docker compose -f compose.example.yml up --build
```

## Docker Image

GitHub Actions publishes the public image to:

```text
ghcr.io/barryshtpeas/realdebrid-downloader
```

Production tags:

- `latest` from the default branch.
- Branch/SHA tags for traceable builds.
- Release tag names when a Git tag is pushed.

The app version uses `major.minor.bugfix` semantic versioning and is defined in `app/__init__.py`. Release tags should use `vX.Y.Z` and match the app version displayed in the web UI and `/api/version`.

Development tags are published by the `Build and publish dev container`
GitHub Actions workflow when changes are pushed to `dev`, or when the workflow is
run manually:

- `dev` for the latest manually published development image.
- `dev-<shortsha>` for a pinned development image built from a specific commit.

To publish a dev image, merge or push the change to `dev`. For an ad hoc build,
open GitHub Actions, run `Build and publish dev container`, and set `ref` to a
branch, tag, or SHA. Deploy `ghcr.io/barryshtpeas/realdebrid-downloader:dev` for
quick testing, or use the matching `dev-<shortsha>` tag when you want a
rollback-safe pinned image. The dev workflow never updates `latest`.

Run the image directly:

```bash
docker run --rm -p 8080:8080 \
  --env-file .env \
  -v "$PWD/config:/config" \
  -v "$PWD/downloads:/downloads" \
  ghcr.io/barryshtpeas/realdebrid-downloader:latest
```

## Deploy with Docker Compose

`compose.example.yml` is a ready-to-run stack (`rd-downloader` + `rd-aria2`).

```bash
cp .env.example .env
# Edit .env: set REALDEBRID_API_TOKEN and ARIA2_RPC_SECRET,
# and set DOWNLOAD_DIR to your own host download directory.
chmod 600 .env
docker compose -f compose.example.yml up -d
```

Then open `http://localhost:8080`.

### Mapping your host download directory

aria2 writes completed files to `/downloads` inside the container, and the app container also mounts that path so it can extract completed archive groups. Point that at any host directory with `DOWNLOAD_DIR` in `.env`:

```env
DOWNLOAD_DIR=/mnt/media/downloads
```

`CONFIG_DIR` and `ARIA2_CONFIG_DIR` map persistent app/aria2 state the same way. All default to local `./` folders if unset.

### Keeping the API token secure

- The Real-Debrid token is read only from `.env` via the compose `env_file` directive. It is never written into `compose.example.yml`.
- `.env` is gitignored, so secrets are not committed. Run `chmod 600 .env` to restrict local read access.
- For hardened setups, Docker secrets can be used instead of an env file.

### Optional: reverse proxy

To serve the app on a domain, put it behind a reverse proxy (Traefik, Caddy, nginx) pointing at the `rd-downloader` container on port `8080`. Keep `rd-aria2` internal to the Docker network — do not expose its RPC port to the host.

## Security

- Never commit real Real-Debrid or GitHub tokens.
- Never log full submitted URLs by default. The app logs only the submitted URL hostname, and the optional diagnostic flag still redacts path and query material.
- Multipart group state stores only original hostnames and operational aria2/file metadata, not full submitted URLs.
- Treat submitted URLs as private operator data.
- The submit result page displays the generated Real-Debrid direct download URL in the browser UI. Protect the app route accordingly.
- Keep aria2 JSON-RPC internal to the Docker network.
- Queue controls can pause, resume, remove, and reorder downloads. Do not expose this app publicly without access control.
- Do not expose `/downloads` through the web app unless an explicit authenticated browsing feature is added later.

## Implementation Status

The production Real-Debrid, aria2, multipart group tracking, and automatic extraction workflow is implemented in `app/main.py` with mocked tests for Real-Debrid, aria2, persistence, and extraction responses. Server-rendered UI helpers live in `app/views.py`, and shared page styling and queue-page JavaScript live in `app/static/`. The default test suite does not require a live Real-Debrid account, aria2 instance, or API token.

## Licence

MIT — see [LICENSE](LICENSE).
