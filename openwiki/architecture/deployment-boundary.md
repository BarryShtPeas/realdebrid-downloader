---
type: architecture
title: Deployment Boundary
description: Separate portable app/container ownership from Grace Compose, Traefik, host mounts, and operator docs.
tags: [architecture, docker, deployment]
sources:
  - id: openwiki-source-d3da66ac58439053884e3d28
    resource: repo://.github/workflows/container.yml
  - id: openwiki-source-281ab1996b93c8af45ba8949
    resource: repo://.github/workflows/dev-container.yml
  - id: openwiki-source-8037e2358a2c4f9b2c722a11
    resource: repo://AGENTS.md
  - id: openwiki-source-b5b12bfd45e72195a45c7364
    resource: repo://compose.example.yml
  - id: openwiki-source-bb1ebe868e35e9e500714501
    resource: repo://Dockerfile
  - id: openwiki-source-23775c3de52f3ab95a13cb8b
    resource: repo://README.md
generated: { by: "codex", at: "2026-08-27T23:21:28.898Z" }
verified:
  - by: openwiki/0.4.3
    at: 2026-08-27T23:21:28.898Z
---

# Deployment Boundary

This repo owns the standalone Real-Debrid Downloader app source, tests, Dockerfile, compose example, Firefox extension, and image publishing workflows. Keep app docs and examples self-hostable and free of private Grace hostnames, internal paths, and operator-specific assumptions.

`Dockerfile` builds the production image from `python:3.12-slim`, installs Python dependencies plus 7-Zip, creates `/config` and `/downloads`, exposes port `8080`, and runs `python -m app.main` as an unprivileged user.

`compose.example.yml` is a portable reference stack. It runs `rd-downloader` and an internal `rd-aria2`, reads local secrets from `.env`, maps `/config` and `/downloads`, and does not expose aria2's JSON-RPC port to the host.

The production image workflow builds `ghcr.io/barryshtpeas/realdebrid-downloader` on `main`, tags, and pull requests; pushes publish `latest`, branch/SHA, or release tags as appropriate. The dev workflow publishes `dev` and `dev-<shortsha>` images from `dev` or a manually supplied ref.

Grace Compose wiring, Traefik labels, host mounts, live secret files, monitoring, and operator docs belong in `unraid` and `home-ops-docs`, not in this app repo.

## Related Pages

- [Real-Debrid Downloader Quickstart](../quickstart.md)
