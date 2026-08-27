---
type: architecture
title: App Flow
description: Map web/API submission, Real-Debrid, aria2, playback, queue, magnet, and multipart group behavior.
tags: [architecture, real-debrid, aria2, fastapi]
sources:
  - id: openwiki-source-21c0a295e6c6f5529dc70d5f
    resource: repo://app/main.py
  - id: openwiki-source-caaaaa27bb534ea14311d6a7
    resource: repo://app/static/queue.js
  - id: openwiki-source-da0e085cfc22dcff1113b602
    resource: repo://app/views.py
  - id: openwiki-source-23775c3de52f3ab95a13cb8b
    resource: repo://README.md
  - id: openwiki-source-aee60addf228e6c9bf133836
    resource: repo://tests/test_app.py
generated: { by: "codex", at: "2026-08-27T23:21:28.898Z" }
verified:
  - by: openwiki/0.4.3
    at: 2026-08-27T23:21:28.898Z
---

# App Flow

Real-Debrid Downloader is a FastAPI app with a web UI and JSON API for submitting Real-Debrid-supported hoster links and magnet links to an internal aria2 worker.

The download path accepts one URL, one magnet link, or free text containing multiple links. Hoster links are checked against Real-Debrid host metadata when available, checked with `/unrestrict/check`, unrestricted with `/unrestrict/link`, and submitted to aria2 over JSON-RPC.

Magnet submissions call Real-Debrid torrent endpoints: add magnet, read torrent info, select all files, wait for generated links, unrestrict each returned link, and submit the resulting downloads to aria2.

The Play path supports one streamable hoster URL at a time. It creates a process-local playback session, serves a local `/play/{session}/stream` URL, and proxies browser range requests to the Real-Debrid stream.

Multipart groups are stored in `/config/download-groups.json` by default. The group monitor polls aria2, tracks part completion, extracts supported archives with 7-Zip into `/downloads/<group-name>/`, deletes archive parts only after successful extraction, and skips extraction for non-archive downloads.

The queue UI is server-rendered and uses Server-Sent Events from `/queue/events` for live refresh when JavaScript is available. Queue and API responses are sanitized so clients do not receive Real-Debrid tokens, aria2 RPC details, full submitted URLs, magnet hashes, generated direct URLs, or local download paths.

## Related Pages

- [Real-Debrid Downloader Quickstart](../quickstart.md)
- [Test Strategy](../workflows/test-strategy.md)
