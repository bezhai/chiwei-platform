"""Google Generative AI 客户端

提供 CustomChatGoogleGenerativeAI 类，修复了底层库的初始化问题。
"""

from typing import Self, cast

from google.genai.client import Client
from google.genai.types import HttpOptions
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_google_genai._common import get_user_agent
from pydantic import SecretStr, model_validator


class CustomChatGoogleGenerativeAI(ChatGoogleGenerativeAI):
    """
    Custom implementation of ChatGoogleGenerativeAI to patch underlying library issues.

    Key fixes:
    1. Injects `api_version="v1"` into HttpOptions to resolve initialization errors.
    2. Simplifies client initialization for the standard Developer API.
    """

    @model_validator(mode="after")
    def validate_environment(self) -> Self:
        """
        Validates parameters and explicitly builds the Google GenAI Client
        with the necessary patches.
        """

        # Override temperature to 1.0 to ensure stability
        # (Original logic had complex checks for Gemini 3+, here simplified to enforcement)
        self.temperature = 1.0

        if self.top_p is not None and not 0 <= self.top_p <= 1:
            raise ValueError("top_p must be in the range [0.0, 1.0]")

        if self.top_k is not None and self.top_k <= 0:
            raise ValueError("top_k must be positive")

        additional_headers = self.additional_headers or {}
        self.default_metadata = tuple(additional_headers.items())

        _, user_agent = get_user_agent("ChatGoogleGenerativeAI")
        headers = {"User-Agent": user_agent, **additional_headers}

        google_api_key = None
        if not self.credentials:
            if isinstance(self.google_api_key, SecretStr):
                google_api_key = self.google_api_key.get_secret_value()
            else:
                google_api_key = self.google_api_key

        if isinstance(self.base_url, dict):
            raise ValueError(
                "In this fixed version, base_url must be a string, not a dict."
            )

        base_url = self.base_url

        http_options = HttpOptions(
            base_url=cast(str, base_url),
            headers=headers,
            client_args=self.client_args,
            async_client_args=self.client_args,
            api_version="v1",  # <--- PATCHED HERE: The missing parameter
        )

        if not google_api_key:
            msg = (
                "API key required for Gemini Developer API. Provide `api_key` "
                "parameter or set GOOGLE_API_KEY/GEMINI_API_KEY environment variable."
            )
            raise ValueError(msg)

        self.client = Client(api_key=google_api_key, http_options=http_options)

        return self
