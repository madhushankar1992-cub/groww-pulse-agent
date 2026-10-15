import json
from typing import Any

from groww_pulse.config import ModelConfig
from groww_pulse.errors import ErrorCode, PulseError
from groww_pulse.llm.base import Budget, Effort


class AnthropicLLM:
    """Claude via the official SDK with structured JSON output.

    Server-side refusal fallback is enabled so a policy decline is retried on
    another model inside the same call instead of failing the run.
    """

    def __init__(self, config: ModelConfig, budget: Budget):
        try:
            import anthropic
        except ImportError:
            raise PulseError(ErrorCode.LLM_UNAVAILABLE) from None
        self._anthropic = anthropic
        # Credentials resolve from ANTHROPIC_API_KEY or an `ant auth login` profile.
        self._client = anthropic.Anthropic(timeout=config.request_timeout_seconds, max_retries=3)
        self._budget = budget
        self.model_ref = config.model

    def complete_json(self, *, system: str, user: str, schema: dict[str, Any],
                      effort: Effort, max_tokens: int) -> dict[str, Any]:
        self._budget.check()
        errors = self._anthropic
        try:
            response = self._client.beta.messages.create(
                model=self.model_ref,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"effort": effort,
                               "format": {"type": "json_schema", "schema": schema}},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        # The SDK raises TypeError when no API key or auth profile can be resolved.
        except (errors.AuthenticationError, errors.PermissionDeniedError, TypeError):
            raise PulseError(ErrorCode.LLM_AUTH_FAILED) from None
        except errors.BadRequestError:
            raise PulseError(ErrorCode.LLM_REQUEST_INVALID) from None
        except (errors.RateLimitError, errors.APIStatusError, errors.APIConnectionError):
            raise PulseError(ErrorCode.LLM_UNAVAILABLE) from None

        usage = response.usage
        self._budget.record(usage.input_tokens, usage.output_tokens)
        if response.stop_reason == "refusal":
            raise PulseError(ErrorCode.LLM_REFUSED)
        if response.stop_reason == "max_tokens":
            raise PulseError(ErrorCode.LLM_OUTPUT_INVALID)
        text = next((b.text for b in response.content if b.type == "text"), None)
        try:
            data = json.loads(text) if text else None
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise PulseError(ErrorCode.LLM_OUTPUT_INVALID)
        return data


def build_llm(config: ModelConfig, budget: Budget):
    # Compatibility for callers using the original import location.
    from groww_pulse.llm.factory import build_llm as build
    return build(config, budget)
