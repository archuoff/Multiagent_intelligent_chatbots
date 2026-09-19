# JLR Multi-Agent System

## Clean Discussion Notes

**Status:** Working architecture notes based on the design discussions so far.  
**Purpose:** Capture the agreed direction for the Phase 1 backend before implementation.

---

## 1. Core System Direction

This system is an internal JLR multi-agent platform with 6 frontend agents:

- Lead Engineer Grooming
- Benchmarking
- ADAS Packaging Study
- Supplier Database
- EDS
- Material Database / New Edge Technologies

These should **not** become 6 separate backend implementations.

The backend should be:

- shared
- configuration-driven
- agent-isolated through `agent_id`, source scope, authorization, and response policy

The main architecture is built around **two pipelines**:

1. `Data ingestion pipeline`
2. `Query pipeline`

### 1.1 Overall architecture diagram

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│                           Frontend Agent Layer                              │
│  LE Grooming | Benchmarking | ADAS | Supplier DB | EDS | Material DB       │
│  Each agent has its own chat UX, but no separate backend implementation.   │
└──────────────────────────────────────────────────────────────────────────────┘
                                       |
                                       v
┌──────────────────────────────────────────────────────────────────────────────┐
│                            Access and API Layer                             │
│  Chat UI -> API Gateway -> AuthN -> AuthZ -> Agent Scope Resolution        │
│  Resolves: user identity, allowed agent_id, source scope, session_id       │
└──────────────────────────────────────────────────────────────────────────────┘
                                       |
                                       v
┌──────────────────────────────────────────────────────────────────────────────┐
│                        Shared Orchestration Layer                            │
│  Session Load | Context Resolution | Intent Router | Source Discovery       │
│  Typed Planning | Query Rewrite | Execution Coordination                    │
└──────────────────────────────────────────────────────────────────────────────┘
                    |                          |                         |
                    |                          |                         |
                    v                          v                         v
┌──────────────────────────┐   ┌──────────────────────────┐   ┌──────────────────────┐
│ Agent Configuration      │   │ Session and Memory       │   │ Query Pipeline        │
│ Registry                 │   │ Store                    │   │ Runtime execution     │
│ agent_id policies        │   │ chat history             │   │ over approved data    │
│ prompts/templates        │   │ summaries                │   │ sources               │
│ source permissions       │   │ follow-up context        │   │                      │
└──────────────────────────┘   └──────────────────────────┘   └──────────────────────┘
                                                                  |
                                                                  v
┌──────────────────────────────────────────────────────────────────────────────┐
│                             Shared Data Layer                               │
│  PostgreSQL / Structured Store  |  Vector Store  |  Data Catalog           │
│  Deterministic facts            |  Semantic RAG  |  Source/schema registry │
└──────────────────────────────────────────────────────────────────────────────┘
                                       ^
                                       |
┌──────────────────────────────────────────────────────────────────────────────┐
│                            Ingestion Pipeline                               │
│  Source Intake -> Parsing/Analysis -> Canonical JSON -> Branching          │
│  -> SQL Materialization -> Chunking/Embeddings -> Catalog Update           │
└──────────────────────────────────────────────────────────────────────────────┘
                                       ^
                                       |
┌──────────────────────────────────────────────────────────────────────────────┐
│                               Source Layer                                  │
│  Excel | PDF | PPT/PPTX | DOCX | HTML | Future approved enterprise types   │
└──────────────────────────────────────────────────────────────────────────────┘
                                       |
                                       v
┌──────────────────────────────────────────────────────────────────────────────┐
│                     Feedback and Improvement Loop                            │
│  User feedback -> SME review -> approved source update -> re-ingestion     │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. High-Level Agent Character

The agents differ mainly by source mix and query style:

| Agent | Main nature |
|---|---|
| LE Grooming | Mostly knowledge/process guidance |
| Benchmarking | Mostly structured comparison and analysis |
| ADAS | Strong hybrid of structured specs + engineering documents |
| Supplier Database | Mostly structured lookup |
| EDS | Knowledge-heavy reasoning with references and lessons learned |
| Material Database | Hybrid of structured material data + technical documents |

This confirms that the backend must support:

- structured querying
- semantic retrieval
- hybrid answering

---

## 3. Key Design Principles

