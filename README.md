# Aurora RAG

## Architecture

The codebase follows a layered Clean Architecture. Dependencies point inward; the delivery layer talks to application services, application services depend on domain ports, and infrastructure implements those ports.

```
app/core/        # framework-agnostic settings and shared errors
app/domain/      # entities, value objects, repository ports, ChatML rules
app/application/ # use cases: dataset, export, RAG chat, runtime config, auth
app/infrastructure/ # SQLAlchemy repositories/unit of work, file storage, Chroma/Ollama, security
app/delivery/    # FastAPI routers, request/response schemas, auth dependencies, middleware
app/main.py      # composition root: wires routers and exception handlers
```

Each request opens one `UnitOfWork` (`app.delivery.dependencies.get_uow`). Repositories only flush; application services decide when to commit, so multi-step mutations stay atomic. HTTP concerns such as ETags and status codes remain in the delivery layer.

## Runtime chat and configuration

`POST /api/v1/chat/completions` uses Chroma retrieval and Ollama. Send `messages` with only `user` and `assistant` roles; the last message must be from `user`. The server owns the RAG prompt and model selection. An optional request `model` must match the configured `chat.model`. `stream` defaults to `true` and returns SSE; `false` returns a Chat Completions JSON response. Chat and configuration routes require a Bearer token from `POST /auth/token`.

Request limits: at most 32 messages, 16,384 characters per message, and 100,000 characters across the transcript. System/tool roles and extra request fields are rejected.

The chat request body is capped at 1 MiB before JSON parsing, including unauthenticated requests; larger bodies return `413`.

`GET /api/v1/configuration` lists the allowlisted values; `PUT /api/v1/configuration/{key}` updates one key using `{"value": "..."}`. Values are loaded for each request. Seeded keys are `chat.model=llama3`, `embedding.model=nomic-embed-text`, `retrieval.k=4`, and `rag.prompt`. Ollama must be running and the selected chat/embedding models must be available before use. The prompt must contain exactly one plain `{context}` and one plain `{question}` field. Prompt instructions to ignore commands inside retrieved code are best-effort; they are not a secret-scanning or prompt-injection security boundary.

`embedding.model` can change only while Chroma contains no vectors. Once vectors exist, the API rejects changes; operators must rebuild the collection and re-ingest with the selected model. The `/ingest` and `/query` endpoints keep their existing request/response and authentication behavior, while reading the current runtime configuration for model, prompt, and retrieval settings.

## Codebase indexing and MCP

The codebase API indexes local project directories into project-specific Chroma collections. It is separate from `/ingest`, `/query`, and the existing `source_code` collection. The `legacy` search scope exposes that existing collection; `legacy` is reserved and cannot be indexed or deleted as a project.

Set a strong `AURORA_CODEBASE_TOKEN` and `AURORA_CODEBASE_ROOTS` before starting the API. The latter is a list of absolute allowed directory roots separated by the operating system's path separator (`:` on Linux). All `/codebase` endpoints require `Authorization: Bearer <token>` and fail closed if no token is configured. Keep the token private; bind the API to a trusted interface or use TLS and appropriate network controls.

```bash
export AURORA_CODEBASE_TOKEN='<strong-random-secret>'
export AURORA_CODEBASE_ROOTS='/absolute/path/to/projects'
uvicorn app.main:app --host 127.0.0.1
```

With Ollama running and the configured embedding model installed, index a project under an allowed root:

```bash
curl --fail-with-body -X POST 'http://127.0.0.1:8000/codebase/projects/my-project/index' \
  -H "Authorization: Bearer $AURORA_CODEBASE_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"path":"/absolute/path/to/projects/my-project"}'
```

Indexing is synchronous. A successful re-index publishes a new generation and removes stale source results; failures preserve the published generation. Source paths in project results are relative to the indexed directory, with line ranges. The scanner honors `.gitignore` and excludes symbolic links, known secret files, binary files, dependencies and build output. Large files are skipped and reported in the response. Filename exclusions do not detect secrets embedded in otherwise allowed source files; review a project before indexing it.

Files over 1 MiB are skipped. A scan fails without publishing if it exceeds 64 MiB in aggregate, 10,000 files, 50,000 chunks, 100,000 directory entries, 64 directory levels, or 60 seconds. Each `.gitignore` is limited to 1 MiB and 10,000 rules; aggregate ignore data is limited to 8 MiB and 50,000 rules. Individual ignore-pattern matches are limited to 50 ms. Embeddings are inserted in batches of 128. An index or delete already in progress for a project returns `409`; reads continue against the previously published generation during a re-index.

| Method and path | Purpose |
|---|---|
| `POST /codebase/projects/{project_id}/index` | Index or synchronize a local project. Body: `{"path":"<absolute path>"}`. |
| `DELETE /codebase/projects/{project_id}` | Remove a project's index. |
| `GET /codebase/projects` | List indexed project IDs and the `legacy` scope. |
| `POST /codebase/search` | Search one scope. Body: `{"project_id":"my-project","query":"...","limit":10}`. |

Configure a coding agent to launch the MCP server over stdio. Substitute the absolute Aurora RAG directory and the same bearer token; never commit a real token to project configuration:

