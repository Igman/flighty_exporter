"""Real-world message bodies: HTML, nested multiparts, forwarded messages,
legacy charsets and transfer encodings, long emails and PDF attachments.

Driven through the command line against the fake Ollama; the assertions are
on what the fake received, the exit status, the printed summaries and the
output files. Every email, person and flight here is invented.
"""

from __future__ import annotations

import base64
import quopri
from email.message import EmailMessage

import pytest

from fake_ollama import (
    TOO_LARGE_ERROR,
    Chat,
    Decision,
    chat_user_text,
    decision_body,
    too_many_tokens_error,
)
from mailbox_fixtures import FLIGHT_BODY, OWNER, email, image_only_pdf, raw_email, text_pdf, write_mbox
from test_extract import FLIGHT_RECORD, read_csv

SENDER = "Fictair <noreply@fictair.example>"
PDF_DELIMITER = "----- PDF attachment: "


def run_ok(workspace, *args: str):
    result = workspace.run(*args)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def classify_and_extract(workspace, mbox, gate: float = 0.9):
    workspace.ollama.default_decision = Decision(gate, "booking confirmation")
    run_ok(workspace, "classify", "--mailbox", str(mbox))
    return run_ok(workspace, "extract", "--mailbox", str(mbox))


def decision_for(workspace, subject: str) -> dict:
    [request] = [r for r in workspace.ollama.decision_requests() if r["state"]["subject"] == subject]
    return request


def chat_text_for(workspace, subject: str) -> str:
    [text] = [
        chat_user_text(r) for r in workspace.ollama.chat_requests() if f"Subject: {subject}\n" in chat_user_text(r)
    ]
    return text


# -- HTML ------------------------------------------------------------------------

ITINERARY_HTML = """<!DOCTYPE html>
<html><head><title>Fictair booking</title>
<style>td { color: #123456; } .hidden { display: none; }</style>
<script>var tracking = "SECRET-SCRIPT-TEXT";</script>
</head>
<body>
<!-- layout comment that must not appear -->
<div><img src="https://cdn.fictair.example/logo.png" alt="Fictair"><img src="spacer.gif" alt=""></div>
<h1>Booking confirmed &ndash; reference QZX7PL</h1>
<p>Dear Robin&nbsp;Quill, thank you for flying with us &amp; see you on board.</p>
<table>
  <tr><th>Flight</th><th>From</th><th>To</th><th>Departs</th></tr>
  <tr><td>ZZ1234</td><td>Lisbon (LIS)</td><td>Oslo (OSL)</td><td>12 May 2025 07:40</td></tr>
</table>
<p>Manage your trip <a href="https://www.fictair.example/manage?ref=QZX7PL">online</a>.</p>
</body></html>
"""


def html_only_email(subject: str, html: str) -> EmailMessage:
    msg = email(subject, sender=SENDER)
    msg.set_content(html, subtype="html")
    return msg


def test_html_only_message_is_classified_and_extracted_from_its_text(workspace):
    subject = "Your Fictair booking QZX7PL"
    mbox = write_mbox(workspace.root / "mail.mbox", [html_only_email(subject, ITINERARY_HTML)])
    workspace.ollama.chats[subject] = Chat(FLIGHT_RECORD)

    result = classify_and_extract(workspace, mbox)

    assert result.count("extracted") == 1
    classifier_body = decision_body(decision_for(workspace, subject))
    extractor_text = chat_text_for(workspace, subject)
    extractor_body = extractor_text.split("\n\n", 1)[1]  # after the From/Subject/Date lines
    for text in (classifier_body, extractor_body):
        assert "Booking confirmed – reference QZX7PL" in text
        assert "Dear Robin Quill, thank you for flying with us & see you on board." in text
        assert "Manage your trip online." in text
        assert "Fictair" in text  # non-empty image alt text
        for noise in ("<", "&amp;", "&nbsp;", "SECRET-SCRIPT-TEXT", "color", "layout comment",
                      "Fictair booking\n", "https://", "spacer"):
            assert noise not in text, noise
    assert extractor_text.endswith(classifier_body)


