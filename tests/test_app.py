from __future__ import annotations

import json
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
    yield
    app.dependency_overrides.clear()


def test_healthz() -> None:
    async def run_test() -> None:
        async with app_client() as client:
            response = await client.get("/healthz")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    anyio.run(run_test)


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


def test_submit_shows_required_no_download_message() -> None:
    class FakeDownloader:
        async def submit(self, url: str) -> DownloadResult:
            return DownloadResult(ok=False, message=NO_DOWNLOAD_MESSAGE)

    async def run_test() -> None:
        response_text = await main.submit("https://example.com/file", FakeDownloader())  # type: ignore[arg-type]

        assert NO_DOWNLOAD_MESSAGE in response_text

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
                assert payload["method"] == "aria2.addUri"
                assert payload["params"] == [
                    "token:aria-secret",
                    ["https://download.example/file"],
                    {
                        "dir": "/downloads",
                        "max-connection-per-server": "8",
                        "split": "8",
                    },
                ]
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

        assert methods == [
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
        app.dependency_overrides[main.get_aria2_client] = lambda: FakeAria2()
        async with app_client() as client:
            response = await client.get("/queue")

        assert response.status_code == 200
        text = response.text
        assert "Download Queue" in text
        assert "active.iso" in text
        assert "waiting.iso" in text
        assert "done.iso" in text
        assert '/queue/active-1/pause' in text
        assert '/queue/waiting-1/move' in text
        assert '/queue/stopped-1/clear' in text
        assert '/queue/clear-stopped' in text

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
        app.dependency_overrides[main.get_aria2_client] = lambda: FakeAria2()
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
        app.dependency_overrides[main.get_aria2_client] = lambda: FakeAria2()
        async with app_client() as client:
            response = await client.get("/queue")

        assert response.status_code == 200
        assert "Download queue is temporarily unavailable." in response.text
        assert "aria-secret" not in response.text
        assert "aria2.test" not in response.text

    anyio.run(run_test)


def make_settings(**overrides: Any) -> Settings:
    values = {
        "realdebrid_api_token": "token-1",
        "realdebrid_api_base_url": "https://api.real-debrid.test/rest/1.0",
        "aria2_rpc_url": "https://aria2.test/jsonrpc",
        "aria2_rpc_secret": "aria-secret",
        "aria2_download_dir": "/downloads",
        "aria2_max_connection_per_server": "8",
        "aria2_split": "8",
        "submitted_url_logging": False,
    }
    values.update(overrides)
    return Settings(**values)


def app_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )
