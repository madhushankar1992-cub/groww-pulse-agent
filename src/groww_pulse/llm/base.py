from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Literal, Protocol

from groww_pulse.config import ModelConfig
from groww_pulse.errors import ErrorCode, PulseError

Effort = Literal["low", "medium", "high"]


class LLM(Protocol):
    model_ref: str

    def complete_json(self, *, system: str, user: str, schema: dict[str, Any],
                      effort: Effort, max_tokens: int) -> dict[str, Any]:
        """Return a JSON object matching `schema`, or raise PulseError with a safe code."""


@dataclass
class Budget:
    """Per-run token and cost guard shared by every model call."""

    config: ModelConfig
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    on_record: Callable[[dict], None] | None = field(default=None, repr=False)
    _lock: Lock = field(default_factory=Lock, repr=False)

    @property
    def cost_usd(self) -> float:
        return (self.input_tokens * self.config.input_usd_per_mtok
                + self.output_tokens * self.config.output_usd_per_mtok) / 1_000_000

    def check(self) -> None:
        if (self.input_tokens > self.config.max_input_tokens_per_run
                or self.output_tokens > self.config.max_output_tokens_per_run
                or self.cost_usd > self.config.max_cost_usd_per_run):
            raise PulseError(ErrorCode.BUDGET_EXCEEDED)

    def record(self, input_tokens: int, output_tokens: int) -> None:
        with self._lock:
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens
            self.calls += 1
            if self.on_record is not None:
                self.on_record(self.summary())
        self.check()

    def summary(self) -> dict[str, Any]:
        return {"calls": self.calls, "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens, "cost_usd": round(self.cost_usd, 4)}


def strict_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties,
            "required": required if required is not None else list(properties),
            "additionalProperties": False}
