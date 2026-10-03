# KnowledgeDock — Development Roadmap

Single source of truth for build progress. Update `[ ]` → `[x]` as each item is
completed and verified. Never mark an item complete without tests/verification.

Deployment setup (Atlas / Gemini / Render): `.agents/DEPLOYMENT.md`

---

## Progress

```text
Phase 01 Foundation          [x]  8/8
Phase 02 Authentication      [x]  6/6
Phase 03 Workspaces          [x]  6/6
Phase 04 Documents           [ ]  0/7
Phase 05 Ingestion           [ ]  0/7
Phase 06 Retrieval           [ ]  0/6
Phase 07 RAG                 [ ]  0/7
Phase 08 Reliability         [ ]  0/8
Phase 09 Frontend            [ ]  0/6
Phase 10 Production Readiness [ ]  0/9

Overall: [████░░░░░░░░░░░░░░░░░░░░░░░░░░░░] 30% (20/67)
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

Branch: `phase-01-health-endpoint`

Scope: project structure, configuration, FastAPI app, MongoDB connection, health
endpoint, Docker. See `README.md` §18 Phase 1 and `SKILL.md` §27 items 1–5.

- [x] `src/knowledgedock/` layout: `api/`, `core/`, `domain/`, `application/`, `infrastructure/`, `workers/`, `templates/`, `static/`
- [x] Typed settings module loading env vars via `python-decouple` (no hard-coded secrets)
- [x] FastAPI application factory + lifespan handler
- [x] Jinja2 `StaticFiles` + `Jinja2Templates` wired (Bootstrap + HTMX via CDN)
- [x] MongoDB async client + connection lifecycle (ping on startup) against Atlas
- [x] `GET /health` (liveness) and `GET /health/ready` (MongoDB readiness)
- [x] Multi-stage Dockerfile with uv layer caching + `.dockerignore` + `.env.sample` wired

### Verification evidence

**Local** — 38 tests pass, `ruff check` and `ruff format --check` clean.
`/health`, `/healthz`, `/health/ready`, `/readyz` respond correctly,
`/static/app.css` 200, JSON access logs carry `request_id`.

**Production** (`https://knowledgedock.onrender.com`, Render free tier,
Atlas M0 in Oregon):

```text
GET /              200   Bootstrap shell, CDN CSS + HTMX only
GET /health        200   {"status":"ok"}
GET /healthz       200   {"status":"ok"}
GET /readyz        200   {"status":"ready","database":"up"}
GET /health/ready  200   {"status":"ready","database":"up"}
GET /api/docs      404   confirms ENVIRONMENT=production is in effect
```

`/readyz` returning `database: up` is the load-bearing check: it proves Atlas
credentials, TLS, SRV resolution and Network Access all work from inside the
container. `/api/docs` returning 404 proves the production flag landed, so the
OpenAPI schema is not published on a public unauthenticated URL.

**Secrets** — `.env` is untracked. `SECRET_KEY`, `GEMINI_API_KEY` and the Atlas
password were searched across all 185 objects in git history and appear in none.

`domain/`, `application/` and `workers/` are still empty — Phases 2, 3/4 and 5
create them.

- [x] Deployed to Render free tier, image builds, container binds `$PORT`
- [x] **Production database verified** — `/readyz` returns
      `{"status":"ready","database":"up"}` from inside the container

### Phase 01 constraints (fixed by deployment target)

