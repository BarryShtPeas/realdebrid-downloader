from __future__ import annotations

import json
import tempfile
import uuid
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest

import app.main as main
from app.main import (
    Aria2Client,
    DownloadResult,
    Downloader,
    NO_DOWNLOAD_MESSAGE,
    QueueItem,
    QueueSnapshot,
    RealDebridClient,
    Settings,
    app,
    get_downloader,
)


@pytest.fixture(autouse=True)
def clear_dependency_overrides() -> None:
    app.dependency_overrides.clear()
    main.PLAY_SESSIONS.clear()
    yield
    app.dependency_overrides.clear()
    main.PLAY_SESSIONS.clear()


def test_healthz() -> None:
    async def run_test() -> None:
        async with app_client() as client:
            response = await client.get("/healthz")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    anyio.run(run_test)


def test_swagger_openapi_documents_rdd_api_only() -> None:
    async def run_test() -> None:
        async with app_client() as client:
            response = await client.get("/openapi.json")
            docs_response = await client.get("/docs")

        assert response.status_code == 200
        payload = response.json()
        assert payload["info"]["title"] == "Real-Debrid Downloader"
        assert payload["info"]["version"] == main.__version__
        assert "/api/version" in payload["paths"]
        assert "/api/submit" in payload["paths"]
        assert "/api/queue" in payload["paths"]
        assert "/submit" not in payload["paths"]
        assert "/queue/events" not in payload["paths"]
        assert "/queue/{gid}/pause" not in payload["paths"]
        assert docs_response.status_code == 200
        assert "Swagger UI" in docs_response.text

    anyio.run(run_test)


def test_api_version_and_pages_include_app_version() -> None:
    async def run_test() -> None:
        async with app_client() as client:
            version_response = await client.get("/api/version")
            page_response = await client.get("/")

        assert version_response.status_code == 200
        assert version_response.json() == {
            "name": "Real-Debrid Downloader",
            "version": main.__version__,
        }
        assert f"v{main.__version__}" in page_response.text

    anyio.run(run_test)


def test_settings_clamps_aria2_max_concurrent_downloads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARIA2_MAX_CONCURRENT_DOWNLOADS", raising=False)
    assert Settings.from_env().aria2_max_concurrent_downloads == 1

    monkeypatch.setenv("ARIA2_MAX_CONCURRENT_DOWNLOADS", "2")
    assert Settings.from_env().aria2_max_concurrent_downloads == 2

    monkeypatch.setenv("ARIA2_MAX_CONCURRENT_DOWNLOADS", "invalid")
    assert Settings.from_env().aria2_max_concurrent_downloads == 1

    monkeypatch.setenv("ARIA2_MAX_CONCURRENT_DOWNLOADS", "0")
    assert Settings.from_env().aria2_max_concurrent_downloads == 1

    monkeypatch.setenv("ARIA2_MAX_CONCURRENT_DOWNLOADS", "9")
    assert Settings.from_env().aria2_max_concurrent_downloads == 3


def test_firefox_extension_manifest_matches_app_version() -> None:
    manifest_path = Path("extensions/firefox-rdd/manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["manifest_version"] == 3
    assert manifest["version"] == main.__version__
    assert manifest["name"] == "RDD Sender"
    assert manifest["browser_specific_settings"]["gecko"]["id"] == "rdd-sender@realdebrid-downloader.local"
    assert set(manifest["permissions"]) == {"menus", "notifications", "storage"}
    assert manifest["optional_host_permissions"] == ["http://*/*", "https://*/*"]
    assert "host_permissions" not in manifest
    assert manifest["icons"] == {"48": "icon.svg", "96": "icon.svg"}
    assert manifest["background"]["scripts"] == ["background.js"]
    assert manifest["options_ui"]["page"] == "options.html"
    assert manifest["action"]["default_icon"] == {"48": "icon.svg", "96": "icon.svg"}


def test_firefox_extension_has_self_distribution_release_tooling() -> None:
    extension_root = Path("extensions/firefox-rdd")
    package = json.loads((extension_root / "package.json").read_text(encoding="utf-8"))
    readme = (extension_root / "README.md").read_text(encoding="utf-8")
    gitignore = Path(".gitignore").read_text(encoding="utf-8")

    assert package["private"] is True
    assert package["version"] == main.__version__
    assert package["devDependencies"]["web-ext"].startswith("^8.")
    assert "web-ext lint" in package["scripts"]["lint"]
    assert "web-ext build" in package["scripts"]["build"]
    assert "web-ext sign" in package["scripts"]["sign"]
    assert "--channel unlisted" in package["scripts"]["sign"]
    assert "WEB_EXT_API_KEY" in readme
    assert "WEB_EXT_API_SECRET" in readme
    assert "Install Add-on From File" in readme
    assert "app/__init__.py" in readme
    assert "extensions/firefox-rdd/web-ext-artifacts/" in gitignore


def test_firefox_extension_uses_rdd_api_without_private_defaults() -> None:
    extension_root = Path("extensions/firefox-rdd")
    background = (extension_root / "background.js").read_text(encoding="utf-8")
    options = (extension_root / "options.js").read_text(encoding="utf-8")
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in extension_root.iterdir()
        if path.is_file() and path.suffix in {".js", ".json", ".html", ".md"}
    )

    assert "/api/submit" in background
    assert "/api/version" in options
    assert "browser.storage.local" in background
    assert "browser.storage.local" in options
    assert "browser.permissions.request" in options
    assert "grace" not in combined.lower()
    assert "lillystone" not in combined.lower()
    assert "REALDEBRID_API_TOKEN" not in combined
    assert "ARIA2_RPC_SECRET" not in combined


def test_submit_success_does_not_echo_submitted_url() -> None:
    submitted_url = "https://rapidgator.example/private/file?token=secret"

    class FakeDownloader:
        async def submit(self, url: str) -> DownloadResult:
            assert url == submitted_url
            return DownloadResult(
                ok=True,
                message="Download submitted to aria2.",
                aria2_gid="abc123",
                filename="release.iso",
                direct_url="https://download.example/release.iso?rd=direct",
                host_supported=True,
            )

    async def run_test() -> None:
        response_text = await main.submit(submitted_url, FakeDownloader())  # type: ignore[arg-type]

        assert "Download submitted to aria2." in response_text
        assert "release.iso" in response_text
        assert "abc123" in response_text
        assert "https://download.example/release.iso?rd=direct" in response_text
        assert submitted_url not in response_text
        assert "token=secret" not in response_text

    anyio.run(run_test)


def test_api_submit_returns_sanitized_json() -> None:
    submitted_text = "https://rapidgator.example/private/file?token=secret"

    class FakeDownloader:
        async def submit_text(self, text: str) -> main.SubmissionResult:
            assert text == submitted_text
            download = DownloadResult(
                ok=True,
                message="Download submitted to aria2.",
                aria2_gid="abc123",
                filename="release.iso",
                direct_url="https://download.example/release.iso?rd=direct",
                host_supported=True,
                group_id="group-1",
                local_path="/downloads/release.iso",
                submitted_hostname="rapidgator.example",
            )
            return main.SubmissionResult(
                ok=True,
                message="Download submitted to aria2.",
                downloads=[download],
                group=main.DownloadGroup(
                    id="group-1",
                    name="release",
                    created_at=main.now_iso(),
                    updated_at=main.now_iso(),
                    original_hosts=["rapidgator.example"],
                    parts=[
                        main.DownloadGroupPart(
                            "abc123",
                            "release.iso",
                            "/downloads/release.iso",
                            "submitted",
                        ),
                    ],
                ),
            )

    async def run_test() -> None:
        app.dependency_overrides[get_downloader] = dependency_override(FakeDownloader())
        async with app_client() as client:
            response = await client.post("/api/submit", json={"url": submitted_text})

        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        assert payload["downloads"][0]["aria2_gid"] == "abc123"
        assert payload["downloads"][0]["filename"] == "release.iso"
        assert payload["downloads"][0]["submitted_hostname"] == "rapidgator.example"
        assert payload["group"]["id"] == "group-1"
        response_text = response.text
        assert submitted_text not in response_text
        assert "token=secret" not in response_text
        assert "download.example" not in response_text
        assert "/downloads/release.iso" not in response_text

    anyio.run(run_test)


def test_submit_shows_required_no_download_message() -> None:
    class FakeDownloader:
        async def submit(self, url: str) -> DownloadResult:
            return DownloadResult(ok=False, message=NO_DOWNLOAD_MESSAGE)

    async def run_test() -> None:
        response_text = await main.submit("https://example.com/file", FakeDownloader())  # type: ignore[arg-type]

        assert NO_DOWNLOAD_MESSAGE in response_text

    anyio.run(run_test)


