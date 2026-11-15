"""Preflight checks and the privacy safeguard, driven through the command line.

Before a pass sends anything to the configured Ollama address, the address
must be on this machine (unless overridden). Just before the first model
request, the server must be reachable, version 0.35.0 or later, and have the
pass's model pulled. Tests assert on exit status, printed messages and what
the fake server received.
"""

from __future__ import annotations

import socket
import time

import pytest

from fake_ollama import Chat, Decision, FakeOllama
from mailbox_fixtures import FLIGHT_BODY, email, write_mbox
from openflights_fixtures import unreachable_url
from test_extract import FLIGHT_RECORD, FLIGHT_SUBJECT
from test_markup import flight_reservation, json_ld_html, markup_email

NON_LOCAL = "http://192.0.2.10:11434"  # TEST-NET-1: never routed
DECISION_MODEL = "nimble"
EXTRACTION_MODEL = "gemma4:12b-it-q4_K_M"
PASS_MODEL = {"classify": DECISION_MODEL, "extract": EXTRACTION_MODEL, "audit": EXTRACTION_MODEL}
MARKUP_SUBJECT = "Your Fictair e-ticket QZX7PL"


def dead_address() -> str:
    """A local address nothing listens on."""
    return unreachable_url().removesuffix("/data/")


def address_config(workspace, address: str, extra: str = "", *, pulled: bool = True) -> None:
    workspace.write_config(f'[ollama]\naddress = "{address}"\n{extra}', pulled=pulled)


def mailbox(workspace):
    """One message with machine-readable flight data (never sent to a model)
    and two that need the models."""
    return write_mbox(
        workspace.root / "mail.mbox",
        [
            markup_email(MARKUP_SUBJECT, json_ld_html(flight_reservation("1234"))),
            email(FLIGHT_SUBJECT, FLIGHT_BODY),
            email("Second itinerary", FLIGHT_BODY),
        ],
    )


def prepare(workspace, command: str):
    """A mailbox, and for extract and audit a store classified with every
    model message above the threshold. Returns the mailbox path."""
    mbox = mailbox(workspace)
    workspace.ollama.default_decision = Decision(0.9, "booking confirmation")
    workspace.ollama.default_chat = Chat(record=FLIGHT_RECORD)
    if command != "classify":
        result = workspace.run("classify", "--mailbox", str(mbox))
        assert result.returncode == 0, result.stderr
    workspace.ollama.requests.clear()
    return mbox


def assert_pass_did_its_full_work(workspace, command: str, mbox) -> None:
    """Rerun with a healthy server: the pass does all of its work, so the
    failed run stored nothing."""
    result = workspace.run(command, "--mailbox", str(mbox))
    assert result.returncode == 0, result.stderr
    if command == "classify":
        assert result.count("classified") == 2
        assert result.count("already classified") == 0
        assert result.count("resolved from machine-readable data") == 1
        assert result.count("already resolved from machine-readable data") == 0
    elif command == "extract":
        assert result.count("extracted") == 2
        assert result.count("already extracted") == 0
    else:
        assert "already extracted (reused): 0" in result.stdout
        assert "sent to the extraction model: 2" in result.stdout


# -- the three stop conditions, for every model command ----------------------------


@pytest.mark.parametrize("command", ["classify", "extract", "audit"])
def test_unreachable_server_stops_the_pass_before_anything_is_stored(workspace, command):
    mbox = prepare(workspace, command)
    address = dead_address()
    address_config(workspace, address)

    result = workspace.run(command, "--mailbox", str(mbox))

    assert result.returncode == 1
    assert f"the Ollama server at {address} is not reachable" in result.stderr
    assert "`ollama serve`" in result.stderr and "Ollama app" in result.stderr
    assert f"rerun {command}" in result.stderr
    assert result.stdout == ""  # no half summary
    if command == "classify":
        assert not (workspace.root / "flighty_exporter.db").exists()

    workspace.write_config()
    assert_pass_did_its_full_work(workspace, command, mbox)


