import asyncio
import json
import re
from contextlib import asynccontextmanager, contextmanager
from datetime import timedelta
from typing import Literal

import anyio
from pydantic import Field, ValidationError, model_validator

from groww_pulse.config import Contract, read_yaml
from groww_pulse.errors import ErrorCode, PulseError
from groww_pulse.integrations.contract import check_capabilities, load_contract

OUTPUTS = {
    "docs_create_document": {"document_id", "url"},
    "docs_write_content": {"document_id", "revision_id"},
    "docs_get_document": {"document_id", "title", "text", "url", "revision_id"},
    "docs_find_by_marker": {"documents"},
    "gmail_create_draft": {"draft_id", "message_id"},
    "gmail_get_draft": {"draft_id", "to", "subject", "body_text", "is_draft"},
    "gmail_find_drafts": {"drafts"},
    "docs_get_page_count": {"page_count"},
    "gmail_update_draft": {"draft_id", "message_id"},
}


class MCPConfig(Contract):
    transport: Literal["stdio", "streamable_http"] = "stdio"
    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None
    timeout_seconds: int = Field(default=30, ge=1, le=120)
    recipient_env: str = Field(default="PULSE_DRAFT_RECIPIENT", pattern=r"^[A-Z][A-Z0-9_]+$")

    @model_validator(mode="after")
    def coherent(self):
        if self.transport == "stdio" and not self.command:
            raise ValueError("missing command")
        if self.transport == "streamable_http" and (not self.url or not re.match(r"https?://", self.url)):
            raise ValueError("missing URL")
        return self


def load_mcp_config(path):
    try:
        return MCPConfig.model_validate(read_yaml(path, ErrorCode.CONFIG_INVALID))
    except ValidationError:
        raise PulseError(ErrorCode.CONFIG_INVALID) from None


class SafeClient:
    def __init__(self, backend, contract=None):
        self.backend = backend
        self.contract = contract or load_contract()
        self.optional = ()
        self.checked = False

    def check(self):
        report = check_capabilities(self.contract, self.backend.list_tools())
        if not report.ok:
            raise PulseError(ErrorCode.CAPABILITY_UNAVAILABLE)
        self.optional = report.available_optional
        self.checked = True

    def call(self, name, **arguments):
        if name not in self.contract.allowlist or not self.checked:
            raise PulseError(ErrorCode.CAPABILITY_UNAVAILABLE)
        fields = {**self.contract.required, **self.contract.optional}[name]
        if set(arguments) != set(fields):
            raise PulseError(ErrorCode.MCP_RESPONSE_INVALID)
        data = self.backend.call(name, arguments)
        if not isinstance(data, dict):
            raise PulseError(ErrorCode.MCP_RESPONSE_INVALID)
        result = {k: v for k, v in data.items() if k in OUTPUTS[name]}
        for container, fields in (("documents", {"document_id", "title", "url"}),
                                  ("drafts", {"draft_id", "subject"})):
            if container in result:
                if not isinstance(result[container], list) or len(result[container]) > 100:
                    raise PulseError(ErrorCode.MCP_RESPONSE_INVALID)
                if not all(isinstance(v, dict) for v in result[container]):
                    raise PulseError(ErrorCode.MCP_RESPONSE_INVALID)
                result[container] = [{k: v for k, v in item.items() if k in fields}
                                     for item in result[container]]
        return result


@asynccontextmanager
async def session_context(config):
    import os

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamablehttp_client
    # Child process stderr is untrusted and may contain account details.
    with open(os.devnull, "w") as sink:
        transport = (stdio_client(StdioServerParameters(command=config.command, args=list(config.args)),
                                  errlog=sink) if config.transport == "stdio" else
                     streamablehttp_client(config.url, timeout=config.timeout_seconds,
                                           sse_read_timeout=config.timeout_seconds))
        async with transport as streams:
            async with ClientSession(streams[0], streams[1],
                                     read_timeout_seconds=timedelta(seconds=config.timeout_seconds)) as session:
                await session.initialize()
                yield session


class SDKBackend:
    def __init__(self, portal, session, timeout):
        self.portal, self.session, self.timeout = portal, session, timeout

    def list_tools(self):
        async def request():
            async with asyncio.timeout(self.timeout):
                tools, cursor = {}, None
                for _ in range(20):
                    result = await self.session.list_tools(cursor=cursor)
                    tools.update({t.name: frozenset(t.inputSchema.get("properties", {})) for t in result.tools})
                    cursor = result.nextCursor
                    if not cursor:
                        return tools
                raise PulseError(ErrorCode.CAPABILITIES_INVALID)
        try:
            return self.portal.call(request)
        except Exception:
            raise PulseError(ErrorCode.MCP_UNAVAILABLE) from None

    def call(self, name, arguments):
        async def request():
            async with asyncio.timeout(self.timeout):
                result = await self.session.call_tool(name, arguments)
                if result.isError:
                    raise PulseError(ErrorCode.MCP_UNAVAILABLE)
                if result.structuredContent is not None:
                    return result.structuredContent
                blocks = [c.text for c in result.content if c.type == "text"]
                if len(blocks) != 1 or len(blocks[0]) > 200_000:
                    raise PulseError(ErrorCode.MCP_RESPONSE_INVALID)
                return json.loads(blocks[0])
        try:
            return self.portal.call(request)
        except Exception:
            raise PulseError(ErrorCode.MCP_UNAVAILABLE) from None


@contextmanager
def connect_mcp(config):
    try:
        with anyio.from_thread.start_blocking_portal() as portal:
            with portal.wrap_async_context_manager(session_context(config)) as session:
                yield SafeClient(SDKBackend(portal, session, config.timeout_seconds))
    except PulseError:
        raise
    except Exception:
        raise PulseError(ErrorCode.MCP_UNAVAILABLE) from None
