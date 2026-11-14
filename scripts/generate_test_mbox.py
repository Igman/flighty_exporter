#!/usr/bin/env python3
"""Generate a synthetic Gmail-Takeout-style mbox for testing.

Every person, booking reference and address is invented. The structure
(who sends what, when, and the flight facts) is decided here with a fixed
seed; a local Ollama model only writes the subject and body text.

Usage:
    python3 scripts/generate_test_mbox.py [--out test.mbox] [--model gemma4:12b]
                                          [--limit N] [--cache PATH]

Writes <out> and <out>.truth.json (ground truth for the flight emails).
Standard library only.
"""

import argparse
import datetime as dt
import email.utils
import html
import json
import mailbox
import os
import random
import re
import sys
import time
import urllib.request
from email.message import EmailMessage

OWNER_NAME = "Jordan Ellis"
OWNER_ADDR = "jordan.ellis@example.com"
OLLAMA_URL = os.environ.get("OLLAMA_HOST_URL", "http://localhost:11434")
SEED = 20261113

CITIES = ["Lisbon", "Denver", "Osaka", "Toronto", "Cape Town", "Austin", "Vienna",
          "Seattle", "Dublin", "Mexico City", "Singapore", "Portland", "Madrid",
          "Chicago", "Auckland", "Boston", "Berlin", "Vancouver"]
TOPICS = ["a kitchen renovation", "the quarterly roadmap", "a birthday dinner",
          "a marathon training plan", "a book club pick", "a broken dishwasher",
          "the school fundraiser", "a camping trip", "a job referral",
          "the budget review", "a new puppy", "a design critique",
          "an onboarding checklist", "a podcast recommendation", "tax paperwork"]
PRODUCTS = ["running shoes", "a standing desk", "noise-cancelling headphones",
            "a cast-iron pan", "printer ink", "a rain jacket", "a phone case",
            "coffee beans", "a desk lamp", "hiking socks", "a backpack",
            "a mechanical keyboard", "bike lights", "a yoga mat"]
PEOPLE = ["Maya Okafor", "Tom Brandt", "Lucia Ferreira", "Sam Whitlock",
          "Hana Kobayashi", "Dev Malhotra", "Erin Castellano", "Noah Lindqvist",
          "Grace Adeyemi", "Paul Renner"]