- Retrieval accuracy is more important than fluent but weak answers.
- Structured data and unstructured knowledge must be treated differently.
- Excel must not be treated as only plain text for embeddings.
- Traceability and source-grounding are mandatory.
- The system should be practical for Phase 1, not over-engineered.
- Advanced techniques like Graph RAG can be added later, but should not be the initial foundation.

---

## 4. Finalized Data Ingestion Direction

### 4.1 Ingestion goal

Convert raw files into a reliable internal representation that can support:

- SQL querying for structured data
- semantic retrieval for document-like content
- shared metadata/catalog lookup
- provenance and auditability

### 4.2 Ingestion architecture diagram

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│                              Source Intake                                  │
│  Upload | Sync | Batch import | agent_id assignment | source metadata      │
│  version tag | ownership | confidentiality scope                           │
└──────────────────────────────────────────────────────────────────────────────┘
                                       |
                                       v
┌──────────────────────────────────────────────────────────────────────────────┐
│                         Intake Control Layer                                │
│  Raw file storage | file hash | type detection | ingestion job tracking    │
└──────────────────────────────────────────────────────────────────────────────┘
                                       |
                    +------------------+------------------+
                    |                                     |
                    v                                     v
┌───────────────────────────────┐            ┌────────────────────────────────┐
│ Docling Extraction Path       │            │ Excel Analysis Path            │
│ PDF / PPT / DOCX / HTML       │            │ workbook -> sheet -> region    │
│ headings, tables, pages,      │            │ classify STRUCTURED /          │
│ sections, metadata            │            │ DOCUMENT_LIKE / MIXED          │
└───────────────────────────────┘            └────────────────────────────────┘
                    |                                     |
                    +------------------+------------------+
                                       |
                                       v
┌──────────────────────────────────────────────────────────────────────────────┐
│                      Canonical Representation Layer                         │
│  Canonical Master JSON                                                     │
│  Preserves structure, provenance, source location, metadata, version       │
└──────────────────────────────────────────────────────────────────────────────┘
              |                           |                           |
              |                           |                           |
              v                           v                           v
┌──────────────────────┐      ┌────────────────────────┐   ┌──────────────────────┐
│ Structured Branch    │      │ Semantic Branch        │   │ Catalog Branch       │
│ table/row extraction │      │ structure-aware        │   │ register document,   │
│ schema inference     │      │ chunking               │   │ tables, chunks,      │
│ SQL materialization  │      │ embeddings             │   │ provenance, version  │
└──────────────────────┘      └────────────────────────┘   └──────────────────────┘
              |                           |                           |
              +-------------+-------------+-------------+-------------+
                                            |
                                            v
┌──────────────────────────────────────────────────────────────────────────────┐
│                               Storage Layer                                 │
│  Raw File Store | PostgreSQL | Vector Store | Catalog Store                │
└──────────────────────────────────────────────────────────────────────────────┘
                                            |
                                            v
┌──────────────────────────────────────────────────────────────────────────────┐
│                        Validation and Publish Layer                          │
│  ingestion checks | warnings | version activation | reprocessing support    │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 4.3 Ingestion flow in step-by-step pipeline form

