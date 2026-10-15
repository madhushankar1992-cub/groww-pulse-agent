# Inputs and steps to run the current implementation

Updated: 12 October 2026.

**Related files:** [README](../README.md) · [Requirements](problemStatement.md) · [Architecture](architecture.md) · [Implementation plan](implementation-plan.md) · [Environment example](../.env.example) · [Groq settings](../config/settings.groq.example.yaml) · [Default settings](../config/settings.example.yaml) · [MCP server example](../config/mcp_server.example.yaml)

**Scope:** configure and run the already implemented functionality. Do not start Phase 6, Phase 7, or additional implementation work without the user's instruction. Creating this checklist does not run analysis or publish anything.

## 1. Open the project terminal

Optional shortcuts are available in [scripts/run.ps1](../scripts/run.ps1), with examples in [scripts/README.md](../scripts/README.md). The runner uses the existing CLI; `-DryRun` displays arguments without executing a workflow.

Use PowerShell from the project root:

```powershell
Set-Location 'C:\AI NXT LEAP\AI Agent'
```

The project virtual environment and Groq dependencies are already installed in this workspace.

## 2. Add your Groq API key locally

Create `.env` from the example only if it does not already exist:

```powershell
if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
}
```

Open `.env` in your editor and set:

```dotenv
GROQ_API_KEY=replace_with_your_actual_key
```

- Save the actual key only in `.env`, which is git-ignored. Do not put it in this file, `.env.example`, or chat.
- Only `GROQ_API_KEY` is needed for model calls; Groq is the default provider.
- `PULSE_DRAFT_RECIPIENT` can remain blank for a local preview.
- The CLI reads `.env` from the current directory. An existing environment variable takes precedence over the value in `.env`.

**Current next step:** save the key and tell the assistant it is ready. No key is needed to read this checklist or validate configuration.

## 3. Check the Groq configuration

The supplied [Groq configuration](../config/settings.groq.example.yaml) selects `openai/gpt-oss-120b`, a $3 per-run cost limit, and small batches (30-review discovery sample, 5-review classification batches, one call at a time) that fit the Groq free tier's 8,000 tokens-per-minute limit. The cap is checked before and after model calls; it is not a provider-side spending guarantee.

```powershell
.venv\Scripts\python.exe -m groww_pulse --config config/settings.groq.example.yaml config check
```

This checks configuration and shows the current reporting window. It does not validate your API key or call a model.

## 4. Choose the review snapshot

The latest export is `raw_exports/fetch_2026-10-12/google_play_2026-10-12.jsonl` (11,000 public Google Play reviews, 1 August–11 October 2026, fetched 12 October with `groww_pulse fetch`). It was imported with `--as-of 2026-10-12`.

The current snapshot stored in the local database is:

```text
eb0f1ca4-f1c9-4395-8566-67e3767e1241
```

It covers the latest complete reporting week, **5–11 October 2026**, with **120 eligible weekly reviews** (55 one-star, 46 five-star) and **1,238 eligible history reviews**. Reviews need at least nine safe words and must pass the English and emoji filters.

Use this snapshot for the first Groq trial. The older snapshot `e6a6d261-fd83-463b-b815-60131f684fe3` (28 September–4 October, 69 weekly reviews) is kept for history only.

To refresh the data later: run `groww_pulse fetch --confirm-public-source`, then `import` with the new manifest and file, then `snapshot --import-id <id>`.

## 5. Run a local preview when ready

After saving the key, the following command starts a new Groq run on the known snapshot. This makes billable Groq model calls using sanitised evidence, but does not connect to the MCP server or create a Google Doc or Gmail draft.

```powershell
.venv\Scripts\python.exe -m groww_pulse --config config/settings.groq.example.yaml preview --snapshot-id eb0f1ca4-f1c9-4395-8566-67e3767e1241
```

On `PREVIEW_READY`, inspect the files under `artifacts/<run_id>/`:

- `pulse.md`: the rendered note.
- `pulse.json`: structured pulse and content hash.
- `evidence.json`: quote and action evidence links.
- `validation.json`: word count and validation results.

The note must contain three themes, three verified quotes, and three actions within 250 words. A valid result is not guaranteed: insufficient evidence, exhausted repair attempts, provider errors, or budget limits produce a declared failure or blocked outcome.

## 6. Inspect or resume that run

Replace `<run_id>` with the ID printed by the preview command:

```powershell
.venv\Scripts\python.exe -m groww_pulse --config config/settings.groq.example.yaml inspect <run_id>
.venv\Scripts\python.exe -m groww_pulse --config config/settings.groq.example.yaml resume <run_id>
```

Use the same configuration when resuming. Do not resume an old Anthropic run with the Groq configuration; start a new Groq preview instead.

| Outcome or code | Next step |
| --- | --- |
| `PREVIEW_READY` | Review the local artifacts. |
| `LLM_AUTH_FAILED` | Check that `.env` contains a valid `GROQ_API_KEY` and that an old environment value is not overriding it. |
| `LLM_RATE_LIMITED` | Groq rejected the request for its tokens-per-minute limit (free tier: 8,000 for `gpt-oss-120b`; HTTP 413/429). Keep `discovery_sample_size: 30` and `classify_batch_size: 5` (the default in `config/settings.groq.example.yaml`), wait a minute and retry, or upgrade to the Groq Dev tier before raising them. |
| `LLM_UNAVAILABLE` | Check provider availability or account rate limits; retry/resume after resolving the issue. |
| `LLM_REQUEST_INVALID` | Check model access and request compatibility; report the code without sharing the key. |
| `CONFIG_CHANGED` | Restore the original run configuration or start a new run. |
| `FILTER_REFRESH_REQUIRED` | Use the refreshed snapshot above, or re-import and create a new snapshot. |
| `INSUFFICIENT_EVIDENCE` | Review the evidence counts; do not manufacture missing themes or quotes. |
| `VALIDATION_FAILED` | Review the reason codes before deciding on further changes. |
| `BUDGET_EXCEEDED` / `RUN_TIMEOUT` | The capped run stops; do not repeatedly resume it expecting the cap to reset. |

## 7. Publication inputs — deferred

These are not required for preview. Do not run publication as part of this checklist without a separate instruction.

- A running separate Pulse MCP Server implementing `config/mcp_contract.yaml`.
- `config/mcp_server.local.yaml`, based on the example, with the actual server command or HTTP endpoint.
- Google authentication configured in that server.
- `PULSE_DRAFT_RECIPIENT` in `.env` with the intended draft recipient.
- One-page verification through the server or an explicit manual layout check.

Publication creates a Google Doc and an unsent Gmail draft. It does not send email. No real publication has been performed during implementation.

## Current verification and stopping point

- Groq integration: implemented; 165 tests and Ruff passed.
- Live Groq trial: pending the user's key and instruction to run.
- Live Google publication: pending the separate server setup and instruction.
- Further implementation phases: on hold until explicitly requested.

See [implementation-plan.md](implementation-plan.md) and [README.md](../README.md) for the full project details.