def test_play_resolves_hoster_link_without_aria2(tmp_path: Any) -> None:
    async def run_test() -> None:
        settings = make_settings(group_state_file=str(tmp_path / "groups.json"))
        downloader = Downloader(settings)
        calls: list[str] = []

        class FakeRealDebrid:
            async def supported_domains(self) -> set[str]:
                calls.append("supported_domains")
                return {"rapidgator.example"}

            async def check_link(self, submitted_url: str) -> dict[str, Any]:
                calls.append("check_link")
                return {"supported": 1}

            async def unrestrict_link(self, submitted_url: str) -> dict[str, Any]:
                calls.append("unrestrict_link")
                return {
                    "id": "rd-file-1",
                    "download": "https://download.example/movie.mp4?token=rd-secret",
                    "filename": "movie.mp4",
                    "mimeType": "video/mp4",
                    "streamable": 1,
                }

        class FakeAria2:
            async def add_uri(self, direct_url: str) -> str:
                raise AssertionError("play must not submit to aria2")

        downloader.realdebrid = FakeRealDebrid()  # type: ignore[assignment]
        downloader.aria2 = FakeAria2()  # type: ignore[assignment]

        result = await downloader.prepare_play("https://rapidgator.example/movie.mp4")

        assert result.ok is True
        assert result.filename == "movie.mp4"
        assert result.mime_type == "video/mp4"
        assert result.realdebrid_id == "rd-file-1"
        assert result.direct_url == "https://download.example/movie.mp4?token=rd-secret"
        assert calls == ["supported_domains", "check_link", "unrestrict_link"]
        assert not Path(settings.group_state_file).exists()

    anyio.run(run_test)


def test_play_page_renders_local_video_without_leaking_urls() -> None:
    submitted_url = "https://rapidgator.example/private/movie.mp4?token=submitted-secret"
    direct_url = "https://download.example/movie.mp4?token=rd-secret"

    class FakeDownloader:
        async def prepare_play(self, text: str) -> main.PlayResult:
            assert text == submitted_url
            return main.PlayResult(
                ok=True,
                message="Ready to play.",
                filename="movie.mp4",
                direct_url=direct_url,
                mime_type="video/mp4",
                streamable=True,
                submitted_hostname="rapidgator.example",
            )

    async def run_test() -> None:
        app.dependency_overrides[get_downloader] = dependency_override(FakeDownloader())
        async with app_client() as client:
            response = await client.post("/play", data={"url": submitted_url})

        assert response.status_code == 200
        assert "<video" in response.text
        assert '<source src="/play/' in response.text
        assert "/stream" in response.text
        assert "movie.mp4" in response.text
        assert submitted_url not in response.text
        assert "submitted-secret" not in response.text
        assert direct_url not in response.text
        assert "rd-secret" not in response.text
        assert len(main.PLAY_SESSIONS) == 1

    anyio.run(run_test)


def test_play_rejects_multi_link_and_magnet_submissions() -> None:
    async def run_test() -> None:
        async with app_client() as client:
            multi = await client.post(
                "/play",
                data={
                    "url": (
                        "https://rapidgator.example/one.mp4\n"
                        "https://rapidgator.example/two.mp4"
                    ),
                },
            )
            magnet = await client.post(
                "/play",
                data={"url": "magnet:?xt=urn:btih:SECRET_HASH&dn=movie"},
            )

        assert multi.status_code == 200
        assert main.PLAY_MULTI_LINK_MESSAGE in multi.text
        assert magnet.status_code == 200
        assert main.PLAY_MAGNET_MESSAGE in magnet.text
        assert "SECRET_HASH" not in magnet.text
        assert not main.PLAY_SESSIONS

    anyio.run(run_test)


def test_play_returns_no_download_when_unrestrict_has_no_url() -> None:
    async def run_test() -> None:
        downloader = Downloader(make_settings())

        class FakeRealDebrid:
            async def supported_domains(self) -> set[str]:
                return {"rapidgator.example"}

            async def check_link(self, submitted_url: str) -> dict[str, Any]:
                return {"supported": 1}

            async def unrestrict_link(self, submitted_url: str) -> dict[str, Any]:
                return {"filename": "missing.mp4", "streamable": 1}

        class FakeAria2:
            async def add_uri(self, direct_url: str) -> str:
                raise AssertionError("play must not submit to aria2")

        downloader.realdebrid = FakeRealDebrid()  # type: ignore[assignment]
        downloader.aria2 = FakeAria2()  # type: ignore[assignment]

        result = await downloader.prepare_play("https://rapidgator.example/missing.mp4")

        assert result.ok is False
        assert result.message == NO_DOWNLOAD_MESSAGE

    anyio.run(run_test)


def test_play_stream_proxies_range_and_media_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run_test() -> None:
        main.PLAY_SESSIONS["session-1"] = main.PlaySession(
            id="session-1",
            direct_url="https://download.example/movie.mp4?token=rd-secret",
            filename="movie.mp4",
            mime_type="video/mp4",
            created_at=main.time.monotonic(),
        )
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            assert request.headers["range"] == "bytes=0-99"
            return httpx.Response(
                206,
                content=b"video-bytes",
                headers={
                    "content-type": "video/mp4",
                    "content-range": "bytes 0-10/100",
                    "accept-ranges": "bytes",
                    "content-length": "11",
                },
            )

        transport = httpx.MockTransport(handler)

        class MockAsyncClient(httpx.AsyncClient):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                kwargs["transport"] = transport
                super().__init__(*args, **kwargs)

        async with app_client() as client:
            monkeypatch.setattr(main.httpx, "AsyncClient", MockAsyncClient)
            response = await client.get(
                "/play/session-1/stream",
                headers={"Range": "bytes=0-99"},
            )

        assert response.status_code == 206
        assert response.content == b"video-bytes"
        assert response.headers["content-type"].startswith("video/mp4")
        assert response.headers["content-range"] == "bytes 0-10/100"
        assert response.headers["accept-ranges"] == "bytes"
        assert requests[0].url.host == "download.example"

    anyio.run(run_test)


def test_play_stream_advertises_byte_ranges_when_upstream_has_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run_test() -> None:
        main.PLAY_SESSIONS["session-1"] = main.PlaySession(
            id="session-1",
            direct_url="https://download.example/movie.mp4?token=rd-secret",
            filename="movie.mp4",
            mime_type="video/mp4",
            created_at=main.time.monotonic(),
        )

        def handler(request: httpx.Request) -> httpx.Response:
            assert "range" not in request.headers
            return httpx.Response(
                200,
                content=b"video-bytes",
                headers={
                    "content-type": "video/mp4",
                    "content-length": "11",
                },
            )

        transport = httpx.MockTransport(handler)

        class MockAsyncClient(httpx.AsyncClient):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                kwargs["transport"] = transport
                super().__init__(*args, **kwargs)

        async with app_client() as client:
            monkeypatch.setattr(main.httpx, "AsyncClient", MockAsyncClient)
            response = await client.get("/play/session-1/stream")

        assert response.status_code == 200
        assert response.headers["accept-ranges"] == "bytes"
        assert response.headers["content-length"] == "11"

    anyio.run(run_test)


def test_play_stream_supports_head_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run_test() -> None:
        main.PLAY_SESSIONS["session-1"] = main.PlaySession(
            id="session-1",
            direct_url="https://download.example/movie.mp4?token=rd-secret",
            filename="movie.mp4",
            mime_type="video/mp4",
            created_at=main.time.monotonic(),
        )
        methods: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            methods.append(request.method)
            return httpx.Response(
                200,
                headers={
                    "content-type": "video/mp4",
                    "content-length": "1000",
                    "accept-ranges": "bytes",
                },
            )

        transport = httpx.MockTransport(handler)

        class MockAsyncClient(httpx.AsyncClient):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                kwargs["transport"] = transport
                super().__init__(*args, **kwargs)

        async with app_client() as client:
            monkeypatch.setattr(main.httpx, "AsyncClient", MockAsyncClient)
            response = await client.head("/play/session-1/stream")

        assert response.status_code == 200
        assert response.content == b""
        assert response.headers["content-type"].startswith("video/mp4")
        assert response.headers["content-length"] == "1000"
        assert response.headers["accept-ranges"] == "bytes"
        assert methods == ["HEAD"]

    anyio.run(run_test)