```text
Source file is added to the system
        |
        v

1. Source Intake - file arrives through upload, sync, or batch import
   and is tagged with `agent_id`, owner, version metadata, and source scope
        |
        v
2. Raw File Storage - preserve the original file unchanged for audit,
   traceability, and future reprocessing
        |
        v
3. File Type Detection - identify whether the source is `xlsx`, `pdf`,
   `ppt/pptx`, `docx`, or `html`
        |
        v
4. Parser / Analyzer Routing - choose the extraction path
        |
        +- document-like file (`pdf` / `ppt` / `pptx` / `docx` / `html`)
        |    -> route to `Docling`
        |
        \- spreadsheet (`xlsx`)
             -> route to `Excel Workbook Analyzer`
                  |
                  v
5. Content Extraction
        |
        +- `Docling` path
        |    -> extract headings, sections, paragraphs, tables,
        |       page/slide metadata, and source structure
        |
        \- `Excel Analyzer` path
             -> inspect workbook, sheets, used ranges, regions,
                tables, notes, comments, formulas, merged headers,
                repeated headers, and totals
                  |
                  v
6. Canonical Master JSON Creation - normalize extracted content into one
   shared intermediate representation with provenance and metadata
                  |
                  v
7. Content Classification / Branching
                  |
                  +- structured content
                  |    -> SQL materialization path
                  |
                  +- semantic/document-like content
                  |    -> chunking + embedding path
                  |
                  \- mixed Excel content
                       -> split and send to both paths
                            |
                            v
8. Chunking Strategy
        |
        +- documents
        |    -> structure-aware chunks by section / paragraph group / table /
        |       slide block, with light overlap where useful
        |
        \- Excel
             -> row-level semantic chunks for structured rows where useful
             -> text-block chunks for notes/comments/narrative regions
             -> preserve parent table context
                  |
                  v
9. Embedding / Indexing - generate embeddings for semantic chunks and store
   them with source metadata and access scope
                  |
                  v
10. Structured SQL Materialization - convert structured Excel/table content
    into SQL-queryable representation
                  |
                  v
11. Catalog Update - register document, dataset, table, column, chunk,
    provenance, version, and access metadata
                  |
                  v
12. Ingestion Validation - verify extraction completeness, structure quality,
    classification confidence, and materialization success
                  |
                  v
13. Version Publish - activate the ingested version for query-time use while
    retaining older versions for audit and rollback support
```

### 4.4 Supported source types

Phase 1 architecture discussion covered:

- `xlsx`
- `pdf`
- `ppt/pptx`
- `docx`
- `html`

Note:
for Phase 1, spreadsheet intake may be standardized to `xlsx` to reduce parser variance.

### 4.5 Extraction strategy

#### For PDF / PPT / DOCX / HTML

Use `Docling` for parsing and extraction.

Extract and preserve:

- headings
- section hierarchy
- paragraphs
- bullet lists
- tables
- image or caption references
- page / slide numbers
- metadata

Do **not** flatten directly into raw text before preserving structure.

#### For Excel

Use a **hybrid** ingestion strategy.

First analyze:

- workbook
- sheets
- used ranges
- regions
- tables
- notes / comments / text areas
- merged cells
- multi-row headers
- repeated headers
- subtotal / total rows
- formulas

Then classify sheet or region as:

- `STRUCTURED`
- `DOCUMENT_LIKE`
- `MIXED`

### 4.6 Canonical master JSON

All extracted content should be normalized into a shared `canonical master JSON`.

This canonical representation is:

- an intermediate model
- not the final serving store
- common across file types
- rich enough to preserve hierarchy, metadata, and provenance

It exists so that downstream logic can branch cleanly into:

- SQL materialization
- semantic chunking
- embedding/indexing
- catalog registration

### 4.7 Excel hybrid branching

Excel should follow both paths where needed.

#### Structured path

Structured tables/regions should be converted into SQL-queryable data.

This is the **primary truth path** for:

- equality filters
- range filters
- count
- sum
- avg
- min/max
- group by
- sort
- joins
- exhaustive row retrieval

#### Semantic path

Excel should also support semantic retrieval by creating:

- row-level semantic representations
- text-block chunks for notes/comments/narrative regions
- optional table summaries

Example idea:

- structured row kept as data
- the same row also rendered into a natural-language semantic text form

Important:
semantic indexing of Excel is a **secondary retrieval aid**, not the authoritative truth for structured questions.

### 4.8 Chunking strategy

#### Documents

Use `structure-aware chunking`.

Chunk by:

- section
- subsection
- paragraph group
- table
- slide topic block

Use:

- light overlap where useful
- parent-child linkage

#### Excel

Use:

- row-level chunks for structured rows when semantic search is useful
- text-block chunks for narrative areas
- parent table context for row chunks

Do not rely on naive overlap as the main Excel context method.

For Excel, `parent-child context` is more important than simple sliding overlap.

### 4.9 Embedding strategy

Create embeddings for:

- document semantic chunks
- Excel narrative chunks
- row-level semantic representations
- optional table summaries

Each embedded unit should carry metadata such as:

- `agent_id`
- `document_id`
- source file
- source type
- page / slide / sheet
- section / table / row id
- version
- security scope

### 4.10 Why this ingestion design was selected

- `Docling` is useful because it preserves document structure.
- Excel needs both SQL and semantic handling.
- Canonical JSON keeps parsing and downstream storage concerns separate.
- Chunking should happen after structure preservation.
- SQL remains the authoritative source for structured spreadsheet questions.

