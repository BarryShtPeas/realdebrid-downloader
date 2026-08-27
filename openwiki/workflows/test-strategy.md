---
type: workflow
title: Test Strategy
description: Explain the repository test suite, mocked dependencies, API/schema assertions, extension checks, and validation commands.
tags: [workflow, tests, pytest, real-debrid]
sources:
  - id: openwiki-source-8037e2358a2c4f9b2c722a11
    resource: repo://AGENTS.md
  - id: openwiki-source-23775c3de52f3ab95a13cb8b
    resource: repo://README.md
  - id: openwiki-source-aee60addf228e6c9bf133836
    resource: repo://tests/test_app.py
generated: { by: "codex", at: "2026-08-27T23:21:28.898Z" }
verified:
  - by: openwiki/0.4.3
    at: 2026-08-27T23:21:28.898Z
---

# Test Strategy

Run the default Python test suite with `.venv/bin/python -m pytest -q`. Do not assume a global `pytest` command is installed in agent containers.

The default suite uses mocked Real-Debrid API and aria2 JSON-RPC behavior. It should not require a live Real-Debrid account, live aria2 instance, API token, or production download path.

`tests/test_app.py` covers health and version endpoints, OpenAPI exposure, settings parsing, hoster submission, magnet/torrent submission, playback behavior, queue actions, multipart grouping, extraction flow, and sanitized API responses.

The suite also checks Firefox extension packaging expectations: manifest v3, version match with `app/__init__.py`, stable Gecko ID, permissions, release tooling, and absence of private/operator-specific defaults.

Use Docker or Compose checks separately only when the environment has Docker tooling and daemon access. Report whether validation covered Python-only tests or container checks as well.

## Related Pages

- [Real-Debrid Downloader Quickstart](../quickstart.md)
- [App Flow](../architecture/app-flow.md)