def test_table_itinerary_stays_readable(workspace):
    subject = "Itinerary table"
    mbox = write_mbox(workspace.root / "mail.mbox", [html_only_email(subject, ITINERARY_HTML)])

    run_ok(workspace, "classify", "--mailbox", str(mbox))

    lines = decision_body(decision_for(workspace, subject)).splitlines()
    assert "Flight | From | To | Departs" in lines
    assert "ZZ1234 | Lisbon (LIS) | Oslo (OSL) | 12 May 2025 07:40" in lines


def test_stub_plain_part_gives_way_to_rich_html(workspace):
    subject = "Your trip details"
    msg = email(subject, "View this email in your browser.\n", sender=SENDER, html=ITINERARY_HTML)
    mbox = write_mbox(workspace.root / "mail.mbox", [msg])

    classify_and_extract(workspace, mbox)

    for text in (decision_body(decision_for(workspace, subject)), chat_text_for(workspace, subject)):
        assert "ZZ1234 | Lisbon (LIS) | Oslo (OSL) | 12 May 2025 07:40" in text
        assert "View this email in your browser" not in text


def test_short_plain_part_is_kept_when_the_html_says_no_more(workspace):
    subject = "Short note"
    msg = email(subject, "Flight ZZ88 leaves at noon.\n", html="<p>Flight <b>ZZ88</b> leaves at noon.</p>")
    mbox = write_mbox(workspace.root / "mail.mbox", [msg])

    run_ok(workspace, "classify", "--mailbox", str(mbox))

    assert decision_body(decision_for(workspace, subject)) == "Flight ZZ88 leaves at noon."


def test_empty_plain_part_falls_back_to_html(workspace):
    subject = "Empty plain part"
    msg = email(subject, "\n", html="<p>Flight <b>ZZ88</b> leaves at noon.</p>")
    mbox = write_mbox(workspace.root / "mail.mbox", [msg])

    run_ok(workspace, "classify", "--mailbox", str(mbox))

    assert decision_body(decision_for(workspace, subject)) == "Flight ZZ88 leaves at noon."


def test_nested_multiparts_are_followed(workspace):
    # multipart/mixed( multipart/alternative( text/plain stub,
    #   multipart/related( text/html, image/png ) ), application/pdf )
    subject = "Nested booking"
    msg = email(subject, "See below.\n", sender=SENDER)
    related = EmailMessage()
    related.set_content(ITINERARY_HTML, subtype="html")
    related.add_related(b"\x89PNG fake", maintype="image", subtype="png", cid="<logo@fixtures.example>")
    msg.make_alternative()
    msg.attach(related)
    msg.add_attachment(text_pdf("E-TICKET ZZ1234 for Robin Quill, seat 14C"), maintype="application",
                       subtype="pdf", filename="eticket.pdf")
    mbox = write_mbox(workspace.root / "mail.mbox", [msg])

    classify_and_extract(workspace, mbox)

    request = decision_for(workspace, subject)
    assert "ZZ1234 | Lisbon (LIS) | Oslo (OSL) | 12 May 2025 07:40" in request["state"]["body"]
    assert "eticket.pdf" in request["state"]["attachments"]
    assert "seat 14C" in chat_text_for(workspace, subject)


def test_forwarded_message_attachment_is_read_after_the_outer_body(workspace):
    subject = "Fwd: my flight"
    inner = EmailMessage()
    inner["From"] = SENDER
    inner["To"] = "Sam Ferrow <sam.ferrow@example.test>"
    inner["Subject"] = "Your flight to Oslo is confirmed"
    inner["Date"] = "Mon, 03 Mar 2025 18:00:00 +0000"
    inner.set_content(FLIGHT_BODY)
    outer = email(subject, "Here is the itinerary I mentioned.\n", sender=OWNER)
    outer.add_attachment(inner)
    mbox = write_mbox(workspace.root / "mail.mbox", [outer])

    classify_and_extract(workspace, mbox)

    for text in (decision_body(decision_for(workspace, subject)), chat_text_for(workspace, subject)):
        assert text.index("Here is the itinerary I mentioned.") < text.index("Subject: Your flight to Oslo is confirmed")
        assert "Flight ZZ1234  Lisbon (LIS) -> Oslo (OSL)" in text


