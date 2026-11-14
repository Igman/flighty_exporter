"""Shared test seam: run the command line in a temporary workspace.

Tests build an mbox of invented emails and a config file in a temp directory,
run ``flighty-exporter`` as a real subprocess against a FakeOllama, and assert
only on exit status, printed output and what the fake received.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest

from fake_ollama import FakeOllama
from openflights_fixtures import write_reference_data

SRC = Path(__file__).resolve().parents[1] / "src"

# A command started in the background gets its own process group on Windows,
# so that a test can send it Ctrl+Break (see ``stop_signals``) without
# reaching pytest itself.
NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str

    def count(self, label: str) -> int:
        """Read a count from the classify summary, e.g. count("classified")."""
        match = re.search(rf"^\s*{re.escape(label)}:\s+(\d+)\s*$", self.stdout, re.MULTILINE)
        assert match, f"no '{label}' line in output:\n{self.stdout}\n--- stderr ---\n{self.stderr}"
        return int(match.group(1))


@dataclass
class Workspace:
    root: Path
    ollama: FakeOllama

    @property
    def home(self) -> Path:
        """The home folder for the commands (HOME, and USERPROFILE, which is
        what Windows reads): its default OpenFlights cache holds the fixture
        reference data, so a config without ``[export] data_dir`` never reads
        the user's cache or the network."""
        return self.root / "home"

    @property
    def config_path(self) -> Path:
        return self.root / "flighty_exporter.toml"

    def write_config(self, text: str | None = None, *, pulled: bool = True, **ollama_overrides: str) -> Path:
        """Write a config file pointing at the fake server (unless overridden).

        Models named under [models] are added to the fake's model list, as if
        the user had pulled what they configured; ``pulled=False`` leaves the
        list alone (to test a missing model)."""
        if text is None:
            address = ollama_overrides.get("address", self.ollama.url)
            text = f'[ollama]\naddress = "{address}"\n'
        self.config_path.write_text(text, encoding="utf-8")
        if pulled:
            try:
                models = tomllib.loads(text).get("models", {})
            except tomllib.TOMLDecodeError:
                models = {}
            for name in models.values() if isinstance(models, dict) else ():
                if isinstance(name, str) and name not in self.ollama.models:
                    self.ollama.models.append(name)
        return self.config_path

    def command(self, *args: str) -> list[str]:
        return [sys.executable, "-m", "flighty_exporter", "--config", str(self.config_path), *args]

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONUNBUFFERED"] = "1"
        env["HOME"] = env["USERPROFILE"] = str(self.home)
        return env

    def run(self, *args: str, timeout: float = 60) -> Result:
        proc = subprocess.run(
            self.command(*args),
            cwd=self.root,
            env=self.env(),
            capture_output=True,
            encoding="utf-8",
            timeout=timeout,
        )
        return Result(proc.returncode, proc.stdout, proc.stderr)

    def start(self, *args: str) -> subprocess.Popen[str]:
        return subprocess.Popen(
            self.command(*args),
            cwd=self.root,
            env=self.env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            creationflags=NEW_PROCESS_GROUP,
        )


@pytest.fixture
def workspace(tmp_path: Path):
    with FakeOllama() as ollama:
        ws = Workspace(tmp_path, ollama)
        write_reference_data(ws.home / ".cache" / "flighty_exporter" / "openflights")
        ws.write_config()
        yield ws
