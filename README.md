# Groww Review Pulse Agent

This agent turns public Google Play reviews of Groww (`com.nextbillion.groww`) into a one-page weekly pulse: the top 3 themes, 3 verbatim quotes, and 3 action ideas. It publishes the pulse to Google Docs and creates a Gmail draft through the **separate Pulse MCP Server** project. This repository is the agent only.

- [Problem statement](Docs/problemStatement.md) · [Architecture](Docs/architecture.md) · [Implementation plan](Docs/implementation-plan.md)
- **Live app (Streamlit):** https://groww-pulse-age-5evygnyufunrac3ndmrcyc.streamlit.app/
- **Code:** https://github.com/madhushankar1992-cub/groww-pulse-agent · **MCP server:** https://gmail-docs-mcp.vercel.app/mcp
- Status: **Phases 0-6 are implemented on the agent side.** Anthropic and Groq adapters are available. Local tests use fictional model output and MCP resources. A live model run and real Google Docs/Gmail acceptance still need credentials and the separate MCP server.

## Setup (Windows, project-local Python)

Start with [Inputs and run checklist](Docs/input.md) for API-key setup, the existing review snapshot, preview commands, and deferred publication inputs. It is the step-by-step guide for the current implementation; further phases remain on hold until requested.

```powershell
$env:UV_CACHE_DIR = "$PWD\.cache\uv"; $env:UV_PYTHON_INSTALL_DIR = "$PWD\.tools\python"
.\.tools\uv.exe venv --python 3.12 .venv
.\.tools\uv.exe pip install --python .venv\Scripts\python.exe -e ".[fetch,llm,workflow]" pytest ruff
.\.tools\uv.exe pip install --python .venv\Scripts\python.exe "en_core_web_sm @ https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl"
```

Run commands with `.venv\Scripts\python.exe -m groww_pulse ...`. The `pulse` script is also installed in the venv.

## Commands

For PowerShell shortcuts, use [scripts/run.ps1](scripts/run.ps1). Run it without arguments for help, or add `-DryRun` to inspect a command without executing it. See [script examples](scripts/README.md).

```powershell
pulse config check                                  # validate settings and show the reporting window
pulse contract check --tools tools.json             # check a captured MCP list_tools dump against config/mcp_contract.yaml
pulse fetch --confirm-public-source                 # optional: download public Play reviews to raw_exports/ (+ manifest)
pulse import --manifest raw_exports/<x>.manifest.yaml --file raw_exports/<x>.jsonl
pulse snapshot                                      # freeze history + reporting week from the latest import
pulse analyze                                       # taxonomy, weekly ranking, verified quotes
pulse preview                                       # checkpointed analysis and validated local artifacts
pulse publish --mcp-config config/mcp_server.local.yaml
pulse inspect <run_id>                              # status, reason, last completed node, resource references
pulse resume <run_id> --mcp-config config/mcp_server.local.yaml
pulse confirm-layout <run_id>                        # only after personally checking that the Doc is one page
pulse resume <run_id> --mcp-config config/mcp_server.local.yaml
```

Use `pulse --config config/settings.local.yaml <command>` to override defaults. Copy `config/settings.example.yaml` to create it.

Exit codes: `0` OK, `1` failure, `2` invalid input, `3` not enough data (empty week), `4` blocked (coverage missing / MCP capability missing).

## Preview and publication

Set `GROQ_API_KEY` for model calls. Groq is the default provider in the code and example settings. The CLI loads the project `.env` file without overriding existing environment variables. Preview uses no MCP connection. It writes `pulse.md`, `pulse.json`, `evidence.json`, and `validation.json` under `artifacts/<run_id>/`. All visible note text counts toward the 250-word limit; quotes are never altered to fit it.

### Groq setup

Install the Groq extra (already installed in this workspace):

```powershell
.\.tools\uv.exe --cache-dir .cache/uv pip install --python .venv\Scripts\python.exe -e ".[groq,workflow]"
```

Copy `.env.example` to `.env` if you do not already have one, then set `GROQ_API_KEY` in `.env`. Keep `.env.example` blank. Run from the project directory:

```powershell
.venv\Scripts\python.exe -m groww_pulse --config config/settings.groq.example.yaml preview
```

The example selects `openai/gpt-oss-120b`. The adapter also supports `openai/gpt-oss-20b`; changing the model in a local config should include its corresponding pricing. Provider-only configuration (`model: {provider: groq}`) supplies the 120B model and rates automatically. Groq's listed input/output prices per million tokens, checked 11 October 2026, were $0.15/$0.60 for 120B and $0.075/$0.30 for 20B. See [Groq models](https://console.groq.com/docs/models) and [structured outputs](https://console.groq.com/docs/structured-outputs).