| Constraint | Consequence |
|---|---|
| Render free web service | Single instance, 512 MB RAM, no separate worker, no Redis, ephemeral disk, spins down after 15 min idle (~1 min cold start) |
| Render injects `PORT` (default 10000) | App must bind `0.0.0.0:$PORT`; never hard-code a port |
| Render terminates TLS and proxies | `--proxy-headers` on; trusted forwarded IPs from the platform |
| Render has no static egress IP | Atlas Network Access must allow `0.0.0.0/0`; app-level auth + rate limiting are the real defence |
| MongoDB Atlas M0 | 100 ops/sec; max **3** Atlas Search/Vector indexes total → use exactly **one** vector index with `workspace_id` as a `filter` field, not a separate index. TLS always on. |
| Gemini free tier | Flash-class models only (Pro is paid). KnowledgeDock uses `gemini-3.8-flash` for chat and `gemini-embedding-001`. Never enable billing. Live quota lives in AI Studio → Dashboard → Usage. |
| `gemini-embedding-001` | 128–3072 dims, **recommended 768**; 2048 input-token limit per chunk → `GEMINI_EMBEDDING_MAX_INPUT_TOKENS=1800`. Non-3072 dims are not pre-normalised, but Atlas `cosine` normalises internally. |
| Gemini task types | Documents embed with `RETRIEVAL_DOCUMENT`, queries with `RETRIEVAL_QUERY`. Using the same task type for both degrades retrieval quality. |
| Ephemeral filesystem | Raw uploads live in `/tmp` and are disposable; chunks + vectors in MongoDB are the source of truth |
| Same-origin UI only | No CORS config, no external frontend framework, no CDN-hosted app code — only Bootstrap/HTMX CSS+JS from CDN |

Full setup walkthrough: `.agents/DEPLOYMENT.md`

Notes:

---

## Phase 02 — Authentication

Branch: `phase-02-authentication`

Scope: upload, validation, metadata, processing status. See README §18 Phase 4 and SKILL.md §7–§9.

- [x] `users` collection model + unique index on `email`
- [x] `POST /auth/register` — validation, duplicate detection, password hashing
- [x] `POST /auth/login` — credential verification, controlled failure responses
- [x] JWT access token issue + `auth` dependency for protected routes
- [x] Current-user resolution dependency (`get_current_user`)
- [x] Password reset flow (request token → set new password)

### Layering

```text
api/auth.py                 routes only: take input, call one use case, shape output
api/dependencies.py         cookie handling + get_current_user
application/auth/           business rules: policies (email, password), use cases
domain/users.py             User + PasswordResetToken entities, session_version
domain/errors.py            typed errors -> HTTP status + stable code
infrastructure/             argon2id, PyJWT, MongoUserRepository (+ in-memory double)
```

### Behaviour that was designed, not defaulted

| Rule | Why it exists |
|---|---|
| Login returns identical status, body and work for unknown address and wrong password | Otherwise the endpoint enumerates accounts. Verified by test, including a timing comparison. |
| Argon2id cost pinned in code (19 MiB), not in `.env` | Meaningful only relative to hardware. Render free tier has 512 MB; the 64 MiB default would be a quarter of the container for one login. |
| Password length capped at 128 | An unbounded input is free CPU denial of service against the hasher, from an unauthenticated caller. |
| `session_version` column, bumped on every password change | Makes stateless JWTs revocable. A bare token cannot express "log this user out everywhere". |
| Reset tokens stored as SHA-256 digests | Read access to the database must not yield a usable reset link. |
| Reset token consumed with `find_one_and_delete` | Single use even if two requests race. |
| TTL index on `expires_at` | MongoDB purges spent tokens. No sweeper job. |
| Response to a reset request is byte-identical for known and unknown addresses | Returning the token for a known address made it an oracle. Caught by a test during this phase. |
| Unique index is the authority for duplicate email; the pre-check is an optimisation | Two simultaneous registrations both pass a pre-check. |
| Cookie is `HttpOnly`, `SameSite=Lax`, `Secure` in production only | No token reachable from JS; a `Secure` cookie would never be sent over local HTTP. |

### Verification

**109 tests pass** (38 foundation + 71 auth). `ruff check` and `ruff format`
clean.

Covered: registration and duplicates, email normalisation, password policy,
argon2id storage, login and failure modes, enumeration (status, body, timing),
cookie flags, `Authorization: Bearer` as a second access path, session
revocation, reset request/confirm/expiry/single-use/re-request, the HTML form
surface, and the error contract (stable shape, no traceback, HTML vs JSON).

**Verified against the real Atlas cluster**, not only the in-memory double:

```text
register 201 | duplicate 409 | UPPERCASE duplicate 409
login 200 with HttpOnly cookie | me 200 | nav shows session
wrong password 401 | unknown account 401 | logout 200 | me 401
form register 303 | form login 303
Atlas row: argon2id hash, no plaintext, session_version 1, _id is a UUID
```

