# KnowledgeDock — Development Roadmap

Single source of truth for build progress. Update `[ ]` → `[x]` as each item is
completed and verified. Never mark an item complete without tests/verification.

---

## Progress

```text
Phase 01 Foundation          [ ]  0/6
Phase 02 Authentication      [ ]  0/6
Phase 03 Workspaces          [ ]  0/5
Phase 04 Documents           [ ]  0/6
Phase 05 Ingestion           [ ]  0/7
Phase 06 Retrieval           [ ]  0/6
Phase 07 RAG                 [ ]  0/7
Phase 08 Reliability         [ ]  0/8
Phase 09 Frontend            [ ]  0/6
Phase 10 Production Readiness [ ]  0/8

Overall: [████░░░░░░░░░░░░░░░░░░░░░░░░░░] 0% (0/65)
```

Legend: `[ ]` not started · `[x]` complete

---

## Branch Convention

Every phase gets its own branch. Branch name = `<phase>-<core-functionality>`.

```text
phase-01-project-structure
phase-01-database-connection
phase-02-user-registration
phase-02-jwt-authentication
phase-03-workspace-creation
phase-04-document-upload
phase-05-text-chunking
phase-06-vector-search
phase-07-rag-pipeline
phase-08-rate-limiting
phase-09-dashboard
phase-10-integration-tests
```

- Create the branch before touching functionality for a phase.
- Merge into `main` manually (checkout main → merge). No PRs.
- Never delete a sub branch.

---

## Phase 01 — Foundation

Branch: `phase-01-foundation`

Scope: project structure, configuration, FastAPI app, MongoDB connection, health
endpoint, Docker. See `README.md` §18 Phase 1 and `SKILL.md` §27 items 1–5.

- [ ] `src/` layout: `api/`, `core/`, `domain/`, `application/`, `infrastructure/`, `workers/`, `templates/`, `static/`
- [ ] Typed settings module loading env vars via pydantic-settings (no hard-coded secrets)
- [ ] FastAPI application factory + lifespan handler
- [ ] MongoDB async client + connection lifecycle (ping on startup)
- [ ] `GET /health` (liveness) and `GET /health/ready` (MongoDB readiness)
- [ ] Dockerfile + docker-compose (api + mongo) + `.env.sample` wired

Notes:

---

## Phase 02 — Authentication

Branch: `phase-02-authentication`

Scope: user model, registration, login, authentication, authorization.

- [ ] `users` collection model + unique index on `email`
- [ ] `POST /auth/register` — validation, duplicate detection, password hashing
- [ ] `POST /auth/login` — credential verification, controlled failure responses
- [ ] JWT access token issue + `auth` dependency for protected routes
- [ ] Current-user resolution dependency (`get_current_user`)
- [ ] Password reset flow (request token → set new password)

Notes:

---

## Phase 03 — Workspaces

Branch: `phase-03-workspaces`

Scope: workspace creation, membership/access, workspace isolation.

- [ ] `workspaces` collection model + `owner_id` index
- [ ] `POST /workspaces` — create workspace for authenticated user
- [ ] `GET /workspaces` / `GET /workspaces/{id}` — access-checked reads
- [ ] `workspace_members` collection + role model (owner/member)
- [ ] Membership endpoints (invite/add, list, remove)
- [ ] Workspace access dependency enforcing isolation on every protected op

Notes:

---

## Phase 04 — Documents

Branch: `phase-04-documents`

Scope: upload, validation, metadata, processing status.

- [ ] `documents` collection model with explicit status enum (PENDING/PROCESSING/READY/FAILED)
- [ ] Local file storage adapter (temp dir, later S3-compatible) + content hashing
- [ ] `POST /documents` upload — content-type allowlist, size limit, workspace check, 202 response
- [ ] Explicit state-transition guard (client cannot set status)
- [ ] `GET /documents` — paginated, workspace-filtered list
- [ ] `GET /documents/{id}` — status, `processing_error`, chunk count
- [ ] `DELETE /documents/{id}` — soft delete + chunk cleanup

Notes:

---

## Phase 05 — Ingestion

Branch: `phase-05-ingestion`

Scope: text extraction, normalization, chunking, embedding generation, vector storage.

