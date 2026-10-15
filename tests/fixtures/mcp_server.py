"""Fictional stdio MCP server used only by integration tests."""
from mcp.server.fastmcp import FastMCP

server = FastMCP("pulse-test-server")
documents, drafts = {}, {}


@server.tool()
def docs_create_document(title: str, marker: str) -> dict:
    rid = f"doc_{len(documents)}"
    documents[rid] = {"document_id": rid, "title": title, "marker": marker, "text": "",
                      "url": f"https://docs.google.com/document/d/{rid}/edit", "revision_id": "r1"}
    return documents[rid]


@server.tool()
def docs_write_content(document_id: str, content_markdown: str) -> dict:
    documents[document_id]["text"] = content_markdown
    return {"document_id": document_id, "revision_id": "r2"}


@server.tool()
def docs_get_document(document_id: str) -> dict:
    return documents[document_id]


@server.tool()
def docs_find_by_marker(marker: str) -> dict:
    return {"documents": [d for d in documents.values() if d["marker"] == marker]}


@server.tool()
def gmail_create_draft(to: str, subject: str, body_text: str, marker: str) -> dict:
    rid = f"draft_{len(drafts)}"
    drafts[rid] = {"draft_id": rid, "message_id": "message_0", "to": to, "subject": subject,
                   "body_text": body_text, "marker": marker, "is_draft": True}
    return drafts[rid]


@server.tool()
def gmail_get_draft(draft_id: str) -> dict:
    return drafts[draft_id]


@server.tool()
def gmail_find_drafts(marker: str) -> dict:
    return {"drafts": [d for d in drafts.values() if d["marker"] == marker]}


@server.tool()
def docs_get_page_count(document_id: str) -> dict:
    return {"page_count": 1}


if __name__ == "__main__":
    server.run(transport="stdio")
