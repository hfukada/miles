import asyncio
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient
from helpers import ORIGIN_LAT, ORIGIN_LNG, FakeOsrm, meadow_payload, none_sampler
from mcp.server.fastmcp import FastMCP

from miles import api, areas_mcp, db, drive_mcp, network, search_mcp

SLOW_S = 0.2


def _server() -> FastMCP:
    server = FastMCP("test")
    drive_mcp.register_drive_tools(server)
    areas_mcp.register_area_tools(server)
    search_mcp.register_search_tools(server)
    return server


@pytest.fixture
def slow_osrm(osrm: FakeOsrm) -> FakeOsrm:
    osrm.delay_s = SLOW_S
    conn = db.connect()
    network.build_network(conn, 1, [meadow_payload()], none_sampler())
    conn.close()
    return osrm


async def _longest_stall_during(call) -> float:
    """The longest gap between ticks of a task sharing the event loop with
    `call`."""
    loop = asyncio.get_running_loop()
    stamps: list[float] = [loop.time()]
    running = True

    async def ticker() -> None:
        while running:
            await asyncio.sleep(0.01)
            stamps.append(loop.time())

    task = asyncio.create_task(ticker())
    await call()
    running = False
    await task
    return max(b - a for a, b in zip(stamps, stamps[1:]))


def test_a_slow_osrm_call_does_not_block_the_event_loop(slow_osrm: FakeOsrm):
    server = _server()
    answers: list = []

    async def call():
        answers.append(await server.call_tool(
            "find_trailheads", {"area_id": 1, "lat": ORIGIN_LAT, "lng": ORIGIN_LNG, "max_drive_minutes": 10.0}
        ))

    assert asyncio.run(_longest_stall_during(call)) < SLOW_S / 2
    content = answers[0]
    starts = json.loads((content[0] if isinstance(content, tuple) else content)[0].text)
    assert starts[0]["kind"] == "origin" and len(starts) > 1


def test_an_http_request_is_served_while_an_osrm_search_waits(slow_osrm: FakeOsrm):
    """The HTTP trailhead endpoint is a plain def, which FastAPI runs on a
    worker thread, so a request made meanwhile is answered at once."""
    client = TestClient(api.app)
    url = f"/api/areas/1/trailheads?lat={ORIGIN_LAT}&lng={ORIGIN_LNG}&max_drive_minutes=10"
    done: dict[str, float] = {}
    worker = threading.Thread(target=lambda: done.update(status=client.get(url).status_code))
    worker.start()
    while not slow_osrm.calls:
        time.sleep(0.005)
    began = time.monotonic()
    assert client.get("/api/health").status_code in (200, 503)
    quick = time.monotonic() - began
    worker.join()

    assert done["status"] == 200
    assert quick < SLOW_S / 2
