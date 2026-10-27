from __future__ import annotations

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from codebase_mcp import server as mcp_server


def transport_for(handler):
    return httpx.MockTransport(handler)


def test_missing_configuration_does_not_construct_authenticated_client(monkeypatch):
    monkeypatch.delenv("AURORA_RAG_URL", raising=False)
    monkeypatch.delenv("AURORA_CODEBASE_TOKEN", raising=False)

    with pytest.raises(mcp_server.AuroraConfigurationError, match="AURORA_RAG_URL"):
        mcp_server.AuroraRagClient.from_environment()


@pytest.mark.asyncio
async def test_search_tool_requires_explicit_project_and_nonempty_query():
    with pytest.raises(ToolError, match="project_id"):
        await mcp_server.search_codebase("", "query")
    with pytest.raises(ToolError, match="query"):
        await mcp_server.search_codebase("legacy", " ")


@pytest.mark.asyncio
async def test_upstream_failure_is_client_visible_without_token_leak():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "token secret-token is invalid"})

    async with mcp_server.AuroraRagClient(
        url="https://aurora.example.test",
        token="secret-token",
        transport=transport_for(handler),
    ) as client:
        with pytest.raises(mcp_server.AuroraResponseError, match="HTTP 401") as error:
            await client.list_projects()

    assert "secret-token" not in str(error.value)