### Three bugs the test doubles could not find

All three were found by running against real Atlas, which is the argument for
doing that rather than trusting mocks:

1. **PyMongo refuses to encode a native `uuid.UUID`** under its default
   `UuidRepresentation.UNSPECIFIED`. Fixed with `uuidRepresentation="standard"`
   on the client.
2. **`find_one()` returns an awaitable.** Passing it into another coroutine
   produced a document that was actually a coroutine, failing as a confusing
   subscript error. `_as_user` is now a plain function.
3. **The production startup path was broken.** The repository was constructed
   before `mongo.connect()`, so `database()` raised on every real boot. Tests
   injected a repository and never executed that line. The app also now maps
   `PyMongoError` to `503 service_unavailable`, because with a non-raising
   `connect()` an Atlas outage otherwise surfaces as an unhandled 500 — and must
   never be reported to the user as "incorrect email or password".

Notes: cookie-based sessions. The `Authorization: Bearer` path is accepted too so
API consumers are not forced through a browser cookie jar; the cookie wins when
both are present. Minimal login/register/error templates were added because
cookie auth is not demonstrable without them — Phase 9 owns the real screens.

---

## Phase 03 — Workspaces

Branch: `phase-03-workspaces`

Scope: workspace creation, membership/access, workspace isolation.

- [x] `workspaces` collection model + `owner_id` index
- [x] `POST /workspaces` — create workspace for authenticated user
- [x] `GET /workspaces` / `GET /workspaces/{id}` — access-checked reads
- [x] `workspace_members` collection + role model (owner/member)
- [x] Membership endpoints (invite/add, list, remove)
- [x] Workspace access dependency enforcing isolation on every protected op

### The isolation gate

`AuthorizeWorkspace` is the single place "does this user belong here" is
answered. Routes receive a `WorkspaceAccess`, which cannot be constructed without
a membership row having been read, so a handler cannot skip the check — the
route does not run without it. Phases 4–7 depend on this rather than
re-deriving access.

```text
require_workspace_access
    └── get_current_user          401 before the workspace is even looked up
        └── AuthorizeWorkspace    reads workspace_members for (workspace, user)
            └── WorkspaceAccess   {workspace_id, user_id, role}
```

### Two tiers of refusal

| Caller | Response | Reasoning |
|---|---|---|
| Not a member | **404** | A 403 would confirm the workspace exists, letting anyone enumerate workspace ids. 404 makes "not yours" and "does not exist" identical — verified by asserting the two responses match byte for byte. |
| Member, wrong role | **403** | A member already knows the workspace exists, so there is nothing left to hide. |

A non-member attempting to add themselves to a workspace gets 404, not 403; a
member attempting the same gets 403. Both paths are tested.

### Rules that were designed, not defaulted

| Rule | Reason |
|---|---|
| Owner row is written with the workspace, in one call | A workspace must never exist with no owner row, which would make it invisible to everyone including its creator. |
| Owner cannot be removed or leave | Removing them orphans every document. Transferring ownership is a separate, explicit operation, not implied here. |
| A member may remove only themselves | Otherwise any member could evict anyone. |
| Deleted workspace deletes membership rows too | An orphaned row would still appear in someone's dashboard. |
| Listing joins `workspace_members` → `workspaces` instead of denormalising | A `workspace_ids` array on the user would save a round trip but becomes a second source of truth that drifts. Two indexed reads on M0's 100 ops/sec is cheaper than that class of bug. |
| Unique `(workspace_id, user_id)` index | Double-add is impossible, not merely unlikely. |
| Members are added by email, no pending invite | A pending invitation must be delivered, and Phase 2 declined to add a mail provider. Rejecting unknown addresses is an enumeration vector, but only to someone already inside the workspace. |
| Workspace collection created without one | Zero workspaces is a valid state and Phase 9 renders an empty state for it. |

### Verification

**155 tests pass** (109 previous + 46 new). `ruff check` and `ruff format` clean.

