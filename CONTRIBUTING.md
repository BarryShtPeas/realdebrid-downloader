# Contributing

This project uses a simple branch-based release flow so development builds can be tested without changing the production container tag.

## Branch Flow

- `main` is the production branch.
- `dev` is the integration branch for development deployments.
- Feature work should happen on short-lived branches such as `feature/queue-ui` or `fix/aria2-error`.
- Open pull requests from feature branches into `dev`.
- After testing the dev image, open a pull request from `dev` into `main`.

Do not commit secrets, submitted URLs, cookies, or local deployment details. Use a local, gitignored `.env` for credentials.

## Container Tags

Production images are built by the main container workflow:

- Pushing to `main` publishes `ghcr.io/barryshtpeas/realdebrid-downloader:latest`.
- Pushing a `v*` Git tag publishes the matching release image tag.
- Traceable SHA tags are published for production pushes.

Development images are built by the dev container workflow:

- Pushing to `dev` publishes `ghcr.io/barryshtpeas/realdebrid-downloader:dev`.
- The same build also publishes `ghcr.io/barryshtpeas/realdebrid-downloader:dev-<shortsha>`.
- The `dev` tag is movable and intended for test deployments.
- The `dev-<shortsha>` tag is immutable enough for rollback and audit.

`dev` builds must never update `latest`.

## Versioning

The app uses `major.minor.bugfix` semantic versioning. The source of truth is `app.__version__` in `app/__init__.py`, and that value is shown in the web UI, `/api/version`, and the generated OpenAPI metadata.

- Increment `major` for incompatible API or runtime behavior changes.
- Increment `minor` for backward-compatible features.
- Increment `bugfix` for backward-compatible fixes.
- Release Git tags should be `vX.Y.Z` and match `app.__version__`.

## Development Workflow

Before starting development, make sure the environment has the repo tooling:
Python 3.12, Python package installation through `pip` or `uv`, Docker with the
Compose plugin for full-stack runs, and `7z` when testing extraction outside the
Docker image. In restricted helper containers, install user-space tooling such
as `uv` under the container user and use a writable cache, for example
`UV_CACHE_DIR="$PWD/.uv-cache"`.

1. Create a feature branch from `dev`.
2. Make the change and add or update tests.
3. Run the default test suite:

   ```bash
   .venv/bin/python -m pytest
   ```

4. Open a pull request into `dev`.
5. Merge to `dev` after review; GitHub Actions publishes the `dev` image.
6. Deploy or test `ghcr.io/barryshtpeas/realdebrid-downloader:dev`.
7. Promote by opening a pull request from `dev` into `main`.

## Manual Dev Builds

The `Build and publish dev container` workflow can also be run manually from GitHub Actions. Set `ref` to a branch, tag, or commit SHA to build. Manual runs publish the same `dev` and `dev-<shortsha>` tags.

## Pull Request Expectations

- Keep the app self-hostable and generic for any operator.
- Update `README.md` when environment variables, runtime behavior, image tags, Real-Debrid assumptions, aria2 behavior, or deployment steps change.
- Prefer mocked Real-Debrid and aria2 JSON-RPC tests.
- Do not require a live Real-Debrid account for the default test suite.
