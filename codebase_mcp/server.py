"""Aurora RAG codebase MCP server.

The server is a thin, read-only adapter around the authenticated Aurora
codebase API. It has no filesystem, database, or backend-app access.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, TypedDict

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError


class CodebaseSearchResult(TypedDict):
    content: str
    path: str
    start_line: int | None
    end_line: int | None
    score: float


class AuroraConfigurationError(Exception):
    """The MCP server is missing required local configuration."""


class AuroraResponseError(Exception):
    """Aurora returned an unusable or unsuccessful response."""


class AuroraRagClient:
    """Small authenticated client for the read-only Aurora codebase endpoints."""

    def __init__(
        self,
        *,
        url: str,
        token: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not url.strip():
            raise AuroraConfigurationError("AURORA_RAG_URL is required")
        if not token.strip():
            raise AuroraConfigurationError("AURORA_CODEBASE_TOKEN is required")

        self._client = httpx.AsyncClient(
            base_url=url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(20.0, connect=5.0),
            transport=transport,
        )

    @classmethod
    def from_environment(cls) -> AuroraRagClient:
        return cls(
            url=os.environ.get("AURORA_RAG_URL", ""),
            token=os.environ.get("AURORA_CODEBASE_TOKEN", ""),
        )

    async def __aenter__(self) -> AuroraRagClient:
        await self._client.__aenter__()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self._client.__aexit__(exc_type, exc, traceback)

    async def _json_request(self, method: str, path: str, **kwargs: Any) -> Mapping[str, Any]:
        try:
            response = await self._client.request(method, path, **kwargs)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise AuroraResponseError(f"Aurora API returned HTTP {exc.response.status_code}") from None
        except httpx.RequestError:
            raise AuroraResponseError("Aurora API request failed") from None
        except ValueError:
            raise AuroraResponseError("Aurora API returned invalid JSON") from None

        if not isinstance(payload, Mapping):
            raise AuroraResponseError("Aurora API returned an invalid response")
        return payload

    async def list_projects(self) -> list[str]:
        payload = await self._json_request("GET", "/codebase/projects")
        projects = payload.get("projects")
        if not isinstance(projects, list):
            raise AuroraResponseError("Aurora API returned invalid projects")

        project_ids: list[str] = []
        for project in projects:
            if not isinstance(project, Mapping) or not isinstance(project.get("project_id"), str):
                raise AuroraResponseError("Aurora API returned invalid project data")
            project_ids.append(project["project_id"])
        return project_ids

    async def search(self, *, project_id: str, query: str, limit: int) -> list[CodebaseSearchResult]:
        payload = await self._json_request(
            "POST",
            "/codebase/search",
            json={"project_id": project_id, "query": query, "limit": limit},
        )
        results = payload.get("results")
        if not isinstance(results, list):
            raise AuroraResponseError("Aurora API returned invalid search results")

        normalized: list[CodebaseSearchResult] = []
        for result in results:
            if not isinstance(result, Mapping):
                raise AuroraResponseError("Aurora API returned invalid search result data")
            if not isinstance(result.get("content"), str) or not isinstance(result.get("path"), str):
                raise AuroraResponseError("Aurora API returned invalid source result")
            score = result.get("score")
            if not isinstance(score, int | float) or isinstance(score, bool):
                raise AuroraResponseError("Aurora API returned invalid result score")

            start_line = result.get("start_line")
            end_line = result.get("end_line")
            if start_line is not None and not isinstance(start_line, int):
                raise AuroraResponseError("Aurora API returned invalid start line")
            if end_line is not None and not isinstance(end_line, int):
                raise AuroraResponseError("Aurora API returned invalid end line")

            normalized.append(
                {
                    "content": result["content"],
                    "path": result["path"],
                    "start_line": start_line,
                    "end_line": end_line,
                    "score": float(score),
                }
            )
        return normalized


def _tool_error(exc: Exception) -> ToolError:
    if isinstance(exc, (AuroraConfigurationError, AuroraResponseError)):
        return ToolError(str(exc))
    return ToolError("Aurora codebase request failed")


async def _with_client(operation: Any) -> Any:
    try:
        async with AuroraRagClient.from_environment() as client:
            return await operation(client)
    except (AuroraConfigurationError, AuroraResponseError) as exc:
        raise _tool_error(exc) from None


server = MCPServer(
    "aurora-rag-codebase",
    description="Read-only semantic access to explicitly selected Aurora RAG codebases.",
    instructions=(
        "Use list_projects before search_codebase when the project identifier is unknown. "
        "Always provide an explicit project_id, or the literal legacy project_id 'legacy'."
    ),
)


@server.tool(
    name="list_projects",
    title="List Aurora codebase projects",
    description="List project identifiers available through the Aurora RAG codebase API.",
)
async def list_projects() -> list[str]:
    return await _with_client(lambda client: client.list_projects())


@server.tool(
    name="search_codebase",
    title="Search an Aurora codebase",
    description=(
        "Semantically search one explicitly selected Aurora codebase project or the legacy project. "
        "Returns source results directly, including content, path, line range, and score."
    ),
)
async def search_codebase(project_id: str, query: str, limit: int = 10) -> list[CodebaseSearchResult]:
    if not isinstance(project_id, str) or not project_id.strip():
        raise ToolError("project_id must be an explicitly selected project identifier or 'legacy'")
    if not isinstance(query, str) or not query.strip():
        raise ToolError("query must not be empty")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ToolError("limit must be an integer between 1 and 100")

    return await _with_client(lambda client: client.search(project_id=project_id, query=query, limit=limit))


def main() -> None:
    """Run the server over standard MCP stdio transport."""

    server.run(transport="stdio")


if __name__ == "__main__":
    main()
