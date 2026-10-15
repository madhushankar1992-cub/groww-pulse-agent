# Groww Weekly Review Pulse Agent — Problem Statement

**Project:** Review Pulse **Agent** (this repository)
**Companion project:** Pulse **MCP Server** for Google Docs and Gmail (separate repository, with its own problem statement, architecture, and implementation plan)
**Current scope:** Google Play only. Apple App Store is deferred.
**Original brief:** [problemstatement.txt](problemstatement.txt), kept unchanged
**Required inputs and run steps:** [input.md](input.md)
**Related documents:** [Architecture](architecture.md) · [Implementation plan](implementation-plan.md)

---

## 1. Context

Groww ([Stocks, Mutual Fund, IPO on Google Play](https://play.google.com/store/apps/details?id=com.nextbillion.groww&hl=en_IN), package `com.nextbillion.groww`) receives a steady stream of public Google Play reviews. Nobody reads all of them. Product, Support, and Leadership need a **weekly pulse** they can scan in minutes. It answers three questions: what users care about, what they actually said, and what the team should do next.

The original brief describes one end-to-end system. This work splits it into **two independently built projects**:

| Project | Owns | Does not own |
| --- | --- | --- |
| **Pulse Agent** (this project) | Review import, privacy sanitisation, theme analysis, quote selection, pulse composition, validation, orchestration, and run state. It calls MCP tools to publish. | Google OAuth, Google API/REST code, and token storage. It does not implement the MCP server. |
| **Pulse MCP Server** (separate) | Google Docs and Gmail tools exposed over MCP, account connection, credentials, and Google API calls. | Review data, analysis, and pulse content decisions. |

The two projects meet at the **MCP tool contract** (architecture §9), which this project specifies as a consumer. The agent must be fully buildable and testable, up to a validated local preview, without the MCP server existing. Publication works against any server that satisfies the contract.

## 2. Problem

Individual reviews are noisy, repetitive, multilingual, and full of personal detail. A simple script ("fetch, summarise, email") produces output that cannot be trusted:

- **Invented quotes.** A model asked to "give three user quotes" paraphrases or fabricates them.
- **Invented statistics.** A model asked "what are the top themes" guesses frequencies instead of counting them.
- **PII leakage.** Reviewer names, emails, phone numbers, and account identifiers end up in prompts, logs, and the published doc.
- **False success.** A week with four reviews still yields a confident three-theme pulse, and a failed Docs call still reports "done".
- **Duplicate side effects.** A retried run creates a second Google Doc or a second Gmail draft.

The agent's job is to make each of these failure modes **impossible or explicit**. Code, not the model, controls counts, quotes, limits, privacy, and external writes. Every run ends in a declared outcome with a recorded reason.

## 3. Objective

Build an agent that, for a given reporting week:

1. Imports roughly the last **8–12 weeks** of public Groww Google Play reviews.
2. Removes reviewer PII **before** anything is stored, logged, or sent to a model.
3. Groups reviews into **at most 5 themes**.
4. Writes a **one-page weekly note, ≤ 250 words**, with the **top 3 themes**, **3 verbatim user quotes**, and **3 action ideas**.
5. Publishes the note to **Google Docs** and creates a **Gmail draft** to the user or an alias. Both go through the Pulse MCP Server's tools, and the draft is never sent.
6. Decides for itself when to retry, when to stop for insufficient evidence, and when to block for a human. Each of these decisions is bounded and recorded.

## 4. Intended Users

| Audience | Value |
| --- | --- |
| Product / Growth | Prioritise fixes and improvements from recurring, counted signals. |
| Support | Align macros and messaging with what users are actually reporting. |
| Leadership | A one-page health check without reading raw reviews. |
| Operator (you) | Run, preview, publish, resume, and inspect the agent from a CLI. |

## 5. Functional Requirements

### FR-01 — Review acquisition (Google Play only)

- Ingest Groww Google Play reviews covering **8–12 weeks** (default 10 completed weeks).
- Accepted inputs:
  - **Public export file** (CSV, JSON, or JSONL) with a manifest declaring source, product, coverage dates, filters, and column mapping. This is the primary path.
  - **Optional public fetcher** that collects publicly visible reviews (no login, no authenticated store access) and writes the same export file and manifest format. The operator must confirm this is permitted for their use before enabling it.
- Validate store = `google_play` and product = `com.nextbillion.groww`, and reject anything else.
- Record date range, filters, truncation, and coverage status, so the pulse never implies coverage it lacks.
- Never scrape behind a login, never automate in ways that violate store terms, and never require Apple data.

### FR-02 — Privacy before persistence

- Drop structured identity fields (reviewer name, user image/profile URL, review ID tied to a profile, device info) on read.
- Detect and redact identifiers in review text: emails, phone numbers, URLs, @handles, long numeric identifiers (account, PAN/Aadhaar-like, UPI IDs), and personal names where detectable.
- Run sanitisation **before** data reaches the database, the graph state, logs, model prompts, or published output.
- Mark redactions visibly (for example `[redacted]`). Exclude a row when its meaning cannot be preserved safely.
- Errors and logs carry codes and counts only, never review text, filenames, or identifiers.

### FR-03 — Theme analysis

- Discover a taxonomy of **≤ 5 themes** from the sanitised history. Examples: onboarding/KYC, payments/UPI, withdrawals, app performance, F&O/trading, support. These are examples, not a fixed list.
- Assign every eligible reporting-week review exactly **one primary theme**.
- Compute counts and shares **in code**, rank themes deterministically, and highlight the top 3.

### FR-04 — Weekly pulse composition

- Produce a note of **≤ 250 words** across all visible text (title, labels, quotes, and coverage line), fitting **one page**.
- Exactly **3 themes**, **3 quotes**, and **3 action ideas**.
- Quotes are **verbatim substrings** of sanitised reporting-week reviews, extracted by code from verified character spans. They are never written or "cleaned up" by the model, and translations are never presented as quotes.
- Each action idea links to the theme and review evidence that supports it. It must not assert unverified root causes or make commitments.
- When evidence is insufficient (too few reviews, fewer than 3 supportable themes, or fewer than 3 quote-safe reviews), stop with `INSUFFICIENT_EVIDENCE` rather than padding the note.

### FR-05 — Publication through the Pulse MCP Server

- Connect to the separately deployed Pulse MCP Server as an **MCP client**, discover its tools, and verify them against the required contract before any write.
- Create (or reconcile) the Google Doc containing the note, then read it back to verify the content.
- Create a Gmail **draft** to the configured self/alias recipient containing the note and the Doc link, then read it back to verify it.
- **Never** invoke any send, forward, delete, or share-permission tool, even if the server exposes one.
- No Google OAuth, Google API client, or credential handling in this project.

### FR-06 — Agentic control, durability, and outcomes

- Orchestrate as a typed state graph. Routing decisions (retry, repair, stop, or block) are **explicit conditional edges with caps**, not open-ended loops.
- Persist checkpoints so an interrupted run resumes from its last completed step without repeating external writes.
- Every run ends in one declared terminal outcome with a reason code: `PREVIEW_READY`, `COMPLETE`, `INSUFFICIENT_EVIDENCE`, `VALIDATION_FAILED`, `BLOCKED`, or `FAILED`.

### FR-07 — Operator interface

CLI commands to validate config, fetch (optional), import, preview (no external writes), publish, resume, and inspect runs. All output is content-free except for the explicitly generated preview artifacts.

## 6. Non-Functional Requirements

| Area | Requirement |
| --- | --- |
| Privacy | Zero reviewer PII in the database, checkpoints, logs, model payloads, Doc, or draft. Verified by automated scans in tests. |
| Truthfulness | 100% of quotes match source spans, and 100% of counts match repository queries. |
| Determinism | Same snapshot plus same configuration produce the same ranking and quote candidates. Model outputs are persisted, not regenerated on resume. |
| Idempotency | Re-running or resuming the same week never creates a duplicate Doc or draft. |
| Bounded cost/time | Capped model attempts, repairs, MCP retries, token budget, and wall-clock budget per run. |
| Security | Review text is untrusted input. It cannot change config, recipients, tools, or routing (prompt-injection safe). |
| Portability | Runs on Windows/macOS/Linux with Python 3.12. Local SQLite with no external database. |
| Testability | Every node is testable alone, and the MCP server is mockable through the contract. |

## 7. Scope Boundaries

**In scope:** Google Play ingestion, privacy, analysis, composition, validation, LangGraph orchestration, checkpointing, the MCP *client* adapter, CLI, tests, and evaluation.

**Out of scope for this project:**

- Building the Pulse MCP Server, Google auth, or Google API calls. These belong to the companion project.
- Apple App Store reviews (deferred).
- Automatic email sending, public sharing of the Doc, and replying to reviews.
- Authenticated scraping, Groww customer data, or any non-public source.
- A web UI. The CLI is sufficient.

## 8. Operating Defaults (proposed, configurable)

These are design choices, not brief requirements:

- History: 10 completed weeks, configurable between 8 and 12.
- Reporting week: the latest complete Monday–Sunday week in `Asia/Kolkata`.
- Evidence floor: ≥ 2 reviews per highlighted theme, ≥ 3 distinct quote-safe reviews, and ≥ 10 eligible reviews in the reporting week.
- Language: the English privacy pipeline is validated first. Non-English scripts are excluded and counted, and Romanised Hinglish passes through as English-script text.
- Stack: Python 3.12, Pydantic, LangGraph, SQLite, and the official MCP Python SDK, with a provider-neutral LLM adapter.
- Trigger: operator-run CLI first. Weekly scheduling is an optional later phase.

## 9. Deliverables

1. The Pulse Agent package (`src/groww_pulse`) with a CLI.
2. The MCP tool contract the agent requires (architecture §9), handed to the MCP Server project.
3. A sanitised review store, snapshot, theme assignments, and evidence records for each run.
4. A verified one-page Google Doc and an unsent Gmail draft for at least one real reporting week.
5. A test suite (unit, workflow, contract, and privacy) and an evaluation report.
6. A README and runbook covering setup, operation, recovery, and limitations.

## 10. Acceptance Criteria

| ID | Check |
| --- | --- |
| AC-01 | A full run completes on Google Play data alone, with no Apple configuration or input. |
| AC-02 | Product identity, the 8–12-week history, reporting dates, filters, and coverage are recorded for every run. |
| AC-03 | At most 5 themes exist, and the top-3 ranking matches counts recomputed from the database. |
| AC-04 | Exactly 3 quotes, each a verified verbatim span of a distinct reporting-week review. |
| AC-05 | Exactly 3 action ideas, each linked to a highlighted theme and its review evidence. |
| AC-06 | The note is ≤ 250 words (all visible text), and the Doc is verified as one page. |
| AC-07 | Automated scans find no reviewer PII in the DB, checkpoints, logs, captured model payloads, Doc, or draft. |
| AC-08 | All Docs and Gmail operations go through Pulse MCP Server tools. The agent contains no Google API code. |
| AC-09 | The Doc content and draft content are read back and match. The draft contains the note and/or the Doc link. |
| AC-10 | The draft is verified unsent, and a tool audit shows zero send/forward/share calls. |
| AC-11 | Sparse data, model failures, MCP failures, and interruptions produce explicit outcomes. A resumed run creates no duplicates. |
| AC-12 | With the MCP server absent, `preview` still produces a fully validated local pulse. |

## 11. Definition of Done

A public Groww Google Play export has been turned into an evidence-backed weekly pulse. It is published as a one-page Google Doc and an unsent Gmail draft through the separate Pulse MCP Server. The pulse meets every theme, quote, action, length, privacy, and integration requirement above, and the acceptance evidence is recorded. Apple App Store support is not required.
