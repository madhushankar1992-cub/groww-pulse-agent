from groww_pulse.errors import ErrorCode, PulseError


def model_ref(config):
    # Preserve existing Anthropic cache keys; never reuse them for a Groq deployment.
    return f"groq:{config.model}" if config.provider == "groq" else config.model


def build_llm(config, budget):
    if config.provider == "groq":
        from groww_pulse.llm.groq_llm import GroqLLM
        return GroqLLM(config, budget)
    if config.provider == "anthropic":
        from groww_pulse.llm.anthropic_llm import AnthropicLLM
        return AnthropicLLM(config, budget)
    raise PulseError(ErrorCode.CONFIG_INVALID)