# (category, count, labels, senders, instruction)
NON_FLIGHT = [
    ("airline_marketing", 12, ["Category Promotions"],
     [("United Airlines", "deals@news.united.example"),
      ("British Airways", "offers@email.ba.example"),
      ("Lufthansa", "newsletter@mailing.lufthansa.example"),
      ("Delta Air Lines", "promo@e.delta.example"),
      ("Alaska Airlines", "deals@ifly.alaskaair.example")],
     "a promotional email from the airline advertising a fare sale or a new route to {city}. "
     "It mentions example prices and destinations but is NOT a booking: no booking reference, "
     "no passenger, no specific flight number or travel date for the recipient."),
    ("loyalty_statement", 6, ["Category Updates"],
     [("MileagePlus", "statements@mileageplus.united.example"),
      ("Executive Club", "club@email.ba.example"),
      ("Miles & More", "statement@miles-and-more.example")],
     "a monthly frequent-flyer account statement showing a points balance, tier status and a "
     "partner offer. It does not list any individual flight details."),
    ("hotel_booking", 10, ["Inbox", "Category Updates"],
     [("Harbourline Hotels", "reservations@harbourline.example"),
      ("Staywell", "bookings@staywell.example"),
      ("The Alder Inn", "frontdesk@alderinn.example")],
     "a hotel reservation confirmation in {city} with a confirmation number, check-in and "
     "check-out dates, room type and nightly rate. No flights are mentioned."),
    ("train_ticket", 6, ["Inbox", "Category Updates"],
     [("Northline Rail", "tickets@northlinerail.example"),
      ("EuroTrack", "noreply@eurotrack.example")],
     "a train ticket confirmation with a booking code, departure and arrival stations, train "
     "number, coach and seat. It is rail travel, not a flight."),
    ("car_rental", 5, ["Inbox", "Category Updates"],
     [("Kestrel Car Hire", "reservations@kestrelcars.example"),
      ("Roadwise Rentals", "confirm@roadwise.example")],
     "a rental car reservation confirmation for pick-up at {city} airport, with a reservation "
     "number, vehicle class, pick-up and return times. No flight number is included."),
    ("rideshare_receipt", 6, ["Inbox", "Category Updates"],
     [("Hopp", "receipts@hopp.example"), ("Zipcab", "noreply@zipcab.example")],
     "a ride receipt for a trip to or from the airport in {city}, with fare breakdown, "
     "distance and driver first name."),
    ("travel_insurance", 3, ["Inbox"],
     [("Safeguard Travel", "policy@safeguardtravel.example")],
     "a travel insurance policy confirmation for a trip to {city} with a policy number, "
     "coverage dates and a summary of cover. It does not list flights."),
    ("airport_services", 3, ["Inbox", "Category Updates"],
     [("ParkJet", "bookings@parkjet.example"), ("SkyLounge Pass", "hello@skyloungepass.example")],
     "a confirmation for airport parking or lounge access at {city} airport with a booking "
     "code, dates and price. No flight number or airline booking reference."),
    ("shopping", 22, ["Inbox", "Category Updates"],
     [("Marlowe & Finch", "orders@marlowefinch.example"),
      ("Bolt Outfitters", "shipping@boltoutfitters.example"),
      ("Pantry Post", "hello@pantrypost.example"),
      ("Gearwell", "noreply@gearwell.example")],
     "an order confirmation or shipping notification for {product}, with an order number, "
     "item list, total and delivery estimate."),
    ("newsletter", 22, ["Category Promotions"],
     [("The Morning Ledger", "editors@morningledger.example"),
      ("Byte Digest", "digest@bytedigest.example"),
      ("Trailhead Weekly", "hello@trailheadweekly.example"),
      ("Kitchen Notes", "team@kitchennotes.example")],
     "a newsletter issue with three or four short items. One item may touch on {topic}."),
    ("bank", 12, ["Inbox", "Category Updates"],
     [("Calder Bank", "alerts@calderbank.example"),
      ("Northgate Card Services", "statements@northgatecard.example")],
     "a bank or credit card notification: a statement-ready notice, a large transaction "
     "alert or a payment-due reminder, with masked account digits."),
    ("work", 25, ["Inbox"],
     None,
     "a work email from a colleague about {topic}: meeting scheduling, a status update, or a "
     "request for review. Conversational, signed with the sender's first name."),
    ("personal", 18, ["Inbox"],
     None,
     "a personal email from a friend or relative about {topic}. Casual tone. It may mention in "
     "passing that someone is travelling or 'landing around 5', but gives no airline, flight "
     "number or booking details."),
    ("social", 10, ["Category Social"],
     [("Linkwell", "notifications@linkwell.example"),
      ("Photostream", "no-reply@photostream.example"),
      ("Meetloop", "info@meetloop.example")],
     "an automated social network notification: new connection, comment, event invitation or "
     "weekly activity summary."),
    ("saas", 12, ["Inbox", "Category Updates"],
     [("Docstack", "security@docstack.example"),
      ("Planboard", "no-reply@planboard.example"),
      ("Vaultkey", "account@vaultkey.example")],
     "an account email from a software service: password reset, new sign-in alert, trial "
     "ending, or invoice available."),
    ("utility", 10, ["Inbox", "Category Updates"],
     [("Brightgrid Energy", "billing@brightgrid.example"),
      ("Fibrelane", "accounts@fibrelane.example"),
      ("Streamhouse", "billing@streamhouse.example")],
     "a utility or subscription bill notice with an amount due, billing period and due date."),
    ("event_ticket", 5, ["Inbox", "Category Updates"],
     [("Tixly", "orders@tixly.example"), ("Stagegate", "tickets@stagegate.example")],
     "a ticket confirmation for a concert, theatre show or sports match in {city}, with order "
     "number, seat details and venue."),
    ("restaurant", 3, ["Inbox", "Category Updates"],
     [("TableReady", "reservations@tableready.example")],
     "a restaurant reservation confirmation in {city} with party size, date and time."),
]

