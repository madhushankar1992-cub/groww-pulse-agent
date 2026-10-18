# Runbook — Groww Weekly Review Pulse Agent

Run every command from the project root. The CLI reads `.env` automatically. Never paste keys or tokens into chat, issues or commits.

## 1. Setup

1. Install Python 3.12 packages as in the [README](../README.md#setup-windows-project-local-python).
2. Copy `.env.example` to `.env` and fill in:
   - `GROQ_API_KEY` — model access.
   - `MCP_AUTH_TOKEN` — bearer token of the gmail-docs-mcp server (same value as on Vercel).
   - `PULSE_DRAFT_RECIPIENT` — the single address that receives the note.
   - `PULSE_GOOGLE_DOC_ID` — Doc ID or full Doc link.
3. `pulse config check --as-of <date>` shows the reporting window (latest complete Mon–Sun week).

## 2. Weekly flow

| Step | Command | Notes |
| --- | --- | --- |
| Fetch reviews | `pulse fetch --confirm-public-source` | Public Google Play listing, en/IN, newest first. Writes `raw_exports/fetch_<date>/`. |
| Import | `pulse import --manifest <m.yaml> --file <x.jsonl> --as-of <date>` | Sanitises, drops reviews with 8 or fewer words, emoji and non-English text. |
| Snapshot | `pulse snapshot --import-id <id> --as-of <date>` | Freezes history and the reporting week. |
| Preview | `pulse preview --snapshot-id <id>` | Groq analysis and a validated note in `artifacts/<run_id>/`. No Google access. |
| Read the note | open `artifacts/<run_id>/pulse.md` | Check three themes, three quotes, three actions, at most 250 words. |
| Publish (direct) | `pulse publish-direct <run_id>` then `--send --approve` | Appends to the Doc and sends one email. **Irreversible.** |
| Publish (draft) | `pulse publish --snapshot-id <id> --mcp-config config/mcp_server.local.yaml` | Doc plus an unsent draft. |
| Evaluate | `pulse evaluate` | Scores the labelled fictional set (Phase 6). |

The Streamlit app (`streamlit run app.py`) wraps the same commands.

## 3. Troubleshooting

| Code | Meaning and fix |
| --- | --- |
| `CONFIG_INVALID` | A value in `.env` is missing or malformed (token, recipient, Doc ID). |
| `LLM_AUTH_FAILED` | `GROQ_API_KEY` is missing or wrong. |
| `LLM_RATE_LIMITED` | Groq free-tier tokens-per-minute limit. Keep the small batches in `settings.groq.example.yaml`, wait a minute, or upgrade the Groq tier. |
| `TAXONOMY_INVALID` | The model returned a theme set outside 3–5 themes. Start a new preview. |
| `MCP_UNAVAILABLE` | Server unreachable, token wrong, or Google login expired. While the Google consent screen is in Testing, the refresh token expires after 7 days: re-run the server's `auth` command. |
| `APPROVAL_REQUIRED` | `--send` was used without `--approve`. |
| `RECONCILIATION_REQUIRED` | A write may or may not have happened. Check the Doc and inbox by hand; do not re-run. |
| `VALIDATION_FAILED` | The note broke a rule (words, quotes, evidence). Re-run the preview. |
| `COVERAGE_MISSING` | The export does not cover the reporting week. Fetch newer reviews. |

## 4. Limitations

- **Export sampling:** the public listing returns the newest reviews only; coverage is declared in the manifest.
- **Exact-row duplicates:** identical reviews on one day are counted separately; overlapping exports reconcile to the larger count.
- **English-only privacy:** name and identifier detection is tuned for English; non-English and emoji reviews are excluded.
- **Model uncertainty:** themes and summaries come from a model. Counts and quotes are computed and verified in code; actions need human judgement.
- **Evaluation set:** the 60 labelled reviews are fictional and deliberately clear-cut, so the agreement score is an upper bound.
- **Ephemeral hosting:** on Streamlit Community Cloud the database, artifacts and publication ledger are lost on restart.
