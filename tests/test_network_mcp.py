import pytest
from helpers import call_tool
from mcp.server.fastmcp import FastMCP

from miles import network_mcp


def _set_route_preferences(patch: str) -> dict:
    server = FastMCP("test")
    network_mcp.register_network_tools(server)
    return call_tool(server, "set_route_preferences", {"patch": patch})


@pytest.mark.parametrize("patch", ["5", '"text"', "[1, 2]", "null"])
def test_a_patch_that_is_not_an_object_is_an_error_not_a_crash(patch: str):
    assert "JSON object" in _set_route_preferences(patch)["error"]


def test_a_valid_patch_applies():
    assert _set_route_preferences('{"max_continuous_uncomfortable_m": 1000}')["max_continuous_uncomfortable_m"] == 1000
