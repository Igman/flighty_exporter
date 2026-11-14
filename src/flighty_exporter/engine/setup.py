"""Guided setup's three answers: checking them and writing them to the config file.

Setup asks for the mailbox file, the user's name variants and thoroughness.
This module is everything setup decides or writes, shared by the ``setup``
command and the application's setup screen (which only collects the answers
and shows what comes back):

- ``mailbox_problem`` says why a path is not a usable mailbox (missing, a
  folder, a zip file, unreadable, not an mbox), or None;
- ``parse_name_variants`` splits what was typed into variants, and
  ``check_names`` applies the config loader's own check to them;
- ``write_answers`` writes the three settings into the config file, creating
  it if needed. The file is edited as text, setting by setting: every other
  setting, comment and section stays byte for byte as it was. The result is
  read back with the config loader before it replaces the file, so a file
  the loader would reject is never written.
- ``answers_from_config`` and ``answers_from_session`` give the answers to
  prefill.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from ..config import THOROUGHNESS, Config, ConfigError, check_passenger_names, load_config
from .session import SessionDescription

TAKEOUT_MBOX = "Takeout/Mail/All mail Including Spam and Trash.mbox"

NEW_FILE_HEADER = (
    "# Written by flighty-exporter setup. Every other setting has its default;\n"
    "# flighty_exporter.example.toml lists them all. Relative paths are read from\n"
    "# this file's folder.\n"
)


class SetupError(Exception):
    """An answer cannot be used, or the config file cannot be updated; the
    message says why. Nothing has been written."""


@dataclass(frozen=True)
class SetupAnswers:
    mailbox: str  # as shown to and typed by the user; empty when not known
    names: tuple[str, ...]
    thoroughness: str


@dataclass(frozen=True)
class WriteResult:
    config_path: Path
    created: bool  # False: an existing file was updated
    mailbox: Path  # absolute, as the loader reads it back
    mailbox_setting: str  # the text written for `mailbox`
    names: tuple[str, ...]
    thoroughness: str


# -- the mailbox ------------------------------------------------------------------------


def mailbox_path(text: str, base: Path) -> Path | None:
    """The path typed (or pasted) as ``text``, relative paths read from
    ``base``; None for nothing typed. Surrounding quotes and the backslashes
    a terminal adds before spaces when a file is dragged in are removed."""
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1]
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.exists() and "\\ " in text:
        path = Path(text.replace("\\ ", " ")).expanduser()
    return path if path.is_absolute() else base / path


def mailbox_problem(path: Path | None) -> str | None:
    """Why ``path`` is not a mailbox setup can use, or None: it must exist,
    be a regular file, be readable and begin like an mbox (a first line
    starting with "From ")."""
    if path is None:
        return "Enter the path to your mailbox file (the .mbox file from Gmail Takeout)."
    if not path.exists():
        missing = f"There is no file at {path}."
        if not path.parent.is_dir():
            missing += f" The folder {path.parent} does not exist either."
        return missing + " Check the path, or drag the .mbox file into the terminal to paste its path."
    if path.is_dir():
        return _folder_problem(path)
    if not path.is_file():
        return f"{path} is not a regular file. Point at the mailbox (.mbox) file itself."
    try:
        with path.open("rb") as handle:
            start = handle.read(1024)
    except OSError as exc:
        return (
            f"{path} cannot be read ({exc.strerror or exc}). If it is in a protected folder such as Downloads "
            "or Documents, allow your terminal to read it (System Settings > Privacy & Security), or move the "
            "file somewhere else."
        )
    if start.startswith(b"PK\x03\x04") or path.suffix.lower() == ".zip":
        return (
            f"{path} is a zip archive, not a mailbox. Google Takeout downloads come zipped: unzip it, then point "
            f"at the .mbox file inside (usually {TAKEOUT_MBOX})."
        )
    if start.startswith(b"\x1f\x8b") or path.name.lower().endswith((".tgz", ".tar.gz", ".gz")):
        return (
            f"{path} is a compressed archive, not a mailbox. Unpack it, then point at the .mbox file inside "
            f"(usually {TAKEOUT_MBOX})."
        )
    if not start:
        return f"{path} is empty. An mbox file begins with a line starting with \"From \"."
    if not start.startswith(b"From "):
        first = start.split(b"\n", 1)[0][:60].decode("utf-8", "replace")
        return (
            f"{path} does not look like an mbox file: an mbox begins with a line starting with \"From \", but "
            f"this file begins with {first!r}. Point at the .mbox file from Gmail Takeout."
        )
    return None


def _folder_problem(path: Path) -> str:
    found = sorted(p for pattern in ("*.mbox", "*/*.mbox", "*/*/*.mbox") for p in path.glob(pattern) if p.is_file())
    takeout = "takeout" in path.name.lower() or (path / "Takeout").is_dir() or (path / "Mail").is_dir()
    what = "a Google Takeout folder" if takeout else "a folder"
    if found:
        others = f" (it holds {len(found)} .mbox files; choose the one with your mail)" if len(found) > 1 else ""
        return f"{path} is {what}, not a file. Point at the .mbox file inside it: {found[0]}{others}."
    if takeout:
        return (
            f"{path} is a Google Takeout folder, but it holds no .mbox file. Gmail's mail is exported as an "
            f".mbox file under Takeout/Mail; if there is none, export Mail again in Google Takeout."
        )
    return f"{path} is a folder, not a file. Point at the mailbox (.mbox) file itself."


# -- name variants ------------------------------------------------------------------------


def parse_name_variants(text: str) -> list[str]:
    """Name variants typed one per line, or on one line separated by commas."""
    parts = text.splitlines() if "\n" in text.strip() else text.split(",")
    return [" ".join(part.split()) for part in parts if part.strip()]


def check_names(names: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """The variants tidied, by the config loader's own check; ``SetupError``
    for one that names no one (such as a title on its own)."""
    try:
        return check_passenger_names(list(names))
    except ConfigError as exc:
        raise SetupError(str(exc)) from exc


def check_thoroughness(value: str) -> str:
    if value not in THOROUGHNESS:
        raise SetupError(f'thoroughness must be "quick" or "thorough", got {value!r}')
    return value


# -- prefilled answers ----------------------------------------------------------------------


def answers_from_config(config: Config) -> SetupAnswers:
    return SetupAnswers(
        str(config.mailbox) if config.mailbox is not None else "", config.passenger_names, config.thoroughness
    )


def answers_from_session(description: SessionDescription, config: Config) -> SetupAnswers:
    """What the session was started with, where it recorded it; the config
    file's values for the rest."""
    fallback = answers_from_config(config)
    return SetupAnswers(
        str(description.mailbox) if description.mailbox is not None else fallback.mailbox,
        description.passenger_names if description.passenger_names is not None else fallback.names,
        description.thoroughness if description.thoroughness in THOROUGHNESS else fallback.thoroughness,
    )


