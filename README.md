<p align="center">
  <img src="src/knowledgedock/static/logo.png" alt="KnowledgeDock logo" width="120" height="120" />
</p>

<h1 align="center">KnowledgeDock</h1>

<p align="center"><strong>Your documents, answered intelligently.</strong></p>

<p align="center">
  <em>An API-first AI knowledge retrieval platform — upload documents, search semantically, and ask questions answered with retrieved context.</em>
</p>

---

## Overview

KnowledgeDock is an API-first AI knowledge retrieval application that lets users upload documents, process their content, search the stored knowledge semantically, and ask questions that are answered using retrieved document context.

The project is designed to showcase **backend engineering + practical AI integration**, rather than machine-learning model development.

The goal is to demonstrate how a backend engineer can build an AI-powered application with clear architecture, reliable workflows, data isolation, background processing, retrieval, and production-oriented engineering practices.

---

## 1. Problem

Organizations accumulate information in:

- Product documentation
- Internal guides
- Technical manuals
- FAQs
- Policies
- Support documentation
- Markdown/text documents

Finding the correct information manually becomes slow as the amount of documentation grows.

Traditional keyword search also has a limitation:

> A user may know what they want to ask without knowing the exact words used in the document.

For example, a document may say:

> "Credentials can be regenerated from the account security settings."

A user may ask:

> "How do I reset my API key?"

A normal keyword search may not understand that these two statements are related.

KnowledgeDock solves this by combining:

1. Document storage
2. Text extraction
3. Chunking
4. Embeddings
5. Semantic/vector search
6. Retrieval-Augmented Generation (RAG)
7. LLM-based answer generation
8. Source references

---

# 2. Project Goal

KnowledgeDock should demonstrate that a backend developer understands:

- REST API design
- Authentication and authorization
- MongoDB data modeling
- Indexing
- Background processing
- Asynchronous programming
- File processing
- Vector/semantic retrieval
- RAG architecture
- LLM API integration
- Rate limiting
- Usage tracking
- Error handling
- Retry and timeout strategies
- Logging
- Docker
- Clean application architecture
- Frontend/backend communication

The project is **not** intended to demonstrate ML model training.

The AI model is treated as an external service.

The main engineering responsibility remains the backend.

---

# 3. Technology Stack

## Backend

- Python
- FastAPI
- Pydantic
- Uvicorn

## Database

- MongoDB
- MongoDB Vector Search / Atlas Vector Search where available

MongoDB stores:

- Users
- Workspaces
- Documents
- Document chunks
- Embedding/vector information
- Conversations
- Messages
- Usage information
- Processing status
- Audit information

## AI

The application should use an external LLM/embedding provider through a provider abstraction.

Possible providers:

- OpenAI
- Gemini
- Another compatible provider

The application should **not tightly couple business logic to one provider**.

Use an internal interface/service boundary so the provider can be replaced later.

## Frontend

The frontend intentionally remains lightweight.

- Vanilla JavaScript
- Bootstrap CSS
- HTMX
- HTML templates

Do not introduce React, Vue, Angular, or another frontend framework.

The frontend exists mainly to demonstrate that the backend APIs actually work through a usable interface.

## Infrastructure

- Docker
- Docker Compose
- Environment variables for configuration

---

# 4. High-Level Architecture

