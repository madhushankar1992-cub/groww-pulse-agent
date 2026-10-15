"""Direct publication through the gmail-docs-mcp server.

That server exposes only ``send_email`` (sends immediately) and ``append_to_google_doc``
(append-only). Both are irreversible, so this module is deliberately strict:

* nothing is written without ``approve=True`` (the operator has read the preview);
* a write-ahead ledger records each step before the call, and a step whose outcome is
  unknown is never repeated automatically;
* the pulse must have passed validation, and the email goes to exactly one address.
"""

import asyncio
import json
import os
import re
from datetime import timedelta
from pathlib import Path

from groww_pulse.errors import ErrorCode, PulseError

DEFAULT_URL = "https://gmail-docs-mcp.vercel.app/mcp"
EMAIL = re.compile(r"[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+")
DOC_ID = re.compile(r"(?:docs\.google\.com/document/d/)?([A-Za-z0-9_-]{20,200})")


def settings(env=os.environ):
    """Read connection settings from the environment; never from files that are committed."""
    token = env.get("MCP_AUTH_TOKEN", "").strip()
    recipient = env.get("PULSE_DRAFT_RECIPIENT", "").strip()
    match = DOC_ID.search(env.get("PULSE_GOOGLE_DOC_ID", "").strip())
    if not token or not EMAIL.fullmatch(recipient) or not match:
        raise PulseError(ErrorCode.CONFIG_INVALID)
    return {"url": env.get("GMAIL_DOCS_MCP_URL", DEFAULT_URL).strip() or DEFAULT_URL,
            "token": token, "recipient": recipient, "document_id": match.group(1)}


async def _call(cfg, name, arguments, timeout):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {cfg['token']}"}
    async with streamablehttp_client(cfg["url"], headers=headers, timeout=timeout,
                                     sse_read_timeout=timeout) as streams:
        async with ClientSession(streams[0], streams[1],
                                 read_timeout_seconds=timedelta(seconds=timeout)) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments)
    blocks = [c.text for c in result.content if c.type == "text"]
    try:
        data = json.loads(blocks[0]) if len(blocks) == 1 else {}
    except ValueError:
        data = {}
    if result.isError:
        code = data.get("error_code") if isinstance(data, dict) else None
        transient = (None, "AUTH_REQUIRED", "RATE_LIMITED", "UPSTREAM_ERROR")
        raise PulseError(ErrorCode.MCP_UNAVAILABLE if code in transient else ErrorCode.MCP_RESPONSE_INVALID)
    if not isinstance(data, dict):
        raise PulseError(ErrorCode.MCP_RESPONSE_INVALID)
    return data


def call_tool(cfg, name, arguments, timeout=60):
    try:
        return asyncio.run(asyncio.wait_for(_call(cfg, name, arguments, timeout), timeout + 15))
    except PulseError:
        raise
    except Exception:  # Exception text may contain account details; report a code only.
        raise PulseError(ErrorCode.MCP_UNAVAILABLE) from None


def load_pulse(run_dir):
    folder = Path(run_dir)
    try:
        body = (folder / "pulse.md").read_text(encoding="utf-8")
        report = json.loads((folder / "validation.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise PulseError(ErrorCode.RUN_NOT_FOUND) from None
    if report.get("errors") or report.get("valid") is not True:
        raise PulseError(ErrorCode.VALIDATION_FAILED)
    subject = body.splitlines()[0].lstrip("# ").strip() if body.strip() else ""
    if not subject:
        raise PulseError(ErrorCode.VALIDATION_FAILED)
    return subject[:200], body


class Ledger:
    """Write-ahead record of external writes, keyed by run id."""

    def __init__(self, path):
        self.path = Path(path)

    def _all(self):
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise PulseError(ErrorCode.INTERNAL_ERROR) from None  # fail closed: state unreadable

    def get(self, run_id, step):
        return self._all().get(run_id, {}).get(step)

    def put(self, run_id, step, value):
        data = self._all()
        data.setdefault(run_id, {})[step] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temp.replace(self.path)


def publish(run_dir, *, approve=False, dry_run=True, env=os.environ,
            ledger_path="data/direct_publications.json", caller=call_tool):
    """Validate (always), then append to the Doc and send the email (only if approved).

    ``dry_run`` makes no call at all: settings, the Doc ID format and the pulse are checked
    locally. With ``approve`` the server first validates the email with ``dry_run=true``.
    """
    cfg = settings(env)
    subject, body = load_pulse(run_dir)
    run_id = Path(run_dir).name
    doc_url = f"https://docs.google.com/document/d/{cfg['document_id']}/edit"
    email = {"to": [cfg["recipient"]], "subject": subject,
             "body": body + "\n\nFull pulse document: " + doc_url, "is_html": False}
    report = {"run_id": run_id, "dry_run": dry_run, "email_validated": True,
              "doc_append": "SKIPPED", "email": "SKIPPED"}
    if dry_run:
        return report  # local checks only: nothing is sent to the server
    if not approve:
        raise PulseError(ErrorCode.APPROVAL_REQUIRED)
    caller(cfg, "send_email", {**email, "dry_run": True})  # server-side validation, never sends
    ledger = Ledger(ledger_path)
    for step in ("doc_append", "email"):
        status = ledger.get(run_id, step)
        if status in ("PENDING", "UNKNOWN"):
            raise PulseError(ErrorCode.RECONCILIATION_REQUIRED)  # outcome unknown: check by hand
        if isinstance(status, dict):
            report[step] = "ALREADY_DONE"
    if report["doc_append"] != "ALREADY_DONE":
        ledger.put(run_id, "doc_append", "PENDING")
        try:
            caller(cfg, "append_to_google_doc", {"document": cfg["document_id"], "text": body,
                                                 "add_leading_newline": True})
        except PulseError:
            ledger.put(run_id, "doc_append", "UNKNOWN")
            raise
        ledger.put(run_id, "doc_append", {"status": "DONE"})
        report["doc_append"] = "DONE"
    if report["email"] != "ALREADY_DONE":
        ledger.put(run_id, "email", "PENDING")
        try:
            sent = caller(cfg, "send_email", email)
        except PulseError:
            ledger.put(run_id, "email", "UNKNOWN")
            raise
        ledger.put(run_id, "email", {"status": "SENT", "message_id": str(sent.get("message_id", ""))[:100]})
        report["email"] = "SENT"
    report["document_url"] = doc_url
    return report
