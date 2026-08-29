# Agent Instructions

This repository is the standalone, self-hostable source for the Real-Debrid downloader web app and its local Firefox sender extension. Keep it deployable as a Docker image with no dependency on any private infrastructure.

For cross-repo Codex task routing, start in `/mnt/cache/repos/home-ops-docs` and read `codex/repository-manifest.yaml`. The central Codex operating model lives in `/mnt/cache/repos/home-ops-docs/codex/agent-operating-model.md`.

## Grace Task Workspace Guard

- Use `ssh grace-codex-container` as the default entrypoint for Grace-hosted agent/dev work. Confirm `HOSTNAME=grace-codex-container` before operating on Grace repos.
- Work from `/mnt/cache/repos/<repo>` or `/mnt/cache/repos/.worktrees/<task-slug>/<repo>`, not `/mnt/user/repos`.
- Use `ssh grace` only for host-only Docker, publish, deploy, ownership, or recovery operations; do not mount the Docker socket into the Codex container.
- Never make `/mnt/user/appdata/docker` or `/mnt/user/appdata/homeassistant` Git checkouts.
- Treat live state as authoritative when reconciling drift; live changes supersede stale docs and repo state until intentionally folded back into source.
- At task start, run `/mnt/cache/repos/home-ops-docs/scripts/grace-estate-status.sh` when available so dirty Grace repo state is visible before edits.
- Start standalone app behavior, tests, extension, and image-source work in this repo.
- If the request may also require Grace Compose wiring, Traefik routes, image tags, deployment docs, monitoring, or repo instruction changes, create a task workspace before editing:
  `cd /mnt/cache/repos/home-ops-docs && scripts/codex-task-worktree.sh <task-slug> home-ops-docs realdebrid-downloader unraid`.
- If worktree creation fails with `Read-only file system` or sandbox permission errors, rerun the same helper command with sandbox escalation/approval; do not switch to an app-managed handoff worktree solely for that failure.
- Continue active cross-repo work from `/mnt/cache/repos/.worktrees/<task-slug>/realdebrid-downloader`; keep `/mnt/cache/repos/realdebrid-downloader` clean as the canonical checkout.
- If app work started in the canonical checkout becomes cross-repo, stop before more edits and move or commit the current task changes into a task workspace.

Keep this AGENTS-only Grace operator context out of app docs, examples, code,
tests, and image artifacts so the project remains self-hostable.

## Maintenance Rules

- Keep the app repo standalone: source, tests, Dockerfile, compose example, and image-publishing workflow all live here.
- Never commit real Real-Debrid tokens, GHCR tokens, aria2 RPC secrets, submitted URLs, cookies, or credentials. Secrets belong only in a local, gitignored `.env`.
- Keep the repo free of private/operator-specific references (hostnames, internal IPs, personal deployment paths). Docs and examples must stay generic enough for any self-hoster.
- Do not log full submitted URLs by default. If diagnostic logging is required, redact tokens, query strings, and path material that could identify private downloads.
- Update `README.md` whenever environment variables, runtime behavior, Real-Debrid API assumptions, aria2 behavior, image tags, or deployment steps change.
- Update `extensions/firefox-rdd/README.md` when extension behavior, permissions, storage, install steps, or validation expectations change.
- Prefer tests with mocked Real-Debrid API and aria2 JSON-RPC responses. Do not require a live Real-Debrid account for the default test suite.
- Run the Python test suite with `.venv/bin/python -m pytest -q`. Do not assume a global `pytest` command is installed or on `PATH` in agent containers.
- Do not assume Docker is available in an LLM or helper container. If you validate a change, clearly report whether you ran only the Python test suite or also Docker/Compose checks.
- Treat Docker socket access as privileged host access. Keep agent helper containers generic and separate from the production app image.

## Current Maintained Behavior

The maintained app flow is implemented and covered by mocked tests:

1. Accept one hoster URL, magnet link, or free text containing multiple links from the web form or `POST /api/submit`.
2. Use Real-Debrid supported-host metadata when available, but still attempt unrestrict when metadata is unavailable or inconclusive.
3. Call Real-Debrid unrestrict endpoints for hoster links.
4. Add magnet links through the Real-Debrid torrent endpoints, select files, poll for generated links, and submit each unrestricted result.
5. Submit generated direct download URLs to aria2 through JSON-RPC without exposing aria2 RPC details to browser clients.
6. Track multipart archives and torrent files as persisted groups in `/config/download-groups.json`, then extract completed archive groups with 7-Zip.
7. Serve a sanitized queue UI and JSON API that expose useful status without Real-Debrid tokens, aria2 secrets, submitted full URLs, magnet hashes, generated direct URLs, or local download paths.
8. Keep the Firefox extension in `extensions/firefox-rdd` aligned with the JSON API; it stores only the configured RDD base URL.

## Validation

For Python/app changes, run:

```bash
.venv/bin/python -m pytest -q
```

For Docker/Compose changes, run what is available and report exactly what succeeded:

```bash
docker compose -f compose.example.yml config
```

If `extensions/firefox-rdd` exists and extension files changed, validate at least the manifest JSON and JavaScript syntax when local tooling is available:

```bash
python3 -m json.tool extensions/firefox-rdd/manifest.json >/dev/null
node --check extensions/firefox-rdd/background.js
node --check extensions/firefox-rdd/options.js
```

Also load `extensions/firefox-rdd/manifest.json` as a temporary add-on in Firefox for behavioral checks when browser access is part of the requested work. Do not publish, sign, or submit the extension unless explicitly requested.