def test_long_tracking_urls_are_replaced(workspace):
    subject = "Links everywhere"
    tracking = "https://click.fictair.example/track/abc?u=" + "x" * 80
    body = f"Check in now: {tracking}\nShort link: https://fictair.example/ci\n"
    html = f'<p>Check in at <a href="{tracking}">{tracking}</a></p>'
    mbox = write_mbox(workspace.root / "mail.mbox", [email(subject, body), email("HTML links", "\n", html=html)])

    classify_and_extract(workspace, mbox)

    plain = decision_body(decision_for(workspace, subject))
    assert plain == "Check in now: [link]\nShort link: https://fictair.example/ci"
    assert "Check in now: [link]" in chat_text_for(workspace, subject)
    assert decision_body(decision_for(workspace, "HTML links")) == "Check in at [link]"


# -- charsets and transfer encodings ---------------------------------------------

TEXT_LATIN = "Réservation confirmée: vol ZZ1234 Lisboa → Oslo, départ 07h40"


@pytest.mark.parametrize(
    "charset, encoding, text",
    [
        ("iso-8859-1", "quoted-printable", "Réservation confirmée: vol ZZ1234, départ 07h40, siège 14C"),
        ("windows-1252", "8bit", "“Your seat” costs €12 – flight ZZ1234"),
        # windows-1252 bytes labelled latin-1, as many mailers do: read as windows-1252.
        ("iso-8859-1", "8bit", "“Smart quotes” labelled latin-1 – flight ZZ1234"),
        ("shift_jis", "base64", "ご予約ありがとうございます。便名 ZZ1234 リスボン発 オスロ行き"),
        ("iso-2022-jp", "7bit", "ご予約ありがとうございます。便名 ZZ1234"),
        ("utf-8", "base64", "Бронирование подтверждено: рейс ZZ1234 ✈"),
        ("utf-8", "quoted-printable", TEXT_LATIN),
    ],
)
def test_charsets_and_transfer_encodings_decode(workspace, charset, encoding, text):
    data = text.encode("windows-1252" if charset == "iso-8859-1" else charset)
    if encoding == "base64":
        data = base64.encodebytes(data)
    elif encoding == "quoted-printable":
        data = quopri.encodestring(data)
    subject = f"Encoded {charset} {encoding}"
    msg = raw_email(subject, data, content_type=f'text/plain; charset="{charset}"', transfer_encoding=encoding)
    mbox = write_mbox(workspace.root / "mail.mbox", [msg])

    classify_and_extract(workspace, mbox)

    assert decision_body(decision_for(workspace, subject)) == text
    assert chat_text_for(workspace, subject).endswith(text)


def test_html_part_in_a_legacy_charset_decodes(workspace):
    subject = "Latin HTML"
    html = "<p>Réservation confirmée&nbsp;: vol ZZ1234</p>".encode("iso-8859-1")
    msg = raw_email(subject, base64.encodebytes(html), content_type="text/html; charset=iso-8859-1",
                    transfer_encoding="base64")
    mbox = write_mbox(workspace.root / "mail.mbox", [msg])

    run_ok(workspace, "classify", "--mailbox", str(mbox))

    assert decision_body(decision_for(workspace, subject)) == "Réservation confirmée : vol ZZ1234"


