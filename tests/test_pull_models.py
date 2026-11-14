"""Downloading missing models through the local server, driven through ``pull-models``.

``pull-models`` lists the configured models the server does not have, with
their approximate sizes, and downloads them through Ollama's pull interface
only when given ``--yes``. Tests assert on printed output, exit status and
what the fake server received.
"""

from __future__ import annotations

from test_preflight import NON_LOCAL, dead_address

MISSING_DECISION = ["gemma4:12b-it-q4_K_M"]


def pull_bodies(workspace) -> list:
    return [r.body for r in workspace.ollama.requests if r.path == "/api/pull"]


def test_without_yes_it_lists_what_is_missing_and_downloads_nothing(workspace):
    workspace.ollama.models = []

    result = workspace.run("pull-models")

    assert result.returncode == 0, result.stderr
    assert "Missing on the Ollama server at" in result.stdout
    assert "  nimble ([models] decision): about 9.3 GB" in result.stdout
    assert "  gemma4:12b-it-q4_K_M ([models] extraction): about 8.0 GB" in result.stdout
    assert "Nothing was downloaded. Run `flighty-exporter pull-models --yes` to download them." in result.stdout
    assert pull_bodies(workspace) == []


def test_an_unknown_model_size_is_said_to_be_unknown(workspace):
    workspace.write_config(
        f'[ollama]\naddress = "{workspace.ollama.url}"\n\n[models]\nextraction = "fictional-reader:3b"\n',
        pulled=False,
    )

    result = workspace.run("pull-models")

    assert result.returncode == 0, result.stderr
    assert "  fictional-reader:3b ([models] extraction): size not known until the download starts" in result.stdout
    assert "nimble" not in result.stdout.split("Missing", 1)[1]


def test_nothing_missing_means_nothing_to_do(workspace):
    result = workspace.run("pull-models", "--yes")

    assert result.returncode == 0, result.stderr
    assert "Both models are already on the Ollama server" in result.stdout
    assert pull_bodies(workspace) == []


def test_with_yes_it_downloads_the_missing_model_with_progress(workspace):
    workspace.ollama.models = list(MISSING_DECISION)

    result = workspace.run("pull-models", "--yes")

    assert result.returncode == 0, result.stderr
    assert pull_bodies(workspace) == [{"model": "nimble", "stream": True}]
    assert "Downloading nimble ([models] decision, about 9.3 GB)" in result.stdout
    assert "  pulling manifest" in result.stdout
    assert "  pulling 5f1e2d: 0 bytes of 2.0 kB (0%)" in result.stdout
    assert "  pulling 5f1e2d: 1.0 kB of 2.0 kB (50%)" in result.stdout
    assert "  pulling 5f1e2d: 2.0 kB of 2.0 kB (100%)" in result.stdout
    assert "  verifying sha256 digest" in result.stdout
    assert "nimble is now on the Ollama server." in result.stdout
    # The server now lists it: the check passes.
    check = workspace.run("pull-models")
    assert "Both models are already on the Ollama server" in check.stdout


def test_both_missing_models_are_downloaded_in_order(workspace):
    workspace.ollama.models = []

    result = workspace.run("pull-models", "--yes")

    assert result.returncode == 0, result.stderr
    assert [body["model"] for body in pull_bodies(workspace)] == ["nimble", "gemma4:12b-it-q4_K_M"]


def test_a_pull_error_is_reported_and_exits_non_zero(workspace):
    workspace.ollama.models = list(MISSING_DECISION)
    workspace.ollama.pull_errors["nimble"] = "pull model manifest: file does not exist"

    result = workspace.run("pull-models", "--yes")

    assert result.returncode == 1
    assert (
        f"error: the Ollama server at {workspace.ollama.url} could not pull nimble: "
        "pull model manifest: file does not exist" in result.stderr
    )
    assert "now on the Ollama server" not in result.stdout
    assert "Missing on the Ollama server" in workspace.run("pull-models").stdout


def test_a_refused_pull_request_is_reported(workspace):
    workspace.ollama.models = list(MISSING_DECISION)
    workspace.ollama.pull_status = 500

    result = workspace.run("pull-models", "--yes")

    assert result.returncode == 1
    assert f"could not pull nimble: scripted failure" in result.stderr


def test_an_unreachable_server_is_reported_with_the_existing_message(workspace):
    address = dead_address()
    workspace.write_config(f'[ollama]\naddress = "{address}"\n')

    result = workspace.run("pull-models", "--yes")

    assert result.returncode == 1
    assert f"the Ollama server at {address} is not reachable" in result.stderr
    assert "then rerun pull-models" in result.stderr


def test_a_non_local_address_is_refused_before_any_request(workspace):
    workspace.write_config(f'[ollama]\naddress = "{NON_LOCAL}"\n')

    result = workspace.run("pull-models", "--yes")

    assert result.returncode == 1
    assert f"refusing to use the Ollama server at {NON_LOCAL}" in result.stderr
    assert workspace.ollama.paths() == []