@pytest.mark.parametrize("command", ["classify", "extract", "audit"])
def test_too_old_server_stops_the_pass_before_anything_is_stored(workspace, command):
    mbox = prepare(workspace, command)
    workspace.ollama.version = "0.34.9"

    result = workspace.run(command, "--mailbox", str(mbox))

    assert result.returncode == 1
    assert "is version 0.34.9, but flighty-exporter needs Ollama 0.35.0 or later" in result.stderr
    assert "Update Ollama" in result.stderr
    assert result.stdout == ""
    assert workspace.ollama.preflight_requests() == ["/api/version"]
    assert workspace.ollama.pass_requests() == []
    if command == "classify":
        assert not (workspace.root / "flighty_exporter.db").exists()

    workspace.ollama.version = "0.40.16"
    assert_pass_did_its_full_work(workspace, command, mbox)


@pytest.mark.parametrize("command", ["classify", "extract", "audit"])
def test_missing_model_stops_the_pass_with_the_pull_command(workspace, command):
    mbox = prepare(workspace, command)
    model = PASS_MODEL[command]
    workspace.ollama.models = [m for m in workspace.ollama.models if not m.startswith(model)]

    result = workspace.run(command, "--mailbox", str(mbox))

    assert result.returncode == 1
    setting = "[models] decision" if command == "classify" else "[models] extraction"
    assert f"the model {model} ({setting}) is not pulled" in result.stderr
    assert f"`ollama pull {model}`" in result.stderr
    assert result.stdout == ""
    assert workspace.ollama.preflight_requests() == ["/api/version", "/api/tags"]
    assert workspace.ollama.pass_requests() == []

    workspace.ollama.models.append(model)
    assert_pass_did_its_full_work(workspace, command, mbox)


def test_each_pass_checks_only_the_model_it_uses(workspace):
    mbox = mailbox(workspace)
    workspace.ollama.default_decision = Decision(0.9, "booking confirmation")

    workspace.ollama.models = ["nimble:latest"]  # no extraction model
    classify = workspace.run("classify", "--mailbox", str(mbox))
    assert classify.returncode == 0, classify.stderr
    extract = workspace.run("extract", "--mailbox", str(mbox))
    assert extract.returncode == 1
    assert f"`ollama pull {EXTRACTION_MODEL}`" in extract.stderr

    workspace.ollama.models = [EXTRACTION_MODEL]  # no decision model
    extract = workspace.run("extract", "--mailbox", str(mbox))
    assert extract.returncode == 0, extract.stderr
    assert extract.count("extracted") == 2


# -- model name matching ---------------------------------------------------------------


@pytest.mark.parametrize(
    "configured, listed",
    [
        ("nimble", ["nimble:latest"]),
        ("nimble:latest", ["nimble:latest"]),
        ("NIMBLE", ["nimble:latest"]),
        ("nimble", ["Nimble:Latest"]),
        ("Gemma4:12B-it-Q4_K_M", ["gemma4:12b-it-q4_K_M"]),
    ],
)
def test_model_names_match_with_implicit_latest_tag_and_any_case(workspace, configured, listed):
    mbox = write_mbox(workspace.root / "mail.mbox", [email("Plain note")])
    address_config(workspace, workspace.ollama.url, f'\n[models]\ndecision = "{configured}"\n', pulled=False)
    workspace.ollama.models = listed

    result = workspace.run("classify", "--mailbox", str(mbox))

    assert result.returncode == 0, result.stderr
    assert result.count("classified") == 1


@pytest.mark.parametrize(
    "configured, listed",
    [
        ("nimble:9b", ["nimble:latest"]),  # an explicit tag must match exactly
        ("nimble", ["nimble:9b"]),  # no tag means latest
        ("nimbl", ["nimble:latest"]),
    ],
)
def test_model_with_another_tag_is_missing(workspace, configured, listed):
    mbox = write_mbox(workspace.root / "mail.mbox", [email("Plain note")])
    address_config(workspace, workspace.ollama.url, f'\n[models]\ndecision = "{configured}"\n', pulled=False)
    workspace.ollama.models = listed

    result = workspace.run("classify", "--mailbox", str(mbox))

    assert result.returncode == 1
    assert f"`ollama pull {configured}`" in result.stderr
    assert workspace.ollama.pass_requests() == []


