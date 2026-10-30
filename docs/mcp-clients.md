# Connect coding agents to Aurora RAG MCP

This guide connects Codex, OMP, Pi, or Claude Code to Aurora's local codebase MCP server. The MCP server runs as a local **stdio** process and calls Aurora's HTTP API; it is not an HTTP MCP endpoint on port 8007.

The server exposes two read-only tools:

- `list_projects`: list indexed codebase project IDs.
- `search_codebase`: search indexed code and return source snippets.

It does not index or delete projects. Index a project through Aurora's API before searching it.

## Prerequisites

1. Run Aurora and publish its API on the host at `127.0.0.1:8007` (the default Docker Compose mapping in this project).
2. Index at least one codebase project. The MCP `list_projects` tool should return its ID.
3. Install `uv` and Python 3.12.13 or later on the machine that runs the coding agent.
4. From the Aurora repository, install the development dependencies; the MCP SDK is in the `dev` dependency group:

   ```bash
   cd /absolute/path/to/aurora-rag
   uv sync --frozen
   ```

Use the **absolute path** to this repository in the agent configurations below.

## Provide the MCP server credentials

The MCP process reads these environment variables:

| Variable | Value |
|---|---|
| `AURORA_RAG_URL` | Aurora API base URL, for this host `http://127.0.0.1:8007` (no `/codebase` suffix). |
| `AURORA_CODEBASE_TOKEN` | The codebase bearer token configured for Aurora, matching `AURORA_CODEBASE_TOKEN` in `.env.docker`. |

This is not the JWT secret, the RAG token, or a dashboard login token. Do not put the token in a committed project config or prompt.

The examples use environment-variable references so the token stays out of configuration files. In a shell where you will launch the agent, set the URL and enter the token without echoing it or putting it in shell history:

```bash
export AURORA_RAG_URL=http://127.0.0.1:8007
read -rsp 'AURORA_CODEBASE_TOKEN: ' AURORA_CODEBASE_TOKEN
printf '\n'
export AURORA_CODEBASE_TOKEN
```

Paste the value of `AURORA_CODEBASE_TOKEN` from your private `.env.docker` when prompted. The variable remains available only in that shell session. Start the coding agent from that same shell. For GUI-launched agents, use the agent's user-level secret/environment mechanism instead; a GUI started independently may not inherit terminal environment variables.

## Codex

Codex CLI, the IDE extension, and the Codex desktop app share `~/.codex/config.toml`. Add this entry there, replacing `/absolute/path/to/aurora-rag`:

```toml
[mcp_servers.aurora-rag]
command = "uv"
args = ["--directory", "/absolute/path/to/aurora-rag", "run", "--frozen", "python", "-m", "codebase_mcp.server"]
env = { AURORA_RAG_URL = "http://127.0.0.1:8007" }
env_vars = ["AURORA_CODEBASE_TOKEN"]
```

`env_vars` forwards the token from the local Codex process to this stdio server. Launch Codex from the shell where the token was set. Check the connection with `codex mcp list` or `/mcp` inside the Codex TUI.

Official instructions: [Codex MCP](https://developers.openai.com/codex/mcp).

## OMP

Add a user-level server entry to `~/.omp/agent/mcp.json` (create the parent directory if needed). Replace the repository path:

```json
{
  "mcpServers": {
    "aurora-rag": {
      "command": "uv",
      "args": [
        "--directory", "/absolute/path/to/aurora-rag",
        "run", "--frozen", "python", "-m", "codebase_mcp.server"
      ],
      "env": {
        "AURORA_RAG_URL": "http://127.0.0.1:8007",
        "AURORA_CODEBASE_TOKEN": "${AURORA_CODEBASE_TOKEN}"
      }
    }
  }
}
```

Launch OMP from the shell where the token was set. Use `/mcp reload` to reload configuration, `/mcp list` to inspect servers, and `/mcp test aurora-rag` to test this server.

Official instructions: [OMP MCP](https://omp.sh/docs/mcp).

## Pi

Pi has built-in MCP support. Add a user-level server from the shell where the token is set; replace the repository path:

```bash
pi mcp add aurora-rag \
  --env AURORA_RAG_URL=http://127.0.0.1:8007 \
  --env 'AURORA_CODEBASE_TOKEN=${AURORA_CODEBASE_TOKEN}' \
  -- uv --directory /absolute/path/to/aurora-rag run --frozen python -m codebase_mcp.server
```

The command writes to Pi's user MCP configuration. Verify with `pi mcp list`; use `/reload` in Pi after changing configuration.

Official instructions: [Pi MCP](https://pi.dev/docs/latest/mcp).

## Claude Code

Create or update `.mcp.json` in the Aurora repository root. Replace the repository path:

```json
{
  "mcpServers": {
    "aurora-rag": {
      "type": "stdio",
      "command": "uv",
      "args": [
        "--directory", "/absolute/path/to/aurora-rag",
        "run", "--frozen", "python", "-m", "codebase_mcp.server"
      ],
      "env": {
        "AURORA_RAG_URL": "http://127.0.0.1:8007",
        "AURORA_CODEBASE_TOKEN": "${AURORA_CODEBASE_TOKEN}"
      }
    }
  }
}
```

Claude Code expands the environment variable when loading `.mcp.json`. Start Claude Code from the shell where the token was set and approve the project MCP server when prompted. Check with `claude mcp list` or `/mcp`.

Official instructions: [Claude Code MCP](https://code.claude.com/docs/en/mcp).

## Verify the connection

1. In the agent's MCP server list, confirm `aurora-rag` is connected.
2. Call `list_projects`; confirm the indexed project ID appears.
3. Call `search_codebase` with that exact ID and a code-related query.

The MCP server uses `AURORA_CODEBASE_TOKEN` as a bearer token against `/codebase/projects` and `/codebase/search`. It does not call an MCP endpoint on port 8007.

## Troubleshooting

- **`AURORA_RAG_URL is required` or `AURORA_CODEBASE_TOKEN is required`:** the MCP process did not receive the corresponding variable. Check the agent config and launch environment.
- **HTTP 401:** check that `AURORA_CODEBASE_TOKEN` matches the codebase token in Aurora's `.env.docker`. Do not substitute `JWT_SECRET`, the RAG token, or dashboard credentials.
- **Connection refused / API request failed:** confirm Aurora is running and that `http://127.0.0.1:8007/openapi.json` is reachable from the machine running the MCP process.
- **No projects returned:** index the project first, then call `list_projects` again.
- **Agent runs in a container or remote environment:** `127.0.0.1` refers to that environment, not the host. Configure `AURORA_RAG_URL` to an address reachable from the agent environment; do not expose the API publicly just to make MCP connect.

## Official client references

- [Codex MCP](https://developers.openai.com/codex/mcp)
- [OMP MCP](https://omp.sh/docs/mcp)
- [Pi MCP](https://pi.dev/docs/latest/mcp)
- [Claude Code MCP](https://code.claude.com/docs/en/mcp)
