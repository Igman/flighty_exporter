"""What every model pass needs before it starts, shared by the single
commands and the run engine: the locality check, the two model clients, and
the store opened with the preflight check run lazily.

The preflight check runs just before a client's first model request, so a
pass that turns out to need none never contacts the server. Until it has
passed, the pass's writes are held, so a failed check leaves the store as it
was (and a store this run created is removed again).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from ..config import Config
from ..ollama.connection import PreflightError
from ..ollama.decision import DecisionClient
from ..ollama.extraction import ExtractionClient
from ..ollama.preflight import PREFLIGHT_TIMEOUT, check_address, check_server
from ..store import Store

DECISION_SETTING = "[models] decision"
EXTRACTION_SETTING = "[models] extraction"


def address_warning(config: Config, allow_remote: bool) -> str | None:
    """Refuse a non-local Ollama address (``PreflightError``) before anything
    is sent to it; return the warning to show when the override lets one
    through."""
    return check_address(config.ollama_address, config.ollama_allow_remote or allow_remote)


def decision_client(config: Config) -> DecisionClient:
    return DecisionClient(
        config.ollama_address,
        config.decision_model,
        config.ollama_timeout_seconds,
        max_body_chars=config.classify_max_body_chars,
    )


def extraction_client(config: Config) -> ExtractionClient:
    """The extraction client extract and audit both use, so both send the same requests."""
    return ExtractionClient(
        config.ollama_address,
        config.extraction_model,
        timeout=config.ollama_timeout_seconds,
        num_ctx=config.extract_num_ctx,
        num_predict=config.extract_num_predict,
        retries=config.extract_retries,
        max_input_chars=config.extract_max_input_chars,
    )


@contextmanager
def pass_store(
    store_path: Path,
    client: DecisionClient | ExtractionClient,
    setting: str,
    command: str,
    config: Config,
    warn: Callable[[str], None],
) -> Iterator[Store]:
    """Open the store for a model pass, with the preflight check run just
    before the client's first request (``command`` is named in its messages,
    ``warn`` receives its warnings). Writes are held until the check has
    passed; if it fails they are rolled back, and a store this run created
    is removed again."""
    existed = store_path.exists()
    store = Store(store_path)
    store.hold_writes()

    def preflight() -> None:
        check_server(
            config.ollama_address,
            client.model,
            setting,
            command,
            timeout=min(PREFLIGHT_TIMEOUT, config.ollama_timeout_seconds),
            warn=warn,
        )
        store.release_writes(keep=True)

    client.before_first_request = preflight
    failed = False
    try:
        yield store
    except PreflightError:
        failed = True
        raise
    finally:
        store.release_writes(keep=not failed)
        store.close()
        if failed and not existed:
            for suffix in ("", "-wal", "-shm", "-journal"):
                Path(f"{store_path}{suffix}").unlink(missing_ok=True)