# The ten flight emails. Facts are fixed here so the ground truth is exact.
FLIGHTS = [
    dict(key="ua-confirm", kind="booking", sent="2019-02-20T09:14", lang="English",
         sender=("United Airlines", "receipts@united.example"), pnr="K7QM2L",
         passengers=[OWNER_NAME],
         segments=[dict(date="2019-03-14", airline="United Airlines", flight="UA328",
                        origin="SFO", dest="ORD", dep="08:05", arr="14:20")],
         instruction="an airline booking confirmation and e-ticket receipt"),
    dict(key="ba-confirm", kind="booking", sent="2021-07-11T18:42", lang="English",
         sender=("British Airways", "bookings@ba.example"), pnr="X4TRW9",
         passengers=[OWNER_NAME],
         segments=[dict(date="2021-09-02", airline="British Airways", flight="BA286",
                        origin="SFO", dest="LHR", dep="19:30", arr="13:45 (+1 day)"),
                   dict(date="2021-09-16", airline="British Airways", flight="BA285",
                        origin="LHR", dest="SFO", dep="11:15", arr="14:10")],
         instruction="an airline booking confirmation for a return trip"),
    dict(key="ba-checkin", kind="check-in", sent="2021-09-01T19:31", lang="English",
         sender=("British Airways", "checkin@ba.example"), pnr="X4TRW9",
         passengers=[OWNER_NAME],
         segments=[dict(date="2021-09-02", airline="British Airways", flight="BA286",
                        origin="SFO", dest="LHR", dep="19:30", arr="13:45 (+1 day)")],
         instruction="an online check-in reminder saying check-in is now open for the "
                     "outbound flight"),
    dict(key="lh-confirm", kind="booking", sent="2022-03-30T07:55", lang="English",
         sender=("Lufthansa", "booking@lufthansa.example"), pnr="P9ZD4H",
         passengers=[OWNER_NAME],
         segments=[dict(date="2022-05-20", airline="Lufthansa", flight="LH455",
                        origin="SFO", dest="FRA", dep="14:40", arr="10:30 (+1 day)"),
                   dict(date="2022-05-21", airline="Lufthansa", flight="LH1232",
                        origin="FRA", dest="VIE", dep="12:15", arr="13:40")],
         instruction="an airline booking confirmation for a one-way trip with a connection"),
    dict(key="lh-change", kind="change", sent="2022-04-26T13:02", lang="English",
         sender=("Lufthansa", "service@lufthansa.example"), pnr="P9ZD4H",
         passengers=[OWNER_NAME],
         segments=[dict(date="2022-05-20", airline="Lufthansa", flight="LH455",
                        origin="SFO", dest="FRA", dep="14:40", arr="10:30 (+1 day)"),
                   dict(date="2022-05-21", airline="Lufthansa", flight="LH1238",
                        origin="FRA", dest="VIE", dep="15:05", arr="16:30")],
         instruction="a schedule change notice: the connecting flight LH1232 has been replaced "
                     "by the later flight shown below; list the full updated itinerary"),
    dict(key="dl-confirm", kind="booking", sent="2023-10-02T21:17", lang="English",
         sender=("Delta Air Lines", "DeltaAirLines@t.delta.example"), pnr="HJ3N6V",
         passengers=[OWNER_NAME],
         segments=[dict(date="2023-11-08", airline="Delta Air Lines", flight="DL1432",
                        origin="SEA", dest="ATL", dep="06:45", arr="14:38")],
         instruction="an airline booking confirmation and receipt"),
    dict(key="dl-cancel", kind="cancellation", sent="2023-10-29T10:06", lang="English",
         sender=("Delta Air Lines", "DeltaAirLines@t.delta.example"), pnr="HJ3N6V",
         passengers=[OWNER_NAME],
         segments=[dict(date="2023-11-08", airline="Delta Air Lines", flight="DL1432",
                        origin="SEA", dest="ATL", dep="06:45", arr="14:38")],
         instruction="a cancellation confirmation: the customer cancelled this trip and an "
                     "eCredit has been issued; state clearly that the flight below is cancelled"),
    dict(key="agency-itinerary", kind="booking", sent="2018-06-18T15:23", lang="English",
         sender=("Northwind Travel", "itineraries@northwindtravel.example"), pnr="M2KQ8B",
         passengers=[OWNER_NAME],
         segments=[dict(date="2018-07-22", airline="Air Canada", flight="AC759",
                        origin="YYZ", dest="SFO", dep="17:55", arr="20:25")],
         instruction="an itinerary and e-ticket from a corporate travel agency, written by an "
                     "agent, with the airline booking reference and an agency record number"),
    dict(key="as-other-passenger", kind="booking", sent="2024-01-15T12:48", lang="English",
         sender=("Alaska Airlines", "confirmation@alaskaair.example"), pnr="RVL5TC",
         passengers=["Priya Raman"],
         segments=[dict(date="2024-02-10", airline="Alaska Airlines", flight="AS1084",
                        origin="SFO", dest="PDX", dep="09:20", arr="11:05")],
         instruction="an airline booking confirmation sent to the purchaser; the traveller is "
                     "a different person from the recipient"),
    dict(key="ib-confirm-es", kind="booking", sent="2017-09-12T08:31", lang="Spanish",
         sender=("Iberia", "confirmacion@iberia.example"), pnr="T6WY3F",
         passengers=[OWNER_NAME],
         segments=[dict(date="2017-10-05", airline="Iberia", flight="IB3170",
                        origin="MAD", dest="BCN", dep="07:45", arr="09:05")],
         instruction="an airline booking confirmation written entirely in Spanish"),
]

