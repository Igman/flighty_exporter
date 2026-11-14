"""Config file: TOML, every setting optional with a default.

Relative paths in the file are resolved against the file's own directory, so a
config next to the mailbox works wherever the tool is run from.

    mailbox = "Takeout/Mail/All mail Including Spam and Trash.mbox"
    store = "flighty_exporter.db"

    [ollama]
    address = "http://localhost:11434"
    timeout_seconds = 300
    allow_remote = false   # true lets email content go to a non-local address

    [models]
    decision = "nimble"
    extraction = "gemma4:12b-it-q4_K_M"

    [classify]
    threshold = 0.05
    max_body_chars = 12000

    [extract]
    num_ctx = 16384
    max_input_chars = 36000
    num_predict = 1500
    retries = 2

    [review]
    output = "review.csv"
    failures = "failures.csv"
    min_confidence = 0.7

    [passenger]
    names = []          # e.g. ["Jordan Ellis", "Jordan A. Ellis"]

    [audit]
    sample_size = 25
    seed = 1
    output = "audit.csv"
    auto_include_budget = 2000

    [export]
    output_dir = "."
    data_dir = "~/.cache/flighty_exporter/openflights"
    data_url = "https://raw.githubusercontent.com/jpatokal/openflights/master/data/"

    [run]
    thoroughness = "quick"   # or "thorough"
"""

from __future__ import annotations

import tomllib
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .flights.passengers import names_someone

DEFAULT_CONFIG_NAME = "flighty_exporter.toml"
DEFAULT_DATA_DIR = Path.home() / ".cache" / "flighty_exporter" / "openflights"
DEFAULT_DATA_URL = "https://raw.githubusercontent.com/jpatokal/openflights/master/data/"

# How hard `run` looks for missed flight emails: Quick runs no audit.
QUICK = "quick"
THOROUGH = "thorough"
THOROUGHNESS = (QUICK, THOROUGH)


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    mailbox: Path | None = None
    store: Path = Path(DEFAULT_CONFIG_NAME).with_suffix(".db")
    ollama_address: str = "http://localhost:11434"
    ollama_timeout_seconds: float = 300.0
    # The model commands refuse an Ollama address that is not on this machine
    # (see ``preflight``) unless this is set (or --allow-remote is given).
    ollama_allow_remote: bool = False
    decision_model: str = "nimble"
    extraction_model: str = "gemma4:12b-it-q4_K_M"
    # Gate probability at or above which a message goes to the extract pass.
    # Deliberately low: the probabilities are uncalibrated and the threshold is
    # only applied when selecting messages for extraction, never at classify time.
    threshold: float = 0.05
    # How much of the cleaned body (in characters) the decision model sees, so
    # that the whole prompt fits its 8,192-token and 64 KiB limits. An estimate:
    # a prompt the server still rejects is retried with the body halved.
    classify_max_body_chars: int = 12000
    # Extraction request settings: context length (long email plus schema),
    # output cap, and how many times an invalid or cut-off answer is retried.
    extract_num_ctx: int = 16384
    extract_num_predict: int = 1500
    extract_retries: int = 2
    # Cap on the extraction input (body, then PDF text) in characters, to stay
    # inside num_ctx. A cut input is still extracted and counted as truncated.
    extract_max_input_chars: int = 36000
    review_output: Path = Path("review.csv")
    failures_output: Path = Path("failures.csv")
    # Confidence cut-off: a flight whose confidence (after halving once per
    # failed code, flight-number, route or date check) is below it is flagged
    # "low confidence" and pre-set to not keep in the review table.
    review_min_confidence: float = 0.7
    # Where export writes the import file and the manual-add list.
    export_output_dir: Path = Path(".")
    # Per-user cache of OpenFlights' airports.dat and airlines.dat, downloaded
    # from data_url on first use.
    export_data_dir: Path = DEFAULT_DATA_DIR
    export_data_url: str = DEFAULT_DATA_URL
    # The user's name variants as they appear on bookings. Bookings whose
    # passengers include none of them are left out of the review table. Empty
    # turns passenger filtering off.
    passenger_names: tuple[str, ...] = ()
    # Audit: messages sampled per gate-probability band, the seed that makes
    # the sample reproducible, and the CSV the report is written to.
    audit_sample_size: int = 25
    audit_seed: int = 1
    audit_output: Path = Path("audit.csv")
    # How thorough `run` is (QUICK or THOROUGH); `run --thoroughness` overrides it.
    thoroughness: str = QUICK
    # Thorough runs: the most not-yet-extracted messages a lowered threshold
    # may add for the run to lower the session's threshold on its own.
    audit_auto_include_budget: int = 2000


