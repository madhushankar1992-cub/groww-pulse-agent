"""Streamlit front end + back end for the Groww Review Pulse agent.

Runs the same CLI commands as the terminal workflow, so there is one code path.
Secrets (GROQ_API_KEY, MCP_AUTH_TOKEN, PULSE_DRAFT_RECIPIENT, PULSE_GOOGLE_DOC_ID) come from
Streamlit secrets or the environment, never from the repository.

Hardening notes (things that went wrong on the hosted app):
* Streamlit re-runs this script on every click. Long jobs therefore run as background processes
  that survive a re-run, and a second click re-attaches instead of starting a colliding run.
* Error codes are translated into plain instructions.
* A failed validation (the model's wording tripped a safety check) is retried automatically.
"""

import json
import os
import subprocess
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

import streamlit as st
import yaml

ROOT = Path(__file__).parent
CONFIG = ROOT / "config" / "settings.groq.example.yaml"
UPLOADS = ROOT / "raw_exports" / "uploads"
JOB_DIR = ROOT / "data" / "logs"
SECRET_KEYS = ("GROQ_API_KEY", "MCP_AUTH_TOKEN", "PULSE_DRAFT_RECIPIENT", "PULSE_GOOGLE_DOC_ID",
               "GMAIL_DOCS_MCP_URL")
PREVIEW_ATTEMPTS = 3
JOB_TIMEOUT_SECONDS = 1500

ADVICE = {
    "LLM_RATE_LIMITED": "Groq's free tier is busy (tokens per minute). Wait one minute, then press the button once.",
    "LLM_AUTH_FAILED": "The Groq key is missing or wrong. Check GROQ_API_KEY in Secrets.",
    "LLM_UNAVAILABLE": "Groq could not be reached. Try again in a minute.",
    "VALIDATION_FAILED": "The note broke a safety rule on every attempt. Press Run preview again.",
    "PUBLICATION_CONFLICT": "Another run is still working. Wait for it to finish, or reboot the app.",
    "SNAPSHOT_NOT_FOUND": "That snapshot no longer exists (the app restarted). Upload and import the file again in tab 1.",
    "IMPORT_NOT_FOUND": "That import no longer exists. Upload and import the file again in tab 1.",
    "COVERAGE_MISSING": "The file does not cover the whole reporting week. Use a newer export or another date.",
    "TOO_FEW_WEEKLY_REVIEWS": "Too few eligible reviews this week to build a trustworthy note.",
    "INSUFFICIENT_EVIDENCE": "Not enough clear evidence for three themes and quotes this week.",
    "FORMAT_INVALID": "The file is not a valid JSONL export (needs content, score, at).",
    "COLUMNS_INVALID": "The file must have the columns content, score and at.",
    "FILE_UNREADABLE": "The file could not be read. Check it is UTF-8 JSONL.",
    "INPUT_LIMIT_EXCEEDED": "The file is too large (limit 100 MB or 100,000 rows).",
    "PRIVACY_MODEL_UNAVAILABLE": "The name-detection model is missing on this server. Reboot the app.",
    "CONFIG_INVALID": "A secret is missing or malformed. Check the sidebar for red crosses.",
    "MCP_UNAVAILABLE": "The mail/Doc server rejected or could not be reached (token wrong or expired).",
    "APPROVAL_REQUIRED": "Tick the approval box first.",
    "RECONCILIATION_REQUIRED": "A send may already have happened. Check the Doc and your inbox before trying again.",
    "RUN_NOT_FOUND": "That run no longer exists (the app restarted). Run the preview again.",
    "BUDGET_EXCEEDED": "The cost cap for this run was reached.",
    "RUN_TIMEOUT": "The run took too long. Press Run preview again.",
    "INTERNAL_ERROR": "Unexpected problem. Reboot the app and try again.",
}

SECRETS_FOUND: list[str] = []
SECRETS_ERROR = ""


def _flatten(node, out):
    """Collect KEY=value pairs, also from [sections] in case the secrets were pasted under one."""
    for key in list(node.keys()):
        value = node[key]
        if hasattr(value, "keys"):
            _flatten(value, out)
        else:
            out[str(key).strip()] = str(value).strip()


try:  # Streamlit Cloud secrets -> environment (visible to the CLI subprocesses).
    _found: dict[str, str] = {}
    _flatten(st.secrets, _found)
    SECRETS_FOUND = sorted(_found)
    for key in SECRET_KEYS:
        if _found.get(key) and not os.environ.get(key):
            os.environ[key] = _found[key]
except Exception as exc:  # Only the error class is shown, never its text.
    SECRETS_ERROR = type(exc).__name__


def explain(code) -> str:
    code = str(code or "")
    return f"{code}: {ADVICE[code]}" if code in ADVICE else (code or "Unknown error")


def cli(*args):
    return [sys.executable, "-m", "groww_pulse", "--config", str(CONFIG), *args]


def parse(text: str, code: int):
    try:
        return code, json.loads(text)
    except ValueError:
        return code, {"status": "ERROR", "code": "INTERNAL_ERROR"}