def test_play_stream_error_is_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run_test() -> None:
        main.PLAY_SESSIONS["session-1"] = main.PlaySession(
            id="session-1",
            direct_url="https://download.example/movie.mp4?token=rd-secret",
            filename="movie.mp4",
            mime_type="video/mp4",
            created_at=main.time.monotonic(),
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="secret upstream failure")

        transport = httpx.MockTransport(handler)

        class MockAsyncClient(httpx.AsyncClient):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                kwargs["transport"] = transport
                super().__init__(*args, **kwargs)

        async with app_client() as client:
            monkeypatch.setattr(main.httpx, "AsyncClient", MockAsyncClient)
            response = await client.get("/play/session-1/stream")

        assert response.status_code == 502
        assert "Playback stream is temporarily unavailable." in response.text
        assert "rd-secret" not in response.text
        assert "download.example" not in response.text
        assert "secret upstream failure" not in response.text

    anyio.run(run_test)


def test_form_magnet_submit_redirects_to_queue_and_runs_in_background(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_tasks: list[Any] = []
    submitted_texts: list[str] = []
    magnet = "magnet:?xt=urn:btih:BACKGROUND_HASH"

    def fake_create_task(coro: Any) -> object:
        created_tasks.append(coro)
        return object()

    class FakeDownloader:
        async def submit_text(self, text: str) -> main.SubmissionResult:
            submitted_texts.append(text)
            return main.SubmissionResult(
                ok=True,
                message="submitted",
                downloads=[DownloadResult(ok=True, message="ok", aria2_gid="gid-1")],
            )

    async def run_test() -> None:
        monkeypatch.setattr(main.asyncio, "create_task", fake_create_task)
        app.dependency_overrides[get_downloader] = dependency_override(FakeDownloader())
        async with app_client() as client:
            response = await client.post("/submit", data={"url": magnet})

        assert response.status_code == 303
        assert response.headers["location"].startswith("/queue?")
        assert "Magnet+submission+started" in response.headers["location"]
        assert submitted_texts == []
        assert len(created_tasks) == 1

        await created_tasks[0]
        assert submitted_texts == [magnet]

    anyio.run(run_test)


def test_downloader_attempts_unrestrict_when_supported_hosts_are_inconclusive() -> None:
    async def run_test() -> None:
        settings = make_settings()
        downloader = Downloader(settings)
        calls: list[str] = []

        class FakeRealDebrid:
            async def supported_domains(self) -> None:
                calls.append("supported_domains")
                return None

            async def check_link(self, submitted_url: str) -> dict[str, Any]:
                calls.append("check_link")
                return {"supported": 0}

            async def unrestrict_link(self, submitted_url: str) -> dict[str, Any]:
                calls.append("unrestrict_link")
                return {
                    "download": "https://download.example/file.bin",
                    "filename": "file.bin",
                }

        class FakeAria2:
            async def add_uri(self, direct_url: str) -> str:
                calls.append(f"add_uri:{direct_url}")
                return "gid-1"

        downloader.realdebrid = FakeRealDebrid()  # type: ignore[assignment]
        downloader.aria2 = FakeAria2()  # type: ignore[assignment]

        result = await downloader.submit("https://unknown.example/private")

        assert result.ok is True
        assert result.aria2_gid == "gid-1"
        assert result.direct_url == "https://download.example/file.bin"
        assert calls == [
            "supported_domains",
            "check_link",
            "unrestrict_link",
            "add_uri:https://download.example/file.bin",
        ]

    anyio.run(run_test)


def test_downloader_returns_no_download_when_unrestrict_has_no_url() -> None:
    async def run_test() -> None:
        settings = make_settings()
        downloader = Downloader(settings)

        class FakeRealDebrid:
            async def supported_domains(self) -> set[str]:
                return {"example.com"}

            async def check_link(self, submitted_url: str) -> dict[str, Any]:
                return {}

            async def unrestrict_link(self, submitted_url: str) -> dict[str, Any]:
                return {"filename": "missing.bin"}

        class FakeAria2:
            async def add_uri(self, direct_url: str) -> str:
                raise AssertionError("aria2 should not be called without a direct URL")

        downloader.realdebrid = FakeRealDebrid()  # type: ignore[assignment]
        downloader.aria2 = FakeAria2()  # type: ignore[assignment]

        result = await downloader.submit("https://example.com/private")

        assert result.ok is False
        assert result.message == NO_DOWNLOAD_MESSAGE

    anyio.run(run_test)


def test_real_debrid_and_aria2_clients_use_expected_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run_test() -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/rest/1.0/hosts/domains":
                return httpx.Response(200, json=["rapidgator.net", "example.com"])
            if request.url.path == "/rest/1.0/unrestrict/check":
                assert request.headers["authorization"] == "Bearer token-1"
                assert request.content == b"link=https%3A%2F%2Fexample.com%2Ffile"
                return httpx.Response(200, json={"supported": 1})
            if request.url.path == "/rest/1.0/unrestrict/link":
                assert request.headers["authorization"] == "Bearer token-1"
                return httpx.Response(
                    200,
                    json={
                        "download": "https://download.example/file",
                        "filename": "file.bin",
                    },
                )
            if request.url.path == "/jsonrpc":
                payload = json.loads(request.content)
                if payload["method"] == "aria2.changeGlobalOption":
                    assert payload["params"] == [
                        "token:aria-secret",
                        {"max-concurrent-downloads": "1"},
                    ]
                elif payload["method"] == "aria2.addUri":
                    assert payload["params"] == [
                        "token:aria-secret",
                        ["https://download.example/file"],
                        {
                            "dir": "/downloads",
                            "max-connection-per-server": "8",
                            "split": "8",
                        },
                    ]
                else:
                    raise AssertionError(f"unexpected aria2 method {payload['method']}")
                return httpx.Response(
                    200,
                    json={"jsonrpc": "2.0", "id": payload["id"], "result": "gid-2"},
                )
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)

        class MockAsyncClient(httpx.AsyncClient):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                kwargs["transport"] = transport
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(main.httpx, "AsyncClient", MockAsyncClient)
        settings = make_settings(
            realdebrid_api_base_url="https://rd.test/rest/1.0",
            aria2_rpc_url="https://aria2.test/jsonrpc",
        )

        rd = RealDebridClient(settings)
        aria2 = Aria2Client(settings)

        assert await rd.supported_domains() == {"rapidgator.net", "example.com"}
        assert await rd.check_link("https://example.com/file") == {"supported": 1}
        unrestricted = await rd.unrestrict_link("https://example.com/file")
        assert unrestricted["download"] == "https://download.example/file"
        assert await aria2.add_uri(unrestricted["download"]) == "gid-2"
        assert [request.url.path for request in requests] == [
            "/rest/1.0/hosts/domains",
            "/rest/1.0/unrestrict/check",
            "/rest/1.0/unrestrict/link",
            "/jsonrpc",
            "/jsonrpc",
        ]

    anyio.run(run_test)


def test_real_debrid_torrent_client_uses_expected_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run_test() -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/rest/1.0/torrents/addMagnet":
                assert request.method == "POST"
                assert request.headers["authorization"] == "Bearer token-1"
                assert request.content == b"magnet=magnet%3A%3Fxt%3Durn%3Abtih%3Aabc123"
                return httpx.Response(201, json={"id": "torrent-1", "uri": "https://rd.example/torrent-1"})
            if request.url.path == "/rest/1.0/torrents/info/torrent-1":
                assert request.method == "GET"
                assert request.headers["authorization"] == "Bearer token-1"
                return httpx.Response(
                    200,
                    json={
                        "id": "torrent-1",
                        "status": "downloaded",
                        "files": [{"id": 1, "path": "/movie.mkv", "selected": 1}],
                        "links": ["https://hoster.example/torrent-file"],
                    },
                )
            if request.url.path == "/rest/1.0/torrents/selectFiles/torrent-1":
                assert request.method == "POST"
                assert request.headers["authorization"] == "Bearer token-1"
                assert request.content == b"files=all"
                return httpx.Response(202)
            if request.url.path == "/rest/1.0/torrents/delete/torrent-1":
                assert request.method == "DELETE"
                assert request.headers["authorization"] == "Bearer token-1"
                return httpx.Response(204)
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)

        class MockAsyncClient(httpx.AsyncClient):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                kwargs["transport"] = transport
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(main.httpx, "AsyncClient", MockAsyncClient)
        rd = RealDebridClient(make_settings(realdebrid_api_base_url="https://rd.test/rest/1.0"))

        added = await rd.add_magnet("magnet:?xt=urn:btih:abc123")
        info = await rd.torrent_info("torrent-1")
        await rd.select_torrent_files("torrent-1")
        await rd.delete_torrent("torrent-1")

        assert added["id"] == "torrent-1"
        assert info["links"] == ["https://hoster.example/torrent-file"]
        assert [request.url.path for request in requests] == [
            "/rest/1.0/torrents/addMagnet",
            "/rest/1.0/torrents/info/torrent-1",
            "/rest/1.0/torrents/selectFiles/torrent-1",
            "/rest/1.0/torrents/delete/torrent-1",
        ]

    anyio.run(run_test)