def test_mislabelled_charset_is_decoded_not_unreadable(workspace):
    subject = "Labelled UTF-8 but is latin-1"
    msg = raw_email(subject, "Café on board flight ZZ1234".encode("latin-1"), content_type="text/plain; charset=utf-8")
    mbox = write_mbox(workspace.root / "mail.mbox", [msg])

    result = run_ok(workspace, "classify", "--mailbox", str(mbox))

    assert result.count("failed") == 0
    assert decision_body(decision_for(workspace, subject)) == "Café on board flight ZZ1234"


# -- long emails -----------------------------------------------------------------

TERMS = "".join(f"Clause {i}: the carrier's conditions of carriage apply to this ticket.\n" for i in range(1500))


def long_email(subject: str = "Booking with long terms") -> EmailMessage:
    return email(subject, FLIGHT_BODY + "\nTerms and conditions\n" + TERMS + "END-OF-TERMS\n", sender=SENDER)


def test_long_email_is_truncated_for_the_classifier_and_whole_for_the_extractor(workspace):
    subject = "Booking with long terms"
    mbox = write_mbox(workspace.root / "mail.mbox", [long_email(subject), email("Tiny", "Tiny body.\n")])
    workspace.write_config(f'[ollama]\naddress = "{workspace.ollama.url}"\n\n[extract]\nmax_input_chars = 200000\n')
    workspace.ollama.chats[subject] = Chat(FLIGHT_RECORD)

    result = classify_and_extract(workspace, mbox)

    request = decision_for(workspace, subject)
    body = request["state"]["body"]
    assert len(body) == 12000  # the default [classify] max_body_chars
    assert body.startswith(FLIGHT_BODY.strip()[:100])
    assert "END-OF-TERMS" not in body
    assert request["state"]["sender"] == SENDER and request["state"]["subject"] == subject
    assert decision_body(decision_for(workspace, "Tiny")) == "Tiny body."

    text = chat_text_for(workspace, subject)
    assert "Clause 1499" in text and "END-OF-TERMS" in text
    assert result.count("inputs truncated") == 0


def test_classifier_body_budget_comes_from_config(workspace):
    subject = "Booking with long terms"
    mbox = write_mbox(workspace.root / "mail.mbox", [long_email(subject)])
    workspace.write_config(f'[ollama]\naddress = "{workspace.ollama.url}"\n\n[classify]\nmax_body_chars = 500\n')

    run_ok(workspace, "classify", "--mailbox", str(mbox))

    assert len(decision_body(decision_for(workspace, subject))) == 500


def test_prompt_over_the_token_limit_is_retried_with_the_body_halved(workspace):
    subject = "Booking with long terms"
    mbox = write_mbox(workspace.root / "mail.mbox", [long_email(subject)])
    # 12,000 characters plus the questions is over 3,000 "tokens"; 6,000 is not.
    workspace.ollama.decision_token_limit = 3000

    result = run_ok(workspace, "classify", "--mailbox", str(mbox))

    assert result.count("classified") == 1
    assert result.count("failed") == 0
    assert [len(decision_body(r)) for r in workspace.ollama.decision_requests()] == [12000, 6000]


def test_prompt_over_the_size_limit_is_retried_with_the_body_halved(workspace):
    subject = "Booking with long terms"
    mbox = write_mbox(workspace.root / "mail.mbox", [long_email(subject)])
    workspace.write_config(f'[ollama]\naddress = "{workspace.ollama.url}"\n\n[classify]\nmax_body_chars = 100000\n')
    workspace.ollama.decision_byte_limit = 64 * 1024

    result = run_ok(workspace, "classify", "--mailbox", str(mbox))

    assert result.count("classified") == 1
    assert [len(decision_body(r)) for r in workspace.ollama.decision_requests()] == [100000, 50000]


