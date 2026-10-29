# Aurora RAG AI Operations API — Progress

Plan: [Aurora RAG AI Operations API plan](../plans/ai-operations-api.md)

## Status: Implementation Complete — API Validated; Authenticated UI Unverified

- [x] Confirm Aurora native dataset, revision, export, and chat endpoint contracts against the running OpenAPI on port 8007.
- [x] Confirm Dashboard behaviors to preserve: ChatML/Alpaca, current-revision editing, per-session chat controls, and export polling/download.
- [x] Extend domain, API, validation, export, and migration for both dataset formats.
- [x] Extend chat request schema and model generation options while preserving default RAG behavior.
- [x] Align Dashboard requests and adapters with Aurora's native paths and DTOs; route chat and corrected-response examples to Aurora.
- [x] Update operational/API documentation.
- [x] Run targeted integration tests (`26 passed`) against an isolated disposable PostgreSQL database.
- [x] Run Dashboard TypeScript check: `pnpm exec tsc --noEmit`.
- [x] Live OpenAPI smoke confirms dataset/chat routes and Alpaca/generation schema; unauthenticated dataset read returns `401`.
- [x] Browser access attempt observed redirect to `/auth/login`; authenticated feature interactions remain unverified because no test session was available.

The API contract and backend workflows are validated. The browser could not exercise authenticated feature interactions without an authenticated test session.