---

## 5. Finalized Query Pipeline Direction

### 5.1 Core goal

Take a user query and produce an answer that is:

- accurate to the question
- grounded in approved sources
- relevant to the active agent
- traceable
- consistent

### 5.2 Query architecture diagram

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│                              Query Entry                                    │
│  User query from active agent chat UI                                       │
└──────────────────────────────────────────────────────────────────────────────┘
                                       |
                                       v
┌──────────────────────────────────────────────────────────────────────────────┐
│                        Access and Context Layer                             │
│  AuthN | AuthZ | agent scope | session load | recent turns                 │
│  conversation summary | context resolution                                 │
└──────────────────────────────────────────────────────────────────────────────┘
                                       |
                                       v
┌──────────────────────────────────────────────────────────────────────────────┐
│                       Routing and Planning Layer                            │
│  Intent classification -> upfront clarification if needed                  │
│  -> source discovery -> typed decomposition -> subquery rewriting          │
└──────────────────────────────────────────────────────────────────────────────┘
                                       |
                     +-----------------+-----------------+
                     |                                   |
                     v                                   v
┌──────────────────────────────┐            ┌──────────────────────────────┐
│ Structured Execution Path    │            │ Semantic Execution Path      │
│ SQL planning                 │            │ keyword retrieval            │
│ SQL validation               │            │ vector retrieval             │
│ read-only SQL execution      │            │ parent-context lookup        │
└──────────────────────────────┘            └──────────────────────────────┘
                     |                                   |
                     +-----------------+-----------------+
                                       |
                                       v
┌──────────────────────────────────────────────────────────────────────────────┐
│                        Evidence Processing Layer                            │
│  parent-context expansion -> rerank/fuse -> evidence validation           │
│  -> corrective retrieval or planner-level clarification                    │
└──────────────────────────────────────────────────────────────────────────────┘
                                       |
                                       v
┌──────────────────────────────────────────────────────────────────────────────┐
│                         Answer Construction Layer                           │
│  context compression -> answer generation -> source labeling               │
└──────────────────────────────────────────────────────────────────────────────┘
                                       |
                                       v
┌──────────────────────────────────────────────────────────────────────────────┐
│                         Feedback and Review Layer                           │
│  user feedback -> quality signal -> SME review trigger if needed          │
└──────────────────────────────────────────────────────────────────────────────┘

Supporting stores used by the query pipeline:

- `Session Store` for session continuity and follow-up understanding
- `Data Catalog` for source discovery and schema narrowing
- `SQL Store` for deterministic structured retrieval
- `Vector Store` for semantic retrieval
```

### 5.3 Query flow in step-by-step pipeline form

```text
User types a query in the Chat UI
        |
        v

1. API Gateway - who are you (AuthN), are you allowed to use this agent (AuthZ),
   and what are the active `agent_id`, `user_id`, and `session_id`
        |
        v
2. Session + Context Load - fetch recent chat turns and a short conversation
   summary so follow-up references can be resolved
        |
        v
3. Intent Classifier / Router - classify the query as:
   `GENERAL` / `KNOWLEDGE` / `DATA` / `HYBRID` / `CLARIFICATION_REQUIRED`
        |
        +- `GENERAL`
        |    -> direct/canned or controlled LLM response
        |    -> done
        |
        +- `CLARIFICATION_REQUIRED` (upfront - not enough information
        |    to route safely)
        |    -> ask follow-up question
        |    -> wait for user reply
        |    -> back to step 2
        |
        \- `KNOWLEDGE` / `DATA` / `HYBRID`
             |
             v
4. Source Discovery - identify relevant sources only inside the current
   agent scope using catalog metadata
             |
             v
5. Typed Decomposition - split into typed subqueries if needed
   (`structured` / `semantic` / `hybrid-supporting`)
             |
             +- missing required parameter at planning stage
             |   (e.g. which OEM? which program? which sensor type?)
             |    -> ask targeted follow-up
             |    -> wait for user reply
             |    -> back to step 2 or step 5 depending on the case
             |
             \- enough to proceed
                  |
                  v
6. Query Rewriting - rewrite each subquery for execution
   - SQL filters for structured path
   - keyword terms for lexical retrieval
   - semantic phrasing for vector retrieval
   - exact/phrase constraints where needed
                  |
                  v