@pytest.mark.parametrize("status, error", [(400, too_many_tokens_error(9000)), (413, TOO_LARGE_ERROR)])
def test_prompt_still_too_large_after_three_halvings_is_a_failure(workspace, status, error):
    subject = "Booking with long terms"
    mbox = write_mbox(workspace.root / "mail.mbox", [long_email(subject), email("Fine one")])
    workspace.ollama.decisions[subject] = Decision(status=status, error=error)

    result = run_ok(workspace, "classify", "--mailbox", str(mbox))

    assert result.count("classified") == 1
    assert result.count("failed") == 1
    sizes = [len(decision_body(r)) for r in workspace.ollama.decision_requests() if r["state"]["subject"] == subject]
    assert sizes == [12000, 6000, 3000, 1500]
    assert "too large" in result.stdout and str(status) in result.stdout


def test_other_http_400_is_not_retried(workspace):
    subject = "Bad request"
    mbox = write_mbox(workspace.root / "mail.mbox", [email(subject)])
    workspace.ollama.decisions[subject] = Decision(status=400, error="invalid question type")

    result = run_ok(workspace, "classify", "--mailbox", str(mbox))

    assert result.count("failed") == 1
    assert len(workspace.ollama.decision_requests()) == 1


# -- PDF attachments ------------------------------------------------------------------

ETICKET = "ELECTRONIC TICKET\nPassenger: Robin Quill\nFlight ZZ1234 Lisbon (LIS) - Oslo (OSL)\nDate: 12 May 2025"


def test_pdf_text_reaches_the_extractor_and_never_the_classifier(workspace):
    subject = "Your e-ticket"
    msg = email(subject, "Your e-ticket is attached.\n", sender=SENDER,
                attachments=[("eticket-QZX7PL.pdf", text_pdf(ETICKET), "application/pdf")])
    mbox = write_mbox(workspace.root / "mail.mbox", [msg])
    workspace.ollama.chats[subject] = Chat(FLIGHT_RECORD)

    result = classify_and_extract(workspace, mbox)

    request = decision_for(workspace, subject)
    assert request["state"]["attachments"] == "eticket-QZX7PL.pdf"
    assert "ELECTRONIC TICKET" not in str(request["state"])
    text = chat_text_for(workspace, subject)
    assert text.index("Your e-ticket is attached.") < text.index(f"{PDF_DELIMITER}eticket-QZX7PL.pdf -----")
    assert f"{PDF_DELIMITER}eticket-QZX7PL.pdf -----\n{ETICKET}" in text
    assert result.count("PDFs not read") == 0

    run_ok(workspace, "review")
    assert [r["flight_number"] for r in read_csv(workspace.root / "review.csv")] == ["ZZ1234"]
    assert read_csv(workspace.root / "failures.csv") == []


def test_pdf_sent_as_octet_stream_is_read(workspace):
    subject = "Ticket as octet-stream"
    msg = email(subject, "Attached.\n", attachments=[("TICKET.PDF", text_pdf(ETICKET), "application/octet-stream")])
    mbox = write_mbox(workspace.root / "mail.mbox", [msg])

    classify_and_extract(workspace, mbox)

    assert f"{PDF_DELIMITER}TICKET.PDF -----\n{ETICKET}" in chat_text_for(workspace, subject)


def bad_pdf_email(subject: str, *, with_bad: bool = True) -> EmailMessage:
    attachments = [("eticket.pdf", text_pdf(ETICKET), "application/pdf")]
    if with_bad:
        attachments += [
            ("scan.pdf", image_only_pdf(), "application/pdf"),
            ("broken.pdf", b"%PDF-1.4\nthis is not really a PDF", "application/pdf"),
            ("huge.pdf", text_pdf(ETICKET, padding=21 * 1024 * 1024), "application/pdf"),
        ]
    return email(subject, "Your documents are attached.\n", sender=SENDER, attachments=attachments)


