# Real-Debrid Downloader

Small self-hosted web app for submitting Real-Debrid-supported hoster links and handing the unrestricted download URL to an internal aria2 downloader.

## Purpose

This app provides a simple web form for Rapidgator and other Real-Debrid-supported hoster URLs. Paste a link, and the app unrestricts it through Real-Debrid and hands the direct download to an internal aria2 worker. It is a lightweight, always-on alternative to keeping a full desktop download manager running.

The runtime model is:

- `rd-downloader`: web app container and Real-Debrid API client.
- `rd-aria2`: internal aria2 JSON-RPC downloader.
- Shared `/downloads` mount where aria2 writes completed files.
- Persistent `/config` mount for app state.

## Download Flow

1. The operator submits a hoster URL.
2. The app checks Real-Debrid supported hosts through `/hosts/domains` when that metadata is available.
3. The app calls `/unrestrict/check` to check whether Real-Debrid currently has a downloadable file for that link.
4. The app calls `/unrestrict/link` with the submitted URL.
5. If Real-Debrid returns a generated direct download URL, the app submits it to aria2 using JSON-RPC.
6. aria2 downloads the file to `/downloads`.

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
| `ARIA2_RPC_URL` | yes | `http://rd-aria2:6800/jsonrpc` | Internal aria2 JSON-RPC URL. |
| `ARIA2_RPC_SECRET` | recommended | none | aria2 RPC token. |
| `ARIA2_DOWNLOAD_DIR` | no | `/downloads` | Directory passed to aria2. |
| `ARIA2_MAX_CONNECTION_PER_SERVER` | no | `8` | aria2 connection tuning. |
| `ARIA2_SPLIT` | no | `8` | aria2 split tuning. |

## Local Development

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

Run the app with aria2:

```bash
docker compose -f compose.example.yml up --build
```

## Docker Image

GitHub Actions publishes the public image to:

```text
ghcr.io/barryshtpeas/realdebrid-downloader
```

Expected tags:

- `latest` from the default branch.
- Branch/SHA tags for traceable builds.
- Release tag names when a Git tag is pushed.

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

aria2 writes completed files to `/downloads` inside the container. Point that at any host directory with `DOWNLOAD_DIR` in `.env`:

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
- Treat submitted URLs as private operator data.
- Keep aria2 JSON-RPC internal to the Docker network.
- Do not expose `/downloads` through the web app unless an explicit authenticated browsing feature is added later.

## Implementation Status

The production Real-Debrid and aria2 workflow is implemented in `app/main.py` with mocked tests for Real-Debrid and aria2 responses. The default test suite does not require a live Real-Debrid account, aria2 instance, or API token.

## Licence

MIT — see [LICENSE](LICENSE).