# -- writing the config file ---------------------------------------------------------------


def write_answers(config_path: Path, mailbox: Path, names: list[str] | tuple[str, ...], thoroughness: str) -> WriteResult:
    """Check the three answers and write them to ``config_path`` (creating
    it if it does not exist), leaving everything else in it as it was.

    ``mailbox`` is written relative to the config file's folder when it is
    inside it, else as an absolute path, so it names the same file when read
    back. Raises ``SetupError`` (writing nothing) for a bad answer, an
    existing file that cannot be read, or a result the loader rejects."""
    problem = mailbox_problem(mailbox)
    if problem is not None:
        raise SetupError(problem)
    names = check_names(names)
    thoroughness = check_thoroughness(thoroughness)
    config_path = Path(os.path.abspath(config_path))
    created = not config_path.exists()
    text = "" if created else _read(config_path)
    setting = mailbox_setting(mailbox, config_path.parent)

    new_text = NEW_FILE_HEADER if created else text
    new_text = set_setting(new_text, None, "mailbox", _toml_string(setting))
    new_text = set_setting(new_text, "passenger", "names", "[" + ", ".join(_toml_string(n) for n in names) + "]")
    new_text = set_setting(new_text, "run", "thoroughness", _toml_string(thoroughness))

    data = tomllib.loads(new_text)
    if (
        data.get("mailbox") != setting
        or data.get("passenger", {}).get("names") != list(names)
        or data.get("run", {}).get("thoroughness") != thoroughness
    ):
        raise SetupError(
            f"could not update {config_path}: its mailbox, [passenger] names or [run] thoroughness setting is "
            "written in a form setup cannot edit (for example a dotted key or an inline table). Set them by hand."
        )
    temporary = config_path.with_name(f".{config_path.name}.setup-tmp")
    temporary.write_text(new_text, encoding="utf-8")
    try:
        config = load_config(temporary)
    except ConfigError as exc:
        temporary.unlink(missing_ok=True)
        raise SetupError(f"{config_path} would not load after setup: {exc}") from exc
    os.replace(temporary, config_path)
    assert config.mailbox is not None
    return WriteResult(config_path, created, config.mailbox, setting, names, thoroughness)


def format_names(names: tuple[str, ...] | list[str]) -> str:
    return ", ".join(names) if names else "none: passenger filtering is off, every booking is kept"