```text
                         ┌──────────────────────┐
                         │       Browser        │
                         │                      │
                         │ HTML + Bootstrap     │
                         │ Vanilla JS + HTMX    │
                         └──────────┬───────────┘
                                    │
                              HTTP / HTMX
                                    │
                                    ▼
                         ┌──────────────────────┐
                         │      FastAPI         │
                         │       API            │
                         └──────────┬───────────┘
                                    │
             ┌──────────────────────┼──────────────────────┐
             │                      │                      │
             ▼                      ▼                      ▼
      ┌─────────────┐       ┌──────────────┐      ┌──────────────┐
      │ Auth / User │       │ Document API │      │ Query API    │
      │ / Workspace │       │              │      │              │
      └─────────────┘       └──────┬───────┘      └──────┬───────┘
                                   │                     │
                                   ▼                     ▼
                            ┌──────────────┐      ┌──────────────┐
                            │ Processing   │      │ Retrieval    │
                            │ Service      │      │ Service      │
                            └──────┬───────┘      └──────┬───────┘
                                   │                     │
                                   ▼                     ▼
                            ┌──────────────┐      ┌──────────────┐
                            │ Chunking +   │      │ Vector Search│
                            │ Embeddings   │      │              │
                            └──────┬───────┘      └──────┬───────┘
                                   │                     │
                                   └──────────┬──────────┘
                                              ▼
                                      ┌──────────────┐
                                      │   MongoDB    │
                                      │              │
                                      │ Documents    │
                                      │ Chunks       │
                                      │ Vectors      │
                                      │ Users        │
                                      │ Conversations│
                                      └──────────────┘

                                              │
                                              │ Retrieved context
                                              ▼

                                      ┌──────────────┐
                                      │ LLM Provider │
                                      │              │
                                      │ Answer       │
                                      │ Generation   │
                                      └──────────────┘
```

---

# 5. Document Ingestion Architecture

Uploading a document should not perform expensive processing directly inside the request lifecycle.

```text
User
 │
 │ Upload document
 ▼
FastAPI
 │
 ├── Validate file
 ├── Authenticate user
 ├── Create document record
 └── Mark status = PENDING
 │
 ▼
Background Processing
 │
 ├── Read document
 ├── Extract text
 ├── Normalize text
 ├── Split into chunks
 ├── Generate embeddings
 ├── Store chunks
 └── Mark document READY
```

The API should return quickly after accepting the document.

The user should be able to observe processing status.

Example:

```text
PENDING
   ↓
PROCESSING
   ↓
READY
```

Failure:

```text
PROCESSING
   ↓
FAILED
```

A failed document should retain enough information to diagnose the failure.

---

# 6. RAG Architecture

KnowledgeDock uses Retrieval-Augmented Generation.

The system should not simply send the user's question directly to an LLM.

Instead:

```text
User Question
      │
      ▼
Create Query Embedding
      │
      ▼
MongoDB Vector Search
      │
      ▼
Relevant Document Chunks
      │
      ▼
Context Builder
      │
      ▼
LLM Prompt
      │
      ▼
Generated Answer
      │
      ▼
Answer + Sources
```

Example:

```text
Question:

"How can I rotate an API key?"
```

The retrieval system may find:

```text
Document: API Security Guide
Chunk: 42
Similarity: 0.91

"API credentials can be regenerated from the Security
section of the dashboard..."
```

That retrieved context is supplied to the LLM.

The final response should include the source document/chunk information so the user can inspect where the answer came from.

---

# 7. Core Workflows

## Workflow 1 — User Registration

```text
User
 │
 ▼
POST /auth/register
 │
 ├── Validate input
 ├── Check existing user
 ├── Hash password
 └── Store user
 │
 ▼
Response
```

---

## Workflow 2 — Login

```text
User
 │
 ▼
POST /auth/login
 │
 ├── Validate credentials
 ├── Verify password
 └── Create authentication token
 │
 ▼
Authenticated session
```

---

## Workflow 3 — Workspace Creation

```text
Authenticated User
 │
 ▼
Create Workspace
 │
 ▼
Workspace ID
 │
 └── Documents belong to this workspace
```

Workspace isolation is important.

A user must never be able to access documents belonging to another workspace.

---

# 8. Document Upload Workflow

```text
Client
 │
 │ POST /documents
 ▼
FastAPI
 │
 ├── Authenticate
 ├── Check workspace access
 ├── Validate file type
 ├── Validate size
 ├── Create document metadata
 └── Queue processing
 │
 ▼
HTTP 202 Accepted
```

The document processor then performs:

```text
Read file
   ↓
Extract text
   ↓
Normalize
   ↓
Chunk
   ↓
Generate embeddings
   ↓
Persist chunks + vectors
   ↓
READY
```

---

# 9. Question Answering Workflow

```text
Client
 │
 │ POST /query
 ▼
FastAPI
 │
 ├── Authenticate
 ├── Validate workspace
 └── Validate question
 │
 ▼
Embedding Service
 │
 ▼
Query Vector
 │
 ▼
MongoDB Vector Search
 │
 ▼
Top-K Relevant Chunks
 │
 ▼
Context Builder
 │
 ├── Remove irrelevant context
 ├── Apply token/context limits
 └── Build grounded prompt
 │
 ▼
LLM Provider
 │
 ▼
Answer
 │
 ├── Answer text
 ├── Sources
 └── Usage metadata
 │
 ▼
Client
```

---

# 10. No-Answer Workflow

The system must not force the LLM to answer when the knowledge base does not contain relevant information.

```text
Question
   ↓
Vector Search
   ↓
Similarity Check
   │
   ├── Relevant
   │      ↓
   │    Generate Answer
   │
   └── Not Relevant
          ↓
       "I couldn't find enough
        information in the
        knowledge base."
```

This is an important part of making the application more reliable.

---

# 11. AI Failure Workflow

External AI services can fail.

Possible failures:

- Timeout
- Rate limit
- Provider outage
- Invalid response
- Token limit
- Temporary network error

The application should handle these explicitly.

```text
Backend
   │
   ▼
AI Provider
   │
   ├── Success ───────────────► Continue
   │
   ├── Timeout ───────────────► Retry / Fail gracefully
   │
   ├── Rate limit ────────────► Backoff / Return controlled error
   │
   └── Provider error ────────► Log + controlled response
```

Never allow an external AI failure to crash the API process.

---

# 12. Data Model

A conceptual MongoDB model:

```text
users
 ├── _id
 ├── email
 ├── password_hash
 ├── created_at
 └── updated_at

workspaces
 ├── _id
 ├── name
 ├── owner_id
 ├── created_at
 └── updated_at

documents
 ├── _id
 ├── workspace_id
 ├── filename
 ├── content_type
 ├── size
 ├── status
 ├── processing_error
 ├── created_at
 └── updated_at

document_chunks
 ├── _id
 ├── workspace_id
 ├── document_id
 ├── chunk_index
 ├── text
 ├── embedding
 └── metadata

conversations
 ├── _id
 ├── workspace_id
 ├── user_id
 ├── created_at
 └── updated_at

messages
 ├── _id
 ├── conversation_id
 ├── role
 ├── content
 ├── sources
 ├── usage
 └── created_at
```

Indexes should be designed around actual query patterns.

Do not create indexes simply because an index exists in the schema.

---

# 13. Backend Architectural Principles

The application should follow clear boundaries:

```text
API Layer
   ↓
Application / Use Cases
   ↓
Domain Logic
   ↓
Infrastructure
   ├── MongoDB
   ├── AI Provider
   ├── File Storage
   └── Background Processing
```

The API layer should not contain business logic.

For example, avoid:

```text
FastAPI route
 ├── authenticate
 ├── query MongoDB
 ├── build prompt
 ├── call OpenAI
 ├── calculate usage
 └── save conversation
```

Prefer:

```text
FastAPI Route
      ↓
Use Case
      ↓
Services
      ↓
Repositories / Providers
```

This keeps the application easier to test and change.

---

# 14. Security Requirements

At minimum:

- Password hashing
- Authentication
- Authorization
- Workspace isolation
- Input validation
- File validation
- File size limits
- API rate limiting
- Environment-based secrets
- No API keys committed to Git
- Safe error messages
- Request logging without sensitive information

AI-specific security:

- Treat uploaded documents as untrusted data.
- Treat retrieved text as untrusted context.
- Defend against prompt injection where practical.
- Never allow document content to override system/application instructions.
- Do not expose private workspace data across tenants.