Covered: creation and name validation, listing only what you belong to, the full
isolation matrix (read, rename, delete, list members, self-add, remove-member),
role enforcement, member lifecycle (add, leave, duplicate, unknown email),
`401` on all eight routes when anonymous, `422` on malformed ids, owner
invariants, and outage behaviour.

One test deserves naming: a removed member's session cookie is still
cryptographically valid, yet their workspace access is refused. That proves the
check runs per request rather than trusting what was true when the token was
issued.

**Verified against real Atlas:**

```text
indexes      workspaces_owner, members_workspace_user_unique (unique), members_user
duplicate member blocked by the unique index
list_for_user owner -> [('smoke-ws','owner')]  member -> [('smoke-ws','member')]
delete       -> 204, and 0 orphaned membership rows remain
```

**One bug the in-memory double hid.** `rename` used `find_one_and_update`
without `return_document`, and PyMongo defaults to `ReturnDocument.BEFORE` — so
the route returned the document as it was *before* the rename. `PATCH
/workspaces/{id}` would have echoed the old name and looked like a no-op. The
double was hand-written and therefore agreed with the expectation.
`TestMongoRepositorySemantics` now pins it against a collection that honours
PyMongo's default.

Notes: no workspace UI yet. Phase 4 needs only the API, and Phase 9 owns the
dashboard, switcher and member management screens.

---

## Phase 04 — Documents

**Settled contract — decided before any code was written. Do not re-litigate.**

### The access model

The workspace is the *only* access boundary. There is no per-document
permission, and the uploader is not part of any access check.

```text
caller is a member of the document's workspace  ->  200, always
caller is not a member                          ->  404
document does not exist                         ->  404
```

| Case | Response | Reasoning |
|---|---|---|
| Same-workspace member, did not upload it | **200** | They are inside the boundary. Uploader is irrelevant. |
| Not a member of the document's workspace | **404** | A 403 confirms the document exists, leaking existence across tenants. With UUIDs the guessing risk is small, but 404 costs nothing and keeps the boundary consistent. |
| Document id does not exist | **404** | Byte-identical to the previous case. Indistinguishable to the caller. |

403 is reserved for **intra-workspace** permissions — private documents, or
viewer vs. editor roles — which do not exist yet. If they are ever added, 403 on
a specific document is reasonable, since a member already knows the workspace
exists.

### Route shape: nested under the workspace

```text
GET    /workspaces/{workspace_id}/documents
POST   /workspaces/{workspace_id}/documents
GET    /workspaces/{workspace_id}/documents/{document_id}
DELETE /workspaces/{workspace_id}/documents/{document_id}
```

Nesting is required, not stylistic. `require_workspace_access` reads
`workspace_id` from the path, so it can only run as a dependency if the workspace
is in the path. A flat `GET /documents/{id}` would have nowhere to resolve
membership from, pushing the check into the use case where a future endpoint
could forget it. Nesting preserves the structural guarantee that a handler
cannot execute without the isolation check having passed.

### Repository scoping — mandatory

Every document query filters on **both** identifiers:

```python
{"_id": document_id, "workspace_id": workspace_id}
```

Not `{"_id": ...}` followed by a comparison. Two reasons:

1. A document that exists in a *different* workspace must produce the same 404,
   and take the same code path, as one that does not exist at all. A
   fetch-then-compare would let the two diverge in status code or timing.
2. It is a single `_id` lookup with an added filter, so the scoping is free.

### Checklist

- [ ] `documents` collection model with explicit status enum (PENDING/PROCESSING/READY/FAILED)
- [ ] Local file storage adapter (temp dir, later S3-compatible) + content hashing
- [ ] `POST /workspaces/{id}/documents` — content-type allowlist, size limit, 202 response
- [ ] Explicit state-transition guard (client cannot set status)
- [ ] `GET /workspaces/{id}/documents` — paginated, workspace-filtered list
- [ ] `GET /workspaces/{id}/documents/{id}` — status, `processing_error`, chunk count
- [ ] `DELETE /workspaces/{id}/documents/{id}` — soft delete + chunk cleanup

Scope: upload, validation, metadata, processing status. See README §18 Phase 4 and SKILL.md §7–§9.

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