```json
{
  "mcpServers": {
    "aurora-rag": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/aurora-rag", "run", "python", "-m", "codebase_mcp.server"],
      "env": {
        "AURORA_RAG_URL": "http://127.0.0.1:8000",
        "AURORA_CODEBASE_TOKEN": "<same-token-as-api>"
      }
    }
  }
}
```

The MCP tools are `list_projects` and `search_codebase`. Search requires an explicit project ID or `legacy` and returns source snippets rather than generated answers. The MCP server does not index or delete projects. Project indexing and retrieval use the runtime `embedding.model`; changing that model is blocked while indexed vectors exist.

## Dataset management

Dataset management remains a separate authenticated PostgreSQL-backed feature. It does not create chat sessions or training examples from chat traffic and does not automate Ollama training.

## Configuration

Copy `.env.example` to `.env` and set every required value: `DATABASE_URL`, `AUTH_USERNAME`, a bcrypt `AUTH_PASSWORD_HASH`, a random `JWT_SECRET` of at least 32 characters, and `OPERATOR_UUID`. There are no database, operator, or JWT defaults. `OPERATOR_UUID` is the single configured audit actor. JWTs expire after 30 minutes. `EXPORT_ROOT` is the only filesystem root used for export files; ready files expire after `EXPORT_RETENTION_DAYS` (default 30). Keep `.env` private.

Generate the bcrypt hash interactively so no password is stored in shell history:

```bash
python -c 'import bcrypt, getpass; print(bcrypt.hashpw(getpass.getpass("Password: ").encode(), bcrypt.gensalt()).decode())'
```

## Migration and run

```bash
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --reload
```

Create migrations with the Alembic hook; new revisions are numbered sequentially (`0003`, `0004`, ...):

```bash
alembic revision --autogenerate -m "describe change"
```

Migration files use the `NNNN_slug.py` format. Do not manually use timestamp-based revision IDs.

Install development-only test dependencies with `pip install -r requirements-dev.txt`.

## Dataset API

FastAPI publishes request/response schemas at `/docs`. All `/datasets` routes require a Bearer token from `POST /auth/token`; that endpoint accepts OAuth2 form fields `username` and `password`.

| Method and path | Purpose |
|---|---|
| `GET /datasets?page=1&per_page=20`, `POST /datasets` | List or create datasets. |
| `GET /datasets/{dataset_id}`, `PATCH /datasets/{dataset_id}`, `POST /datasets/{dataset_id}/archive` | Read, update, or archive a dataset. |
| `GET`/`POST /datasets/{dataset_id}/examples` | List examples or create an example with its first revision. |
| `POST /datasets/{dataset_id}/examples/{example_id}/revisions` | Create a new current revision. |
| `POST /datasets/{dataset_id}/examples/{example_id}/revisions/{revision_id}/validate` | Validate ChatML message structure. |
| `POST /datasets/{dataset_id}/examples/{example_id}/revisions/{revision_id}/review` | Approve or reject a valid revision. |
| `POST /datasets/{dataset_id}/examples/{example_id}/retire` | Retire an example. |
| `POST /datasets/{dataset_id}/exports`, `GET /datasets/{dataset_id}/exports/{export_id}` | Create or inspect an export. |
| `GET /datasets/{dataset_id}/exports/{export_id}/download` | Download a ready, unexpired JSONL file. |

Mutations use `If-Match` with the resource ETag returned by the preceding response. A review response also returns the updated example version in `Example-ETag` because approving a different current revision changes that resource. Missing preconditions return `428`; stale ETags return `412`. Invalid ChatML returns `422`; review before successful validation returns `409`.

Test against a dedicated, disposable PostgreSQL database: the API test fixture downgrades it to `base` and upgrades it before each test.

```bash
TEST_DATABASE_URL='postgresql+asyncpg://user:password@localhost/aurora_rag_test' pytest -q
```

Dataset records use `title` and `format_type=chatml`; each revision carries `schema_version`, `revision_number`, and its own optimistic `version`, while each example carries its own `version`. `system_prompt` and `review_note` belong to revisions. Export manifests carry `export_schema_version`, item count, revision IDs, and SHA-256. Only the current revision of an active example that is valid and approved is exported.

Failed token attempts are throttled in-process after five failures in one minute for the default single-worker local deployment. Multi-worker deployments and proxy-level distributed throttling require an external shared rate limiter and trusted client-IP configuration.

The periodic cleanup marks expired exports as `expired`, removes files only beneath `EXPORT_ROOT`, and retains export metadata while the database schema remains available. A filesystem sweep independently removes UUID-named JSONL files after `EXPORT_RETENTION_DAYS`; stale temporary files are removed after 24 hours when no active export holds its database row lock, or when the export table is unavailable. Export roots are forced to mode `0700`, temporary files to `0600`.

Export metadata uses `requested_by`, relative `object_key`, `checksum_sha256`, `item_count`, `error`, `created_at`, `completed_at`, `expires_at`, and `expired_at`. `expires_at` is the explicit 30-day operational cleanup deadline; download resolves `object_key` only beneath `EXPORT_ROOT`. The manifest retains all exported revision IDs, so its memory usage grows with dataset size.
