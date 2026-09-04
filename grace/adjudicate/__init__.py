from grace.adjudicate.offline import OfflineAdjudicator
from grace.adjudicate.schema import Decision


def make_llm_adjudicator(provider: str | None = None, *, effort: str | None = None,
                         model: str | None = None):
    """Construct the configured LLM adjudicator.

    Both providers return the same `Decision` and are held to the same policy
    gate, so swapping them changes nothing downstream.
    """
    from grace.config import CONFIG

    provider = (provider or CONFIG.provider).lower()
    if provider in ("gemini", "google"):
        from grace.adjudicate.gemini import GeminiAdjudicator

        # An explicitly passed model is PINNED (no fallbacks). GRACE_MODEL from
        # the environment is only a preferred first model; the chain still applies.
        return GeminiAdjudicator(model=model, effort=effort, pin=model is not None)
    if provider in ("anthropic", "claude"):
        from grace.adjudicate.claude import ClaudeAdjudicator

        return ClaudeAdjudicator(model=model, effort=effort)
    raise ValueError(f"unknown GRACE_PROVIDER {provider!r}; expected 'gemini' or 'anthropic'")


def credentials_present(provider: str | None = None) -> tuple[bool, str]:
    """(ok, message) for the configured provider.

    The two SDKs differ here, which is worth knowing:
      * google-genai RAISES at construction with no key, so a Gemini run fails
        fast anyway; this check just reports it before the cohort is copied.
      * the Anthropic client does NOT raise at construction. Without this
        check, every adjudication would fall back to escalate and the run would
        still exit 0 and look successful. That is the failure this guards.
    """
    import os

    from grace.config import CONFIG

    provider = (provider or CONFIG.provider).lower()
    if provider in ("gemini", "google"):
        if os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"):
            return True, ""
        return False, ("--online needs GEMINI_API_KEY (or GOOGLE_API_KEY).\n"
                       "Get one at https://aistudio.google.com/apikey, then:\n"
                       "    export GEMINI_API_KEY=...")
    if os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"):
        return True, ""
    return False, "--online with GRACE_PROVIDER=anthropic needs ANTHROPIC_API_KEY."


def sdk_present(provider: str | None = None) -> tuple[bool, str]:
    from grace.config import CONFIG

    provider = (provider or CONFIG.provider).lower()
    if provider in ("gemini", "google"):
        try:
            import google.genai
        except ImportError:
            return False, 'Gemini needs the SDK: pip install -e ".[llm]"'
        return True, ""
    try:
        import anthropic
    except ImportError:
        return False, 'Anthropic needs the SDK: pip install -e ".[anthropic]"'
    return True, ""


__all__ = [
    "Decision",
    "OfflineAdjudicator",
    "credentials_present",
    "make_llm_adjudicator",
    "sdk_present",
]
