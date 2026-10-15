"""Write-ahead publication ledger. Unknown writes are read/reconciled, never repeated."""

import json
import random
import re
import time

from groww_pulse.composition.renderer import digest
from groww_pulse.errors import ErrorCode, PulseError


def canonical(text):
    return "\n".join(re.sub(r"^\s*(?:#{1,6}\s+|[-*•]\s+|\d+[.)]\s+)", "", line).strip()
                     for line in text.replace("\r\n", "\n").strip().splitlines() if line.strip())


def content_hash(text):
    return digest(canonical(text))


def ref(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value):
        raise PulseError(ErrorCode.MCP_RESPONSE_INVALID)
    return value


class Publisher:
    def __init__(self, conn, client, recipient, attempts=3, sleep=time.sleep):
        if not re.fullmatch(r"[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+", recipient):
            raise PulseError(ErrorCode.CONFIG_INVALID)
        self.conn, self.client, self.recipient = conn, client, recipient
        self.attempts, self.sleep = attempts, sleep

    def key(self, snapshot):
        return digest(f"com.nextbillion.groww|{snapshot.window.report_start}|{snapshot.window.report_end}|1")

    def receipt(self, state, pulse, snapshot):
        key = self.key(snapshot)
        self.conn.execute("INSERT OR IGNORE INTO publication_receipts "
            "(publication_key,run_id,content_hash) VALUES (?,?,?)", (key, state["run_id"], pulse.content_hash))
        row = self.conn.execute("SELECT * FROM publication_receipts WHERE publication_key=?", (key,)).fetchone()
        if row["run_id"] != state["run_id"] or row["content_hash"] != pulse.content_hash:
            raise PulseError(ErrorCode.PUBLICATION_CONFLICT)
        return row

    def update(self, key, **values):
        allowed = {"document_id", "document_url", "doc_hash", "draft_id", "draft_hash", "layout_status", "verified"}
        if not values.keys() <= allowed:
            raise PulseError(ErrorCode.INTERNAL_ERROR)
        self.conn.execute("UPDATE publication_receipts SET " + ",".join(f"{k}=?" for k in values)
                          + " WHERE publication_key=?", (*values.values(), key))

    def read(self, name, **args):
        for attempt in range(self.attempts):
            try:
                return self.client.call(name, **args)
            except (PulseError, TimeoutError, ConnectionError):
                if attempt + 1 == self.attempts:
                    raise PulseError(ErrorCode.MCP_UNAVAILABLE) from None
                self.sleep(min(2 ** attempt + random.random(), 5))

    def operation(self, key, name, args, reconcile):
        opkey = digest(key + "|" + name)
        payload = digest(json.dumps(args, sort_keys=True))
        inserted = self.conn.execute("INSERT OR IGNORE INTO external_operations VALUES (?,?,?,?,?,?)",
            (opkey, key, name, payload, None, "PENDING")).rowcount
        row = self.conn.execute("SELECT * FROM external_operations WHERE operation_key=?", (opkey,)).fetchone()
        if row["payload_hash"] != payload:
            raise PulseError(ErrorCode.PUBLICATION_CONFLICT)
        if not inserted:
            return reconcile()  # Covers crash between intent and call, and after applied call.
        try:
            result = self.client.call(name, **args)
        except Exception:
            self.conn.execute("UPDATE external_operations SET status='UNKNOWN_OUTCOME' WHERE operation_key=?", (opkey,))
            return reconcile()
        # The response is not proof of a side effect; caller must verify by reading.
        return result

    def verified(self, key, name, target):
        self.conn.execute("UPDATE external_operations SET status='VERIFIED', target_ref=? WHERE operation_key=?",
                          (ref(target), digest(key + "|" + name)))

    def title(self, snapshot):
        return f"Groww Weekly Review Pulse | {snapshot.window.report_start}"

    def check_capabilities(self, state, pulse, snapshot):
        self.client.check()
        self.receipt(state, pulse, snapshot)

    def publish_doc(self, state, pulse, snapshot):
        self.client.check()
        row = self.receipt(state, pulse, snapshot)
        key = row["publication_key"]
        title, marker = self.title(snapshot), "groww-pulse:" + key[:16]

        def reconcile_create():
            docs = self.read("docs_find_by_marker", marker=marker).get("documents", [])
            if len(docs) != 1:
                raise PulseError(ErrorCode.RECONCILIATION_REQUIRED)
            doc = self.read("docs_get_document", document_id=ref(docs[0].get("document_id")))
            if doc.get("title") != title or doc.get("text") not in ("", pulse.rendered_body):
                raise PulseError(ErrorCode.RECONCILIATION_REQUIRED)
            return doc

        if row["document_id"]:
            document_id = row["document_id"]
        else:
            created = self.operation(key, "docs_create_document", {"title": title, "marker": marker}, reconcile_create)
            document_id = ref(created.get("document_id"))
            # Never persist an arbitrary connector-supplied URL.
            url = f"https://docs.google.com/document/d/{document_id}/edit"
            self.update(key, document_id=document_id, document_url=url)
        doc = self.read("docs_get_document", document_id=document_id)
        if doc.get("document_id") != document_id or not isinstance(doc.get("text"), str):
            raise PulseError(ErrorCode.MCP_RESPONSE_INVALID)
        if doc.get("title") != title:
            raise PulseError(ErrorCode.HUMAN_EDIT_DETECTED)
        self.verified(key, "docs_create_document", document_id)
        actual, expected = content_hash(doc["text"]), content_hash(pulse.rendered_body)
        if row["doc_hash"] and actual != row["doc_hash"]:
            raise PulseError(ErrorCode.HUMAN_EDIT_DETECTED)
        if actual == expected:
            self.verified(key, "docs_write_content", document_id)
            return
        if doc["text"].strip():
            raise PulseError(ErrorCode.HUMAN_EDIT_DETECTED)

        def reconcile_write():
            current = self.read("docs_get_document", document_id=document_id)
            if content_hash(current.get("text", "")) != expected:
                raise PulseError(ErrorCode.RECONCILIATION_REQUIRED)
            return current
        self.operation(key, "docs_write_content",
                       {"document_id": document_id, "content_markdown": pulse.rendered_body}, reconcile_write)

    def verify_doc(self, state, pulse, snapshot):
        self.client.check()
        row = self.receipt(state, pulse, snapshot)
        key = row["publication_key"]
        document_id = ref(row["document_id"])
        doc = self.read("docs_get_document", document_id=document_id)
        if (doc.get("document_id") != document_id or doc.get("title") != self.title(snapshot)
                or not isinstance(doc.get("text"), str)
                or content_hash(doc["text"]) != content_hash(pulse.rendered_body)):
            raise PulseError(ErrorCode.HUMAN_EDIT_DETECTED)
        self.update(key, doc_hash=content_hash(pulse.rendered_body))
        self.verified(key, "docs_write_content", document_id)
        if "docs_get_page_count" in self.client.optional:
            pages = self.read("docs_get_page_count", document_id=document_id).get("page_count")
            if type(pages) is not int or pages != 1:
                raise PulseError(ErrorCode.LAYOUT_INVALID)
            self.update(key, layout_status="VERIFIED")
        elif row["layout_status"] != "MANUALLY_CONFIRMED":
            self.update(key, layout_status="MANUAL_CHECK_REQUIRED")
            raise PulseError(ErrorCode.MANUAL_CHECK_REQUIRED)

    def draft_payload(self, row, pulse, snapshot):
        return {"to": self.recipient, "subject": self.title(snapshot),
                "body_text": "Weekly review pulse for your review.\nDraft only; please review before sending.\n\n"
                             + pulse.rendered_body + "\n" + row["document_url"],
                "marker": "groww-pulse:" + row["publication_key"][:16]}

    def matches_draft(self, draft, args):
        return (draft.get("is_draft") is True and draft.get("to") == args["to"]
                and draft.get("subject") == args["subject"] and draft.get("body_text") == args["body_text"])

    def create_draft(self, state, pulse, snapshot):
        # Protect the verified Doc on every retry/resume and before another external write.
        self.verify_doc(state, pulse, snapshot)
        row = self.receipt(state, pulse, snapshot)
        key = row["publication_key"]
        args = self.draft_payload(row, pulse, snapshot)
        if row["draft_id"]:
            return

        def reconcile():
            drafts = self.read("gmail_find_drafts", marker=args["marker"]).get("drafts", [])
            if len(drafts) != 1:
                raise PulseError(ErrorCode.RECONCILIATION_REQUIRED)
            draft = self.read("gmail_get_draft", draft_id=ref(drafts[0].get("draft_id")))
            if not self.matches_draft(draft, args):
                raise PulseError(ErrorCode.RECONCILIATION_REQUIRED)
            return draft
        created = self.operation(key, "gmail_create_draft", args, reconcile)
        self.update(key, draft_id=ref(created.get("draft_id")))

    def verify_draft(self, state, pulse, snapshot):
        self.verify_doc(state, pulse, snapshot)
        row = self.receipt(state, pulse, snapshot)
        key = row["publication_key"]
        draft = self.read("gmail_get_draft", draft_id=ref(row["draft_id"]))
        args = self.draft_payload(row, pulse, snapshot)
        if draft.get("draft_id") != row["draft_id"] or not self.matches_draft(draft, args):
            raise PulseError(ErrorCode.HUMAN_EDIT_DETECTED)
        self.update(key, draft_hash=digest(json.dumps(args, sort_keys=True)), verified=1)
        self.verified(key, "gmail_create_draft", row["draft_id"])


def confirm_layout(conn, run_id):
    row = conn.execute("SELECT * FROM publication_receipts WHERE run_id=?", (str(run_id),)).fetchone()
    if not row or not row["doc_hash"] or row["layout_status"] != "MANUAL_CHECK_REQUIRED":
        raise PulseError(ErrorCode.LAYOUT_INVALID)
    conn.execute("UPDATE publication_receipts SET layout_status='MANUALLY_CONFIRMED' WHERE run_id=?", (str(run_id),))
