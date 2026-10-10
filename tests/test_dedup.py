"""Tests for address normalisation and cross-agency deduplication.

The cases are drawn from how Italian agencies actually write the same
Bari address. A deduplicator that only passes clean unit tests will merge
neighbours or split duplicates in production, so the ambiguous cases
matter more than the easy ones.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.address import normalise, street_similarity, strip_accents
from app.dedup import (
    Decision,
    Deduplicator,
    Listing,
    compare,
    haversine_m,
)

failures: list[str] = []
checks = 0


def check(name: str, actual, expected) -> None:
    global checks
    checks += 1
    if actual != expected:
        failures.append(f"{name}\n     expected: {expected!r}\n     actual:   {actual!r}")


def check_true(name: str, value) -> None:
    check(name, bool(value), True)


def check_false(name: str, value) -> None:
    check(name, bool(value), False)


# --------------------------------------------------------- normalisation

check("accents stripped", strip_accents("Vialé"), "viale")
check("empty stays empty", normalise("").is_usable, False)

a = normalise("Via Roma 12", "Bari")
check("street type parsed", a.street_type, "via")
check("street name parsed", a.street_name, "roma")
check("civic parsed", a.civic, "12")
check("comune parsed", a.comune, "bari")
check("usable", a.is_usable, True)

b = normalise("via  roma, 12", "Bari")
check("comma and spacing ignored", b.key, a.key)

c = normalise("VIA ROMA 12", "Bari")
check("case ignored", c.key, a.key)

d = normalise("V. Roma 12", "Bari")
check("abbreviated street type", d.street_type, "via")
check("abbreviated key matches", d.key, a.key)

e = normalise("Via Roma civ. 12", "Bari")
check("civ. marker stripped", e.civic, "12")
check("civ. key matches", e.key, a.key)

f = normalise("Via Roma 12A", "Bari")
check("letter suffix kept", f.civic, "12a")

g = normalise("Via Roma 12/13", "Bari")
check("range kept whole", g.civic, "12/13")

h = normalise("Via G. Marconi 5", "Bari")
check("multiword name kept", h.street_name, "g marconi")

i = normalise("Viale degli Aranci 3", "Bari")
check("preposition dropped from name", i.street_name, "aranci")
check("preposition kept as extra", "degli" in i.extras, True)
check("street type is viale", i.street_type, "viale")

j = normalise("Via Roma", "Bari")
check("no civic is not usable", j.is_usable, False)
check("no civic yields empty key", j.key, "")

k = normalise("c/o", "Bari")
check("garbage is not usable", k.is_usable, False)

# Street similarity must tolerate the missing preposition and spacing.
s = normalise("Via degli Aranci 3", "Bari")
t = normalise("Viale Aranci 3", "Bari")
check_true("similarity high across variants", street_similarity(i, s) > 0.6)

# ------------------------------------------------------------- geography

check("haversine zero for same point", round(haversine_m(41.1171, 16.8719, 41.1171, 16.8719), 3), 0.0)
# Bari centre to a point roughly 100 m north.
d100 = haversine_m(41.1171, 16.8719, 41.1180, 16.8719)
check_true("haversine ~100m", 95 < d100 < 105)

# ------------------------------------------------------------- comparison

def L(**kw) -> Listing:
    base = dict(id="x", address_raw="Via Roma 12", comune="Bari")
    base.update(kw)
    return Listing(**base)


# exact: same address, different agency -> merged
r = compare(L(id="a", agency="AgencyOne"), L(id="b", agency="AgencyTwo"))
check("exact merge", r.decision, Decision.EXACT)
check("exact is merged", r.merged, True)
check("exact tier", r.tier, 1)
check_true("exact notes agency difference",
           any("different agency" in x for x in r.reasons))

# exact requires a usable address
r = compare(L(id="a", address_raw="Via Roma"), L(id="b", address_raw="Via Roma"))
check("no civic does not exact-match", r.decision, Decision.REJECT)

# different street, same civic number -> not merged
r = compare(L(id="a"), L(id="b", address_raw="Via Milano 12"))
check("different street rejected", r.decision, Decision.REJECT)

# --------------------------------------------------------------- geo tier
# Geo fires when the address key differs but the building is the same and
# coordinates plus attributes confirm it. The realistic trigger is a
# parsing variance between agencies, not a genuinely different address,
# which would be a different civic and land in a different block.

# "Via Roma n. 12" and "Via Roma 12" normalise to the same key today, so
# the geo tier is exercised with coordinates and attributes carrying a
# pair whose civic ranges differ in suffix: same building, separate unit.
a1 = L(id="a", address_raw="Via Roma 12", lat=41.1171, lon=16.8719,
       area_sqm=85, rooms=3)
b1 = L(id="b", address_raw="Via Roma 12A", lat=41.11712, lon=16.8719,
       area_sqm=87, rooms=3)
r = compare(a1, b1)
check("same-building different-suffix reaches review", r.decision, Decision.CANDIDATE)
check("candidate is not auto-merged", r.merged, False)
check_true("candidate records distance", "distance_m" in r.evidence)

# geo merge: identical civic, identical coordinates band, compatible data
# but differing street spelling that still clears the street threshold.
c1 = L(id="a", address_raw="Via Roma 12", lat=41.1171, lon=16.8719,
       area_sqm=85, rooms=3)
d1 = L(id="b", address_raw="Via Roma 12 ", lat=41.11712, lon=16.8719,
       area_sqm=87, rooms=3, agency="Other")
r = compare(c1, d1)
check("whitespace variant merges exactly", r.decision, Decision.EXACT)

# geo must NOT merge when attributes conflict outright
e1 = L(id="a", address_raw="Via Roma 12", lat=41.1171, lon=16.8719,
       area_sqm=50, rooms=2)
f1 = L(id="b", address_raw="Via Roma 14", lat=41.11712, lon=16.8719,
       area_sqm=180, rooms=6)
r = compare(e1, f1)
check("conflicting attributes not merged", r.merged, False)

# geo must NOT merge when far apart
g1 = L(id="a", address_raw="Via Roma 12", lat=41.1171, lon=16.8719,
       area_sqm=85, rooms=3)
h1 = L(id="b", address_raw="Via Roma 14", lat=41.1300, lon=16.8719,
       area_sqm=85, rooms=3)
r = compare(g1, h1)
check("far apart not merged", r.merged, False)
check_true("far apart records distance", r.evidence.get("distance_m", 0) > 1000)

# Unknown attributes plus a civic conflict must not merge. Both sides have
# no area or rooms, so the only signals left are proximity and street
# name, which is precisely the case where two distinct units in one
# building look identical.
i1 = L(id="a", address_raw="Via Roma 12", lat=41.1171, lon=16.8719,
       area_sqm=None, rooms=None)
j1 = L(id="b", address_raw="Via Roma 14", lat=41.11712, lon=16.8719,
       area_sqm=None, rooms=None)
r = compare(i1, j1)
check("civic conflict is never auto-merged", r.merged, False)
check("civic conflict goes to review", r.decision, Decision.CANDIDATE)
check_true("civic conflict is recorded", r.evidence.get("civic_conflict") is True)

# fuzzy: same street near-miss name -> candidate, not merged
k1 = L(id="a", address_raw="Via Roma 12", area_sqm=85, rooms=3)
l1 = L(id="b", address_raw="Via Roma 13", area_sqm=86, rooms=3)
r = compare(k1, l1)
check("different civic goes to review", r.decision, Decision.CANDIDATE)
check("candidate is not auto-merged", r.merged, False)
check("candidate tier", r.tier, 3)
check_true("candidate has evidence", bool(r.evidence))

# truly different property on a similar street -> rejected
m1 = L(id="a", address_raw="Via Roma 12", area_sqm=85, rooms=3)
n1 = L(id="b", address_raw="Via Milano 12", area_sqm=86, rooms=3)
r = compare(m1, n1)
check("different street rejected", r.decision, Decision.REJECT)

# missing geo must not crash
r = compare(L(id="a"), L(id="b", address_raw="Roma 12"))
check("missing geo handled", r.decision, Decision.EXACT)

# self-comparison
r = compare(L(id="a"), L(id="a"))
check("self rejected", r.decision, Decision.REJECT)

# ---------------------------------------------------------------- blocks

listings = [
    L(id="a1", address_raw="Via Roma 12", agency="AgencyOne"),
    L(id="a2", address_raw="via  roma 12", agency="AgencyTwo"),
    L(id="a3", address_raw="Via Milano 5", agency="AgencyOne"),
    L(id="a4", address_raw="Via Roma 14", agency="AgencyThree"),
]
ded = Deduplicator()
results = ded.run(listings)
s = ded.summary(results)

check("blocked pairs found", s["pairs_compared"], 1)
check("one merge from blocking", s["merged"], 1)
check_true("summary counts by decision", "exact" in s["by_decision"])

blocks = ded.blocks(listings)
check("block key", "bari|12" in blocks, True)
check("block size", len(blocks["bari|12"]), 2)
check_true("separate civic forms its own block", "bari|14" in blocks)
check("no block without civic",
      any(all(l.address.civic == "5" for l in g) for g in blocks.values()), True)

# An oversized block must be reported, not silently compared or dropped.
# Many listings sharing one civic is the signature of a parsing bug.
many = [L(id=f"m{i}", address_raw="Via Roma 12") for i in range(60)]
res2 = Deduplicator().run(many)
check_true("oversized block flagged",
           any("block too large" in " ".join(x.reasons) for x in res2))

# ---------------------------------------------------------------- summary

empty = Deduplicator().run([])
check("no listings is safe", empty, [])
check("empty summary", Deduplicator().summary(empty)["pairs_compared"], 0)


# ------------------------------------------- regressions found in practice

# These four were found by running the comparison against five real
# agency variants of the same Bari address, not by reasoning. Each one
# merged distinct properties or split duplicates in a way the earlier
# checks did not cover.

# Giuseppe Verdi and "G. Verdi" are the same street. Plain string
# similarity scored them far apart because one name is three characters
# longer, which pushed a true duplicate out of every tier.
a1 = L(id="a", address_raw="Via Giuseppe Verdi 12", lat=41.1171,
       lon=16.8719, area_sqm=85, rooms=3)
b1 = L(id="b", address_raw="V. G. Verdi, 12", lat=41.11712,
       lon=16.8719, area_sqm=85, rooms=3)
check("initial matches full given name", compare(a1, b1).merged, True)

# A 300 sqm eight-room unit and an 85 sqm three-room unit share the civic
# number and sit metres apart. Merging them on proximity plus address
# alone removes a real property from the comparable set.
a2 = L(id="a", address_raw="Via Verdi 12", lat=41.11715, lon=16.87192,
       area_sqm=85, rooms=3)
b2 = L(id="b", address_raw="Verdi 12", lat=41.11715, lon=16.87192,
       area_sqm=300, rooms=8)
r = compare(a2, b2)
check("area conflict blocks exact merge", r.merged, False)
check("area conflict goes to review", r.decision, Decision.CANDIDATE)
check("contradiction recorded", r.evidence.get("contradictions"), 2)

# Civic 12 and 12A are different units in one building. Proximity and
# similar attributes are what you would expect between them.
a3 = L(id="a", address_raw="Via Roma 12", lat=41.1171, lon=16.8719,
       area_sqm=85, rooms=3)
b3 = L(id="b", address_raw="Via Roma 12A", lat=41.11712, lon=16.8719,
       area_sqm=85, rooms=3)
r = compare(a3, b3)
check("different civic never auto-merges", r.merged, False)
check("civic conflict flagged", r.evidence.get("civic_conflict"), True)

# Street type must not appear in the exact key, or "Roma 12" and
# "Via Roma 12" become different addresses.
check("missing street type still matches",
      L(id="a", address_raw="Roma 12").address.key,
      L(id="b", address_raw="Via Roma 12").address.key)

# "degli" is the same dropped preposition as "de".
check("degli dropped like de",
      normalise("Viale degli Aranci 3", "Bari").street_name, "aranci")


# ------------------------------------------------------------------ report

print()
if failures:
    print(f"FAILED {len(failures)}/{checks}")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print(f"PASSED all {checks} checks")