def pulse(*args, timeout=600):
    """Run one short CLI command in the foreground; return (exit_code, parsed_json)."""
    try:
        done = subprocess.run(cli(*args), cwd=ROOT, capture_output=True, text=True,
                              env=os.environ.copy(), timeout=timeout)
    except subprocess.TimeoutExpired:
        return 1, {"status": "ERROR", "code": "RUN_TIMEOUT"}
    return parse(done.stdout, done.returncode)


@st.cache_resource
def jobs() -> dict:
    """Background processes, shared across Streamlit re-runs."""
    return {}


def run_job(name: str, *args):
    """Run a long CLI command in the background and wait for it; a re-run re-attaches to it."""
    registry = jobs()
    entry = registry.get(name)
    if entry is None:
        JOB_DIR.mkdir(parents=True, exist_ok=True)
        out = JOB_DIR / f"job_{name}.out"
        handle = open(out, "w", encoding="utf-8")  # noqa: SIM115 (closed when the job is collected)
        proc = subprocess.Popen(cli(*args), cwd=ROOT, stdout=handle, stderr=subprocess.DEVNULL,
                                env=os.environ.copy(), text=True)
        entry = registry[name] = {"proc": proc, "out": out, "handle": handle, "started": time.time()}
    timed_out = False
    while entry["proc"].poll() is None:
        if time.time() - entry["started"] > JOB_TIMEOUT_SECONDS:
            entry["proc"].kill()
            entry["proc"].wait()
            timed_out = True
            break
        time.sleep(2)
    entry["handle"].close()
    registry.pop(name, None)
    if timed_out:
        return 1, {"status": "ERROR", "code": "RUN_TIMEOUT"}
    return parse(Path(entry["out"]).read_text(encoding="utf-8"), entry["proc"].returncode or 0)


def job_running(name: str) -> bool:
    entry = jobs().get(name)
    return bool(entry and entry["proc"].poll() is None)


def manifest_for(export: Path) -> Path:
    days = []
    for line in export.read_text(encoding="utf-8").splitlines():
        try:
            days.append(json.loads(line)["at"][:10])
        except (ValueError, KeyError, TypeError):
            continue
    if not days:
        raise ValueError("No review rows with an 'at' date were found.")
    path = export.with_suffix(".manifest.yaml")
    path.write_text(yaml.safe_dump({
        "store": "google_play", "product_id": "com.nextbillion.groww",
        "provider_ref": "google-play-public", "acquisition_method": "permitted_public_export",
        "public_export_confirmed": True, "generated_at": datetime.now(UTC).isoformat(),
        "coverage_start": min(days), "coverage_end": date.today().isoformat(),
        "coverage_status": "complete_for_declared_export", "storefront": "IN",
        "source_timezone": "Asia/Kolkata", "format": "jsonl", "encoding": "utf-8",
        "date_precision": "timestamp", "date_format": "iso8601", "default_language": "en",
        "language_filter": ["en"], "sort_order": "newest", "truncated": False,
        "columns": {"text": "content", "date": "at", "rating": "score"}}), encoding="utf-8")
    return path


def spacy_ready() -> bool:
    try:
        import en_core_web_sm  # noqa: F401
        return True
    except ImportError:
        return False


st.set_page_config(page_title="Groww Review Pulse", page_icon=":bar_chart:", layout="wide")
st.title("Groww Weekly Review Pulse")
st.caption("Google Play reviews → 3 themes, 3 quotes, 3 actions → Google Doc + email. "
           "Reviews need at least 9 words, English only, no emoji.")

with st.sidebar:
    st.subheader("Configuration")
    for key in ("GROQ_API_KEY", "MCP_AUTH_TOKEN", "PULSE_DRAFT_RECIPIENT", "PULSE_GOOGLE_DOC_ID"):
        st.write(("✅ " if os.environ.get(key) else "❌ ") + key)
    st.write(("✅ " if spacy_ready() else "❌ ") + "Name-detection model")
    st.caption("Set these in Streamlit Secrets (or .env locally). Values are never displayed.")
    with st.expander("Diagnostics"):
        st.write("Secret names found:", ", ".join(SECRETS_FOUND) or "none")
        if SECRETS_ERROR:
            st.write("Reading secrets failed:", SECRETS_ERROR)
        st.write("Preview running:", "yes" if job_running("preview") else "no")

tab_data, tab_run, tab_publish = st.tabs(["1 · Reviews", "2 · Analyse", "3 · Review & publish"])