# -- version edge cases ----------------------------------------------------------------


@pytest.mark.parametrize("version", ["0.35.0", "0.40.16", "v0.36.1-rc2", "0.35", "1.0.0", "0.35.0-rc1"])
def test_new_enough_versions_are_accepted(workspace, version):
    mbox = write_mbox(workspace.root / "mail.mbox", [email("Plain note")])
    workspace.ollama.version = version

    result = workspace.run("classify", "--mailbox", str(mbox))

    assert result.returncode == 0, result.stderr
    assert "warning" not in result.stderr
    assert workspace.ollama.pass_requests() == ["/v1/systemone"]


@pytest.mark.parametrize("version", ["0.34.9", "0.4.99", "v0.30.0-rc1"])
def test_older_versions_are_refused(workspace, version):
    mbox = write_mbox(workspace.root / "mail.mbox", [email("Plain note")])
    workspace.ollama.version = version

    result = workspace.run("classify", "--mailbox", str(mbox))

    assert result.returncode == 1
    assert f"is version {version}, but flighty-exporter needs Ollama 0.35.0 or later" in result.stderr
    assert workspace.ollama.pass_requests() == []


@pytest.mark.parametrize("version", ["garbage", "", None])
def test_unreadable_version_warns_and_continues(workspace, version):
    mbox = write_mbox(workspace.root / "mail.mbox", [email("Plain note")])
    workspace.ollama.version = version

    result = workspace.run("classify", "--mailbox", str(mbox))

    assert result.returncode == 0, result.stderr
    assert "warning: could not read the Ollama version" in result.stderr
    assert result.count("classified") == 1


def test_a_server_that_is_not_ollama_is_reported(workspace):
    mbox = write_mbox(workspace.root / "mail.mbox", [email("Plain note")])
    workspace.ollama.version_status = 404

    result = workspace.run("classify", "--mailbox", str(mbox))

    assert result.returncode == 1
    assert "answered /api/version with HTTP 404; is it an Ollama server?" in result.stderr
    assert workspace.ollama.pass_requests() == []


# -- nothing to send, nothing checked ------------------------------------------------


def test_no_preflight_when_a_pass_has_nothing_to_send(workspace):
    markup_only = write_mbox(
        workspace.root / "markup.mbox", [markup_email(MARKUP_SUBJECT, json_ld_html(flight_reservation("1234")))]
    )
    address_config(workspace, dead_address())  # Ollama stopped

    classify = workspace.run("classify", "--mailbox", str(markup_only))
    assert classify.returncode == 0, classify.stderr
    assert classify.count("resolved from machine-readable data") == 1
    extract = workspace.run("extract", "--mailbox", str(markup_only))
    assert extract.returncode == 0, extract.stderr
    assert extract.count("extracted") == 0


def test_no_preflight_when_everything_is_already_done(workspace):
    mbox = mailbox(workspace)
    workspace.ollama.default_decision = Decision(0.9, "booking confirmation")
    workspace.ollama.default_chat = Chat(record=FLIGHT_RECORD)
    for command in ("classify", "extract", "audit"):
        result = workspace.run(command, "--mailbox", str(mbox))
        assert result.returncode == 0, result.stderr
    address_config(workspace, dead_address())  # Ollama stopped

    for command in ("classify", "extract", "audit"):
        result = workspace.run(command, "--mailbox", str(mbox))
        assert result.returncode == 0, f"{command}: {result.stderr}"


# -- privacy: the address must be on this machine -----------------------------------


@pytest.mark.parametrize("command", ["classify", "extract", "audit"])
def test_non_local_address_is_refused_before_any_request(workspace, command):
    mbox = prepare(workspace, command)
    address_config(workspace, NON_LOCAL)

    started = time.monotonic()
    result = workspace.run(command, "--mailbox", str(mbox))
    elapsed = time.monotonic() - started

    assert result.returncode == 1
    assert f"refusing to use the Ollama server at {NON_LOCAL}" in result.stderr
    assert "email content would leave this machine" in result.stderr
    assert "allow_remote = true" in result.stderr and "--allow-remote" in result.stderr
    assert "not reachable" not in result.stderr  # no connection was attempted
    assert elapsed < 4  # well under the 5-second connection timeout
    assert result.stdout == ""
    if command == "classify":
        assert not (workspace.root / "flighty_exporter.db").exists()


