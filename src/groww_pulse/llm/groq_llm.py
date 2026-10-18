"""Groq structured outputs, with local validation of the original schema."""

import json
import os
import time

from groww_pulse.errors import ErrorCode, PulseError
from groww_pulse.llm.factory import model_ref


def wire_schema(schema):
    """Use a portable strict-schema subset; enforce all original constraints locally."""
    def visit(node):
        if isinstance(node, list):
            return [visit(v) for v in node]
        if not isinstance(node, dict):
            return node
        out = {}
        for key in ("type", "enum", "const", "$ref", "anyOf", "items"):
            if key in node:
                out[key] = visit(node[key])
        for key in ("properties", "$defs"):
            if key in node:
                out[key] = {name: visit(value) for name, value in node[key].items()}
        if out.get("type") == "object" or "properties" in out:
            out["additionalProperties"] = False
            out["required"] = list(out.get("properties", {}))
        return out
    return visit(schema)


class GroqLLM:
    def __init__(self, config, budget, *, client=None, sleep=time.sleep, rate_limit_waits=6):
        try:
            import groq
            from jsonschema import Draft202012Validator, FormatChecker
        except ImportError:
            raise PulseError(ErrorCode.LLM_UNAVAILABLE) from None
        self.errors = groq
        self.validator, self.formats = Draft202012Validator, FormatChecker()
        self.config, self.budget = config, budget
        self.model_ref = model_ref(config)
        if client is None:
            key = os.environ.get("GROQ_API_KEY", "").strip()
            if not key:
                raise PulseError(ErrorCode.LLM_AUTH_FAILED)
            # SDK retries connection errors, 408/409/429 and 5xx at most twice.
            client = groq.Groq(api_key=key, timeout=config.request_timeout_seconds, max_retries=2)
        self.client = client
        self.sleep, self.rate_limit_waits = sleep, rate_limit_waits

    @staticmethod
    def _delay(exc, attempt):
        """Seconds to wait after a 429: the server's retry-after if present, else a growing pause."""
        try:
            seconds = float(exc.response.headers.get("retry-after", ""))
        except (AttributeError, TypeError, ValueError):
            seconds = 15.0 * (attempt + 1)
        return min(max(seconds, 1.0) + 0.5, 65.0)  # the per-minute window resets within 60 s

    def complete_json(self, *, system, user, schema, effort, max_tokens):
        self.budget.check()
        try:
            self.validator.check_schema(schema)
        except Exception:
            raise PulseError(ErrorCode.LLM_REQUEST_INVALID) from None
        response = None
        for attempt in range(self.rate_limit_waits + 1):
            try:
                response = self.client.chat.completions.create(
                    model=self.config.model,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                    response_format={"type": "json_schema", "json_schema": {
                        "name": "pulse_output", "strict": True, "schema": wire_schema(schema)}},
                    reasoning_effort=effort,
                    max_completion_tokens=max_tokens,
                )
                break
            except (self.errors.AuthenticationError, self.errors.PermissionDeniedError):
                raise PulseError(ErrorCode.LLM_AUTH_FAILED) from None
            except (self.errors.BadRequestError, self.errors.NotFoundError,
                    self.errors.UnprocessableEntityError):
                raise PulseError(ErrorCode.LLM_REQUEST_INVALID) from None
            except self.errors.RateLimitError as exc:
                # 429: the free tier allows a fixed number of tokens per minute. Waiting for the
                # window to reset fixes it, so pause (bounded) instead of failing the whole run.
                if attempt == self.rate_limit_waits:
                    raise PulseError(ErrorCode.LLM_RATE_LIMITED) from None
                self.sleep(self._delay(exc, attempt))
            except self.errors.APIStatusError as exc:
                # 413 means this single request exceeds the per-minute limit; waiting cannot help.
                if getattr(exc, "status_code", None) == 413:
                    raise PulseError(ErrorCode.LLM_RATE_LIMITED) from None
                raise PulseError(ErrorCode.LLM_UNAVAILABLE) from None
            except self.errors.APIConnectionError:
                raise PulseError(ErrorCode.LLM_UNAVAILABLE) from None
        usage = response.usage
        if usage is None or usage.prompt_tokens is None or usage.completion_tokens is None:
            raise PulseError(ErrorCode.LLM_OUTPUT_INVALID)
        self.budget.record(usage.prompt_tokens, usage.completion_tokens)
        if not response.choices:
            raise PulseError(ErrorCode.LLM_OUTPUT_INVALID)
        choice = response.choices[0]
        if choice.finish_reason == "content_filter" or getattr(choice.message, "refusal", None):
            raise PulseError(ErrorCode.LLM_REFUSED)
        if choice.finish_reason != "stop":
            raise PulseError(ErrorCode.LLM_OUTPUT_INVALID)
        try:
            data = json.loads(choice.message.content or "")
        except (ValueError, TypeError):
            raise PulseError(ErrorCode.LLM_OUTPUT_INVALID) from None
        if not isinstance(data, dict) or not self.validator(schema, format_checker=self.formats).is_valid(data):
            raise PulseError(ErrorCode.LLM_OUTPUT_INVALID)
        return data