with tab_data:
    st.write("Upload a public Google Play export (JSONL with `content`, `score`, `at`) "
             "or use one already in `raw_exports/`.")
    upload = st.file_uploader("Reviews export (.jsonl)", type=["jsonl"])
    found = sorted((ROOT / "raw_exports").glob("**/*.jsonl")) if (ROOT / "raw_exports").exists() else []
    choice = st.selectbox("…or choose an existing export", [""] + [str(p.relative_to(ROOT)) for p in found])
    as_of = st.date_input("Report as of (the week before this date is analysed)", value=date.today())
    if st.button("Import and freeze snapshot", type="primary", disabled=job_running("preview")):
        export = None
        if upload is not None:
            UPLOADS.mkdir(parents=True, exist_ok=True)
            export = UPLOADS / f"google_play_{date.today().isoformat()}.jsonl"
            export.write_bytes(upload.getvalue())
        elif choice:
            export = ROOT / choice
        if export is None:
            st.error("Choose or upload an export first.")
        else:
            try:
                manifest = export.with_suffix(".manifest.yaml")
                if upload is not None or not manifest.exists():
                    manifest = manifest_for(export)
                with st.spinner("Sanitising and importing (1–2 minutes)…"):
                    code, imported = pulse("import", "--manifest", str(manifest), "--file", str(export),
                                           "--as-of", as_of.isoformat())
                if code:
                    st.error(explain(imported.get("code")))
                else:
                    code, snap = pulse("snapshot", "--import-id", imported["import_id"],
                                       "--as-of", as_of.isoformat())
                    if code:
                        st.error(explain(snap.get("code") or snap.get("status")))
                    elif snap.get("status") != "READY":
                        st.error(explain(snap.get("status")))
                    else:
                        st.session_state["snapshot"] = snap.get("snapshot_id")
                        st.success("Snapshot ready. Now open tab 2 · Analyse.")
                    st.json(snap.get("counts", {}))
            except (ValueError, UnicodeDecodeError) as exc:
                st.error(explain("FORMAT_INVALID") if isinstance(exc, UnicodeDecodeError) else str(exc))

with tab_run:
    snap_id = st.text_input("Snapshot ID", value=st.session_state.get("snapshot", ""))
    st.info("Runs the Groq analysis (about 4–6 minutes on the free tier, roughly a cent). Press the button "
            f"ONCE and wait. If validation fails it retries by itself, up to {PREVIEW_ATTEMPTS} times. "
            "Nothing is published here.")
    valid_snapshot = True
    try:
        if snap_id:
            UUID(snap_id)
    except ValueError:
        valid_snapshot = False
        st.error("Snapshot ID must be a UUID. Import a file in tab 1 first.")
    busy = job_running("preview")
    if busy:
        st.warning("A preview is already running. Wait for it; do not press the button again.")
    can_run = bool(os.environ.get("GROQ_API_KEY")) and bool(snap_id) and valid_snapshot
    if (st.button("Run preview", disabled=not can_run, type="primary") or busy) and can_run:
        run = {}
        for attempt in range(1, PREVIEW_ATTEMPTS + 1):
            with st.spinner(f"Discovering themes, classifying reviews, writing the note… "
                            f"(attempt {attempt} of {PREVIEW_ATTEMPTS})"):
                code, run = run_job("preview", "preview", "--snapshot-id", snap_id)
            if run.get("status") == "PREVIEW_READY":
                break
            if (run.get("reason_code") or run.get("code")) != "VALIDATION_FAILED":
                break
        if run.get("status") == "PREVIEW_READY":
            st.session_state["run_id"] = run["run_id"]
            st.success(f"Preview ready · {run.get('word_count')} words. Open tab 3 to read it.")
        else:
            st.error(explain(run.get("reason_code") or run.get("code") or run.get("status")))
        st.json({k: run.get(k) for k in ("run_id", "status", "reason_code", "word_count",
                                         "validation_errors", "usage") if k in run})

with tab_publish:
    run_id = st.text_input("Run ID", value=st.session_state.get("run_id", ""))
    try:
        run_id = str(UUID(run_id)) if run_id else ""
    except ValueError:
        st.error("Run ID must be a UUID.")
        run_id = ""
    folder = ROOT / "artifacts" / run_id if run_id else None
    if folder and (folder / "pulse.md").exists():
        st.markdown((folder / "pulse.md").read_text(encoding="utf-8"))
        st.divider()
        if st.button("Validate publication (no changes made)"):
            code, out = pulse("publish-direct", run_id)
            if code:
                st.error(explain(out.get("code")))
            else:
                st.success("Settings and note look valid. Nothing was sent.")
        st.warning("Publishing appends this text to the shared Google Doc and sends one email "
                   "from the connected Gmail account. Neither can be undone, and this app forgets "
                   "what it sent when it restarts, so check the Doc and inbox before sending again.")
        approved = st.checkbox("I have read the note above and approve sending it")
        typed = st.text_input("Type SEND to confirm", value="")
        if st.button("Append to Doc and send email", disabled=not (approved and typed.strip() == "SEND"),
                     type="primary"):
            code, out = pulse("publish-direct", run_id, "--send", "--approve", timeout=180)
            if code == 0:
                st.success("Published: the Doc was updated and the email was sent.")
                st.json({k: out.get(k) for k in ("doc_append", "email", "document_url")})
            else:
                st.error(explain(out.get("code")))
    else:
        st.info("Run a preview first (tab 2), or paste an existing run ID.")
