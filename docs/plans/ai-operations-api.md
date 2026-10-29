# Aurora RAG AI Operations API — Plan

Progress: [Aurora RAG AI Operations API progress](../progress/ai-operations-api.md)
Dashboard integration plan: [Dashboard AI Operations API integration](../../../base-project-apps/services/dashboard/docs/plans/ai-operations-api-integration.md)

## Goal

Make Aurora RAG the complete backend for Dashboard AI Operations without reducing existing behavior: streamed RAG chat with per-session prompt and generation controls, ChatML and Alpaca pre-training datasets, example/revision validation and review, and JSONL exports.

## Contract

- Dataset routes remain native `/datasets` routes. Dataset list uses `page`/`per_page` and `data` + `meta.pagination`; examples use paginated `page`/`per_page` and include the current revision needed by the editor.
- Support `chatml` and `alpaca` dataset formats. Store both content shapes in existing JSONB revision storage, validate by the dataset format, and export their corresponding JSONL record shapes.
- Revision validation and review use the native nested route `/datasets/{dataset_id}/examples/{example_id}/revisions/{revision_id}/...`, Aurora's `If-Match` entity version, and `{approved, review_note}` review payload.
- Export status and download remain dataset-scoped: `/datasets/{dataset_id}/exports/{export_id}`.
- Chat remains authenticated at `/api/v1/chat/completions`, streaming SSE by default. Add optional `system_prompt`, `temperature`, `top_p`, and `max_tokens` fields, enforce bounds, preserve configured RAG policy/context, and pass overrides to the Ollama model.
- Keep unrelated Lahat API consumers untouched.

## Phases

1. Extend domain, persistence, migration, and schemas for Alpaca and per-request chat settings.
2. Validate and export ChatML/Alpaca; return paginated example projections with current revisions.
3. Update Dashboard AI Operations DTOs, routes, chat parsing, cache keys, and user-facing calls.
4. Update service and Dashboard documentation; verify tests, type checks, and the live Aurora destination where available.

## Acceptance

- Both formats can be created, edited, validated, reviewed, and exported without data-shape loss.
- Example list returns the current revision data and stable pagination for the Dashboard editor.
- Chat request overrides are accepted and applied; default RAG behavior and SSE completion remain unchanged.
- Dashboard AI Operations makes no requests to the Lahat base URL; unrelated Layanan Lahat integrations are unchanged.
- Automated checks and a service smoke scenario cover the changed contracts.