SYSTEM = (
    "You write realistic sample emails for software test fixtures. All people and "
    "reference numbers are fictional. Output only the email itself in this exact layout: "
    "the first line is 'Subject: <subject>', then one blank line, then the plain-text body. "
    "No markdown, no asterisks, no commentary before or after, no placeholder text in "
    "square brackets."
)


def ollama_chat(model, prompt, temperature=1.0, num_predict=700):
    payload = {
        "model": model,
        "stream": False,
        "think": False,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": prompt}],
        "options": {"temperature": temperature, "num_predict": num_predict, "num_ctx": 4096},
    }
    req = urllib.request.Request(
        OLLAMA_URL + "/api/chat", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as resp:
        return json.loads(resp.read())["message"]["content"]


def split_subject(text, fallback):
    text = text.strip().replace("\r\n", "\n")
    text = re.sub(r"^```[a-z]*\n|\n```$", "", text).strip()
    m = re.match(r"^\**subject:\**\s*(.+)", text, re.I)
    if not m:
        return fallback, text
    subject = m.group(1).strip().strip("*").strip()
    body = text[m.end():].lstrip("\n")
    return subject, body


def flight_facts(f):
    lines = ["Booking reference: %s" % f["pnr"],
             "Passenger(s): %s" % ", ".join(f["passengers"])]
    for i, s in enumerate(f["segments"], 1):
        d = dt.date.fromisoformat(s["date"])
        lines.append("Flight %d: %s flight %s on %s, %s to %s, departs %s, arrives %s" % (
            i, s["airline"], s["flight"], d.strftime("%A %d %B %Y"),
            s["origin"], s["dest"], s["dep"], s["arr"]))
    return "\n".join(lines)


def flight_ok(f, body):
    squashed = re.sub(r"\s+", "", body).upper()
    if f["pnr"] not in squashed:
        return False
    for s in f["segments"]:
        if s["flight"] not in squashed:
            return False
        if s["origin"] not in body or s["dest"] not in body:
            return False
    return True


def build_plan(rng):
    plan = []
    for cat, count, labels, senders, instruction in NON_FLIGHT:
        for _ in range(count):
            if senders is None:
                person = rng.choice(PEOPLE)
                first, last = person.lower().split(" ")
                domain = "brightwater-labs.example" if cat == "work" else "mailbox.example"
                sender = (person, "%s.%s@%s" % (first, last, domain))
            else:
                sender = rng.choice(senders)
            day = dt.date(2016, 1, 1) + dt.timedelta(days=rng.randrange(0, 3600))
            sent = dt.datetime.combine(day, dt.time(rng.randrange(6, 23), rng.randrange(60)))
            plan.append(dict(
                key=None, category=cat, sender=sender, sent=sent, labels=list(labels),
                instruction=instruction.format(city=rng.choice(CITIES),
                                               topic=rng.choice(TOPICS),
                                               product=rng.choice(PRODUCTS)),
                html=rng.random() < (0.1 if cat in ("work", "personal") else 0.4)))
    # A few messages under labels the tool must skip.
    for item in rng.sample(plan, 6):
        item["labels"] = [rng.choice(["Spam", "Trash"])]
    for f in FLIGHTS:
        plan.append(dict(
            key=f["key"], category="flight", sender=f["sender"],
            sent=dt.datetime.fromisoformat(f["sent"]), labels=["Inbox", "Category Updates"],
            flight=f, html=f["key"] in ("ua-confirm", "dl-confirm", "ib-confirm-es")))
    plan.sort(key=lambda p: p["sent"])
    for i, p in enumerate(plan):
        p["index"] = i
        p["message_id"] = "<%s.%04d@%s>" % (
            p["sent"].strftime("%Y%m%d%H%M"), i, p["sender"][1].split("@")[1])
    return plan


def write_email(model, item):
    if item["category"] == "flight":
        f = item["flight"]
        prompt = (
            "Write %s from %s to the customer %s <%s>. Language: %s.\n"
            "Use exactly these details and do not change any code, number, date or time:\n%s\n"
            "Write airports as city name followed by the three-letter code in brackets. "
            "Add plausible extras such as ticket number, fare, cabin and baggage allowance, "
            "and a short footer. 150 to 280 words."
            % (f["instruction"], f["sender"][0], OWNER_NAME, OWNER_ADDR, f["lang"],
               flight_facts(f)))
        fallback = "Your booking %s" % f["pnr"]
        for attempt in range(4):
            subject, body = split_subject(
                ollama_chat(model, prompt, temperature=0.6, num_predict=900), fallback)
            if flight_ok(f, body):
                return subject, body
        # Guarantee the ground truth is present even if the model drifted.
        return subject, body + "\n\n--- Itinerary details ---\n" + flight_facts(f)
    prompt = (
        "Write %s\nThe sender is %s <%s>. The recipient is %s <%s>. The email is dated %s. "
        "60 to 200 words."
        % (item["instruction"], item["sender"][0], item["sender"][1], OWNER_NAME, OWNER_ADDR,
           item["sent"].strftime("%d %B %Y")))
    fallback = "Message from %s" % item["sender"][0]
    return split_subject(ollama_chat(model, prompt), fallback)


def to_html(body):
    paras = [p for p in body.split("\n\n") if p.strip()]
    inner = "\n".join("<p>%s</p>" % html.escape(p).replace("\n", "<br>\n") for p in paras)
    return ("<html><body style=\"font-family:Arial,sans-serif;font-size:14px\">\n"
            "<table width=\"600\" align=\"center\"><tr><td>\n%s\n</td></tr></table>\n"
            "</body></html>\n" % inner)


def build_message(item, subject, body):
    msg = EmailMessage()
    msg["From"] = email.utils.formataddr(item["sender"])
    msg["To"] = email.utils.formataddr((OWNER_NAME, OWNER_ADDR))
    msg["Subject"] = subject
    msg["Date"] = email.utils.format_datetime(item["sent"].replace(tzinfo=dt.timezone.utc))
    msg["Message-ID"] = item["message_id"]
    msg["X-GM-THRID"] = str(1600000000000000000 + item["index"] * 7919)
    msg["X-Gmail-Labels"] = ",".join(item["labels"])
    msg.set_content(body)
    if item["html"]:
        msg.add_alternative(to_html(body), subtype="html")
    return msg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="test.mbox")
    ap.add_argument("--model", default="gemma4:12b")
    ap.add_argument("--limit", type=int, default=0, help="only generate the first N (smoke test)")
    ap.add_argument("--only-flights", action="store_true")
    ap.add_argument("--cache", default="", help="JSON file to resume from")
    args = ap.parse_args()

    plan = build_plan(random.Random(SEED))
    if args.only_flights:
        plan = [p for p in plan if p["category"] == "flight"]
    if args.limit:
        plan = plan[:args.limit]

    cache = {}
    if args.cache and os.path.exists(args.cache):
        with open(args.cache) as fh:
            cache = json.load(fh)

    start = time.time()
    for n, item in enumerate(plan, 1):
        mid = item["message_id"]
        if mid not in cache:
            subject, body = write_email(args.model, item)
            cache[mid] = [subject, body]
            if args.cache:
                with open(args.cache, "w") as fh:
                    json.dump(cache, fh)
        print("[%3d/%d] %-18s %s" % (n, len(plan), item["category"], cache[mid][0][:70]),
              flush=True)

    if os.path.exists(args.out):
        os.remove(args.out)
    box = mailbox.mbox(args.out)
    truth = []
    for item in plan:
        subject, body = cache[item["message_id"]]
        msg = mailbox.mboxMessage(build_message(item, subject, body))
        msg.set_from("%d@xxx" % (1600000000000000000 + item["index"] * 7919),
                     item["sent"].timetuple())
        box.add(msg)
        if item["category"] == "flight":
            f = item["flight"]
            truth.append(dict(
                message_id=item["message_id"], key=f["key"], kind=f["kind"],
                language=f["lang"], booking_reference=f["pnr"], passengers=f["passengers"],
                facts_verbatim_in_body=flight_ok(f, body),
                segments=[dict(date=s["date"], airline=s["airline"],
                               flight_number=s["flight"], origin=s["origin"],
                               destination=s["dest"]) for s in f["segments"]]))
    box.flush()
    box.close()
    with open(args.out + ".truth.json", "w") as fh:
        json.dump(dict(owner=dict(name=OWNER_NAME, address=OWNER_ADDR), model=args.model,
                       total_messages=len(plan), flight_messages=truth), fh, indent=2)
    print("wrote %s (%d messages, %d flight) in %.0fs" % (
        args.out, len(plan), len(truth), time.time() - start))


if __name__ == "__main__":
    sys.exit(main())
