"""Passenger names: does a name on a booking belong to the user?

Both sides are reduced to tokens: case-folded, accents and diacritics
removed, split on anything that is not a letter, with titles (Mr, Mrs, Dr,
Herr, Mme, ...) dropped. Airline formats therefore read like any other:
"ELLIS/JORDAN MR", "Ellis, Jordan", "Mr Jordan Ellis" and "Jördan Éllis" all
become {"jordan", "ellis"}. In the airline "SURNAME/GIVEN" format a title is
sometimes glued to the end of the given name ("ELLIS/JORDANMR"); that last
token is tried both as written and with the title cut off.

A passenger matches a configured name when every token of the configured
name is among the passenger's tokens (extra middle names on the ticket, in
any order, are fine), or when the passenger has at least two tokens and all
of them are among the configured name's tokens (a ticket that leaves out a
configured middle name). A single-token passenger matches only a
single-token configured name. No fuzzy or initial matching.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

# Courtesy titles in the languages airline mail commonly uses, case-folded and
# without accents or punctuation. Ambiguous words that are also given names
# (Don, Sir) are deliberately left out.
TITLES = frozenset(
    {
        "mr", "mrs", "ms", "mx", "miss", "mstr", "master", "dr", "prof",
        "sr", "sra", "srta", "herr", "frau", "fr", "m", "mme", "mlle",
        "sig", "sigra", "dott", "dra", "mevr", "dhr",
    }
)
# Titles an airline glues to the end of the given name: "ELLIS/JORDANMR".
_GLUED_TITLES = ("mstr", "miss", "mrs", "mr", "ms")

# Letters NFKD does not decompose into a base letter plus a combining mark.
_FOLD = str.maketrans({"ø": "o", "ł": "l", "đ": "d", "ð": "d", "þ": "th", "æ": "ae", "œ": "oe", "ı": "i"})
_LETTERS = re.compile(r"[^\W\d_]+")


def tokens(name: str) -> frozenset[str]:
    """The name's tokens, titles dropped."""
    return frozenset(_token_list(name))


def names_someone(name: str) -> bool:
    """Whether the string names anyone at all (it has a token that is not a title)."""
    return bool(_token_list(name))


def matches_any(passenger: str, configured: Iterable[str]) -> bool:
    candidates = _candidates(passenger)
    for name in configured:
        wanted = tokens(name)
        if wanted and any(_matches(found, wanted) for found in candidates):
            return True
    return False


def _matches(found: frozenset[str], wanted: frozenset[str]) -> bool:
    return wanted <= found or (len(found) >= 2 and found <= wanted)


def _candidates(passenger: str) -> list[frozenset[str]]:
    words = _token_list(passenger)
    candidates = [frozenset(words)]
    if "/" in passenger and words:
        last = words[-1]
        for title in _GLUED_TITLES:
            if last.endswith(title) and len(last) - len(title) >= 2:
                candidates.append(frozenset([*words[:-1], last[: -len(title)]]))
                break
    return [c for c in candidates if c]


def _token_list(name: str) -> list[str]:
    folded = unicodedata.normalize("NFKD", name.casefold().translate(_FOLD))
    plain = "".join(c for c in folded if not unicodedata.combining(c))
    return [word for word in _LETTERS.findall(plain) if word not in TITLES]
