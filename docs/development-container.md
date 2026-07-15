# Remote Agent Development Container

This project can be developed from a remote helper container that has the tools
needed to edit, test, build, and run the app stack. Treat that helper container
as development infrastructure only. It is separate from the production
`rd-downloader` app image, which should stay minimal and self-hostable.

The helper container can run Codex, Claude, or another LLM coding tool. The repo
does not depend on any specific agent, but all agents should see the same
workspace, instructions, and development tooling.

This repo includes a generic `.devcontainer/` setup for that helper container.
It installs Python development tooling, Git/GitHub tooling, Node/npm for
operator-managed LLM CLIs, Docker CLI tooling, the Compose plugin, and 7-Zip.
The production app image still comes only from the root `Dockerfile`.

Build the helper image directly when Docker is available. Pass the repository
owner UID/GID so files created by the helper container remain usable from the
host and existing files such as `.git/HEAD` are readable inside the container:

```bash
HOST_UID="$(stat -c "%u" .)"
HOST_GID="$(stat -c "%g" .)"

docker build \
  --build-arg USER_UID="$HOST_UID" \
  --build-arg USER_GID="$HOST_GID" \
  -f .devcontainer/Dockerfile \
  -t realdebrid-downloader-agent-dev \
  .devcontainer
```

Run it directly from the repo root with the Docker socket and same-path
workspace mount:

```bash
DOCKER_GID="$(stat -c "%g" /var/run/docker.sock)"

docker run --rm -it \
  --group-add "$DOCKER_GID" \
  -e UV_CACHE_DIR="$PWD/.uv-cache" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD:$PWD" \
  -w "$PWD" \
  realdebrid-downloader-agent-dev
```

## Required Tooling

A Docker-capable agent development container should include:

- Python 3.12.
- `uv` and/or `pip`.
- `pytest`.
- Git, OpenSSH client, and GitHub CLI (`gh`).
- Docker CLI with the Docker Compose plugin.
- 7-Zip (`7z`) when testing archive extraction outside the app image.
- Any LLM CLI tools used by the operator, such as Codex or Claude.

Do not bake credentials into the helper image. Mount or configure agent homes,
SSH keys, GitHub authentication, and LLM credentials through the operator's
local environment. The checked-in dev container provides Node/npm but does not
install a specific LLM CLI package by default, because those tools and auth
flows are operator-managed.

## Docker Host Access

For local image builds and full-stack Compose runs, the helper container needs
access to the host Docker daemon, typically by mounting:

```text
/var/run/docker.sock:/var/run/docker.sock
```

This is privileged access. A process with access to the Docker socket can
control containers on the host and may be able to affect host files through bind
mounts. Grant it only to trusted development sessions.

The helper container also needs Docker CLI tooling inside the container. Mounting
the socket alone is not enough.

When the helper container runs as a non-root user, add the Docker socket's host
group ID with `--group-add "$(stat -c "%g" /var/run/docker.sock)"`. Otherwise
the Docker CLI may be installed but unable to talk to the host daemon.

## Path Mapping

When Docker commands run inside a helper container but talk to the host Docker
daemon, bind-mount paths are resolved by the host daemon. Keep the repository at
the same absolute path inside the helper container as it has on the host.

Good:

```text
host path:      /srv/repos/realdebrid-downloader
container path: /srv/repos/realdebrid-downloader
```

Risky:

```text
host path:      /srv/repos/realdebrid-downloader
container path: /workspace/realdebrid-downloader
```

The second layout can make Compose bind mounts and `$PWD`-based commands point
somewhere different from what the agent expects.

## Validation Tiers

Agents without Docker access can still perform the Python-only development loop:

```bash
.venv/bin/python -m pytest
```

When Docker access is available, also validate the container tooling:

```bash
docker version
docker compose version
docker compose -f compose.example.yml config
```

For full-stack local testing, run:

```bash
docker compose -f compose.example.yml up --build
```

GitHub Actions remains the authoritative publisher for the public `dev`,
`dev-<shortsha>`, and `latest` images.

## Security Boundaries

- Keep real Real-Debrid tokens, GHCR tokens, aria2 RPC secrets, submitted URLs,
  cookies, and credentials out of the repo.
- Use the gitignored `.env` file only for local runtime secrets.
- Do not put host-specific paths, private hostnames, internal IPs, or personal
  deployment details in tracked documentation.
- If host-specific agent notes are needed, put them in an ignored local file such
  as `AGENTS.local.md`.