@pytest.mark.parametrize(
    "address, reason",
    [
        ("http://0.0.0.0:11434", "0.0.0.0 is not a loopback address"),
        ("http://10.0.0.5:11434", "10.0.0.5 is not a loopback address"),
        ("http://[fe80::1]:11434", "fe80::1 is not a loopback address"),
        ("http://no-such-host.invalid:11434", "the host name no-such-host.invalid does not resolve"),
    ],
)
def test_other_non_local_addresses_are_refused(workspace, address, reason):
    mbox = write_mbox(workspace.root / "mail.mbox", [email("Plain note")])
    address_config(workspace, address)

    result = workspace.run("classify", "--mailbox", str(mbox))

    assert result.returncode == 1
    assert reason in result.stderr
    assert "email content would leave this machine" in result.stderr


def test_localhost_name_is_local(workspace):
    mbox = write_mbox(workspace.root / "mail.mbox", [email("Plain note")])
    port = workspace.ollama.url.rsplit(":", 1)[1]
    address_config(workspace, f"http://localhost:{port}")

    result = workspace.run("classify", "--mailbox", str(mbox))

    assert result.returncode == 0, result.stderr
    assert "warning" not in result.stderr
    assert workspace.ollama.pass_requests() == ["/v1/systemone"]


def test_localhost_costs_no_more_than_the_loopback_address(workspace):
    # On Windows "localhost" is ::1 before 127.0.0.1, and a refused connection
    # there takes about two seconds: paid on every request (each opens a new
    # connection), the 20 requests here would take over 40 seconds.
    subjects = [f"Plain note {i}" for i in range(20)]
    mbox = write_mbox(workspace.root / "mail.mbox", [email(s) for s in subjects])
    address_config(workspace, f"http://localhost:{workspace.ollama.port}")

    started = time.monotonic()
    result = workspace.run("classify", "--mailbox", str(mbox))
    elapsed = time.monotonic() - started

    assert result.returncode == 0, result.stderr
    assert sorted(workspace.ollama.classified_subjects()) == sorted(subjects)
    assert elapsed < 15, f"classify took {elapsed:.1f}s through localhost"


def _ipv6_loopback() -> bool:
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            probe.bind(("::1", 0))
    except OSError:
        return False
    return "::1" in {info[4][0] for info in socket.getaddrinfo("localhost", None)}


@pytest.mark.skipif(not _ipv6_loopback(), reason="localhost does not resolve to a usable ::1 here")
def test_localhost_reaches_a_server_listening_on_ipv6_only(workspace):
    mbox = write_mbox(workspace.root / "mail.mbox", [email("Plain note"), email("Another note")])
    with FakeOllama(host="::1") as ipv6_only:
        address_config(workspace, f"http://localhost:{ipv6_only.port}")

        result = workspace.run("classify", "--mailbox", str(mbox))

    assert result.returncode == 0, result.stderr
    assert sorted(ipv6_only.classified_subjects()) == ["Another note", "Plain note"]
    assert workspace.ollama.requests == []


@pytest.mark.parametrize("host", ["127.1.2.3", "[::1]"])
def test_loopback_literals_are_local(workspace, host):
    """Nothing listens there, so the pass gets past the locality check and
    stops at reachability instead."""
    mbox = write_mbox(workspace.root / "mail.mbox", [email("Plain note")])
    address_config(workspace, f"http://{host}:{dead_address().rsplit(':', 1)[1]}", "timeout_seconds = 1\n")

    result = workspace.run("classify", "--mailbox", str(mbox))

    assert result.returncode == 1
    assert "refusing" not in result.stderr
    assert "is not reachable" in result.stderr


@pytest.mark.parametrize("address", ["ftp://localhost:11434", "localhost:11434", "http://", "file:///tmp/x"])
def test_address_must_be_http_or_https(workspace, address):
    address_config(workspace, address)

    result = workspace.run("classify", "--mailbox", "whatever.mbox")

    assert result.returncode == 1
    assert "[ollama] address" in result.stderr
    assert workspace.ollama.requests == []