def test_aria2_client_queue_reads_and_controls_use_expected_rpc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run_test() -> None:
        methods: list[tuple[str, list[Any]]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            methods.append((payload["method"], payload["params"]))
            method = payload["method"]
            if method in {"aria2.tellActive", "aria2.tellWaiting", "aria2.tellStopped"}:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": []})
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": "OK"})

        transport = httpx.MockTransport(handler)

        class MockAsyncClient(httpx.AsyncClient):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                kwargs["transport"] = transport
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(main.httpx, "AsyncClient", MockAsyncClient)
        aria2 = Aria2Client(make_settings())

        await aria2.configure_queue_options()
        assert await aria2.tell_active() == []
        assert await aria2.tell_waiting() == []
        assert await aria2.tell_stopped() == []
        await aria2.pause("gid-1")
        await aria2.unpause("gid-1")
        await aria2.remove("gid-1")
        await aria2.remove_download_result("gid-1")
        await aria2.purge_download_result()
        await aria2.move("gid-1", "top")
        await aria2.move("gid-1", "up")
        await aria2.move("gid-1", "down")
        await aria2.move("gid-1", "bottom")
        await aria2.move_to("gid-1", 2)

        assert methods == [
            (
                "aria2.changeGlobalOption",
                ["token:aria-secret", {"max-concurrent-downloads": "1"}],
            ),
            ("aria2.tellActive", ["token:aria-secret"]),
            ("aria2.tellWaiting", ["token:aria-secret", 0, 100]),
            ("aria2.tellStopped", ["token:aria-secret", 0, 50]),
            ("aria2.pause", ["token:aria-secret", "gid-1"]),
            ("aria2.unpause", ["token:aria-secret", "gid-1"]),
            ("aria2.remove", ["token:aria-secret", "gid-1"]),
            ("aria2.removeDownloadResult", ["token:aria-secret", "gid-1"]),
            ("aria2.purgeDownloadResult", ["token:aria-secret"]),
            ("aria2.changePosition", ["token:aria-secret", "gid-1", 0, "POS_SET"]),
            ("aria2.changePosition", ["token:aria-secret", "gid-1", -1, "POS_CUR"]),
            ("aria2.changePosition", ["token:aria-secret", "gid-1", 1, "POS_CUR"]),
            ("aria2.changePosition", ["token:aria-secret", "gid-1", 0, "POS_END"]),
            ("aria2.changePosition", ["token:aria-secret", "gid-1", 2, "POS_SET"]),
        ]

    anyio.run(run_test)


def test_queue_page_renders_downloads_and_controls() -> None:
    active = QueueItem(
        gid="active-1",
        status="active",
        name="active.iso",
        total_length=1000,
        completed_length=500,
        download_speed=100,
        eta_seconds=5,
        error_message=None,
        can_pause=True,
        can_resume=False,
        can_remove=True,
        can_reorder=False,
        can_clear=False,
    )
    waiting = QueueItem(
        gid="waiting-1",
        status="waiting",
        name="waiting.iso",
        total_length=2000,
        completed_length=0,
        download_speed=0,
        eta_seconds=None,
        error_message=None,
        can_pause=True,
        can_resume=False,
        can_remove=True,
        can_reorder=True,
        can_clear=False,
    )
    stopped = QueueItem(
        gid="stopped-1",
        status="complete",
        name="done.iso",
        total_length=3000,
        completed_length=3000,
        download_speed=0,
        eta_seconds=None,
        error_message=None,
        can_pause=False,
        can_resume=False,
        can_remove=False,
        can_reorder=False,
        can_clear=True,
    )

    class FakeAria2:
        async def queue_snapshot(self) -> QueueSnapshot:
            return QueueSnapshot(active=[active], waiting=[waiting], stopped=[stopped])

    async def run_test() -> None:
        app.dependency_overrides[main.get_aria2_client] = dependency_override(FakeAria2())
        async with app_client() as client:
            response = await client.get("/queue")

        assert response.status_code == 200
        text = response.text
        assert "Download Queue" in text
        assert "active.iso" in text
        assert "waiting.iso" in text
        assert "done.iso" in text
        assert '/queue/active-1/pause' in text
        assert 'data-queue-gid="waiting-1"' in text
        assert 'data-can-reorder="true"' in text
        assert 'draggable="true"' in text
        assert 'data-drag-handle' in text
        assert '/queue/waiting-1/move' in text
        assert '/queue/stopped-1/clear' in text
        assert '/queue/clear-stopped' in text
        assert f"v{main.__version__}" in text

    anyio.run(run_test)


def test_queue_javascript_supports_drag_reorder_api_and_pauses_live_updates() -> None:
    script = Path("app/static/queue.js").read_text(encoding="utf-8")

    assert "`/api/queue/${encodeURIComponent(gid)}/move`" in script
    assert "JSON.stringify({ position })" in script
    assert "draggedItem || isSubmittingMove" in script
    assert "pendingHtml = payload.html" in script


def test_api_queue_returns_sanitized_queue_and_group_state() -> None:
    active = QueueItem(
        gid="active-1",
        status="active",
        name="active.iso",
        total_length=1000,
        completed_length=500,
        download_speed=100,
        eta_seconds=5,
        error_message="https://secret.example/private?token=bad",
        can_pause=True,
        can_resume=False,
        can_remove=True,
        can_reorder=False,
        can_clear=False,
    )
    group = main.DownloadGroup(
        id="group-1",
        name="release",
        created_at=main.now_iso(),
        updated_at=main.now_iso(),
        original_hosts=["rapidgator.example"],
        parts=[
            main.DownloadGroupPart(
                aria2_gid="active-1",
                filename="release.iso",
                local_download_path="/downloads/release.iso",
                status="submitted",
                error="https://secret.example/private?token=bad",
                total_length=0,
                completed_length=0,
            ),
        ],
    )

    class FakeAria2:
        async def queue_snapshot(self) -> QueueSnapshot:
            return QueueSnapshot(active=[active], waiting=[], stopped=[])

    class FakeGroupStore:
        def load_groups(self) -> list[main.DownloadGroup]:
            return [group]

    async def run_test() -> None:
        app.dependency_overrides[main.get_aria2_client] = dependency_override(FakeAria2())
        app.dependency_overrides[main.get_group_store] = dependency_override(FakeGroupStore())
        async with app_client() as client:
            response = await client.get("/api/queue")

        assert response.status_code == 200
        payload = response.json()
        assert payload["active"][0]["gid"] == "active-1"
        assert payload["active"][0]["progress_percent"] == 50.0
        assert payload["groups"][0]["id"] == "group-1"
        assert payload["groups"][0]["parts"][0]["status"] == "active"
        assert payload["groups"][0]["parts"][0]["progress_percent"] == 50.0
        response_text = response.text
        assert "secret.example" not in response_text
        assert "token=bad" not in response_text
        assert "/downloads/release.iso" not in response_text

    anyio.run(run_test)


def test_api_queue_action_routes_call_aria2() -> None:
    calls: list[str] = []

    class FakeAria2:
        async def pause(self, gid: str) -> None:
            calls.append(f"pause:{gid}")

        async def unpause(self, gid: str) -> None:
            calls.append(f"unpause:{gid}")

        async def remove(self, gid: str) -> None:
            calls.append(f"remove:{gid}")

        async def remove_download_result(self, gid: str) -> None:
            calls.append(f"clear:{gid}")

        async def purge_download_result(self) -> None:
            calls.append("purge")

        async def move_to(self, gid: str, position: int) -> None:
            calls.append(f"move_to:{gid}:{position}")

    async def run_test() -> None:
        app.dependency_overrides[main.get_aria2_client] = dependency_override(FakeAria2())
        async with app_client() as client:
            responses = [
                await client.post("/api/queue/gid-1/pause"),
                await client.post("/api/queue/gid-1/resume"),
                await client.post("/api/queue/gid-1/remove"),
                await client.post("/api/queue/gid-1/clear"),
                await client.post("/api/queue/clear-stopped"),
                await client.post("/api/queue/gid-1/move", json={"position": 2}),
            ]

        assert all(response.status_code == 200 for response in responses)
        assert all(response.json()["ok"] is True for response in responses)
        assert calls == [
            "pause:gid-1",
            "unpause:gid-1",
            "remove:gid-1",
            "clear:gid-1",
            "purge",
            "move_to:gid-1:2",
        ]

    anyio.run(run_test)


def test_api_queue_move_rejects_invalid_position_without_leaking_details() -> None:
    class FakeAria2:
        async def move_to(self, gid: str, position: int) -> None:
            raise ValueError("token:aria-secret failed at https://aria2.test/jsonrpc")

    async def run_test() -> None:
        app.dependency_overrides[main.get_aria2_client] = dependency_override(FakeAria2())
        async with app_client() as client:
            response = await client.post("/api/queue/gid-1/move", json={"position": -1})

        assert response.status_code == 200
        assert response.json() == {
            "ok": False,
            "message": "Queue action failed. Please try again.",
        }
        assert "aria-secret" not in response.text
        assert "aria2.test" not in response.text

    anyio.run(run_test)


def test_queue_action_routes_call_aria2_and_redirect() -> None:
    calls: list[str] = []

    class FakeAria2:
        async def pause(self, gid: str) -> None:
            calls.append(f"pause:{gid}")

        async def unpause(self, gid: str) -> None:
            calls.append(f"unpause:{gid}")

        async def remove(self, gid: str) -> None:
            calls.append(f"remove:{gid}")

        async def remove_download_result(self, gid: str) -> None:
            calls.append(f"clear:{gid}")

        async def purge_download_result(self) -> None:
            calls.append("purge")

        async def move(self, gid: str, direction: str) -> None:
            calls.append(f"move:{gid}:{direction}")

    async def run_test() -> None:
        app.dependency_overrides[main.get_aria2_client] = dependency_override(FakeAria2())
        async with app_client() as client:
            responses = [
                await client.post("/queue/gid-1/pause"),
                await client.post("/queue/gid-1/resume"),
                await client.post("/queue/gid-1/remove"),
                await client.post("/queue/gid-1/clear"),
                await client.post("/queue/clear-stopped"),
                await client.post("/queue/gid-1/move", data={"direction": "up"}),
            ]

        assert all(response.status_code == 303 for response in responses)
        assert all(response.headers["location"].startswith("/queue?") for response in responses)
        assert calls == [
            "pause:gid-1",
            "unpause:gid-1",
            "remove:gid-1",
            "clear:gid-1",
            "purge",
            "move:gid-1:up",
        ]

    anyio.run(run_test)


def test_queue_page_handles_aria2_failure_without_leaking_secret() -> None:
    class FakeAria2:
        async def queue_snapshot(self) -> QueueSnapshot:
            raise main.UpstreamError("aria2 rejected token:aria-secret at https://aria2.test/jsonrpc")

    async def run_test() -> None:
        app.dependency_overrides[main.get_aria2_client] = dependency_override(FakeAria2())
        async with app_client() as client:
            response = await client.get("/queue")

        assert response.status_code == 200
        assert "Download queue is temporarily unavailable." in response.text
        assert "aria-secret" not in response.text
        assert "aria2.test" not in response.text

    anyio.run(run_test)


def test_queue_events_emits_live_queue_html() -> None:
    active = QueueItem(
        gid="active-1",
        status="active",
        name="live.iso",
        total_length=1000,
        completed_length=250,
        download_speed=100,
        eta_seconds=7,
        error_message=None,
        can_pause=True,
        can_resume=False,
        can_remove=True,
        can_reorder=False,
        can_clear=False,
    )

    class FakeAria2:
        async def queue_snapshot(self) -> QueueSnapshot:
            return QueueSnapshot(active=[active], waiting=[], stopped=[])

    async def run_test() -> None:
        app.dependency_overrides[main.get_aria2_client] = dependency_override(FakeAria2())
        async with app_client() as client:
            response = await client.get("/queue/events?once=true")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert "event: queue" in response.text
        assert "live.iso" in response.text
        assert "25.0%" in response.text

    anyio.run(run_test)


def test_queue_events_error_does_not_leak_aria2_secret_or_url() -> None:
    class FakeAria2:
        async def queue_snapshot(self) -> QueueSnapshot:
            raise main.UpstreamError("token:aria-secret failed at https://aria2.test/jsonrpc")

    async def run_test() -> None:
        app.dependency_overrides[main.get_aria2_client] = dependency_override(FakeAria2())
        async with app_client() as client:
            response = await client.get("/queue/events?once=true")

        assert response.status_code == 200
        assert "event: error" in response.text
        assert "Download queue is temporarily unavailable." in response.text
        assert "aria-secret" not in response.text
        assert "aria2.test" not in response.text

    anyio.run(run_test)


def test_multipart_group_projection_updates_display_without_writing_state(tmp_path: Any) -> None:
    state_file = tmp_path / "groups.json"
    store = main.GroupStateStore(str(state_file))
    store.save_groups(
        [
            main.DownloadGroup(
                id="group-1",
                name="release",
                created_at="2026-07-10T00:00:00+00:00",
                updated_at="2026-07-10T00:00:00+00:00",
                original_hosts=[],
                parts=[
                    main.DownloadGroupPart(
                        aria2_gid="gid-1",
                        filename="release.part1.rar",
                        local_download_path="/downloads/release.part1.rar",
                        status="submitted",
                    ),
                ],
            ),
        ],
    )
    active = QueueItem(
        gid="gid-1",
        status="active",
        name="release.part1.rar",
        total_length=1000,
        completed_length=500,
        download_speed=10,
        eta_seconds=50,
        error_message=None,
        can_pause=True,
        can_resume=False,
        can_remove=True,
        can_reorder=False,
        can_clear=False,
    )

    class FakeAria2:
        async def queue_snapshot(self) -> QueueSnapshot:
            return QueueSnapshot(active=[active], waiting=[], stopped=[])

    async def run_test() -> None:
        app.dependency_overrides[main.get_aria2_client] = dependency_override(FakeAria2())
        app.dependency_overrides[main.get_group_store] = dependency_override(store)
        async with app_client() as client:
            response = await client.get("/queue")

        assert response.status_code == 200
        assert "release.part1.rar - active" in response.text
        assert "500 B / 1000 B" in response.text
        assert "/queue/groups/group-1/clear" in response.text

        persisted = store.load_groups()[0]
        assert persisted.parts[0].status == "submitted"
        assert persisted.parts[0].completed_length == 0

    anyio.run(run_test)


def test_group_state_clear_removes_any_group_metadata(tmp_path: Any) -> None:
    store = main.GroupStateStore(str(tmp_path / "groups.json"))
    active_group = main.DownloadGroup(
        id="active-group",
        name="active",
        created_at="2026-07-10T00:00:00+00:00",
        updated_at="2026-07-10T00:00:00+00:00",
        original_hosts=[],
        parts=[main.DownloadGroupPart("gid-active", "active.rar", "/downloads/active.rar", "active")],
    )
    terminal_group = main.DownloadGroup(
        id="terminal-group",
        name="terminal",
        created_at="2026-07-10T00:00:00+00:00",
        updated_at="2026-07-10T00:00:00+00:00",
        original_hosts=[],
        parts=[main.DownloadGroupPart("gid-done", "done.rar", "/downloads/done.rar", "complete")],
    )
    store.save_groups([active_group, terminal_group])

    assert store.remove_group("active-group") is True
    assert store.remove_group("terminal-group") is True
    assert store.load_groups() == []


def test_group_state_bulk_clear_removes_all_group_metadata(tmp_path: Any) -> None:
    store = main.GroupStateStore(str(tmp_path / "groups.json"))
    groups = [
        main.DownloadGroup(
            id="active-group",
            name="active",
            created_at="2026-07-10T00:00:00+00:00",
            updated_at="2026-07-10T00:00:00+00:00",
            original_hosts=[],
            parts=[main.DownloadGroupPart("gid-active", "active.rar", "/downloads/active.rar", "paused")],
        ),
        main.DownloadGroup(
            id="done-group",
            name="done",
            created_at="2026-07-10T00:00:00+00:00",
            updated_at="2026-07-10T00:00:00+00:00",
            original_hosts=[],
            parts=[main.DownloadGroupPart("gid-done", "done.rar", "/downloads/done.rar", "complete")],
        ),
        main.DownloadGroup(
            id="failed-extract-group",
            name="failed-extract",
            created_at="2026-07-10T00:00:00+00:00",
            updated_at="2026-07-10T00:00:00+00:00",
            original_hosts=[],
            parts=[main.DownloadGroupPart("gid-extract", "extract.rar", "/downloads/extract.rar", "complete")],
            extraction_status="failed",
        ),
    ]
    store.save_groups(groups)

    assert store.clear_eligible_groups() == 3
    assert store.load_groups() == []


def test_group_clear_routes_redirect_with_messages(tmp_path: Any) -> None:
    store = main.GroupStateStore(str(tmp_path / "groups.json"))
    store.save_groups(
        [
            main.DownloadGroup(
                id="done-group",
                name="done",
                created_at="2026-07-10T00:00:00+00:00",
                updated_at="2026-07-10T00:00:00+00:00",
                original_hosts=[],
                parts=[main.DownloadGroupPart("gid-done", "done.rar", "/downloads/done.rar", "complete")],
            ),
            main.DownloadGroup(
                id="active-group",
                name="active",
                created_at="2026-07-10T00:00:00+00:00",
                updated_at="2026-07-10T00:00:00+00:00",
                original_hosts=[],
                parts=[main.DownloadGroupPart("gid-active", "active.rar", "/downloads/active.rar", "active")],
            ),
        ],
    )

    async def run_test() -> None:
        app.dependency_overrides[main.get_group_store] = dependency_override(store)
        async with app_client() as client:
            single = await client.post("/queue/groups/done-group/clear")
            active = await client.post("/queue/groups/active-group/clear")
            bulk = await client.post("/queue/groups/clear")

        assert single.status_code == 303
        assert "Multipart+group+history+entry+cleared" in single.headers["location"]
        assert active.status_code == 303
        assert "Multipart+group+history+entry+cleared" in active.headers["location"]
        assert bulk.status_code == 303
        assert "No+multipart+group+history" in bulk.headers["location"]

    anyio.run(run_test)


def test_api_group_clear_routes_return_json_messages(tmp_path: Any) -> None:
    store = main.GroupStateStore(str(tmp_path / "groups.json"))
    store.save_groups(
        [
            main.DownloadGroup(
                id="done-group",
                name="done",
                created_at="2026-07-10T00:00:00+00:00",
                updated_at="2026-07-10T00:00:00+00:00",
                original_hosts=[],
                parts=[main.DownloadGroupPart("gid-done", "done.rar", "/downloads/done.rar", "complete")],
            ),
            main.DownloadGroup(
                id="active-group",
                name="active",
                created_at="2026-07-10T00:00:00+00:00",
                updated_at="2026-07-10T00:00:00+00:00",
                original_hosts=[],
                parts=[main.DownloadGroupPart("gid-active", "active.rar", "/downloads/active.rar", "active")],
            ),
        ],
    )

    async def run_test() -> None:
        app.dependency_overrides[main.get_group_store] = dependency_override(store)
        async with app_client() as client:
            single = await client.post("/api/queue/groups/done-group/clear")
            active = await client.post("/api/queue/groups/active-group/clear")
            bulk = await client.post("/api/queue/groups/clear")

        assert single.status_code == 200
        assert single.json() == {"ok": True, "message": "Multipart group history entry cleared."}
        assert active.status_code == 200
        assert active.json() == {"ok": True, "message": "Multipart group history entry cleared."}
        assert bulk.status_code == 200
        assert bulk.json()["ok"] is False

    anyio.run(run_test)


def test_extract_urls_from_free_text() -> None:
    text = """
    Here are links:
    "https://rapidgator.example/file.part1.rar",
    (https://rapidgator.example/file.part2.rar)
    duplicate: https://rapidgator.example/file.part1.rar
    not a url: example.com/file
    [http://host.example/archive.zip].
    """

    assert main.extract_urls(text) == [
        "https://rapidgator.example/file.part1.rar",
        "https://rapidgator.example/file.part2.rar",
        "http://host.example/archive.zip",
    ]


def test_extract_urls_accepts_magnet_links_and_deduplicates() -> None:
    text = """
    magnet: magnet:?xt=urn:btih:ABC123&dn=Release
    duplicate: (magnet:?xt=urn:btih:ABC123&dn=Release)
    hoster: https://rapidgator.example/file.rar,
    trailing quote: "magnet:?xt=urn:btih:DEF456".
    """

    assert main.extract_urls(text) == [
        "magnet:?xt=urn:btih:ABC123&dn=Release",
        "https://rapidgator.example/file.rar",
        "magnet:?xt=urn:btih:DEF456",
    ]


def test_multi_url_submit_creates_group_and_does_not_persist_submitted_urls(
    tmp_path: Any,
) -> None:
    async def run_test() -> None:
        settings = make_settings(group_state_file=str(tmp_path / "groups.json"))
        downloader = Downloader(settings)
        submitted_to_aria2: list[str] = []

        class FakeRealDebrid:
            async def supported_domains(self) -> set[str]:
                return {"rapidgator.example"}

            async def check_link(self, submitted_url: str) -> dict[str, Any]:
                return {"supported": 1}

            async def unrestrict_link(self, submitted_url: str) -> dict[str, Any]:
                basename = submitted_url.rsplit("/", 1)[-1].split("?", 1)[0]
                return {
                    "download": f"https://download.example/{basename}?rd=secret",
                    "filename": basename,
                }

        class FakeAria2:
            async def add_uri(self, direct_url: str) -> str:
                submitted_to_aria2.append(direct_url)
                return f"gid-{len(submitted_to_aria2)}"

        downloader.realdebrid = FakeRealDebrid()  # type: ignore[assignment]
        downloader.aria2 = FakeAria2()  # type: ignore[assignment]

        submission = await downloader.submit_text(
            "first https://rapidgator.example/private/movie.part1.rar?token=one\n"
            "second https://rapidgator.example/private/movie.part2.rar?token=two",
        )

        assert submission.ok is True
        assert submission.group is not None
        assert submission.group.name == "movie"
        assert [part.aria2_gid for part in submission.group.parts] == ["gid-1", "gid-2"]
        assert submitted_to_aria2 == [
            "https://download.example/movie.part1.rar?rd=secret",
            "https://download.example/movie.part2.rar?rd=secret",
        ]
        persisted = (tmp_path / "groups.json").read_text()
        assert "rapidgator.example" in persisted
        assert "token=one" not in persisted
        assert "/private/" not in persisted

    anyio.run(run_test)


def test_magnet_submit_unrestricts_ready_torrent_links_and_persists_sanitized_group(
    tmp_path: Any,
) -> None:
    async def run_test() -> None:
        settings = make_settings(
            group_state_file=str(tmp_path / "groups.json"),
            torrent_poll_seconds=0.01,
            torrent_ready_timeout_seconds=1,
        )
        downloader = Downloader(settings)
        calls: list[str] = []

        class FakeRealDebrid:
            async def add_magnet(self, magnet_link: str) -> dict[str, Any]:
                calls.append("add_magnet")
                assert magnet_link == "magnet:?xt=urn:btih:SECRET_HASH&dn=release"
                return {"id": "torrent-1", "uri": "https://rd.example/torrent-1"}

            async def torrent_info(self, torrent_id: str) -> dict[str, Any]:
                calls.append(f"torrent_info:{torrent_id}")
                return {
                    "id": torrent_id,
                    "status": "downloaded",
                    "files": [{"id": 1, "path": "/release.part1.rar", "selected": 1}],
                    "links": [
                        "https://hoster.example/release.part1.rar",
                        "https://hoster.example/release.part2.rar",
                    ],
                }

            async def select_torrent_files(self, torrent_id: str, files: str = "all") -> None:
                calls.append(f"select:{torrent_id}:{files}")

            async def unrestrict_link(self, submitted_url: str) -> dict[str, Any]:
                calls.append(f"unrestrict:{submitted_url}")
                basename = submitted_url.rsplit("/", 1)[-1]
                return {
                    "download": f"https://download.example/{basename}?token=rd-secret",
                    "filename": basename,
                }

        class FakeAria2:
            async def add_uri(self, direct_url: str) -> str:
                calls.append(f"add_uri:{direct_url}")
                return f"gid-{len([call for call in calls if call.startswith('add_uri:')])}"

        downloader.realdebrid = FakeRealDebrid()  # type: ignore[assignment]
        downloader.aria2 = FakeAria2()  # type: ignore[assignment]

        submission = await downloader.submit_text("magnet:?xt=urn:btih:SECRET_HASH&dn=release")

        assert submission.ok is True
        assert submission.message == "Submitted 2 parts to aria2 as one multipart group."
        assert [download.filename for download in submission.downloads] == [
            "release.part1.rar",
            "release.part2.rar",
        ]
        assert submission.group is not None
        assert submission.group.original_hosts == ["magnet link"]
        persisted = (tmp_path / "groups.json").read_text()
        assert "magnet link" in persisted
        assert "SECRET_HASH" not in persisted
        assert "magnet:?" not in persisted
        assert "token=rd-secret" not in persisted
        assert calls == [
            "add_magnet",
            "torrent_info:torrent-1",
            "select:torrent-1:all",
            "torrent_info:torrent-1",
            "unrestrict:https://hoster.example/release.part1.rar",
            "add_uri:https://download.example/release.part1.rar?token=rd-secret",
            "unrestrict:https://hoster.example/release.part2.rar",
            "add_uri:https://download.example/release.part2.rar?token=rd-secret",
        ]

    anyio.run(run_test)


def test_magnet_submit_waits_for_real_debrid_links(tmp_path: Any) -> None:
    async def run_test() -> None:
        settings = make_settings(
            group_state_file=str(tmp_path / "groups.json"),
            torrent_poll_seconds=0.01,
            torrent_ready_timeout_seconds=1,
        )
        downloader = Downloader(settings)
        info_calls = 0

        class FakeRealDebrid:
            async def add_magnet(self, magnet_link: str) -> dict[str, Any]:
                return {"id": "torrent-1"}

            async def torrent_info(self, torrent_id: str) -> dict[str, Any]:
                nonlocal info_calls
                info_calls += 1
                if info_calls == 1:
                    return {
                        "status": "waiting_files_selection",
                        "files": [{"id": 1, "path": "/release.mkv", "selected": 0}],
                        "links": [],
                    }
                if info_calls == 2:
                    return {
                        "status": "downloading",
                        "files": [{"id": 1, "path": "/release.mkv", "selected": 1}],
                        "links": [],
                    }
                return {
                    "status": "downloaded",
                    "files": [{"id": 1, "path": "/release.mkv", "selected": 1}],
                    "links": ["https://hoster.example/release.mkv"],
                }

            async def select_torrent_files(self, torrent_id: str, files: str = "all") -> None:
                assert files == "all"

            async def unrestrict_link(self, submitted_url: str) -> dict[str, Any]:
                return {
                    "download": "https://download.example/release.mkv",
                    "filename": "release.mkv",
                }

        class FakeAria2:
            async def add_uri(self, direct_url: str) -> str:
                return "gid-1"

        downloader.realdebrid = FakeRealDebrid()  # type: ignore[assignment]
        downloader.aria2 = FakeAria2()  # type: ignore[assignment]

        submission = await downloader.submit_text("magnet:?xt=urn:btih:WAITING_HASH")

        assert submission.ok is True
        assert info_calls == 3
        assert submission.downloads[0].aria2_gid == "gid-1"

    anyio.run(run_test)


def test_magnet_terminal_error_returns_sanitized_failure() -> None:
    async def run_test() -> None:
        downloader = Downloader(make_settings(torrent_poll_seconds=0.01, torrent_ready_timeout_seconds=1))

        class FakeRealDebrid:
            async def add_magnet(self, magnet_link: str) -> dict[str, Any]:
                return {"id": "torrent-1"}

            async def torrent_info(self, torrent_id: str) -> dict[str, Any]:
                return {"status": "magnet_error", "files": [], "links": []}

        class FakeAria2:
            async def add_uri(self, direct_url: str) -> str:
                raise AssertionError("aria2 should not be called for a failed torrent")

        downloader.realdebrid = FakeRealDebrid()  # type: ignore[assignment]
        downloader.aria2 = FakeAria2()  # type: ignore[assignment]

        submission = await downloader.submit_text("magnet:?xt=urn:btih:BAD_HASH")

        assert submission.ok is False
        assert submission.message == main.TORRENT_PROCESSING_FAILED_MESSAGE
        assert submission.downloads[0].source_label == "magnet link"
        assert "BAD_HASH" not in submission.message

    anyio.run(run_test)


def test_mixed_hoster_and_magnet_submission_reports_partial_success(tmp_path: Any) -> None:
    async def run_test() -> None:
        settings = make_settings(group_state_file=str(tmp_path / "groups.json"))
        downloader = Downloader(settings)

        class FakeRealDebrid:
            async def supported_domains(self) -> set[str]:
                return {"rapidgator.example"}

            async def check_link(self, submitted_url: str) -> dict[str, Any]:
                return {"supported": 1}

            async def unrestrict_link(self, submitted_url: str) -> dict[str, Any]:
                return {
                    "download": "https://download.example/file.rar",
                    "filename": "file.rar",
                }

            async def add_magnet(self, magnet_link: str) -> dict[str, Any]:
                return {"id": "torrent-1"}

            async def torrent_info(self, torrent_id: str) -> dict[str, Any]:
                return {"status": "dead", "files": [], "links": []}

        class FakeAria2:
            async def add_uri(self, direct_url: str) -> str:
                return "gid-1"

        downloader.realdebrid = FakeRealDebrid()  # type: ignore[assignment]
        downloader.aria2 = FakeAria2()  # type: ignore[assignment]

        submission = await downloader.submit_text(
            "https://rapidgator.example/file.rar\n"
            "magnet:?xt=urn:btih:BAD_HASH",
        )

        assert submission.ok is True
        assert submission.message == "Submitted 1 of 2 parts to aria2. 1 part(s) failed."
        assert [download.ok for download in submission.downloads] == [True, False]
        assert submission.group is not None
        assert submission.group.original_hosts == ["rapidgator.example", "magnet link"]

    anyio.run(run_test)


def test_multi_url_partial_failure_creates_partial_group(tmp_path: Any) -> None:
    async def run_test() -> None:
        settings = make_settings(group_state_file=str(tmp_path / "groups.json"))
        downloader = Downloader(settings)

        class FakeRealDebrid:
            async def supported_domains(self) -> None:
                return None

            async def check_link(self, submitted_url: str) -> dict[str, Any]:
                return {}

            async def unrestrict_link(self, submitted_url: str) -> dict[str, Any]:
                if "bad" in submitted_url:
                    raise main.DownloadUnavailableError
                return {
                    "download": "https://download.example/movie.part1.rar",
                    "filename": "movie.part1.rar",
                }

        class FakeAria2:
            async def add_uri(self, direct_url: str) -> str:
                return "gid-ok"

        downloader.realdebrid = FakeRealDebrid()  # type: ignore[assignment]
        downloader.aria2 = FakeAria2()  # type: ignore[assignment]

        submission = await downloader.submit_text(
            "https://rapidgator.example/movie.part1.rar\n"
            "https://rapidgator.example/bad.part2.rar",
        )

        assert submission.ok is True
        assert "Submitted 1 of 2 parts" in submission.message
        assert submission.group is not None
        assert len(submission.group.parts) == 1
        assert [download.ok for download in submission.downloads] == [True, False]

    anyio.run(run_test)


def test_group_state_round_trip_uses_atomic_replace(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    state_file = tmp_path / "groups.json"
    store = main.GroupStateStore(str(state_file))
    replacements: list[tuple[str, str]] = []
    original_replace = main.os.replace

    def recording_replace(source: str, target: str) -> None:
        replacements.append((source, target))
        original_replace(source, target)

    monkeypatch.setattr(main.os, "replace", recording_replace)
    group = main.DownloadGroup(
        id="group-1",
        name="release",
        created_at="2026-07-10T00:00:00+00:00",
        updated_at="2026-07-10T00:00:00+00:00",
        original_hosts=["rapidgator.example"],
        parts=[
            main.DownloadGroupPart(
                aria2_gid="gid-1",
                filename="release.part1.rar",
                local_download_path="/downloads/release.part1.rar",
                status="complete",
            ),
        ],
    )

    store.save_groups([group])
    loaded = store.load_groups()

    assert replacements
    assert replacements[0][1] == str(state_file)
    assert loaded[0].name == "release"
    assert loaded[0].parts[0].aria2_gid == "gid-1"


def test_group_name_derivation_from_part_filename() -> None:
    assert (
        main.derive_group_name(["Show.Name.S01.part01.rar", "Show.Name.S01.part02.rar"], "fallback")
        == "Show.Name.S01"
    )


def test_extraction_waits_until_all_parts_complete(tmp_path: Any) -> None:
    async def run_test() -> None:
        settings = make_settings(
            app_download_dir=str(tmp_path / "downloads"),
            group_state_file=str(tmp_path / "groups.json"),
        )
        monitor = main.GroupMonitor(settings)
        group = main.DownloadGroup(
            id="group-1",
            name="release",
            created_at=main.now_iso(),
            updated_at=main.now_iso(),
            original_hosts=[],
            parts=[
                main.DownloadGroupPart("gid-1", "release.part1.rar", str(tmp_path / "release.part1.rar"), "complete"),
                main.DownloadGroupPart("gid-2", "release.part2.rar", str(tmp_path / "release.part2.rar"), "active"),
            ],
        )

        assert await monitor._maybe_extract(group) is False
        assert group.extraction_status == "pending"

    anyio.run(run_test)


def test_extraction_uses_first_archive_part_and_deletes_after_success(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run_test() -> None:
        downloads = tmp_path / "downloads"
        downloads.mkdir()
        part1 = downloads / "release.part1.rar"
        part2 = downloads / "release.part2.rar"
        part1.write_text("part1")
        part2.write_text("part2")
        calls: list[tuple[str, str, int]] = []

        async def fake_extract_archive(start_file: str, output_dir: str, timeout_seconds: int) -> main.ExtractionResult:
            calls.append((start_file, output_dir, timeout_seconds))
            return main.ExtractionResult(ok=True)

        monkeypatch.setattr(main, "extract_archive", fake_extract_archive)
        settings = make_settings(
            app_download_dir=str(downloads),
            group_state_file=str(tmp_path / "groups.json"),
            extract_timeout_seconds=42,
        )
        monitor = main.GroupMonitor(settings)
        group = main.DownloadGroup(
            id="group-1",
            name="release",
            created_at=main.now_iso(),
            updated_at=main.now_iso(),
            original_hosts=[],
            parts=[
                main.DownloadGroupPart("gid-1", "release.part2.rar", str(part2), "complete"),
                main.DownloadGroupPart("gid-2", "release.part1.rar", str(part1), "complete"),
            ],
        )

        assert await monitor._maybe_extract(group) is True
        assert calls == [(str(part1), str(downloads / "release"), 42)]
        assert group.extraction_status == "complete"
        assert not part1.exists()
        assert not part2.exists()

    anyio.run(run_test)


def test_group_refresh_ignores_aria2_paths_outside_download_dir(tmp_path: Any) -> None:
    async def run_test() -> None:
        downloads = tmp_path / "downloads"
        downloads.mkdir()
        outside = tmp_path / "outside" / "release.part1.rar"
        settings = make_settings(
            app_download_dir=str(downloads),
            group_state_file=str(tmp_path / "groups.json"),
        )
        monitor = main.GroupMonitor(settings)

        class FakeAria2:
            async def tell_status(self, gid: str) -> dict[str, Any]:
                assert gid == "gid-1"
                return {
                    "gid": gid,
                    "status": "complete",
                    "files": [{"path": str(outside)}],
                }

        monitor.aria2 = FakeAria2()  # type: ignore[assignment]
        group = main.DownloadGroup(
            id="group-1",
            name="release",
            created_at=main.now_iso(),
            updated_at=main.now_iso(),
            original_hosts=[],
            parts=[main.DownloadGroupPart("gid-1", None, None, "active")],
        )

        assert await monitor._refresh_group(group) is True
        assert group.parts[0].filename == "release.part1.rar"
        assert group.parts[0].local_download_path is None
        assert group.parts[0].status == "complete"

    anyio.run(run_test)


def test_extraction_ignores_persisted_paths_outside_download_dir(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run_test() -> None:
        downloads = tmp_path / "downloads"
        outside_dir = tmp_path / "outside"
        downloads.mkdir()
        outside_dir.mkdir()
        outside = outside_dir / "release.part1.rar"
        outside.write_text("part1")

        async def fake_extract_archive(start_file: str, output_dir: str, timeout_seconds: int) -> main.ExtractionResult:
            raise AssertionError("unsafe archive path must not be extracted")

        monkeypatch.setattr(main, "extract_archive", fake_extract_archive)
        settings = make_settings(
            app_download_dir=str(downloads),
            group_state_file=str(tmp_path / "groups.json"),
        )
        monitor = main.GroupMonitor(settings)
        group = main.DownloadGroup(
            id="group-1",
            name="release",
            created_at=main.now_iso(),
            updated_at=main.now_iso(),
            original_hosts=[],
            parts=[main.DownloadGroupPart("gid-1", "release.part1.rar", str(outside), "complete")],
        )

        assert await monitor._maybe_extract(group) is True
        assert group.extraction_status == "skipped"
        assert outside.exists()

    anyio.run(run_test)


def test_extraction_failure_keeps_archive_parts(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run_test() -> None:
        downloads = tmp_path / "downloads"
        downloads.mkdir()
        part1 = downloads / "release.rar"
        part1.write_text("part1")

        async def fake_extract_archive(start_file: str, output_dir: str, timeout_seconds: int) -> main.ExtractionResult:
            return main.ExtractionResult(ok=False, message="bad archive")

        monkeypatch.setattr(main, "extract_archive", fake_extract_archive)
        settings = make_settings(
            app_download_dir=str(downloads),
            group_state_file=str(tmp_path / "groups.json"),
        )
        monitor = main.GroupMonitor(settings)
        group = main.DownloadGroup(
            id="group-1",
            name="release",
            created_at=main.now_iso(),
            updated_at=main.now_iso(),
            original_hosts=[],
            parts=[main.DownloadGroupPart("gid-1", "release.rar", str(part1), "complete")],
        )

        assert await monitor._maybe_extract(group) is True
        assert group.extraction_status == "failed"
        assert group.extraction_error == "bad archive"
        assert part1.exists()

    anyio.run(run_test)


def test_non_archive_download_skips_extraction(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run_test() -> None:
        downloads = tmp_path / "downloads"
        downloads.mkdir()
        dmg = downloads / "installer.dmg"
        dmg.write_text("disk image")

        async def fake_extract_archive(start_file: str, output_dir: str, timeout_seconds: int) -> main.ExtractionResult:
            raise AssertionError("non-archive downloads must not be extracted")

        monkeypatch.setattr(main, "extract_archive", fake_extract_archive)
        settings = make_settings(
            app_download_dir=str(downloads),
            group_state_file=str(tmp_path / "groups.json"),
        )
        monitor = main.GroupMonitor(settings)
        group = main.DownloadGroup(
            id="group-1",
            name="installer",
            created_at=main.now_iso(),
            updated_at=main.now_iso(),
            original_hosts=[],
            parts=[main.DownloadGroupPart("gid-1", "installer.dmg", str(dmg), "complete")],
        )

        assert await monitor._maybe_extract(group) is True
        assert group.extraction_status == "skipped"
        assert group.extraction_error is None
        assert group.extraction_output_path is None
        assert dmg.exists()

    anyio.run(run_test)


def test_old_non_archive_extraction_failure_loads_as_skipped(tmp_path: Any) -> None:
    state_file = tmp_path / "groups.json"
    state_file.write_text(
        json.dumps(
            {
                "groups": [
                    {
                        "id": "group-1",
                        "name": "installer",
                        "created_at": "2026-07-11T00:00:00+00:00",
                        "updated_at": "2026-07-11T00:00:00+00:00",
                        "original_hosts": ["rapidgator.net"],
                        "extraction_status": "failed",
                        "extraction_error": main.NO_SUPPORTED_ARCHIVE_MESSAGE,
                        "parts": [
                            {
                                "aria2_gid": "gid-1",
                                "filename": "installer.dmg",
                                "local_download_path": "/downloads/installer.dmg",
                                "status": "complete",
                            },
                        ],
                    },
                ],
            },
        ),
    )

    group = main.GroupStateStore(str(state_file)).load_groups()[0]

    assert group.extraction_status == "skipped"
    assert group.extraction_error is None


def test_extracting_group_is_resumable_after_restart(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run_test() -> None:
        downloads = tmp_path / "downloads"
        downloads.mkdir()
        part1 = downloads / "release.part1.rar"
        part1.write_text("part1")

        async def fake_extract_archive(start_file: str, output_dir: str, timeout_seconds: int) -> main.ExtractionResult:
            return main.ExtractionResult(ok=True)

        monkeypatch.setattr(main, "extract_archive", fake_extract_archive)
        settings = make_settings(
            app_download_dir=str(downloads),
            group_state_file=str(tmp_path / "groups.json"),
        )
        monitor = main.GroupMonitor(settings)
        group = main.DownloadGroup(
            id="group-1",
            name="release",
            created_at=main.now_iso(),
            updated_at=main.now_iso(),
            original_hosts=[],
            parts=[main.DownloadGroupPart("gid-1", "release.part1.rar", str(part1), "complete")],
            extraction_status="extracting",
        )

        assert await monitor._maybe_extract(group) is True
        assert group.extraction_status == "complete"

    anyio.run(run_test)


def make_settings(**overrides: Any) -> Settings:
    values = {
        "realdebrid_api_token": "token-1",
        "realdebrid_api_base_url": "https://api.real-debrid.test/rest/1.0",
        "aria2_rpc_url": "https://aria2.test/jsonrpc",
        "aria2_rpc_secret": "aria-secret",
        "aria2_download_dir": "/downloads",
        "aria2_max_connection_per_server": "8",
        "aria2_max_concurrent_downloads": 1,
        "aria2_split": "8",
        "submitted_url_logging": False,
        "app_download_dir": "/downloads",
        "group_state_file": f"{tempfile.gettempdir()}/rd-downloader-test-{uuid.uuid4().hex}.json",
        "extract_timeout_seconds": 7200,
        "group_poll_seconds": 30,
        "queue_stream_interval_seconds": 1.0,
        "torrent_poll_seconds": 0.01,
        "torrent_ready_timeout_seconds": 1,
    }
    values.update(overrides)
    return Settings(**values)


def dependency_override(value: Any) -> Any:
    async def override() -> Any:
        return value

    return override


def app_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )
