# JLR Multi-Agent System — Phase 1 Architecture

**Status:** Evolving Phase-1 design. This document reflects the current shared understanding of the ingestion and query architecture for the internal multi-agent platform. It is intended to be updated as implementation progresses — it is not a finalized enterprise specification.

### Contents

1. [System Intent and Design Principles](#1-system-intent-and-design-principles)
2. [System Architecture Overview](#2-system-architecture-overview)
3. [High-Level Agent Observations](#3-high-level-agent-observations)
4. [Agent Configuration Model](#4-agent-configuration-model)
5. [Data Ingestion Pipeline](#5-data-ingestion-pipeline)
6. [Storage Architecture](#6-storage-architecture)
7. [Metadata and Data Catalog](#7-metadata-and-data-catalog)
8. [Query Pipeline](#8-query-pipeline)
9. [Safe Structured Query Flow](#9-safe-structured-query-flow)
10. [Evidence and Citation Model](#10-evidence-and-citation-model)
11. [UAT-Driven Requirements](#11-uat-driven-requirements)
12. [SME Escalation and Knowledge Improvement Loop](#12-sme-escalation-and-knowledge-improvement-loop)
13. [Observability and Auditability](#13-observability-and-auditability)
14. [What Was Rejected or Deferred](#14-what-was-rejected-or-deferred)
15. [Phased Implementation Roadmap](#15-phased-implementation-roadmap)

---

## 1. System Intent and Design Principles

This is an **internal enterprise system** built for a defined set of JLR engineering teams. Its purpose is to give each team a purpose-built chat agent over their own domain knowledge, without building six separate backends.

### Core principles

- **Trust, traceability, and source-groundedness outrank generative flair.** Answers must be defensible — a user should be able to tell where a fact came from.
- **No backend duplication.** Six frontend agents share one backend platform; agent-specific behavior comes from configuration, not from six parallel codebases.
- **Configuration-driven backend.** Agent scope, source access, retrieval behavior, and output formatting are controlled by config (keyed on `agent_id`), not hardcoded per agent.
- **Structured and unstructured data are treated differently, end to end.** A spreadsheet of supplier part numbers and a PDF of lessons-learned narrative are fundamentally different kinds of truth and require different pipelines.
- **Excel is not "just text to embed."** It is a hybrid source containing both structured, queryable data and narrative/document-like content, and must be processed as such.
- **Retrieval accuracy comes before generation quality.** A fluent answer built on the wrong evidence is worse than a plain answer built on the right evidence.
- **Extensible, but not over-engineered for Phase 1.** The architecture should leave room for future capability (e.g., graph-based reasoning, more agents, more source types) without building for hypothetical needs now.

### Supported input types

Phase 1 must handle:

- PDF
- PPT / PPTX
- DOCX
- HTML
- Excel

**Spreadsheet standardization:** for Phase 1, the team may choose to standardize spreadsheet intake to `.xlsx` to reduce parser variance. This is an input-normalization decision only — it does not change how Excel is conceptually treated. Regardless of the intake format, Excel remains a **hybrid source** (structured + document-like) in the architecture.

---

## 2. System Architecture Overview

This section shows how the major runtime and ingestion components fit together, and where agent isolation is enforced. Detailed pipeline-level diagrams appear in their respective sections (ingestion: §5.1, query: §8.1).

```
                          6 Frontend Chat Agents
   (LE Grooming | Benchmarking | ADAS | Supplier DB | EDS | Material DB)
                                   |
                                   v
                             API Gateway
                                   |
                                   v
                   Authentication / Authorization
                                   |
                                   v
                          Orchestration Layer  <----->  Agent Configuration Registry
                                   |                     (per agent_id: scope, source
                |------------------+------------------|   filters, templates, policy)
                |                                     |
                v                                     v
        Session Store /                          Query Pipeline
        Chat History                                   |
                                                         v
                                          Shared Backend Data Layer
                                    +--------------------------------+
                                    |  PostgreSQL / Structured Store  |
                                    |  Vector Index / Semantic Store  |
                                    |  Metadata / Data Catalog        |
                                    +--------------------------------+
                                                         ^
                                                         |
                                               Ingestion Pipeline
                                                         ^
                                                         |
                                                Raw File Storage
                                                         ^
                                                         |
                                    Source Documents (PDF, PPT/PPTX, DOCX, HTML, Excel)

                            SME Feedback / Escalation Loop
        (reads from Query Pipeline output; writes corrections back through
         Ingestion Pipeline into the Shared Backend Data Layer — see §12)
```

### Component summary

| Component | Role |
|---|---|
| **Frontend Chat UI (×6)** | Agent-specific chat experience. Each UI is a thin client — no agent-specific backend logic lives here. |
| **API Gateway** | Single entry point for all agent traffic; routes requests to orchestration. |
| **Authentication / Authorization** | Verifies user identity and resolves permitted agent(s) and data scope. |
| **Orchestration Layer** | Coordinates a request across session, configuration, and the query pipeline. |
| **Agent Configuration Registry** | Holds the per-`agent_id` configuration that differentiates the six agents (§4). |
| **Session Store / Chat History** | Persists conversation turns for context continuity across a session. |
| **Query Pipeline** | Executes the retrieval/answering flow described in §8. |
| **Ingestion Pipeline** | Executes the extraction/indexing flow described in §5. |
| **Raw File Storage** | Durable, unmodified copy of every ingested source file. |
| **PostgreSQL / Structured Store** | Authoritative store for structured data materialized from documents and Excel. |
| **Vector Index / Semantic Store** | Keyword/vector index for semantic retrieval. |
| **Metadata / Data Catalog** | Registry of what exists, where, and under what scope (§7). |
| **SME Feedback / Escalation Loop** | Human-in-the-loop review path for weak or contested answers (§12). |

### Isolation model

The backend is **shared**, not duplicated. Per-agent isolation is enforced through four mechanisms, applied consistently across ingestion, storage, and query:

- **`agent_id`** — the primary key threaded through every record (documents, chunks, rows, catalog entries, session data).
- **Configuration** — agent-specific behavior (sources, prompts, templates, policies) comes from the Agent Configuration Registry, not from separate code paths.
- **Source scope** — each agent is restricted to an explicit set of allowed sources/datasets.
- **Authorization filters** — access control applied at query time, independent of and in addition to source scope.

---

## 3. High-Level Agent Observations

Six frontend agents are planned, each with its own chat experience and knowledge scope:

| Agent | Character |
|---|---|
| **Lead Engineer (LE) Grooming** | Primarily a knowledge/process agent, with some structured support |
| **Benchmarking** | Heavily structured and comparison-driven; some semantic support needed |
| **ADAS Packaging Study** | Strong hybrid agent — structured specs, benchmark data, and engineering documents together |
| **Supplier Database** | Primarily structured lookup |
| **EDS (Engineering Design Specification)** | Knowledge-heavy and reasoning-heavy — citations, risks, lessons learned |
| **Material Database / New Edge Technologies** | Hybrid — material database + technical data sheets + standards/benchmarking |

### Why this matters architecturally

No agent is purely structured and no agent is purely document-based — every agent sits somewhere on a structured-to-semantic spectrum, and several (ADAS, Material Database) sit squarely in the middle. This is the justification for **one shared hybrid backend** rather than six bespoke designs: building six separate retrieval stacks would mean re-solving the same structured/semantic split six times, while a shared hybrid platform lets each agent simply configure *which* sources and *how much* of each retrieval mode it needs.

---

## 4. Agent Configuration Model

The platform is configuration-driven: the six agents are six configurations of one backend, not six implementations. The Agent Configuration Registry (§2) holds one record per `agent_id` with the fields below.

| Field | Purpose |
|---|---|
| `agent_id` | Unique identifier; the isolation key used across ingestion, catalog, and query pipeline |
| Display name | User-facing name shown in the chat UI |
| Allowed knowledge sources | Which documents/datasets/catalog entries this agent may query |
| Source filters / namespaces | Scoping rules applied at retrieval and SQL time (e.g., program, business unit, confidentiality tier) |
| Prompt / system behavior | Agent-specific system instructions, tone, and domain framing |
| Retrieval policy | Defaults for intent routing and hybrid weighting between SQL and semantic retrieval |
| Answer template | Structured response format for recurring, structured question types (§11) |
| Security scope | Access-control tier and permitted user groups |
| Model settings | Model choice and generation parameters |
| Clarification policy | When and how the agent asks follow-up questions before proceeding |
| Escalation policy | Conditions and routing for SME escalation (§12) |

### Why this matters

Configuration is what allows one backend to serve six materially different agents. Adding a seventh agent, or changing what LE Grooming is allowed to see, should be a configuration change — not a code change or a new deployment. This keeps the platform maintainable and keeps the six agents behaviorally consistent in how they handle retrieval, evidence, and trust, even though their content domains differ.

---

## 5. Data Ingestion Pipeline

### 5.1 Ingestion Architecture Diagram

```
        Source Files (PDF, PPT/PPTX, DOCX, HTML, Excel)
                          |
                          v
                 File Type Detection
                          |
              +-----------+-----------+
              |                       |
              v                       v
      Docling Extraction      Excel Structural Analyzer
   (PDF / PPT / DOCX / HTML)   (workbook / sheet / region analysis;
                                classifies STRUCTURED / DOCUMENT_LIKE / MIXED)
              |                       |
              +-----------+-----------+
                          |
                          v
                Canonical Master JSON
             (shared intermediate representation)
                          |
        +--------+--------+--------+--------+
        |        |                 |        |
        v        v                 v        v
    Raw File   SQL              Semantic   Metadata
    Storage  Materialization   Chunking/   Catalog
   (original  (structured      Embedding   (registers document,
    file)     regions/rows)   (semantic    tables, chunks,
                                content)     provenance)
```

**Why canonical JSON sits in the middle:** it is the single normalization point between two very different extraction paths (document parsing vs. spreadsheet analysis) and four different downstream consumers (raw storage, SQL, semantic index, catalog). Without it, each downstream consumer would need its own per-file-type handling. With it, extraction logic stays isolated to the parser/analyzer layer, and everything downstream works off one consistent representation — which is also what makes cross-file-type consistency and traceability practical.

### 5.2 High-level flow

```
source intake
   -> agent assignment + source metadata
   -> raw file storage
   -> file type detection
   -> parser/analyzer selection
   -> content extraction
   -> canonical master JSON creation
   -> branching by content type
        -> chunking strategy
        -> embedding/indexing
        -> structured SQL materialization
   -> catalog update
   -> ingestion validation
   -> version publish
```

### 5.3 Step-by-step breakdown

1. **Source intake** — A file enters the system (upload, sync, or bulk load).
2. **Agent assignment + source metadata** — The file is tagged with `agent_id`, ownership, access scope, and any known source metadata (origin system, business unit, confidentiality level).
3. **Raw file storage** — The original file is stored unmodified, as the durable source of truth for re-processing and audit.
4. **File type detection** — The system determines the actual file type (PDF, PPT/PPTX, DOCX, HTML, Excel).
5. **Parser/analyzer selection** — Based on type, the correct extraction path is chosen: Docling-based document extraction, or the Excel structural analyzer.
6. **Content extraction** — The chosen parser extracts content while preserving structure (see 5.4 and 5.5 below).
7. **Canonical master JSON creation** — Extracted content is normalized into a shared intermediate representation, common across all file types (see 5.6).
8. **Branching by content type** — From the canonical JSON, content is routed to one or both downstream paths depending on whether it is structured, semantic, or mixed.
9. **Chunking strategy** — Semantic/document-like content is chunked in a structure-aware way (see 5.7).
10. **Embedding/indexing** — Chunks are embedded and written to the vector/keyword index (see 5.8).
11. **Structured SQL materialization** — Structured content (tables, structured Excel regions) is materialized into queryable SQL tables.
12. **Catalog update** — The shared data catalog is updated with the new/updated document, its tables, schemas, and locations, so downstream query planning knows what exists (see §7).
13. **Ingestion validation** — Automated checks confirm extraction completeness, schema consistency, and absence of critical failures before publishing.
14. **Version publish** — The processed content becomes the active, queryable version, with prior versions retained for traceability.

### 5.4 Document extraction strategy (PDF / PPT / DOCX / HTML)

- Use **Docling** for parsing and extraction across these formats.
- Preserve structural elements during extraction: headings and section hierarchy, paragraphs, bullet lists, tables, image references, page/slide numbers, and document metadata.
- Convert extracted content into the canonical master JSON — **do not flatten directly into plain text**. Flattening at extraction time destroys the structural information needed for later structure-aware chunking, provenance, and citation.

### 5.5 Excel extraction strategy

Excel requires a dedicated, hybrid approach rather than direct text extraction:

- **Analyze before transforming.** Inspect workbook, sheet, used-range, and region layout first.
- **Classify sheets/regions** into one of:
  - `STRUCTURED` — tabular data suited to SQL (part lists, comparison tables, specs).
  - `DOCUMENT_LIKE` — narrative content (notes, commentary, free-text sheets).
  - `MIXED` — sheets containing both, requiring region-level splitting.
- **Detect structural features**: tables, notes, comments, titles, merged headers, repeated headers, totals, formulas, and metadata.
- Convert the workbook into the same **canonical master JSON** used for documents.
- **Route by classification**:
  - Structured parts → SQL materialization.
  - Semantic/narrative parts → semantic retrieval (chunking + embedding).
- **Secondary retrieval aid**: structured rows may additionally generate row-level semantic representations, so that natural-language queries can still surface relevant rows even though SQL remains the primary access path for structured data.

### 5.6 Canonical master JSON — principles

The canonical master JSON is the shared intermediate representation between raw extraction and all downstream pipelines.

- **Intermediate, not a serving store.** It is not queried directly by end users; it feeds SQL materialization and chunking/embedding.
- **Shared across file types.** Documents and Excel both normalize into the same representation, so downstream logic (chunking, cataloging, validation) does not need per-file-type branches.
- **Rich enough to preserve** hierarchy (sections, tables, slides), provenance (source file, location within file), and metadata (page/slide/sheet numbers, timestamps, ownership).
- **Flexible enough to support both** structured (SQL-bound) and semantic (retrieval-bound) downstream paths from the same source document.

### 5.7 Chunking strategy

**For documents (PDF/PPT/DOCX/HTML):**

- Use **structure-aware chunking**, not fixed-length or naive character splitting.
- Chunk by section, subsection, paragraph group, table, or slide block.
- Apply light overlap only where it aids continuity.
- Maintain **parent-child relationships** (e.g., chunk → parent section → parent document) so retrieved chunks can be expanded to fuller context at query time.

**For Excel:**

- Use **row-level semantic chunks** for structured rows where semantic lookup is genuinely useful.
- Use **text-block chunks** for notes, comments, and narrative regions.
- Preserve **table-level parent context** so a row-level chunk can be traced back to its full table.
- Prefer **parent-child linkage over naive overlap** — Excel content is tabular, not prose, so overlap-based windowing is less meaningful than structural linkage.
- **Do not treat embeddings as the primary truth for structured Excel.** SQL remains authoritative for structured data; embeddings are a secondary access aid.

### 5.8 Embedding strategy

Embeddings are created for:

- Document semantic chunks
- Excel narrative chunks
- Row-level semantic representations from structured Excel
- Optional table summaries (where a compact table-level description aids retrieval)

Each embedded unit carries metadata for filtering, traceability, and access control:

- `agent_id`
- `document_id`
- source file
- source type
- sheet / page / slide reference
- section / table / row ID
- version
- security/access scope

### 5.9 Design rationale

- **Docling for document parsing** — it extracts structure (headings, tables, layout) rather than producing flat text, which is what structure-aware chunking and citation depend on.
- **Dual structured + semantic treatment for Excel** — spreadsheets mix precise tabular data with free-text commentary; collapsing both into plain-text chunks would lose the determinism SQL provides for the tabular part and would misrepresent the narrative part as if it were tabular.
- **Canonical JSON as a shared intermediate** — it decouples "how do we extract this file type" from "how do we chunk/index/materialize this content," so the number of format-specific code paths stays at the extraction layer only, not throughout the pipeline.
- **Chunking after structure preservation, not before** — chunking decisions (section boundaries, table boundaries, parent-child links) are only meaningful if structure is still intact; chunking directly off raw extraction would force chunking to guess at structure that extraction already knew.
- **SQL as primary truth for structured Excel** — structured data (counts, comparisons, lookups) requires deterministic, exact answers; vector similarity is approximate by nature and is not an appropriate source of truth for numeric or categorical facts.

---

## 6. Storage Architecture

Each storage layer has a distinct role. Structured truth lives in SQL; semantic retrieval lives in the vector/keyword index; raw storage exists purely for traceability and reprocessing — no layer is a substitute for another.

| Layer | Stores | Why it exists | Used by |
|---|---|---|---|
| **Raw file storage** | Unmodified original source files | Durable source of truth; enables reprocessing if extraction logic changes, and supports audit back to the original document | Ingestion (source for extraction); audit/traceability lookups |
| **Canonical JSON intermediate** | Normalized structural representation of each source (§5.6) | Decouples extraction from downstream consumption; simplifies reprocessing when chunking/materialization logic changes | Ingestion pipeline only. Whether it is persisted long-term or treated as a transient pipeline artifact is an implementation choice — retaining it makes reprocessing cheaper at the cost of extra storage |
| **PostgreSQL / structured store** | Materialized tables from structured documents and structured Excel regions/rows | Authoritative, deterministic source for structured facts (counts, lookups, comparisons) | Query pipeline `DATA`/`HYBRID` execution (§8.7); safe SQL flow (§9) |
| **Vector index / semantic store** | Embedded document chunks, Excel narrative chunks, row-level semantic representations, optional table summaries | Enables semantic and keyword retrieval over document-like and narrative content | Query pipeline `KNOWLEDGE`/`HYBRID` execution (§8.7) |
| **Metadata / catalog store** | Document/dataset registry, schemas, provenance, versions, security scope (§7) | Lets the query pipeline discover *what exists and where* before running retrieval or SQL | Source discovery (§8.5); schema narrowing (§9) |
| **Session / chat history store** | Conversation turns per user/session | Supports follow-up questions and continuity within a session | Session context load (§8.3, step 2) |
| **Audit/logging store** | Request traces, retrieval logs, SQL logs, answer provenance, feedback (§13) | Supports debugging, compliance, and production operability | Observability (§13); SME escalation review (§12) |

**Summary of truth boundaries:**

- Structured Excel and document-table truth lives primarily in **PostgreSQL**.
- Semantic/narrative retrieval lives in the **vector/keyword index**.
- **Raw storage** exists for traceability and reprocessing, not for direct querying.

---

## 7. Metadata and Data Catalog

The catalog is the platform's registry of *what exists, where it lives, and under what constraints* — it is what makes source discovery (§8.5) and schema narrowing (§9) possible without scanning every source on every query.

### What the catalog stores

- Document registry (one entry per ingested source, with `agent_id` and ownership)
- Source versions (active vs. superseded, tied to ingestion's version-publish step, §5.3)
- Datasets/tables produced by SQL materialization
- Columns and their inferred semantics (e.g., which column is a supplier name vs. a part number)
- Sheet/page/slide provenance, linking catalog entries back to their location in the source file
- Security/access scope for each entry
- Active/inactive version flags
- Relationships or hints between datasets (e.g., two tables sharing a supplier-ID join key)
- References to associated semantic assets (which chunks/embeddings correspond to this document or table)

### How the query pipeline uses the catalog

- **Source discovery (§8.5)** — narrows the search space to datasets/documents actually relevant to the query, within the requesting agent's scope.
- **Schema narrowing (§9)** — surfaces only the relevant tables/columns for SQL planning, instead of exposing the full database.
- **Identifying queryable structured datasets** — determines which parts of a request can be answered deterministically via SQL versus which require semantic retrieval.
- **Routing hybrid queries** — informs the typed query planner (§8.6) which sources should service the structured subquery and which should service the semantic subquery.

---

## 8. Query Pipeline

### 8.1 Query Architecture Diagram

```
                    Chat UI
                       |
                       v
          API Gateway / AuthN-AuthZ  (agent scope)
                       |
                       v
        Session Context Load  <----  Session Store / Chat History
                       |
                       v
   Intent Router (GENERAL / KNOWLEDGE / DATA / HYBRID / CLARIFICATION_REQUIRED)
                       |
                       v
        Source Discovery  <----  Metadata / Data Catalog
                       |
                       v
   Typed Query Planner (structured + semantic subqueries)
                       |
                       v
              Execution Layer
        +-------------+-------------+-------------+
        |             |             |             |
        v             v             v             v
   SQL Store    Vector Index /  Catalog       Session/History
 (PostgreSQL)  Semantic Store  (schema/table    Context
                                 lookups)      (for follow-ups)
        |             |             |             |
        +-------------+-------------+-------------+
                       |
                       v
                Rerank / Fuse
                       |
                       v
              Evidence Validation
     (relevance, sufficiency, source approval, consistency)
                       |
              (insufficient) ------> Corrective Retrieval ---> loops back to
                       |                                       Planner / Execution
                       v
               Context Compression
                       |
                       v
               Answer Generation
                       |
                       v
                Source Labeling
                       |
                       v
               Feedback Capture  ----> SME Escalation Loop (§12)
```

### 8.2 High-level flow

```
user query
   -> AuthN/AuthZ + agent scope
   -> session context load
   -> intent classification
   -> clarification if needed
   -> source discovery
   -> typed query planning
   -> query rewriting
   -> retrieval / SQL execution
   -> parent-context expansion
   -> rerank / fuse
   -> evidence validation
   -> corrective retrieval if needed
   -> context compression
   -> answer generation
   -> source labeling
   -> feedback capture
```

### 8.3 Step-by-step breakdown

1. **AuthN/AuthZ + agent scope** — Authenticate the user and resolve which agent (`agent_id`) and source scope they are permitted to query.
2. **Session context load** — Load prior conversation turns so the query can be interpreted in context (e.g., follow-up questions, pronouns referring to earlier entities).
3. **Intent classification** — Classify the query into one of the five intent categories (see 8.4).
4. **Clarification if needed** — If the query is ambiguous or missing required information, ask the user before proceeding rather than guessing.
5. **Source discovery** — Within the agent's allowed scope, identify which datasets/documents are actually relevant to this query (see 8.5).
6. **Typed query planning** — Decompose the query into typed subqueries (structured, semantic, or both) and determine execution order (see 8.6).
7. **Query rewriting** — Rewrite the query/subqueries for retrieval effectiveness (e.g., normalizing terminology, expanding abbreviations, phrasing for keyword/vector search).
8. **Retrieval / SQL execution** — Execute the planned subqueries against SQL, keyword/vector retrieval, or both (see 8.7; SQL execution specifically follows the safe flow in §9).
9. **Parent-context expansion** — Expand retrieved small units to their fuller parent context before synthesis (see 8.8).
10. **Rerank / fuse** — Combine and rank results across retrieval paths (see 8.9).
11. **Evidence validation** — Explicitly check whether the retrieved evidence actually answers the query (see 8.10) — this is distinct from and follows reranking.
12. **Corrective retrieval if needed** — If evidence is judged insufficient, diagnose why and take a targeted corrective action (see 8.11).
13. **Context compression** — Reduce the validated evidence to what is necessary for generation, cutting noise (see 8.12).
14. **Answer generation** — Generate the answer grounded in the compressed, validated evidence.
15. **Source labeling** — Label the answer's claims by their evidentiary basis (see 8.13).
16. **Feedback capture** — Capture user feedback on the answer to support ongoing quality monitoring and improvement (feeds into §12).

### 8.4 Intent taxonomy

| Intent | Meaning |
|---|---|
| `GENERAL` | Small talk or non-retrieval response — no source lookup required |
| `KNOWLEDGE` | Document/semantic retrieval question |
| `DATA` | Structured/deterministic query (SQL-backed) |
| `HYBRID` | Requires combining structured and semantic retrieval |
| `CLARIFICATION_REQUIRED` | Missing information; user must be asked before the system proceeds |

### 8.5 Source discovery

Before running broad retrieval, the system identifies which datasets/documents within the current agent's scope are actually relevant to the query, using the catalog (§7). This avoids blindly searching every source the agent has access to, which would waste retrieval effort and increase the chance of pulling in irrelevant evidence.

### 8.6 Typed query planning

Complex questions are decomposed into typed subqueries — e.g., a structured subquery ("list suppliers meeting spec X") and a semantic subquery ("what are the known risks with supplier X"). Subqueries without dependencies between them may execute in parallel; dependent subqueries execute in sequence.

### 8.7 Retrieval strategy by intent

| Intent | Strategy |
|---|---|
| `GENERAL` | Direct/canned response or controlled LLM answer (no retrieval) |
| `KNOWLEDGE` | Keyword + vector retrieval |
| `DATA` | SQL-first (see the safe SQL flow, §9) |
| `HYBRID` | SQL and semantic retrieval executed in parallel, then combined |
| `CLARIFICATION_REQUIRED` | Follow-up question to the user |

### 8.8 Parent-context expansion

Retrieval first operates on small units (chunks, rows) for precision, then expands matched results to their parent section, page, or table context before the answer is synthesized. This keeps retrieval precise while ensuring the generation step has enough surrounding context to produce a coherent, correctly-scoped answer.

### 8.9 Reranking / fusion

Results from multiple retrieval paths are combined and ranked using techniques such as **Reciprocal Rank Fusion (RRF)** or similar. Reranking determines relative ordering of candidates — it is **not** a judgment of whether the evidence is actually sufficient or correct to answer the query. That judgment is deliberately a separate, later step (8.10).

### 8.10 Evidence validation

This is a distinct and critical stage: after ranking, the system must explicitly validate whether the retrieved evidence **actually answers the user's query**, checking:

- **Relevance** — does the evidence pertain to what was asked?
- **Sufficiency** — is there enough evidence to answer completely?
- **Source approval** — is the evidence from an approved/authorized source for this agent?
- **Consistency/conflicts** — do multiple pieces of evidence agree, or is there contradiction that needs to be surfaced or resolved?

This is the correct point in the pipeline to decide whether the system has enough to answer accurately — reranking alone (8.9) does not make that determination.

### 8.11 Corrective retrieval

When evidence validation fails, the response is not a blind retry. The failure is first categorized by likely cause, then addressed with a targeted correction:

| Likely failure reason | Corrective action |
|---|---|
| Insufficient recall | Candidate expansion |
| Low relevance | Query rewrite |
| Wrong source | Source switch |
| Missing parameter/entity | Clarification request |
| Structured query required | Structured-query routing (route to SQL) |
| Multi-hop required | Decomposition refinement |
| Conflicting evidence | Clarification request or explicit conflict surfacing |

If corrective retrieval is exhausted without producing sufficient evidence, the query is a candidate for SME escalation (§12).

### 8.12 Context compression

After retrieval, expansion, reranking, and validation, the surviving evidence is compressed to remove noise before it is passed to generation. Compression is intentionally placed **late** in the pipeline — compressing earlier risks discarding evidence before it has been properly validated or expanded.

### 8.13 Source labeling and trust

Generated answers must clearly distinguish between:

- **Direct source facts** — statements taken directly from retrieved, approved content.
- **Summaries synthesized** from retrieved internal source content.
- **Not found in approved source / general knowledge boundary** — explicitly flagged when the system is not answering from approved internal sources.

This labeling is grounded in the evidence and citation model (§10).

### 8.14 Design rationale

- **Intent classification before retrieval** — routing `DATA` questions to SQL and `KNOWLEDGE` questions to retrieval (rather than running everything through one generic retrieval path) is what makes structured answers deterministic and semantic answers well-grounded.
- **Source discovery before broad retrieval** — scoping to relevant sources first keeps retrieval precise and avoids diluting results with irrelevant matches from elsewhere in the agent's corpus.
- **Separating reranking from evidence validation** — ranking answers "what's most similar," while validation answers "is this actually sufficient and correct." Conflating the two risks presenting a top-ranked-but-wrong answer as if it were verified.
- **Categorized corrective retrieval over blind retry** — a retry without diagnosis is likely to reproduce the same failure; categorizing the failure reason lets the system take an action actually suited to the problem (e.g., routing to SQL instead of trying yet another semantic rewrite).
- **Late context compression** — compressing before validation risks discarding the very evidence needed to validate or correct the retrieval.

---

## 9. Safe Structured Query Flow

Structured data access must be deterministic *and* constrained — the system should never expose unconstrained SQL access, and the model should never see more schema than it needs.

### Flow

```
user query
   -> catalog-based dataset discovery
   -> schema narrowing
   -> SQL planning/generation
   -> SQL validation
   -> read-only execution
   -> result validation
   -> answer synthesis
```

- **Catalog-based dataset discovery** — the catalog (§7) identifies which tables are relevant to the query, within the agent's allowed scope.
- **Schema narrowing** — only the relevant tables/columns are surfaced for planning; the model is not given visibility into the full database.
- **SQL planning/generation** — a query is planned/generated against the narrowed schema.
- **SQL validation** — the generated query is checked against the safety controls below before execution.
- **Read-only execution** — the validated query runs against the structured store.
- **Result validation** — results are checked for shape/size sanity before being handed to answer synthesis.
- **Answer synthesis** — the validated result set is used to generate the answer, consistent with the query pipeline's generation step (§8.3, step 14).

### Safety controls

- **`SELECT` only** — no data-modifying statements are permitted.
- **Approved schemas/tables only** — queries are constrained to the agent's allowed dataset scope.
- **No DDL/DML** — schema and data modification are categorically disallowed.
- **Query timeout** — long-running queries are terminated.
- **Row limits / result-size limits** — bounded result sets, to keep answers reviewable and prevent runaway responses.
- **Explicit handling for "return all matching rows"** — requests for exhaustive results are handled deliberately (e.g., paginated or summarized) rather than left to an unbounded query.
- **Parameterization where possible** — reduces injection risk and improves query predictability.
- **Logging/auditability** — every generated and executed query is logged (§13).

### Why this matters

SQL access in this system is a controlled capability, not an open door: the LLM only ever sees the schema relevant to the query (via schema narrowing), and every generated query passes through validation before it can touch the structured store. This keeps `DATA` and `HYBRID` answers deterministic and safe, and keeps the structured store trustworthy as the authoritative source described in §5.9 and §6.

---

## 10. Evidence and Citation Model

To support the source labeling described in §8.13, every piece of retrieved evidence used by the system is represented consistently, regardless of whether it came from SQL or semantic retrieval. An evidence item conceptually carries:

- **Source file** — which document or workbook it came from
- **Source type** — document, structured Excel, Excel narrative, etc.
- **Provenance** — page/slide/sheet/table/row reference locating it within the source
- **Confidence/score** — the retrieval or match confidence associated with this evidence
- **Version** — which version of the source this evidence reflects
- **Evidentiary role** — whether this is a source-derived fact (used directly) or contributes to a synthesized summary

### Why this matters

A consistent evidence representation is what makes traceability, source labeling (§8.13), debugging, and trust possible in practice. If evidence used during retrieval and generation cannot be traced back to a specific source file, location, and version, the system cannot reliably tell a user where an answer came from — which undermines the core design principle in §1 that trust and traceability come before generative polish.

---

## 11. UAT-Driven Requirements

User Acceptance Testing (UAT) feedback from the LE (Lead Engineer) agent surfaced concrete gaps that directly shaped this architecture.

| UAT concern | Architectural requirement |
|---|---|
| Keyword retrieval was too broad (e.g., `PR AIMS` matched generic `AIMS`) | Phrase-aware / exact-aware retrieval |
| Irrelevant backend/source fields were exposed in answers | Response projection layer (control what internal fields surface to the user) |
| Source origin was unclear | Source provenance labeling (§8.13, §10) |
| Long queries retrieved poor references | Semantic reranking and typed query planning (§8.6, §8.9) |
| Repeated questions produced inconsistent outputs | Standardized response templates for structured question types |
| Chat history/session continuity was missing | Session-based history retention (§8.3, step 2) |
| Users wanted clarity on privacy/security and external exposure | Explicit internal-source trust boundary and data-handling clarity |

These items are not incidental UX polish — they are the direct source of several structural decisions in this design, including evidence validation, source labeling, and typed query planning.

---

## 12. SME Escalation and Knowledge Improvement Loop

Retrieval and validation will not resolve every query automatically. When they don't, the system escalates to a subject-matter expert (SME) rather than guessing.

### When escalation is triggered

- Corrective retrieval (§8.11) is exhausted without producing sufficient validated evidence.
- Evidence validation (§8.10) repeatedly detects conflicting evidence that the system cannot resolve on its own.
- A low-confidence answer is about to be returned for a query type flagged as high-stakes by agent configuration (§4, escalation policy).

### What happens during escalation

- The query, the retrieved evidence, and the system's attempted answer (if any) are flagged and routed to an SME review queue.
- The SME reviews the evidence trail — using the same evidence/citation model (§10) the system itself relies on — and either confirms, corrects, or supplies the missing answer.

### How corrections feed back in

- Accepted SME corrections are **not** patched directly into the live index. They are captured as knowledge updates (e.g., a corrected document, an added clarification, a catalog annotation) and pushed back through the normal ingestion pipeline (§5), so they go through the same extraction, validation, and versioning as any other source content.
- This preserves traceability — a correction is itself a versioned, provenance-tracked piece of content, not a silent runtime patch — and prevents uncontrolled drift in system behavior between review cycles.

### Why this matters

This loop is what lets the system improve over time without compromising the trust and traceability principles in §1: every improvement to the knowledge base is itself sourced, versioned, and auditable, the same as any other ingested content.

---

## 13. Observability and Auditability

Production suitability depends on being able to see what the system did and why, not just what it answered.

### What is logged

- **Request tracing** — end-to-end trace of a query through the pipeline (§8).
- **Retrieval logs** — what was searched, what was returned, at what rank/score.
- **Source access logs** — which sources/datasets were accessed, by which agent and user.
- **SQL audit logs** — generated queries, validation outcomes, and execution results (§9).
- **Answer provenance logs** — which evidence items (§10) backed a given answer.
- **Feedback logs** — user feedback captured at the end of the query pipeline (§8.3, step 16).
- **Ingestion warnings/failures** — extraction issues, validation failures, and classification anomalies encountered during ingestion (§5).

### Why this matters

- **Debugging retrieval quality** — when an answer is wrong, logs make it possible to determine whether the failure was in discovery, retrieval, ranking, validation, or generation.
- **Enterprise trust** — users and stakeholders can verify how an answer was produced, not just accept it on faith.
- **Production operations** — logs are the basis for monitoring system health and catching regressions after changes.
- **Compliance/security review** — auditable access and query logs are typically a prerequisite for approving an internal system that touches engineering and supplier data.

---

## 14. What Was Rejected or Deferred

The following approaches were considered but are **not** the Phase-1 path:

- **Treating all Excel as plain-text chunks only.** Rejected because it discards the determinism structured data needs; Excel is handled as a hybrid source instead (§5.5).
- **Relying on embeddings as the main truth source for structured data.** Rejected because vector similarity is approximate, and structured/numeric facts require deterministic lookup — SQL remains authoritative (§5.7, §5.9, §6).
- **Starting with Graph RAG as the foundation.** Deferred — not rejected outright. Graph-based reasoning over entity relationships may be valuable later (e.g., supplier-part-standard relationships), but is not required for a Phase-1 baseline and adds significant complexity.
- **Building a chunkless-RAG-only architecture for Phase 1.** Deferred. Chunkless retrieval concepts are useful to keep in mind, but the chosen design is a **structure-preserving hybrid retrieval** approach, not a chunkless-only one — structure-aware chunking with parent-child linkage is a better fit for the mixed structured/document corpus described here.
- **Blindly chunking everything before preserving structure.** Rejected — this is the reason canonical master JSON creation happens before chunking (§5.6, §5.9): chunking off raw/flattened text loses information chunking itself cannot recover.

---

## 15. Phased Implementation Roadmap

Phase 1 should focus on building a **reliable baseline**, not an experimental advanced-RAG showcase. The remaining sophistication described in this document is intentionally sequenced into later phases rather than attempted all at once.

| Phase | Focus | Key deliverables |
|---|---|---|
| **Phase 1** | Foundation | Canonical ingestion (Docling + Excel structural analysis), Excel hybrid classification, data catalog, basic intent-based routing |
| **Phase 2** | Core retrieval | Safe structured query flow (§9), semantic retrieval, parent-child (structure-aware) retrieval |
| **Phase 3** | Answer quality | Evidence validation (§8.10), corrective retrieval (§8.11), standardized response templates |
| **Phase 4** | Production hardening | SME escalation and feedback loop (§12), observability/auditability (§13), UAT-driven refinements (§11) |
| **Phase 5** | Optional enrichment | Graph-based entity relationships, further retrieval-quality improvements, as justified by production usage |

**The first milestone is a trustworthy, production-suitable baseline system — accurate, traceable, and consistent — not a showcase of advanced retrieval techniques.** Later phases build on that baseline once it is proven reliable across all six agents, rather than adding sophistication before the fundamentals are solid.
