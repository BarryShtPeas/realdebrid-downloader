# Agent Instructions

This repository is the standalone, self-hostable source for the Real-Debrid downloader web app. Keep it deployable as a Docker image with no dependency on any private infrastructure.

## Maintenance Rules

- Keep the app repo standalone: source, tests, Dockerfile, compose example, and image-publishing workflow all live here.
- Never commit real Real-Debrid tokens, GHCR tokens, aria2 RPC secrets, submitted URLs, cookies, or credentials. Secrets belong only in a local, gitignored `.env`.
- Keep the repo free of private/operator-specific references (hostnames, internal IPs, personal deployment paths). Docs and examples must stay generic enough for any self-hoster.
- Do not log full submitted URLs by default. If diagnostic logging is required, redact tokens, query strings, and path material that could identify private downloads.
- Update `README.md` whenever environment variables, runtime behavior, Real-Debrid API assumptions, aria2 behavior, image tags, or deployment steps change.
- Prefer tests with mocked Real-Debrid API and aria2 JSON-RPC responses. Do not require a live Real-Debrid account for the default test suite.
- Run the Python test suite with `.venv/bin/python -m pytest -q`. Do not assume a global `pytest` command is installed or on `PATH` in agent containers.
- Do not assume Docker is available in an LLM or helper container. If you validate a change, clearly report whether you ran only the Python test suite or also Docker/Compose checks.
- Treat Docker socket access as privileged host access. Keep agent helper containers generic and separate from the production app image.

## Current Scope

The initial scaffold is intentionally minimal. Implement the production flow behind the documented contracts:

1. Accept a hoster URL from the web form.
2. Optionally use Real-Debrid supported-host data to provide early feedback.
3. Call Real-Debrid unrestrict endpoints.
4. Submit the generated direct download URL to aria2.
5. Show a clear result without exposing secrets or full submitted URLs in logs.
