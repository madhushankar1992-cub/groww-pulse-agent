# Acceptance Report — Phase 6

Status: **MVP complete; live delivery done on 18 October 2026.** Items marked *Pending* are checks the operator still performs by eye.

## Evaluation targets (labelled fictional set, 60 reviews, 5 themes)

| Target | Required | Measured (live Groq `openai/gpt-oss-120b`) |
| --- | --- | --- |
| Theme agreement | ≥ 85% | **100%** (60/60) |
| Quote validity | 100% | **100%** (60 quotes verbatim) |
| Count accuracy | 100% | **100%** |

The set is fictional and clear-cut, so 100% is an upper bound, not an expected production score. Command: `pulse evaluate`; report in `artifacts/evaluation/evaluation.json`.

## Scenario runs

| Scenario | Evidence |
| --- | --- |
| Normal week | Run `f5b4d9da-2d1c-4407-80ea-cfd6f68aefa0` on snapshot `eb0f1ca4…` (5–11 Oct 2026, 120 eligible reviews): `PREVIEW_READY`, 219 words, no validation errors, 3 themes, 3 quotes, 3 actions. |
| Sparse week | `tests/unit/test_analysis.py`, `test_workflow.py`: too few weekly reviews ends `INSUFFICIENT_EVIDENCE` with zero model calls. |
| MCP failure with recovery | `tests/unit/test_publication.py`, `test_process_recovery.py`, `test_gmail_docs.py`: timeout after an applied write reconciles, never duplicates; unknown outcome blocks re-send. |
| Free-tier rate limit | Live: a 13.7k-token request was rejected with 413 → now `LLM_RATE_LIMITED`; small batches succeed. |

## Privacy scan

Canary PII (emails, phones, URLs, handles, IDs, names) is planted in fixtures and must be absent from the database, checkpoints, logs and artifacts: `tests/privacy/`, `tests/unit/test_privacy.py`. The repository contains no real reviews or secrets (guardrail review, GO).

## Acceptance criteria

| ID | Status | Evidence |
| --- | --- | --- |
| AC-01 | Met | Google Play only; full local run completed with no Apple input. |
| AC-02 | Met | Manifest and snapshot record product, window, filters, coverage (`snapshot` output). |
| AC-03 | Met | At most 5 themes enforced; ranking equals an independent recount (tests and `pulse evaluate`). |
| AC-04 | Met | 3 verified verbatim quotes in `evidence.json` for the run above. |
| AC-05 | Met | 3 actions, each tied to a highlighted theme and quote evidence. |
| AC-06 | Partly | 219 words (≤ 250) verified. One-page Doc check is *Pending* the live Doc. |
| AC-07 | Met locally | Canary scans pass for DB, checkpoints, logs and artifacts. Doc and email scans are *Pending* the live send. |
| AC-08 | Met | All Docs/Gmail calls go through MCP tools; the agent has no Google API code. |
| AC-09 | Pending | Read-back of the live Doc is not available on the direct path (append-only server tools); verify by opening the Doc. |
| AC-10 | **Changed by decision** | The draft-only path (`pulse publish`) keeps this. On 15 October 2026 the user chose direct sending (`publish-direct`), which sends mail and is therefore not draft-only. It requires `--send --approve`, a single recipient and a write-ahead ledger. |
| AC-11 | Met | Explicit outcomes for sparse data, model, rate-limit and MCP failures; resume never duplicates (tests). |
| AC-12 | Met | Preview produced a validated note with no MCP server configured. |

## Verification

- `pytest`: 176 tests pass. `ruff check src tests app.py`: clean.
- Guardrail review before publishing the repository: **GO**.

## Live acceptance run (18 October 2026)

| Item | Value |
| --- | --- |
| Run | `f5b4d9da-2d1c-4407-80ea-cfd6f68aefa0` (week 5–11 Oct 2026, 120 eligible reviews) |
| Word count | 219 (limit 250) |
| Doc | https://docs.google.com/document/d/1PUJW7yKw7vHRH-CnDyvAYStRvF2g7F0aOUlaKUJqb2k/edit (note appended) |
| Email | sent to the configured recipient, Gmail message ID `1a14f9d25ec9169e` |
| Command | `pulse publish-direct <run_id> --send --approve`; ledger shows `doc_append: DONE`, `email: SENT` |
| Server tools seen | `send_email`, `append_to_google_doc` (the live deployment does not expose the Pulse draft tools) |

A second live send was made from the deployed Streamlit app on 18 October 2026 (tab 3, approval box and typed `SEND`): the app reported `doc_append: DONE` and `email: SENT`. This proves the cloud path (upload, Groq analysis, validation, Doc append, email) end to end. Because the cloud app does not keep its ledger across restarts, the Doc and inbox now hold two notes for the week.

The operator confirms by opening the Doc and the inbox. The one-page layout check (AC-06) and the read-back (AC-09) are manual on this path.