- [ ] Background worker (FastAPI BackgroundTasks first; swap for durable queue only when justified)
- [ ] Text extractor per file type: `.txt`, `.md`, `.pdf`, `.html`, `.docx`
- [ ] Normalizer — strip nulls, collapse whitespace, drop control chars, detect empty extraction
- [ ] Chunker — configurable size/overlap, keeps `workspace_id`/`document_id`/`chunk_index`
- [ ] `EmbeddingProvider` interface (Protocol/ABC) + OpenAI implementation
- [ ] Batch embedding generation with retry/backoff and token accounting
- [ ] `document_chunks` collection + Atlas Vector Search index with `workspace_id` filter preserved; persist chunks and mark document READY

Notes:

---

## Phase 06 — Retrieval

Branch: `phase-06-retrieval`

Scope: semantic search, top-K retrieval, similarity threshold, context construction.

- [ ] `RetrievalService` — query embedding → vector search
- [ ] Top-K retrieval with configurable `K`
- [ ] Similarity threshold enforcement + no-answer signal
- [ ] Workspace filter applied at the query level (never post-filtered)
- [ ] Context builder — token/char budget, ordering, de-duplication
- [ ] `POST /search` raw semantic search endpoint (no LLM) for tuning/debugging

Notes:

---

## Phase 07 — RAG

Branch: `phase-07-rag`

Scope: prompt construction, LLM integration, grounded responses, source citations, no-answer fallback.

- [ ] `LLMProvider` interface: `generate_answer(question, context, history)`
- [ ] Grounded prompt builder — strict separation of system instructions / question / untrusted context
- [ ] `POST /query` — retrieve → build context → generate → answer + sources
- [ ] No-answer fallback path when retrieval is below threshold
- [ ] Source citations (`document_id`, `document_name`, `chunk_index`, score)
- [ ] `conversations` + `messages` collections, history-aware follow-up queries
- [ ] Prompt-injection hardening — retrieved text treated as data, never instructions

Notes:

---

## Phase 08 — Reliability

Branch: `phase-08-reliability`

Scope: timeouts, retries, rate limiting, error handling, usage tracking, logging.

- [ ] `ai_usage` collection + usage recording per request (provider, model, tokens, duration)
- [ ] Structured JSON logging with `request_id` correlation
- [ ] Request ID middleware + access log (method, path, status, duration)
- [ ] Global exception handlers — no stack traces in responses
- [ ] Typed error taxonomy (validation/auth/authz/not-found/conflict/provider/internal)
- [ ] Bounded retry with exponential backoff + jitter for provider calls
- [ ] Timeout configuration for all outbound AI calls
- [ ] Rate limiting per user/workspace (Redis only if in-process limiting proves insufficient)

Notes:

---

## Phase 09 — Frontend

Branch: `phase-09-frontend`

Scope: dashboard, document management, upload, processing status, knowledge query interface.

- [ ] Base layout + Bootstrap + HTMX wiring, static asset serving
- [ ] Login / register screens
- [ ] Dashboard with workspace switcher
- [ ] Upload screen (drag-drop + HTMX upload + progress)
- [ ] Document list + live processing status polling (PENDING → PROCESSING → READY/FAILED)
- [ ] Ask Knowledge Base UI — answer rendering with source citations
- [ ] Conversation history view

Notes:

---

## Phase 10 — Production Readiness

Branch: `phase-10-production-readiness`

Scope: tests, Docker, environment configuration, security review, observability, documentation.

- [ ] Auth tests (register, duplicate, login, invalid credentials)
- [ ] Authorization tests (cross-workspace document/workspace access denied)
- [ ] Document lifecycle tests (validation, happy path, failure path, idempotent reprocessing)
- [ ] Retrieval + RAG tests (workspace filter, threshold, no-answer, provider failure)
- [ ] Reliability tests (timeout, retry bounds, controlled provider errors)
- [ ] End-to-end test of the full flow in `SKILL.md` §40
- [ ] Production Dockerfile (multi-stage, non-root) + compose healthchecks
- [ ] Security review — secrets, file limits, upload validation, error leakage
- [ ] Observability — metrics/log fields, usage reporting endpoint `GET /usage`
- [ ] README + `SKILL.md`/roadmap updates documenting final architecture and decisions

Notes:

---

## Definition of Done (per item)

```text
Business behavior works
    +
Authorization + isolation enforced
    +
Errors handled predictably
    +
Tests cover the behavior
    +
Logs are useful
    +
Roadmap updated
```

---

## Decision Log

| # | Phase | Decision | Reason |
|---|-------|----------|--------|
| 1 | | | |