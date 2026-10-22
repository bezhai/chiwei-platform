"""What the agent layer exports for plugins: they import these from :mod:`app.agent`, not from the
modules that implement them."""
from __future__ import annotations

import app.agent
import app.agent.core


def test_the_exceptions_a_model_call_is_retried_on_are_exported_once():
    """A plugin that retries a whole model call itself (world's perception judgement) retries on
    the same exceptions the agent layer retries one call on: the one tuple, not a copy."""
    assert "RETRYABLE_EXCEPTIONS" in app.agent.__all__
    assert app.agent.RETRYABLE_EXCEPTIONS is app.agent.core.RETRYABLE_EXCEPTIONS