# The schema: section -> key -> (Config field, expected type(s)).
_SCHEMA: dict[str | None, dict[str, tuple[str, tuple[type, ...]]]] = {
    None: {"mailbox": ("mailbox", (str,)), "store": ("store", (str,))},
    "ollama": {
        "address": ("ollama_address", (str,)),
        "timeout_seconds": ("ollama_timeout_seconds", (int, float)),
        "allow_remote": ("ollama_allow_remote", (bool,)),
    },
    "models": {
        "decision": ("decision_model", (str,)),
        "extraction": ("extraction_model", (str,)),
    },
    "classify": {
        "threshold": ("threshold", (int, float)),
        "max_body_chars": ("classify_max_body_chars", (int,)),
    },
    "extract": {
        "num_ctx": ("extract_num_ctx", (int,)),
        "num_predict": ("extract_num_predict", (int,)),
        "retries": ("extract_retries", (int,)),
        "max_input_chars": ("extract_max_input_chars", (int,)),
    },
    "review": {
        "output": ("review_output", (str,)),
        "failures": ("failures_output", (str,)),
        "min_confidence": ("review_min_confidence", (int, float)),
    },
    "passenger": {
        "names": ("passenger_names", (list,)),
    },
    "audit": {
        "sample_size": ("audit_sample_size", (int,)),
        "seed": ("audit_seed", (int,)),
        "output": ("audit_output", (str,)),
        "auto_include_budget": ("audit_auto_include_budget", (int,)),
    },
    "export": {
        "output_dir": ("export_output_dir", (str,)),
        "data_dir": ("export_data_dir", (str,)),
        "data_url": ("export_data_url", (str,)),
    },
    "run": {
        "thoroughness": ("thoroughness", (str,)),
    },
}

_PATH_FIELDS = (
    "mailbox", "store", "review_output", "failures_output", "audit_output", "export_output_dir", "export_data_dir",
)


def load_config(path: Path | None) -> Config:
    """Load the config file; a missing default file means all defaults.

    ``path`` is the file named with ``--config``; if None, the default file
    name in the current directory is used when it exists.
    """
    explicit = path is not None
    path = path or Path(DEFAULT_CONFIG_NAME)
    if not path.exists():
        if explicit:
            raise ConfigError(f"config file not found: {path}")
        return _resolve({}, Path.cwd())
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc
    return _resolve(data, path.resolve().parent)


def _resolve(data: dict[str, Any], base: Path) -> Config:
    values: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, dict) and key in _SCHEMA and key is not None:
            section = _SCHEMA[key]
            for sub_key, sub_value in value.items():
                if sub_key not in section:
                    raise ConfigError(f"unknown config setting: [{key}] {sub_key}")
                field, types = section[sub_key]
                values[field] = _check(f"[{key}] {sub_key}", sub_value, types)
        elif key in _SCHEMA[None]:
            field, types = _SCHEMA[None][key]
            values[field] = _check(key, value, types)
        else:
            raise ConfigError(f"unknown config setting: {key}")

    for field in _PATH_FIELDS:
        if field in values:
            values[field] = _path(values[field], base)
        elif field != "mailbox":
            values[field] = base / getattr(Config, field)
    if "threshold" in values and not 0.0 <= values["threshold"] <= 1.0:
        raise ConfigError("[classify] threshold must be between 0 and 1")
    if "review_min_confidence" in values:
        if not 0.0 <= values["review_min_confidence"] <= 1.0:
            raise ConfigError("[review] min_confidence must be between 0 and 1")
        values["review_min_confidence"] = float(values["review_min_confidence"])
    for field, name, minimum in (
        ("extract_num_ctx", "[extract] num_ctx", 1),
        ("extract_num_predict", "[extract] num_predict", 1),
        ("extract_retries", "[extract] retries", 0),
        ("classify_max_body_chars", "[classify] max_body_chars", 1),
        ("extract_max_input_chars", "[extract] max_input_chars", 1),
        ("audit_sample_size", "[audit] sample_size", 1),
        ("audit_auto_include_budget", "[audit] auto_include_budget", 0),
    ):
        if field in values and values[field] < minimum:
            raise ConfigError(f"{name} must be at least {minimum}")
    if "passenger_names" in values:
        names = values["passenger_names"]
        for name in names:
            if not isinstance(name, str) or not names_someone(name):
                raise ConfigError(
                    f"[passenger] names must be a list of names, e.g. [\"Jordan Ellis\"]; {name!r} is not a name"
                )
        values["passenger_names"] = tuple(" ".join(name.split()) for name in names)
    if "thoroughness" in values and values["thoroughness"] not in THOROUGHNESS:
        raise ConfigError(f'[run] thoroughness must be "quick" or "thorough", got {values["thoroughness"]!r}')
    if "ollama_address" in values:
        values["ollama_address"] = _ollama_address(values["ollama_address"])
    return Config(**values)


def _ollama_address(value: str) -> str:
    address = value.strip().rstrip("/")
    try:
        parsed = urllib.parse.urlsplit(address)
        parsed.port  # noqa: B018 (raises ValueError on a bad port)
    except ValueError as exc:
        raise ConfigError(f"[ollama] address is not a valid URL: {value!r} ({exc})") from exc
    if parsed.scheme.lower() not in ("http", "https"):
        raise ConfigError(
            f"[ollama] address must start with http:// or https://, got {value!r} "
            "(e.g. \"http://localhost:11434\")"
        )
    if not parsed.hostname:
        raise ConfigError(f"[ollama] address has no host name: {value!r} (e.g. \"http://localhost:11434\")")
    return address


def _check(name: str, value: Any, types: tuple[type, ...]) -> Any:
    if bool in types:
        if not isinstance(value, bool):
            raise ConfigError(f"config setting {name} must be true or false, got {value!r}")
        return value
    if isinstance(value, bool) or not isinstance(value, types):
        expected = " or ".join(t.__name__ for t in types)
        raise ConfigError(f"config setting {name} must be {expected}, got {value!r}")
    return value


def _path(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else base / path