7. Execute Retrieval / Query - for each subquery, search only approved sources
                  |
                  +- 7a. `DATA`
                  |      -> SQL-first over structured Excel-derived tables
                  |      -> apply access scope + filters
                  |      -> return deterministic result rows
                  |
                  +- 7b. `KNOWLEDGE`
                  |      -> keyword search (BM25/FTS)
                  |      -> vector search over semantic chunks
                  |      -> same authorized scope
                  |
                  +- 7c. `HYBRID`
                  |      -> structured SQL + semantic retrieval in parallel
                  |
                  \- parallel across independent subqueries
                           |
                           v
8. Parent-Context Expansion - expand matched rows/chunks to fuller parent context
   (table / section / page / slide)
                           |
                           v
9. Rerank / Fuse - combine lexical + semantic candidates using RRF or
   a similar ranking method
                           |
                           v
10. Evidence Validation - check whether the retrieved evidence actually
    answers the query
                            |
                            +- weak / insufficient / conflicting
                            |    -> identify failure reason:
                            |       - low relevance
                            |       - insufficient recall
                            |       - wrong source
                            |       - missing entity/parameter
                            |       - structured query required
                            |       - multi-hop required
                            |       - conflicting evidence
                            |
                            |    -> corrective action:
                            |       - rewrite query
                            |       - expand candidates
                            |       - switch source
                            |       - decompose further
                            |       - ask clarification
                            |       - route to SQL
                            |
                            |    -> retry from step 4, 5, 6, or 7 as appropriate
                            |
                            |    -> if exhausted, escalate to SME / owner
                            |
                            \- strong
                                 |
                                 v
11. Context Compression - keep only the validated evidence needed for answering
                                 |
                                 v
12. Generate Answer - compose the answer from approved, validated context
                                 |
                                 v
13. Source Labeling - clearly mark:
    - direct source facts
    - summaries synthesized from source
    - not found in approved source
                                 |
                                 v
14. Feedback Capture - show thumbs up/down, log the quality signal,
    and trigger SME review if needed
                                 |
                                 v