def test_unreadable_pdfs_are_listed_in_the_failures_file_while_the_message_extracts(workspace):
    subject = "Your travel documents"
    mbox = write_mbox(workspace.root / "mail.mbox", [bad_pdf_email(subject)])
    workspace.ollama.chats[subject] = Chat(FLIGHT_RECORD)

    result = classify_and_extract(workspace, mbox)

    assert result.count("extracted") == 1
    assert result.count("failed") == 0
    assert result.count("PDFs not read") == 3
    text = chat_text_for(workspace, subject)
    assert f"{PDF_DELIMITER}eticket.pdf -----" in text
    assert "scan.pdf -----" not in text and "broken.pdf -----" not in text and "huge.pdf -----" not in text

    review = run_ok(workspace, "review")

    assert review.count("failures") == 0
    assert review.count("PDF attachments not read") == 3
    assert [r["flight_number"] for r in read_csv(workspace.root / "review.csv")] == ["ZZ1234"]
    failures = read_csv(workspace.root / "failures.csv")
    assert len(failures) == 3
    for failure in failures:
        assert failure["subject"] == subject
        assert failure["sender"] == SENDER
        assert failure["message_date"] == "2025-03-04 09:15"
    reasons = sorted(f["reason"] for f in failures)
    assert reasons[0].startswith("PDF could not be read") and reasons[0].endswith(": broken.pdf")
    assert reasons[1].startswith("PDF too large to read") and reasons[1].endswith(": huge.pdf")
    assert reasons[2] == "image-only PDF not read: scan.pdf"

    # Extracting the message again (here with another model, from a mailbox
    # where it no longer carries the bad PDFs) replaces the notes.
    write_mbox(mbox.with_name("clean.mbox"), [bad_pdf_email(subject, with_bad=False)])
    workspace.write_config(f'[ollama]\naddress = "{workspace.ollama.url}"\n\n[models]\nextraction = "other-extractor"\n')
    again = run_ok(workspace, "extract", "--mailbox", str(mbox.with_name("clean.mbox")))
    assert again.count("extracted") == 1 and again.count("PDFs not read") == 0
    run_ok(workspace, "review")
    assert read_csv(workspace.root / "failures.csv") == []


def test_extractor_input_is_capped_and_counted(workspace):
    subject = "Long body and a ticket"
    msg = email(subject, FLIGHT_BODY + TERMS, sender=SENDER,
                attachments=[("eticket.pdf", text_pdf(ETICKET), "application/pdf")])
    mbox = write_mbox(workspace.root / "mail.mbox", [msg, email("Short one", FLIGHT_BODY)])
    workspace.ollama.chats[subject] = Chat(FLIGHT_RECORD)

    result = classify_and_extract(workspace, mbox)

    assert result.count("extracted") == 2
    assert result.count("failed") == 0
    assert result.count("inputs truncated") == 1
    text = chat_text_for(workspace, subject)
    assert len(text) <= 36000 + 40
    assert text.startswith(f"From: {SENDER}\nSubject: {subject}")
    assert FLIGHT_BODY.strip() in text
    assert "ELECTRONIC TICKET" not in text  # the body came first and filled the budget
    assert text.rstrip().endswith("[... input truncated ...]")

    run_ok(workspace, "review")
    assert [r["flight_number"] for r in read_csv(workspace.root / "review.csv")] == ["ZZ1234"]
    assert read_csv(workspace.root / "failures.csv") == []


def test_extractor_input_cap_comes_from_config_and_cuts_pdf_text_after_the_body(workspace):
    subject = "Your e-ticket"
    msg = email(subject, "Your e-ticket is attached.\n", sender=SENDER,
                attachments=[("eticket.pdf", text_pdf(ETICKET), "application/pdf")])
    mbox = write_mbox(workspace.root / "mail.mbox", [msg])
    workspace.write_config(f'[ollama]\naddress = "{workspace.ollama.url}"\n\n[extract]\nmax_input_chars = 220\n')

    result = classify_and_extract(workspace, mbox)

    assert result.count("inputs truncated") == 1
    text = chat_text_for(workspace, subject)
    assert "Your e-ticket is attached." in text
    assert f"{PDF_DELIMITER}eticket.pdf -----" in text
    assert "Date: 12 May 2025" not in text
