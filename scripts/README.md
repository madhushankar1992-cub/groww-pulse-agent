# PowerShell runner

`run.ps1` wraps the existing Python CLI. It finds the project root automatically, uses `.venv`, preserves exit codes, and does not display or pass API keys as command-line arguments. The Python CLI loads `.env`.

From the project root:

This laptop currently blocks direct `.ps1` execution. Use a process-only override, which does not change the permanent execution policy:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/run.ps1 -Action Check
```

Use that same invocation prefix with any of the actions below. On systems that already permit local scripts, the shorter form works:

```powershell
.\scripts\run.ps1
.\scripts\run.ps1 -Action Check
.\scripts\run.ps1 -Action Preview -SnapshotId eb0f1ca4-f1c9-4395-8566-67e3767e1241 -DryRun
```

Remove `-DryRun` only when you intend to execute the request. The snapshot above is the latest week: 5–11 October 2026. The default `config/settings.groq.example.yaml` already uses small batches that fit the Groq free tier's 8,000 tokens-per-minute limit (a larger request fails with `LLM_RATE_LIMITED`).

Inspect the latest successful preview (run `f5b4d9da…`, `PREVIEW_READY`):

```powershell
.\scripts\run.ps1 -Action Inspect -RunId f5b4d9da-2d1c-4407-80ea-cfd6f68aefa0
.\scripts\run.ps1 -Action Resume -RunId f5b4d9da-2d1c-4407-80ea-cfd6f68aefa0 -DryRun
```

Resume must use the original config and may incur additional model charges.

Other actions:

| Action | Purpose |
| --- | --- |
| `Analyze` | Themes, classification and quotes; requires `-SnapshotId`. |
| `Preview` | Full local note workflow; requires `-SnapshotId`. |
| `Publish` | Doc and unsent draft; requires snapshot and configured MCP server. |
| `ConfirmLayout` | Records your manual one-page check; requires `-RunId`. |
| `Test` | Runs pytest with a fresh scratch directory in `.cache`. |
| `Lint` | Runs Ruff without changing files. |

Publication is still waiting for the server launch command and recipient setup. No script starts Phase 6 or scheduling. No dependencies are installed automatically. See [the input checklist](../Docs/input.md) for setup and [the implementation plan](../Docs/implementation-plan.md) for scope.
