"""Streamlit front end + back end for the Groww Review Pulse agent.

Runs the same CLI commands as the terminal workflow, so there is one code path.
Secrets (GROQ_API_KEY, MCP_AUTH_TOKEN, PULSE_DRAFT_RECIPIENT, PULSE_GOOGLE_DOC_ID) come from
Streamlit secrets or the environment, never from the repository.
"""

import json
import os
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

import streamlit as st
import yaml

ROOT = Path(__file__).parent
CONFIG = ROOT / "config" / "settings.groq.example.yaml"
UPLOADS = ROOT / "raw_exports" / "uploads"
SECRET_KEYS = ("GROQ_API_KEY", "MCP_AUTH_TOKEN", "PULSE_DRAFT_RECIPIENT", "PULSE_GOOGLE_DOC_ID",
               "GMAIL_DOCS_MCP_URL")

try:  # Streamlit Cloud secrets -> environment (visible to the CLI subprocesses).
    for key in SECRET_KEYS:
        if key in st.secrets and not os.environ.get(key):
            os.environ[key] = str(st.secrets[key])
except Exception:
    pass


def pulse(*args):
    """Run one CLI command; return (exit_code, parsed_json_or_text)."""
    done = subprocess.run([sys.executable, "-m", "groww_pulse", "--config", str(CONFIG), *args],
                          cwd=ROOT, capture_output=True, text=True, env=os.environ.copy(), timeout=1500)
    try:
        return done.returncode, json.loads(done.stdout)
    except ValueError:
        return done.returncode, {"status": "ERROR", "detail": (done.stdout or done.stderr)[-500:]}


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


st.set_page_config(page_title="Groww Review Pulse", page_icon=":bar_chart:", layout="wide")
st.title("Groww Weekly Review Pulse")
st.caption("Google Play reviews → 3 themes, 3 quotes, 3 actions → Google Doc + email. "
           "Reviews need at least 9 words, English only, no emoji.")

with st.sidebar:
    st.subheader("Configuration")
    for key in ("GROQ_API_KEY", "MCP_AUTH_TOKEN", "PULSE_DRAFT_RECIPIENT", "PULSE_GOOGLE_DOC_ID"):
        st.write(("✅ " if os.environ.get(key) else "❌ ") + key)
    st.caption("Set these in Streamlit Secrets (or .env locally). Values are never displayed.")

tab_data, tab_run, tab_publish = st.tabs(["1 · Reviews", "2 · Analyse", "3 · Review & publish"])

with tab_data:
    st.write("Upload a public Google Play export (JSONL with `content`, `score`, `at`) "
             "or use one already in `raw_exports/`.")
    upload = st.file_uploader("Reviews export (.jsonl)", type=["jsonl"])
    found = sorted((ROOT / "raw_exports").glob("**/*.jsonl")) if (ROOT / "raw_exports").exists() else []
    choice = st.selectbox("…or choose an existing export", [""] + [str(p.relative_to(ROOT)) for p in found])
    as_of = st.date_input("Report as of (the week before this date is analysed)", value=date.today())
    if st.button("Import and freeze snapshot", type="primary"):
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
                with st.spinner("Sanitising and importing…"):
                    code, imported = pulse("import", "--manifest", str(manifest), "--file", str(export),
                                           "--as-of", as_of.isoformat())
                if code:
                    st.error("Import failed")
                    st.json(imported)
                else:
                    code, snap = pulse("snapshot", "--import-id", imported["import_id"],
                                       "--as-of", as_of.isoformat())
                    if code:
                        st.error("Snapshot failed")
                    else:
                        st.session_state["snapshot"] = snap.get("snapshot_id")
                        st.success("Snapshot ready")
                    st.json(snap.get("counts", snap))
            except ValueError as exc:
                st.error(str(exc))

with tab_run:
    snap_id = st.text_input("Snapshot ID", value=st.session_state.get("snapshot", ""))
    st.info("Runs the checkpointed Groq analysis (about 3–4 minutes on the free tier, roughly $0.01). "
            "Nothing is published here.")
    if st.button("Run preview", disabled=not os.environ.get("GROQ_API_KEY")):
        with st.spinner("Discovering themes, classifying reviews, composing the note…"):
            code, run = pulse("preview", "--snapshot-id", snap_id)
        if code == 0 and run.get("status") == "PREVIEW_READY":
            st.session_state["run_id"] = run["run_id"]
            st.success(f"Preview ready · {run.get('word_count')} words")
        else:
            st.error(str(run.get("reason_code") or run.get("code") or run.get("detail")))
        st.json(run)

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
            st.json(out)
        st.warning("Publishing appends this text to the shared Google Doc and sends one email "
                   "from the connected Gmail account. Neither can be undone.")
        approved = st.checkbox("I have read the note above and approve sending it")
        if st.button("Append to Doc and send email", disabled=not approved, type="primary"):
            code, out = pulse("publish-direct", run_id, "--send", "--approve")
            if code == 0:
                st.success("Published")
            else:
                st.error(str(out.get("code", "Failed")))
            st.json(out)
    else:
        st.info("Run a preview first, or paste an existing run ID.")