def test_allow_remote_must_be_true_or_false(workspace):
    address_config(workspace, workspace.ollama.url, 'allow_remote = "yes"\n')

    result = workspace.run("classify", "--mailbox", "whatever.mbox")

    assert result.returncode == 1
    assert "[ollama] allow_remote must be true or false" in result.stderr


# -- the override ---------------------------------------------------------------------


def _reaches_fake_through_any_address(workspace) -> bool:
    port = int(workspace.ollama.url.rsplit(":", 1)[1])
    try:
        socket.create_connection(("0.0.0.0", port), timeout=1).close()
        return True
    except OSError:
        return False


@pytest.mark.parametrize("via", ["config", "flag"])
def test_override_sends_to_a_non_local_address_with_a_warning(workspace, via):
    """0.0.0.0 is not a loopback address (so it is refused without the
    override), but a connection to it reaches this machine's fake server,
    so the override can be tested end to end without any network."""
    if not _reaches_fake_through_any_address(workspace):
        pytest.skip("this platform does not connect 0.0.0.0 to the local host")
    mbox = write_mbox(workspace.root / "mail.mbox", [email("Plain note")])
    address = "http://0.0.0.0:" + workspace.ollama.url.rsplit(":", 1)[1]
    args = ["classify", "--mailbox", str(mbox)]
    if via == "config":
        address_config(workspace, address, "allow_remote = true\n")
    else:
        address_config(workspace, address)
        args.append("--allow-remote")

    result = workspace.run(*args)

    assert result.returncode == 0, result.stderr
    assert (
        f"warning: email content is being sent to the Ollama server at {address}, which is not on this machine"
        in result.stderr
    )
    assert "refusing" not in result.stderr
    assert workspace.ollama.pass_requests() == ["/v1/systemone"]

    # The warning is printed on every run, even one that sends nothing new.
    again = workspace.run(*args)
    assert again.returncode == 0, again.stderr
    assert "warning: email content is being sent" in again.stderr


@pytest.mark.parametrize("command", ["classify", "extract", "audit"])
def test_override_lets_the_pass_try_a_remote_address(workspace, command):
    """With the override, a non-routable address gets past the locality
    check, warns, and stops at reachability within the short timeout."""
    mbox = prepare(workspace, command)
    address_config(workspace, NON_LOCAL, "timeout_seconds = 1\n")

    result = workspace.run(command, "--allow-remote", "--mailbox", str(mbox))

    assert result.returncode == 1
    assert f"warning: email content is being sent to the Ollama server at {NON_LOCAL}" in result.stderr
    assert "refusing" not in result.stderr
    assert f"the Ollama server at {NON_LOCAL} is not reachable" in result.stderr


def test_override_with_a_local_address_prints_no_warning(workspace):
    mbox = write_mbox(workspace.root / "mail.mbox", [email("Plain note")])
    address_config(workspace, workspace.ollama.url, "allow_remote = true\n")

    result = workspace.run("classify", "--mailbox", str(mbox))

    assert result.returncode == 0, result.stderr
    assert "warning" not in result.stderr


# -- review and export make no model requests and run no checks -------------------


def test_review_and_export_never_check_the_server(workspace):
    mbox = mailbox(workspace)
    workspace.ollama.default_decision = Decision(0.9, "booking confirmation")
    workspace.ollama.default_chat = Chat(record=FLIGHT_RECORD)
    for command in ("classify", "extract"):
        assert workspace.run(command, "--mailbox", str(mbox)).returncode == 0
    workspace.ollama.requests.clear()
    workspace.ollama.version = "0.1.0"
    workspace.ollama.models = []
    address_config(workspace, NON_LOCAL)

    review = workspace.run("review")
    export = workspace.run("export")

    assert review.returncode == 0, review.stderr
    assert export.returncode == 0, export.stderr
    for result in (review, export):
        assert "refusing" not in result.stderr
        assert "warning: email content" not in result.stderr
    assert workspace.ollama.requests == []
