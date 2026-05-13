"""Provider clients (OpenAI image, xAI Grok, Google Gen AI, Kling)."""


class ProviderNotConfigured(Exception):
    """Raised when a provider's required credentials are not set.

    Mapped to HTTP 503 by the API layer with a user-readable message —
    used to flag locked model choices in the UI without crashing the request.
    """

    def __init__(self, provider: str, hint: str = "") -> None:
        msg = f"{provider} is not configured"
        if hint:
            msg += f". {hint}"
        super().__init__(msg)
        self.provider = provider
        self.hint = hint
