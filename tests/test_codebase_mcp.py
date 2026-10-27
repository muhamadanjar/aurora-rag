from __future__ import annotations

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from codebase_mcp import server as mcp_server


def transport_for(handler):
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_list_projects_uses_bearer_and_returns_project_ids():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers["authorization"]
        assert request.method == "GET"
        assert request.url.path == "/codebase/projects"
        return httpx.Response(200, json={"projects": [{"project_id": "alpha"}, {"project_id": "legacy"}]})

    async with mcp_server.AuroraRagClient(
        url="https://aurora.example.test/",
        token="secret-token",
        transport=transport_for(handler),
    ) as client:
        assert await client.list_projects() == ["alpha", "legacy"]

    assert seen["authorization"] == "Bearer secret-token"


@pytest.mark.asyncio
async def test_search_sends_exact_contract_and_returns_source_results():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/codebase/search"
        assert request.headers["authorization"] == "Bearer secret-token"
        assert request.content == b'{"project_id":"alpha","query":"unit of work","limit":2}'
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "content": "class UnitOfWork: ...",
                        "path": "app/domain/unit_of_work.py",
                        "start_line": 1,
                        "end_line": 4,
                        "score": 0.91,
                    }
                ]
            },
        )

    async with mcp_server.AuroraRagClient(
        url="https://aurora.example.test",
        token="secret-token",
        transport=transport_for(handler),
    ) as client:
        assert await client.search(project_id="alpha", query="unit of work", limit=2) == [
            {
                "content": "class UnitOfWork: ...",
                "path": "app/domain/unit_of_work.py",
                "start_line": 1,
                "end_line": 4,
                "score": 0.91,
            }
        ]


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


@pytest.mark.asyncio
async def test_mcp_registers_only_read_only_codebase_tools():
    tools = await mcp_server.server.list_tools()
    assert {tool.name for tool in tools} == {"list_projects", "search_codebase"}
