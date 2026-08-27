---
type: guide
title: Real-Debrid Downloader Quickstart
description: Startup context for agents working on the standalone Real-Debrid downloader app repository.
tags: [guide, real-debrid, aria2, fastapi]
sources:
  - id: openwiki-source-8037e2358a2c4f9b2c722a11
    resource: repo://AGENTS.md
  - id: openwiki-source-21c0a295e6c6f5529dc70d5f
    resource: repo://app/main.py
  - id: openwiki-source-da0e085cfc22dcff1113b602
    resource: repo://app/views.py
  - id: openwiki-source-b5b12bfd45e72195a45c7364
    resource: repo://compose.example.yml
  - id: openwiki-source-23775c3de52f3ab95a13cb8b
    resource: repo://README.md
  - id: openwiki-source-aee60addf228e6c9bf133836
    resource: repo://tests/test_app.py
generated: { by: "codex", at: "2026-08-27T23:21:28.898Z" }
verified:
  - by: openwiki/0.4.3
    at: 2026-08-27T23:21:28.898Z
---

# Real-Debrid Downloader Quickstart

This repo owns the standalone Real-Debrid Downloader app: FastAPI source, tests, Dockerfile, portable compose example, Firefox extension, and GHCR image workflows.

Keep the repo self-hostable. App docs, examples, code, tests, and image artifacts must avoid private Grace hostnames, internal paths, operator-specific assumptions, Real-Debrid tokens, GHCR tokens, aria2 RPC secrets, submitted URLs, cookies, and credentials.

Primary code lives in `app/main.py`; server-rendered UI helpers live in `app/views.py`; shared styling and queue JavaScript live in `app/static/`; tests live in `tests/test_app.py`; the Firefox extension lives in `extensions/firefox-rdd/`.

Use `.venv/bin/python -m pytest -q` for the default validation loop. The default tests mock Real-Debrid and aria2 behavior.

Use `compose.example.yml` as the portable self-hosting reference. Grace Compose wiring, Traefik labels, host mounts, monitoring, and operator docs belong in `unraid` and `home-ops-docs`.

## Related Pages

- [App Flow](architecture/app-flow.md)
- [Deployment Boundary](architecture/deployment-boundary.md)
- [Firefox Sender Extension](extensions/firefox-sender.md)
- [Test Strategy](workflows/test-strategy.md)
