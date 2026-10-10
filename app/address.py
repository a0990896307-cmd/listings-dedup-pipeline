"""Italian address normalisation for cross-agency deduplication.

The same property in Bari is written as "Via Roma 12", "via Roma, 12",
"VIA ROMA 12", "V. Roma 12" and "Via Roma civ. 12" depending on which
agency listed it. Exact string equality finds none of those, so
normalisation happens before any comparison.

The rules encode how Italian addressing actually works:

- civic numbers ("12", "12A", "12/13") identify the building and are the
  strongest single signal
- the street name may appear in full, abbreviated ("V." for "Via"), or
  with the preposition dropped ("dei" for "dei")
- the comune appears alone or with the province abbreviation
- spacing, punctuation, accents and case carry no information

Anything that cannot be interpreted is preserved rather than discarded, so
an unusual address degrades to fuzzy comparison instead of becoming empty.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from unidecode import unidecode

# Street-type abbreviations that agencies use interchangeably with the
# full word. Mapping to a canonical token lets "V. Roma" and "Via Roma"
# compare equal.
_STREET_TYPES = {
    "via": "via",
    "v": "via",
    "viale": "viale",
    "vl": "viale",
    "piazza": "piazza",
    "pza": "piazza",
    "piazzale": "piazzale",
    "corso": "corso",
    "c.so": "corso",
    "largo": "largo",
    "strada": "strada",
    "str": "strada",
    "vicolo": "vicolo",
    "vic": "vicolo",
    "contrada": "contrada",
    "rione": "rione",
    "quartiere": "quartiere",
    "traversa": "traversa",
    "borgata": "borgata",
    "lungomare": "lungomare",
    "l.mare": "lungomare",
}

# "civ." is how agencies mark the building number. Dropping the marker
# keeps the digits, which is what actually needs to match.
_CIVIL_MARKERS = r"\b(?:civ|civile|num|n|r)?\.?\s*"

# Words that carry no identifying signal once the street type is
# normalised. "di" appears or vanishes between agencies depending on the
# form, so dropping it is what makes "Via degli Aranci" match "Viale
# Aranci".
_NOISE = {
    "di", "dei", "del", "della", "dello", "degli", "delle", "de", "dal",
    "dalla", "dalle", "da", "la", "le", "il", "lo", "i", "gli", "e", "a",
    "al", "alla", "alle", "ai", "agli", "nei", "nel", "nella", "nelle",
    "negli", "presso", "zona", "area", "fraz", "localita", "localita",
}

# Province abbreviations only. Full names are deliberately excluded: a
# Bari address on "Via Napoli" must not be classified as the province of
# Naples just because the street shares its name. Two-letter codes are
# unambiguous; full names are not.
_PROVINCE = re.compile(r"\b(?:BA|NA|LE|TA|BR|FC|SU|CA|SS|OR|NU|PA|CT|RG|EN|AQ|CH|FE|MO|RE|PR|PO|PZ|TR|AR|SP|SV|IM|TO|BI|CN|AL|AO|AT|CZ|VI|VR|BL|FC|AE|MI|MB|TN|TN|CO|SO)\b")


@dataclass
class Address:
    """A normalised address broken into comparable parts."""

    street_type: str = ""
    street_name: str = ""
    civic: str = ""
    comune: str = ""
    province: str = ""
    raw: str = ""
    extras: list[str] = field(default_factory=list)

    @property
    def is_usable(self) -> bool:
        """True when there is enough to compare on.

        Street plus civic number is the minimum for a confident match.
        Street alone will fuzzy-match neighbours, which is exactly the
        false-merge case that costs a comparable sale.
        """
        return bool(self.street_name and self.civic)

    @property
    def key(self) -> str:
        """Exact-match key. Empty when not usable, so callers must not
        treat a blank key as a matchable value.

        Street type is deliberately excluded. Agencies write "Roma 12" as
        often as "Via Roma 12", and including the type would split the
        same address into two keys, pushing a true duplicate down into the
        fuzzy tier where it needs review. The type is still parsed and
        kept on the record for display.
        """
        if not self.is_usable:
            return ""
        return f"{self.comune}|{self.street_name}|{self.civic}"

    def to_dict(self) -> dict:
        return {
            "street_type": self.street_type,
            "street_name": self.street_name,
            "civic": self.civic,
            "comune": self.comune,
            "province": self.province,
            "raw": self.raw,
            "is_usable": self.is_usable,
            "key": self.key,
        }


def strip_accents(text: str) -> str:
    """Fold diacritics so 'Vialé' and 'Viale' compare equal."""
    decomposed = unicodedata.normalize("NFKD", text)
    without = "".join(c for c in decomposed if not unicodedata.combining(c))
    return unidecode(without).lower()


def _tokens(text: str) -> list[str]:
    cleaned = strip_accents(text)
    cleaned = re.sub(r"[^a-z0-9/]+", " ", cleaned)
    return [t for t in cleaned.split() if t]


def extract_civic(tokens: list[str]) -> tuple[str, list[str]]:
    """Pull the civic number out of the token stream.

    Handles "12", "12A", "12/13", "12-13" and the "civ. 12" / "n. 12"
    forms. A range is preserved verbatim because "12/13" is one listing,
    not two. The marker word itself is consumed, otherwise it becomes part
    of the street name and stops matching against the plain form.
    """
    for i, tok in enumerate(tokens):
        # Number, optionally followed by a letter or a range of numbers.
        m = re.fullmatch(r"(\d+)(?:([a-z])|(?:/(\d+[a-z]?)))?", tok)
        if m:
            base, letter, rng = m.groups()
            if letter:
                return f"{base}{letter}", tokens[:i] + tokens[i + 1 :]
            if rng:
                return f"{base}/{rng}", tokens[:i] + tokens[i + 1 :]
            # A bare number in an address is almost always the civic.
            # Guard against it being a floor or an internal code.
            if i > 0:
                rest = tokens[:i] + tokens[i + 1 :]
                # Consume an adjacent marker so it does not pollute the name.
                while rest and re.fullmatch(_CIVIL_MARKERS.strip() or r"x", rest[i - 1] if i - 1 < len(rest) else "", re.I):
                    rest.pop(i - 1)
                return base, rest

    # "civ. 12" and "n. 13" where the number came first in the stream.
    m2 = re.search(r"(?:civ|civile|num|n|r)\.?\s*(\d+[a-z]?(?:/\d+[a-z]?)?)", " ".join(tokens))
    if m2:
        civic = m2.group(1)
        cleaned = re.sub(m2.group(0), " ", " ".join(tokens))
        return civic, cleaned.split()

    return "", tokens


def normalise(raw: str, comune_hint: str = "") -> Address:
    """Parse a raw Italian address string into comparable components."""
    raw = (raw or "").strip()
    tokens = _tokens(raw)

    if not tokens:
        return Address(raw=raw)

    civic, tokens = extract_civic(tokens)

    street_type = ""
    if tokens:
        candidate = tokens[0].strip(".")
        if candidate in _STREET_TYPES:
            street_type = _STREET_TYPES[candidate]
            tokens = tokens[1:]

    # Province is easier to spot on the raw text than on stripped tokens,
    # because it appears glued to the name. Case sensitive: an upper-case
    # BA is a province code, a lowercase "ba" inside a street name is not.
    province = ""
    pm = _PROVINCE.search(raw)
    if pm:
        province = pm.group(0)

    comune = strip_accents(comune_hint).strip() if comune_hint else ""

    name_parts: list[str] = []
    extras: list[str] = []
    for tok in tokens:
        if tok in _NOISE:
            extras.append(tok)
            continue
        if tok == province:
            continue
        name_parts.append(tok)

    street_name = " ".join(name_parts).strip()

    # If no usable comune was supplied, fall back to whatever token is not
    # part of the street name and not noise. Italian addresses usually put
    # the comune last.
    if not comune and name_parts:
        tail = name_parts[-1]
        if tail not in _STREET_TYPES.values() and len(tail) > 2:
            comune = tail
            street_name = " ".join(name_parts[:-1]).strip()

    return Address(
        street_type=street_type,
        street_name=street_name,
        civic=civic,
        comune=comune,
        province=province,
        raw=raw,
        extras=extras,
    )


_INITIALS = re.compile(r"^[a-z]$")


def _token_matches(a: str, b: str) -> bool:
    """Single-letter initials match the full name they abbreviate.

    Agencies write both "Via Giuseppe Verdi" and "V. G. Verdi" for the
    same street. Plain string similarity scores those far apart because
    one string is three characters longer in a four-token name, which is
    exactly the case that produces a missed duplicate.
    """
    if a == b:
        return True
    if _INITIALS.match(a) and b.startswith(a):
        return True
    if _INITIALS.match(b) and a.startswith(b):
        return True
    return False


def street_similarity(a: Address, b: Address) -> float:
    """0..1 similarity of two street names, tolerant of spacing and typos.

    Combines whole-string fuzzy scores with an initial-aware token
    comparison, because the two catch different mistakes: fuzzy ratios
    handle typos and dropped prepositions, token comparison handles
    spelled-out versus abbreviated names.
    """
    from rapidfuzz import fuzz

    if not a.street_name or not b.street_name:
        return 0.0

    ta = a.street_name.split()
    tb = b.street_name.split()

    # Initial-aware ratio: each aligned pair scores 100 when it matches
    # directly or through an initial, 0 otherwise, then averaged. Tokens
    # that appear in only one name count against it, so a name missing an
    # token is penalised rather than ignored.
    if ta and tb:
        pairs = 0
        hits = 0
        for x in ta:
            best = any(_token_matches(x, y) for y in tb)
            pairs += 1
            hits += 1 if best else 0
        for y in tb:
            if not any(_token_matches(x, y) for x in ta):
                pairs += 1
        token_ratio = hits / pairs if pairs else 0.0
    else:
        token_ratio = 0.0

    return max(
        fuzz.token_set_ratio(a.street_name, b.street_name),
        fuzz.token_sort_ratio(a.street_name, b.street_name),
        token_ratio * 100.0,
    ) / 100.0