import json
from pathlib import Path
from typing import Annotated

from pydantic import Field, ValidationError, model_validator

from groww_pulse.config import Contract, read_yaml
from groww_pulse.errors import ErrorCode, PulseError

ToolName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
FieldName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]

DEFAULT_CONTRACT = Path("config/mcp_contract.yaml")


class ToolContract(Contract):
    version: int = Field(ge=1, strict=True)
    required: dict[ToolName, tuple[FieldName, ...]]
    optional: dict[ToolName, tuple[FieldName, ...]] = Field(default_factory=dict)
    forbidden_patterns: tuple[Annotated[str, Field(min_length=3)], ...]

    @model_validator(mode="after")
    def coherent(self):
        if set(self.required) & set(self.optional):
            raise ValueError("tool listed twice")
        for name in [*self.required, *self.optional]:
            if is_forbidden(name, self.forbidden_patterns):
                raise ValueError("contract allows a forbidden tool")
        return self

    @property
    def allowlist(self) -> frozenset[str]:
        return frozenset([*self.required, *self.optional])


class CapabilityReport(Contract):
    ok: bool
    missing_required: tuple[str, ...] = ()
    missing_inputs: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    available_optional: tuple[str, ...] = ()
    ignored_forbidden: tuple[str, ...] = ()
    ignored_other: int = 0


def is_forbidden(name: str, patterns: tuple[str, ...]) -> bool:
    lowered = name.lower()
    return any(p.lower() in lowered for p in patterns)


def load_contract(path: Path = DEFAULT_CONTRACT) -> ToolContract:
    try:
        return ToolContract.model_validate(read_yaml(path, ErrorCode.CAPABILITIES_INVALID))
    except (ValidationError, ValueError):
        raise PulseError(ErrorCode.CAPABILITIES_INVALID) from None


def load_tools_dump(path: Path) -> dict[str, frozenset[str]]:
    """Read a captured MCP `list_tools` result: {"tools": [{"name", "inputSchema"}]}."""
    try:
        if path.stat().st_size > 2_000_000:
            raise PulseError(ErrorCode.CAPABILITIES_INVALID)
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        tools = data["tools"] if isinstance(data, dict) else data
        return {
            str(t["name"]): frozenset((t.get("inputSchema") or {}).get("properties", {}))
            for t in tools
        }
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, AttributeError):
        raise PulseError(ErrorCode.CAPABILITIES_INVALID) from None


def check_capabilities(contract: ToolContract, tools: dict[str, frozenset[str]]) -> CapabilityReport:
    missing = tuple(sorted(n for n in contract.required if n not in tools))
    missing_inputs = {
        name: tuple(sorted(set(fields) - tools[name]))
        for name, fields in {**contract.required, **contract.optional}.items()
        if name in tools and set(fields) - tools[name]
    }
    optional = tuple(sorted(n for n in contract.optional if n in tools and n not in missing_inputs))
    forbidden = tuple(sorted(n for n in tools if is_forbidden(n, contract.forbidden_patterns)))
    other = sum(1 for n in tools if n not in contract.allowlist and n not in forbidden)
    return CapabilityReport(
        ok=not missing and not (set(missing_inputs) & set(contract.required)),
        missing_required=missing,
        missing_inputs=missing_inputs,
        available_optional=optional,
        ignored_forbidden=forbidden,
        ignored_other=other,
    )