def format_written(result: WriteResult) -> list[str]:
    """What setup wrote, one line per setting."""
    shown = result.mailbox_setting
    if shown != str(result.mailbox):
        shown += f" ({result.mailbox})"
    return [
        f"mailbox:         {shown}",
        f"passenger names: {format_names(result.names)}",
        f"thoroughness:    {result.thoroughness}",
    ]


def mailbox_setting(mailbox: Path, config_dir: Path) -> str:
    """The text to write for ``mailbox``: relative to ``config_dir`` when
    the file is inside it, else absolute."""
    absolute = Path(os.path.abspath(mailbox.expanduser()))
    # Folders are compared as written and with symbolic links followed (on
    # macOS /var is /private/var); the file's own name is kept.
    for folder, base in (
        (absolute.parent, Path(os.path.abspath(config_dir))),
        (Path(os.path.realpath(absolute.parent)), Path(os.path.realpath(config_dir))),
    ):
        try:
            return (folder.relative_to(base) / absolute.name).as_posix()
        except ValueError:
            continue
    return str(absolute)


def _read(config_path: Path) -> str:
    try:
        text = config_path.read_text(encoding="utf-8")
        tomllib.loads(text)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise SetupError(
            f"cannot read config file {config_path}: {exc}. Fix it or move it away, then run setup again."
        ) from exc
    return text


def _toml_string(value: str) -> str:
    # JSON's string escapes are TOML basic-string escapes; DEL must be escaped too.
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007F")


_HEADER = re.compile(r"^\s*\[\s*([A-Za-z0-9_-]+)\s*\]\s*(#.*)?$")
_ANY_HEADER = re.compile(r"^\s*\[")


def set_setting(text: str, section: str | None, key: str, value: str) -> str:
    """``text`` with ``key`` in ``section`` (None: the top, before the first
    section) set to the TOML ``value``: an existing assignment is replaced in
    place, keeping its indentation and a trailing comment; a missing one is
    added in its section (after a commented-out example of it if there is
    one), and a missing section is added at the end."""
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += newline
    start, end = _section_range(lines, section)
    if start is None:
        separator = newline if lines and lines[-1].strip() else ""
        return "".join(lines) + f"{separator}[{section}]{newline}{key} = {value}{newline}"
    assignment = re.compile(rf"^(\s*)({re.escape(key)}|\"{re.escape(key)}\"|'{re.escape(key)}')\s*=(.*)$", re.DOTALL)
    for index in range(start, end):
        match = assignment.match(lines[index])
        if match:
            last, comment = _value_extent(lines, index, match.group(3), end)
            indent, spelled = match.group(1), match.group(2)
            suffix = f"  {comment}" if comment else ""
            lines[index : last + 1] = [f"{indent}{spelled} = {value}{suffix}{newline}"]
            return "".join(lines)
    example = re.compile(rf"^\s*#\s*{re.escape(key)}\s*=")
    position = next((i + 1 for i in range(start, end) if example.match(lines[i])), None)
    if position is None:
        position = end
        while position > start and not lines[position - 1].strip():
            position -= 1
    new = [f"{key} = {value}{newline}"]
    if position == end and end < len(lines) and section is None:
        new.append(newline)  # keep a blank line before the first section
    lines[position:position] = new
    return "".join(lines)


def _section_range(lines: list[str], section: str | None) -> tuple[int | None, int]:
    """The lines of ``section``'s body as (first, end); (None, 0) when the
    section is absent. The top of the file is the section None."""
    if section is None:
        end = next((i for i, line in enumerate(lines) if _ANY_HEADER.match(line)), len(lines))
        return 0, end
    for index, line in enumerate(lines):
        match = _HEADER.match(line)
        if match and match.group(1) == section:
            end = next((i for i in range(index + 1, len(lines)) if _ANY_HEADER.match(lines[i])), len(lines))
            return index + 1, end
    return None, 0


def _value_extent(lines: list[str], index: int, rest: str, end: int) -> tuple[int, str]:
    """The last line of the value that starts after ``=`` on line ``index``
    (a multi-line array spans several), and the comment after a one-line value."""
    for last in range(index, end):
        text = rest + "".join(lines[index + 1 : last + 1])
        try:
            tomllib.loads("v =" + text)
        except tomllib.TOMLDecodeError:
            continue
        comment = ""
        if last == index:
            for position, char in enumerate(rest):
                if char == "#":
                    try:
                        tomllib.loads("v =" + rest[:position])
                    except tomllib.TOMLDecodeError:
                        continue
                    comment = rest[position:].rstrip("\r\n")
                    break
        return last, comment
    return index, ""