User sees the answer
```

### 5.4 Corrected high-level flow

```text
user query
-> AuthN/AuthZ + agent scope
-> session load
-> conversation summary + context resolution
-> intent classification
-> upfront clarification if needed
-> source discovery
-> typed decomposition
-> subquery rewriting
-> retrieval / SQL execution
-> parent-context expansion
-> rerank / fuse
-> evidence validation
-> corrective retrieval or planner-level clarification
-> context compression
-> answer generation
-> source labeling
-> feedback capture
```

### 5.5 Why this is the corrected version

This flow improves a few important points:

- session history should help query understanding, not behave like a retrieval source
- clarification can happen both early and later during planning
- decomposition and rewriting are different steps
- reranking is not the same as validation

### 5.6 Session handling

The query pipeline should use:

- current user query
- short conversation summary
- recent turns
- agent configuration and scope

The router / intent classifier should not blindly consume the entire raw history every time.

Session context should support:

- pronoun resolution
- follow-up questions
- continuity inside one chat session

But new sessions should remain isolated.

### 5.7 Intent taxonomy

The agreed intent classes are:

- `GENERAL`
- `KNOWLEDGE`
- `DATA`
- `HYBRID`
- `CLARIFICATION_REQUIRED`

Meaning:

- `GENERAL`: greeting, small talk, meta interaction
- `KNOWLEDGE`: document/semantic retrieval
- `DATA`: structured deterministic query
- `HYBRID`: structured + semantic combination
- `CLARIFICATION_REQUIRED`: not enough information to proceed safely

### 5.8 Clarification handling

Clarification should happen in two different situations.

#### Intent-level clarification

This happens when the query is too vague to route properly.

Examples:

- no clear topic
- incomplete question
- missing core object or domain

#### Planner-level clarification

This happens after the query is already understood at a high level, but a required parameter is missing.

Examples:

- which OEM?
- which sensor type?
- which program?
- which date range?

This means clarification is not only one step at the start. It can reappear later if planning detects a missing field.

### 5.9 Source discovery

Before broad retrieval, the system should identify relevant sources inside the current agent scope.

This uses the catalog to narrow:

- relevant datasets
- relevant tables
- relevant document sets
- relevant source versions

This is important for retrieval precision.

### 5.10 Typed decomposition

Complex questions should be decomposed into typed subqueries, such as:

- `structured`
- `semantic`
- `hybrid-supporting`

Example:

- structured subquery: components longer than 100 mm that failed validation
- semantic subquery: mitigation guidance for those failures

Independent subqueries may run in parallel.

### 5.11 Query rewriting

Query rewriting should happen at two levels.

#### Query normalization / context resolution

Before planning, resolve:

- pronouns
- abbreviations
- obvious follow-up references

#### Subquery rewriting

After decomposition, rewrite each subquery for its retrieval mode:

- SQL filter planning
- keyword search terms
- vector retrieval phrasing
- exact-match / phrase constraints

### 5.12 Retrieval strategy by intent

| Intent | Main strategy |
|---|---|
| `GENERAL` | direct or controlled LLM answer |
| `KNOWLEDGE` | keyword + vector retrieval |
| `DATA` | SQL-first |
| `HYBRID` | SQL + semantic retrieval in parallel |
| `CLARIFICATION_REQUIRED` | ask follow-up |

### 5.13 Parent-context expansion

Retrieve smaller units first for precision, then expand to parent context:

- parent section
- parent page
- parent slide
- parent table

This improves final answer grounding.

### 5.14 Reranking / fusion

Use fusion and reranking such as `RRF`.

But `RRF` only helps answer:

- what evidence ranks higher

It does **not** answer:

- does this evidence really satisfy the user’s question?

That second question belongs to evidence validation.

### 5.15 Evidence validation

This is the layer that decides whether the retrieved answer is accurate enough for the query.

It should check:

- relevance
- sufficiency
- source approval
- consistency/conflict

This is where the system decides whether it can answer confidently.

### 5.16 Corrective retrieval

If evidence is weak, do not retry blindly.

Diagnose likely failure reason, such as:

- insufficient recall
- low relevance
- wrong source
- missing parameter/entity
- structured query required
- multi-hop required
- conflicting evidence

Then apply targeted correction:

- query rewrite
- candidate expansion
- source switch
- deeper decomposition
- clarification request
- structured routing

### 5.17 Context compression

Context compression should happen late:

- after expansion
- after reranking
- after validation

This prevents useful evidence from being removed too early.

### 5.18 Source labeling

Final answers should clearly distinguish:

- direct source facts
- summaries synthesized from retrieved internal sources
- not found in approved source

This is important for trust.

---

## 6. LE Agent UAT Learnings Converted Into Design

The LE-agent UAT gave strong guidance for the architecture.

Main issues observed:

- keyword retrieval too broad
- irrelevant source fields exposed to users
- unclear source origin
- weak references for longer queries
- inconsistent repeated answers
- no session continuity
- concerns about privacy / external exposure

These translate into design requirements:

- phrase-aware and exact-aware retrieval
- response projection layer
- source provenance labeling
- typed planning and better reranking
- standard answer templates for structured query types
- session-based history
- explicit internal-source trust boundary

---

## 7. Techniques Discussed and Their Role

### Included for Phase 1 direction

- hybrid retrieval
- structure-aware chunking
- parent-child retrieval
- parent-context expansion
- reranking / fusion
- evidence validation
- corrective retrieval
- context compression

### Discussed but not chosen as the foundation

- `Graph RAG`
- `chunkless RAG`

Reason:

These may become useful later, but they should not replace the more practical Phase 1 baseline:

- strong ingestion
- SQL + semantic hybrid retrieval
- source-grounded answer pipeline

---

## 8. Things To Keep Explicit In Future Design Docs

When we refine this further, the docs should clearly separate:

- session context for understanding
- retrieval sources for evidence
- reranking
- evidence validation
- clarification at router stage
- clarification at planner stage

This separation matters because these are often mixed together in vague RAG diagrams.

---

## 9. Practical Phase 1 Recommendation

Phase 1 should focus on building a reliable baseline:

- canonical ingestion
- Excel hybrid analysis
- SQL materialization for structured Excel
- semantic chunking with metadata
- shared catalog
- intent-aware routing
- evidence validation
- grounded and traceable answers

The first goal is not to build the most advanced RAG system.
The first goal is to build one that is:

- accurate
- understandable
- debuggable
- trustworthy
