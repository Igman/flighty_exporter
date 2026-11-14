"""What every part of the model-server group shares: the HTTP opener all
requests to Ollama go through, and the errors that stop a model pass.

- ``OllamaUnreachable``: the server could not be reached at all mid-pass.
- ``PreflightError``: the pass cannot start (see ``preflight``).

Errors about one message (``DecisionError``, ``ExtractionFailed``) stay with
their clients: the run continues past them.
"""

from __future__ import annotations

import urllib.request

# Every request to Ollama goes through this opener, which ignores proxy
# settings (HTTP_PROXY and the like): a proxy would carry email content off
# this machine even when the address itself is local.
OLLAMA_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class OllamaUnreachable(Exception):
    """The server could not be reached at all; the run should stop."""


class PreflightError(Exception):
    """The pass cannot start; the message says why and what to do."""