- [ ] Base layout + Bootstrap 5 CDN + HTMX CDN, FastAPI `Jinja2Templates` + `StaticFiles`
- [ ] Login / register screens
- [ ] Dashboard with workspace switcher
- [ ] Upload screen (drag-drop + HTMX upload + progress)
- [ ] Document list + live processing status polling (PENDING → PROCESSING → READY/FAILED)
- [ ] Ask Knowledge Base UI — answer rendering with source citations
- [ ] Conversation history view

Notes: same-origin only. No JS framework, no build step, no CORS config.

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
- [ ] Render deployment verified — env vars set, health check path `/health`, `$PORT` honoured, Atlas IP allowlist includes Render egress
- [ ] Production Dockerfile verified (multi-stage, non-root, layer caching) + image size check
- [ ] Security review — secrets, file limits, upload validation, error leakage
- [ ] Observability — structured JSON logs, usage reporting endpoint `GET /usage`
- [ ] README + `SKILL.md`/roadmap updates documenting final architecture and decisions

Notes: Docker is a build/deploy artifact, not a local MongoDB stack. There is no
local MongoDB compose file — Atlas M0 is the only database. `uvicorn` is the
rendered web server; Render injects `PORT` (10000 by default).

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
| 1 | 01 | PyMongo's native `AsyncMongoClient`; **no `motor`, no `python-dotenv`** | `motor` is EOL and PyMongo 4.13+ ships async in the core driver, so one package covers the driver and its types. `python-dotenv` is redundant because `python-decouple` already resolves `.env`. Both were briefly added for a scratch connectivity script; `scripts/check_mongo.py` now uses `pymongo` + `decouple`, so neither belongs in the production image. |
| 2 | 01 | Mongo startup failure does **not** raise | On Render the process must bind `$PORT` even when Atlas is unreachable, otherwise Render restart-loops. `/health` stays 200, `/health/ready` returns 503 and reports the truth |
| 3 | 01 | `python-decouple` instead of `pydantic-settings` | Decouple is dotenv-native: it resolves `.env` locally and the process environment on Render through one code path. Every variable is declared by name in `load_settings()`, so the configuration surface is one auditable list. `pydantic` stays as FastAPI's own schema dependency. |
| 4 | 01 | Decouple's `cast` runs *after* the emptiness check in `_require` | `Config.get` applies `cast` to whatever it returns, so `cast=str` on a missing variable yields the literal `"None"` instead of raising. Reading the raw value first is what makes "variable not set" a legible error. |
| 5 | 01 | Standard-library JSON logging, no structlog; `LOG_FORMAT` removed from config | Render's log viewer wants JSON and nothing else consumes logs. A 15-line formatter avoids adding a dependency just for JSON output, so the format is not a configurable knob |
| 6 | 01 | `create_app(settings, *, mongo_manager=None)` seam | Lets tests exercise the real HTTP stack including lifespan without a live Atlas, instead of monkeypatching internals |
| 7 | 01 | Project is not pip-installed in the Docker image; `PYTHONPATH=/app/src` | Keeps `uv sync` from rebuilding the project on every code change, which is what makes the layer cache work |
| 8 | 01 | Atlas M0 allows only 3 vector/search indexes | Phase 5 will declare `workspace_id` as a `filter` field inside the single vector index instead of relying on extra indexes |
| 9 | 01 | No Bootstrap JavaScript bundle; no `BOOTSTRAP_ADMIN_*` variables | Only the Bootstrap stylesheet is loaded from CDN. No Bootstrap JS component is in use, so the Popper-carrying bundle is dead weight. There is no bootstrap admin user: Phase 2 registration creates the first account. |
| 10 | 01 | `.env.sample` trimmed to 36 variables, every one read by name in `load_settings()` | Only values the business rules in README §14/§15 actually consume. Removed tuning knobs with no functional role: `ARGON2_*`, `MONGODB_MAX_POOL_SIZE`, `MONGODB_CONNECT_TIMEOUT_MS`, `MONGODB_SOCKET_TIMEOUT_MS`, `RATE_LIMIT_ENABLED`, `LOG_FORMAT`, `LOG_FILE`, `CORS_ORIGINS`, `PUBLIC_BASE_URL`, `FRONTEND_*`, all `*_CONTAINER_PORT`. |
| 11 | 01 | Docker base images pinned to Debian `trixie`, not `bookworm` | uv stopped publishing `-bookworm-slim` after `0.9.30`; from `0.10.x` the variant is `-trixie-slim`, so the pinned tag no longer existed and the build failed on `FROM`. Builder and runtime must stay on the same Debian release: wheels with compiled extensions (`pydantic-core`, `uvloop`) resolve against the builder's glibc and would fail at import time, not build time, on an older base. |
| 12 | 01 | `groupadd` + `useradd` with an explicit `--gid`, not `adduser` | Two builds failed here. First: `adduser --system` does **not** create a same-named group (it takes the primary group from `/etc/default/useradd`), so `chown appuser:appuser` failed with `invalid group`. Second: an earlier revision claimed `useradd` might be absent from slim — the build log disproved it, since `adduser` is a perl wrapper that shells out to `useradd` and printed a `useradd` warning. Naming `--gid` and `--uid` removes every default this depends on. |
| 13 | 01 | Tests clear every `.env.sample` key from `os.environ` | decouple resolves the process environment before the `.env` file, so a variable exported in the developer's shell silently overrides the test fixture. Test outcomes must not depend on whose terminal ran them. |
| 14 | 01 | Connectivity check lives in `scripts/check_mongo.py`, outside `src/` | It is a developer tool, not application code. The Dockerfile copies only `src/`, so it never reaches the image, and `.dockerignore` keeps it out of the build context too. |
| 15 | 01 | Atlas and Render are both in Oregon (`us-west-2`) | Colocation keeps every Mongo round trip inside the same region. This matters more than usual here: `$vectorSearch` runs as an aggregation pipeline, so a retrieval is several round trips, and M0 is limited to 100 ops/sec. A cross-region deployment would put an ocean between the API and the database on every question. |
| 16 | 01 | Serve `/healthz` and `/readyz` as aliases of `/health` and `/health/ready` | A deploy sat in `In progress` for 10+ minutes with a healthy container because the host polled `/healthz` and got a 404 every 10 seconds. Infrastructure probe paths are configured per host, so the app answers both spellings rather than depending on one being chosen correctly. The aliases are hidden from the OpenAPI schema — they are for probes, not API consumers. |
| 17 | 02 | Session travels in an `HttpOnly` cookie; `Authorization: Bearer` accepted as a second path | Chosen because the UI is server-rendered HTMX, so no token handling is needed in JavaScript. The bearer path is still accepted so an API-first product is not forced through a browser cookie jar, and one token service serves both. `Secure` follows the environment: required over HTTPS, and it would simply never be sent over local HTTP. |
| 18 | 02 | `session_version` column on the user, compared on every authenticated request | A JWT is stateless and therefore cannot be revoked, which is unacceptable when a password changes. The token carries the version it was minted with; a password change increments the stored value, so every existing token stops validating. This is how the app logs a user out everywhere without server-side session storage. |
| 19 | 02 | Reset link is written to the log in development, never returned by the API | There is no mail provider and `SKILL.md` §3 forbids adding one speculatively. Returning the token in the response was the obvious shortcut and it was wrong: the response then differed for known and unknown addresses, turning the endpoint into an enumeration oracle. A test caught it. Production logs a `delivery: UNCONFIGURED` marker so the gap is visible instead of silently swallowing resets. |
| 20 | 02 | Both `/auth/*` (JSON) and `/ui/*` (HTML) routes call the same use case objects | HTMX posts forms while API consumers post JSON. Duplicating the rules would let the two surfaces disagree about what a valid registration is. Two thin routes, one use case. Phase 9 replaces the `/ui` handlers with the real screens. |
| 21 | 02 | Database errors become `503 service_unavailable`, never `401` | `MongoManager.connect()` does not raise by design (Phase 01 decision 2), so an Atlas outage leaves the client object present and the failure appears later as a `ServerSelectionTimeoutError` on a query. Unhandled, that is a 500. Worse, inside login it would sit right next to the credential check, so an outage could plausibly be reported to the user as 'incorrect email or password'. |
| 22 | 02 | Every auth route was verified against real Atlas, not only the in-memory double | The double hid three startup-only bugs, including one that made the production boot path raise. `tests/conftest.py` now keeps a `FakeDatabase` and an ordering-checking `FakeMongoManager` so the production object graph is constructed in tests too. |
| 23 | 03 | One isolation gate, `AuthorizeWorkspace`, returning `WorkspaceAccess` | SKILL.md §6 requires three checks on every protected operation. Doing them per route would mean forty ways to get it subtly wrong across phases 4-7. Handing handlers a `WorkspaceAccess` value makes the check structural: the route cannot execute without it, and the value cannot be constructed without reading `workspace_members`. |
| 24 | 03 | Non-membership returns 404, not 403 | A 403 confirms the workspace exists, so any authenticated user could enumerate workspace identifiers by probing the difference between 403 and 404. A member already knows the workspace exists, so there is nothing left to hide and 403 is correct there. The test asserts a non-member's response and a fabricated id's response are identical. |
| 25 | 03 | Membership is a join, not a denormalised array on the user | Storing `workspace_ids` on the user document would save one round trip on every list. It also creates a second source of truth that drifts the moment a member is removed or a workspace is deleted, and the drift is silent. Atlas M0 allows 100 ops/sec; two indexed reads is cheaper than that bug class. |
| 26 | 03 | Add-by-email with no pending invite state | The roadmap said 'invite/add'. A pending invitation has to be delivered, which needs a mail provider, and Phase 2 explicitly declined to invent one. So membership is granted directly and only for an address that already has an account. Pending invites plus an expiry and a resolution path would roughly double this phase for a portfolio project with no way to send mail. |
| 27 | 03 | The owner cannot be removed and cannot leave | Removing them leaves every document in the workspace with nobody able to delete or transfer it. Ownership transfer is a real feature and is deliberately not implied here, because 'promote someone else' is an explicit decision that should not happen as a side effect of a removal. |
| 28 | 03 | Workspace creation writes the workspace and its owner row together | Two separate writes leave a window where the workspace exists with no owner row, making it invisible to everyone including its creator. The insert is rolled back if the membership write fails. |
| 29 | 03 | Deleted workspaces delete their membership rows | Otherwise an orphaned `workspace_members` row still matches `list_for_user`, so the deleted workspace keeps appearing in that person's dashboard. Confirmed against Atlas: 0 rows remain after delete. |
| 30 | 03 | `rename` uses `return_document=ReturnDocument.AFTER` | PyMongo's `find_one_and_update` defaults to BEFORE. Without this the route returns the pre-update document and the rename appears to do nothing. The hand-written test double agreed with the expectation instead of the driver, so only a real cluster exposed it. There is now a test using a collection that honours PyMongo's default. |
| 31 | 03/04 | The workspace is the only access boundary; there is no per-document permission | Every member of a workspace can see every document in it, and the uploader is irrelevant to access. This is the simplest model that satisfies README §6 and SKILL.md §6, and it keeps a single authorization concept in the whole system. A per-document permission model would need its own entity, its own endpoints and its own tests, for a portfolio project whose requirements never mention it. Revisit only if a requirement actually asks for private documents. |
| 32 | 03/04 | Non-members get 404, and the response is byte-identical to a missing resource | A 403 answers 'this exists but is not yours', which leaks existence across tenants. 404 makes 'not yours' and 'does not exist' the same response. Verified across all six workspace routes by comparing the two bodies, not just the status codes. This is the guard Phase 4 inherits. |
| 33 | 04 | Document routes are nested under the workspace | `require_workspace_access` resolves membership from a path parameter, so the workspace has to be in the path for the check to run as a dependency. A flat `/documents/{id}` would move the check into the use case, where a future endpoint could omit it. Nesting keeps 'a handler cannot execute without the isolation check' structurally true. |
| 34 | 04 | Document queries filter on `(id, workspace_id)` together | A document in another workspace must take the same code path as one that does not exist, so the two cannot diverge in status code or response timing. Querying both fields in one `_id` lookup also makes the scoping free rather than a second step that could be forgotten. |
