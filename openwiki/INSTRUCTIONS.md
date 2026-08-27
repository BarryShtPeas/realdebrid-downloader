# OpenWiki Instructions

Generate concise, operational LLM documentation for the standalone Real-Debrid downloader app repository.

Emphasize:

- Repository boundaries: this repo owns app source, tests, Dockerfile, compose example, and image publishing workflow.
- Portability: app docs and examples must stay self-hostable and avoid private Grace hostnames, internal paths, or operator-specific assumptions.
- Secret safety: never expose Real-Debrid tokens, GHCR tokens, aria2 RPC secrets, submitted URLs, cookies, or credentials.
- Runtime flow: document URL submission, Real-Debrid unrestrict/torrent handling, aria2 handoff, queue behaviour, and mocked test strategy.
- Cross-repo routing: Grace deployment wiring, Traefik labels, host mounts, monitoring, and operator docs belong in `unraid` and `home-ops-docs`.
- Documentation split: generic app docs stay in this repo; durable Grace human docs belong in `home-ops-docs`; generated LLM docs belong under repo-local `openwiki/`.

Keep pages short, source-grounded, and useful for future app, API, extension, and deployment-boundary work.