---

# 15. Observability

The backend should produce useful logs for:

- Request ID
- Endpoint
- HTTP status
- Processing duration
- Document processing failures
- AI provider failures
- Retrieval failures
- Background job failures

AI usage should track information such as:

```text
provider
model
input_tokens
output_tokens
total_tokens
request_duration
```

Do not log secrets or complete private documents unnecessarily.

---

# 16. Frontend

The frontend is intentionally simple.

Use:

- HTML
- Bootstrap CSS
- HTMX
- Vanilla JavaScript

Expected screens:

```text
Login
  ↓
Dashboard
  ├── Documents
  ├── Upload Document
  ├── Processing Status
  └── Ask Knowledge Base
```

The frontend should demonstrate the backend rather than become the main engineering challenge.

Avoid adding a JavaScript framework.

---

# 17. Docker

Development should be reproducible with Docker.

Conceptually:

```text
Docker Compose
 │
 ├── FastAPI
 │
 ├── MongoDB
```
---

# 18. Development Phases

## Phase 1 — Foundation

- Project structure
- Configuration
- FastAPI application
- MongoDB connection
- Health endpoint
- Docker setup

## Phase 2 — Authentication

- User model
- Registration
- Login
- Authentication
- Authorization

## Phase 3 — Workspaces

- Workspace creation
- Workspace membership/access
- Workspace isolation

## Phase 4 — Documents

- Upload
- Validation
- Metadata
- Processing status

## Phase 5 — Ingestion

- Text extraction
- Normalization
- Chunking
- Embedding generation
- Vector storage

## Phase 6 — Retrieval

- Semantic search
- Top-K retrieval
- Similarity threshold
- Context construction

## Phase 7 — RAG

- Prompt construction
- LLM integration
- Grounded responses
- Source citations
- No-answer fallback

## Phase 8 — Reliability

- Timeouts
- Retries
- Rate limiting
- Error handling
- Usage tracking
- Logging

## Phase 9 — Frontend

- Dashboard
- Document management
- Upload
- Processing status
- Knowledge query interface

## Phase 10 — Production Readiness

- Tests
- Docker
- Environment configuration
- Security review
- Observability
- README documentation

---

# 19. What This Project Should Demonstrate

After completion, a reviewer should be able to see:

```text
Backend Engineering
       +
Database Design
       +
Async Processing
       +
API Design
       +
Reliability
       +
AI Integration
       +
RAG
       +
Docker
       +
Simple Frontend
```

The project should answer an interviewer's question:

> "Can this developer integrate AI into a real backend system?"

without pretending that the developer is an ML researcher.

---

# 20. Non-Goals

Do NOT turn this project into:

- A custom LLM training platform
- A machine-learning research project
- A ChatGPT clone
- A huge microservices system
- A Kubernetes project
- A frontend-heavy application
- A collection of unrelated AI features

Keep the system small enough to finish and deep enough to discuss.

---

# 21. Final Architecture Goal

The finished project should feel like a small production system:

```text
                         KNOWLEDGEDOCK

                             Client
                               │
                               ▼
                    ┌───────────────────┐
                    │ FastAPI Application│
                    └─────────┬─────────┘
                              │
            ┌─────────────────┼─────────────────┐
            │                 │                 │
            ▼                 ▼                 ▼
         Auth             Documents          Query
            │                 │                 │
            │                 ▼                 ▼
            │             Processing         Retrieval
            │                 │                 │
            │                 ▼                 ▼
            └────────────► MongoDB ◄────────────┘
                              │
                              │
                       Vector Search
                              │
                              ▼
                       Context Builder
                              │
                              ▼
                         LLM Provider
                              │
                              ▼
                       Answer + Sources
```

KnowledgeDock should remain an **AI-enabled backend system**, not an AI model project.

That distinction is intentional and is central to the portfolio value of the project.