Both models use strict JSON output; the original schema is also validated locally, including constraints simplified for the wire schema. Authentication, refusals, malformed outputs, rate limits and budget failures become content-free error codes. SDK transport retries are capped at two retries per request. API-key values are never stored in config or model prompts.

Switching providers changes the run configuration: start a new `preview` run instead of resuming an Anthropic run with Groq settings. Existing snapshots can be reused; provider-specific taxonomy caches are kept separate. Use the same Groq config when resuming the new run.

For publication, copy `config/mcp_server.example.yaml` to `config/mcp_server.local.yaml`, configure the separate server, and set the recipient environment variable named in that file (default `PULSE_DRAFT_RECIPIENT`). The server owns Google login and formatting. The agent only creates an unsent draft; it cannot send or share. `COMPLETE` requires verified content, recipient, draft state and one-page layout. No secrets belong in a tracked file.

On `MANUAL_CHECK_REQUIRED`, inspect the returned Doc, run `confirm-layout`, then `resume`. A human content edit, ambiguous write, duplicate publication, missing capability or missing credentials blocks safely. `RECONCILIATION_REQUIRED` needs operator investigation; repeated resume will not issue another uncertain create. Resume uses the same config and run ID. Another run for an already reserved reporting week blocks with `PUBLICATION_CONFLICT`.

Old snapshots return `FILTER_REFRESH_REQUIRED`: re-import the original export and create a new snapshot. Existing snapshots are preserved. The refreshed sample has 69 eligible weekly reviews for 28 September-4 October 2026. English detection is conservative and may reject some valid English or miss unfamiliar code-switching. The export locale alone is not accepted as proof of language.

The word budget and provenance checks are deterministic; semantic quality of summaries/actions still needs review. A model response lost before local persistence can incur cost again on retry; persisted model outputs and measured usage are reused. MCP writes use a separate durable intent/reconciliation ledger.

## Privacy

- `pulse fetch` writes only text, rating and timestamp. Reviewer names, avatars and review IDs are never written.
- `pulse import` redacts emails, UPI IDs, phone and account numbers, PAN, links, handles and names (spaCy) **before** storing. Any removed value is replaced by `[redacted]` and never kept.
- Reviews with fewer than nine safe words, emoji, or unsupported/uncertain language are excluded before analysis.
- `raw_exports/`, `data/` and `artifacts/` are git-ignored. CLI output and errors contain only codes and counts.

## Tests

```powershell
.venv\Scripts\python.exe -m pytest -p no:cacheprovider --basetemp .cache/test-run-1
.venv\Scripts\python.exe -m ruff check src tests
```

All test data is fictional.

## Two publication paths

| Path | Command | What it does | Reversible? |
| --- | --- | --- | --- |
| Draft-only (MCP workflow above) | `pulse publish` | Creates a Doc and an **unsent** Gmail draft through the Pulse MCP Server. | Yes |
| Direct (gmail-docs-mcp) | `pulse publish-direct <run_id> --send --approve` | **Appends** the note to one Google Doc and **sends** one email. | **No** |

`publish-direct` without `--send` only checks settings and the pulse locally. It needs `MCP_AUTH_TOKEN`, `PULSE_DRAFT_RECIPIENT` (the one recipient) and `PULSE_GOOGLE_DOC_ID` in `.env`, and uses [gmail-docs-mcp](https://github.com/madhushankar1992-cub/gmail-docs-mcp). A write-ahead ledger (`data/direct_publications.json`) never repeats a step whose outcome is unknown; on ephemeral hosting the ledger is lost on restart, so check the Doc and inbox before re-running.

## Weekly schedule (Phase 7)

`pulse weekly --confirm-public-source` runs fetch, import, snapshot and preview, then stops; it never sends. `scripts/schedule.ps1 -Install` registers it as a Windows task for Mondays 09:00. Sending stays manual (`publish-direct`). See the [runbook](Docs/runbook.md).

## Streamlit app

```powershell
.venv\Scripts\python.exe -m streamlit run app.py
```

Deploy on Streamlit Community Cloud: connect this repository, choose `app.py` and Python 3.12, paste the values from `.streamlit/secrets.toml.example` into **Secrets**, and set the app to **private** (the app can send email). Upload a Google Play JSONL export in the first tab; data is not stored in the repository.
