import json
from pathlib import Path

import pytest
from aiohttp import web

from syncfield.adapters.insta360_go3s.wifi.osc_client import (
    OscDownloadError,
    OscHttpClient,
)


@pytest.fixture
async def osc_server(aiohttp_server):
    """Fake OSC HTTP server that mimics the Go3S endpoints we hit."""

    async def info(request):
        return web.json_response(
            {"manufacturer": "Insta360", "model": "Go 3S", "firmwareVersion": "8.0.4.11"}
        )

    async def execute(request):
        body = await request.json()
        if body["name"] == "camera.listFiles":
            return web.json_response(
                {
                    "results": {
                        "entries": [
                            {
                                "name": "VID_FAKE.mp4",
                                "fileUrl": "/DCIM/Camera01/VID_FAKE.mp4",
                                "size": 12,
                            }
                        ]
                    },
                    "state": "done",
                }
            )
        return web.json_response({"state": "error"}, status=400)

    async def get_file(request):
        return web.Response(body=b"hello world!", headers={"Content-Length": "12"})

    app = web.Application()
    app.router.add_get("/osc/info", info)
    app.router.add_post("/osc/commands/execute", execute)
    app.router.add_get("/DCIM/Camera01/VID_FAKE.mp4", get_file)
    return await aiohttp_server(app)


@pytest.mark.asyncio
async def test_probe_returns_camera_model(osc_server):
    client = OscHttpClient(host=f"127.0.0.1:{osc_server.port}", scheme="http")
    info = await client.probe(timeout=2.0)
    assert info.model == "Go 3S"


@pytest.mark.asyncio
async def test_list_files_returns_entries(osc_server):
    client = OscHttpClient(host=f"127.0.0.1:{osc_server.port}", scheme="http")
    files = await client.list_files()
    assert len(files) == 1
    assert files[0].name == "VID_FAKE.mp4"
    assert files[0].size == 12


@pytest.mark.asyncio
async def test_download_writes_atomic_file(osc_server, tmp_path):
    client = OscHttpClient(host=f"127.0.0.1:{osc_server.port}", scheme="http")
    target = tmp_path / "overhead.mp4"
    progress_calls: list[tuple[int, int]] = []

    await client.download(
        remote_path="/DCIM/Camera01/VID_FAKE.mp4",
        local_path=target,
        expected_size=12,
        on_progress=lambda done, total: progress_calls.append((done, total)),
    )

    assert target.exists()
    assert target.read_bytes() == b"hello world!"
    assert not (tmp_path / "overhead.mp4.part").exists()
    assert progress_calls[-1] == (12, 12)


@pytest.mark.asyncio
async def test_download_size_mismatch_raises_and_cleans_up(osc_server, tmp_path):
    client = OscHttpClient(host=f"127.0.0.1:{osc_server.port}", scheme="http")
    target = tmp_path / "overhead.mp4"
    with pytest.raises(OscDownloadError):
        await client.download(
            remote_path="/DCIM/Camera01/VID_FAKE.mp4",
            local_path=target,
            expected_size=99999,  # wrong size triggers atomic failure
        )
    assert not target.exists()
    assert not (tmp_path / "overhead.mp4.part").exists()
