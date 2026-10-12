"""
Cross-source deal de-duplication.

The same deal reaches us several times: Bar & Bench, Legal Era and LiveLaw
all re-publish the same law-firm press release, and the legacy export
overlaps the live scrape. build_directory.py keys deals on URL, so without a
content-level stage every one of those would be counted as a separate deal and
every lawyer credited once per outlet.

Design (and why):

  * Per-source files stay raw. Merging happens at build time, over every
    source at once, so the result does not depend on scrape order and a
    better rule can be applied to history by simply rebuilding.
  * Two kinds of evidence, never mixed up.
      HARD   - the article IS the same article: same URL, same host+slug, same
               headline, identical opening text. Merged on that alone.
      SOFT   - two different articles about one deal. Merged only when firm
               overlap, headline entities or copied press-release text agree
               AND none of the veto checks fire.
  * Vetoes beat evidence. A conflicting deal value, publication dates far
    apart, or a named entity that appears on one side only (a different
    target company, a different funding round) stops a soft merge no matter
    how similar the rest looks. A false merge silently deletes a real deal,
    which is worse than showing one deal twice.
  * Complete linkage. A candidate joins a cluster only if it matches EVERY
    member, so A~B and B~C can never chain A and C together.
  * Everything is explainable: each merge records the rule that fired and the
    measured similarities, written to dedup_report.json; near-misses are
    listed there too so a person can audit the boundary.
"""
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).parent))
from extraction import (  # noqa: E402
    BARE_INR_RE, MONEY_RE, canonical_firm, known_firms_in_text, normalize_name,
    parse_money,
)

# ---- tunables (calibrated on the real corpus; see tests/test_dedup.py) ------
LEDE_COPY_SIM = 0.55          # word-3-gram Jaccard of opening text => copied press release
HEADLINE_SIM = 0.75           # overlap coefficient of significant headline tokens
HEADLINE_SIM_WITH_PEOPLE = 0.5
PEOPLE_SHARED_MIN = 3
NESTED_SAME_COUNSEL_MIN = 3   # shared firm, one headline nested in the other, same deal type
NESTED_ENTITIES_MIN = 5       # no shared firm, one headline nested in the other
PEOPLE_JACCARD_MIN = 0.6
VALUE_TOLERANCE = 0.15        # same deal quoted in INR vs USD at different rates
DATE_WINDOW_DAYS = 14
HEADLINE_DATE_WINDOW_DAYS = 45  # identical headline, republished later

SOURCE_BY_HOST = {
    "barandbench.com": "Bar & Bench",
    "legaleraonline.com": "Legal Era",
    "livelaw.in": "LiveLaw",
}
# lower rank number wins when two records are otherwise equally rich
SOURCE_RANK = {"Bar & Bench": 0, "Legal Era": 1, "LiveLaw": 2}

_STOP = set("""
a an and are as at be been by for from has have had in into is it its of on
or that the their this to was were will with after amid over under via
advised advises advise advising advice acted acts act acting acting
represented represents represent representing assisted assists assist
counsel legal lead led team firm firms law laws llp llc ltd limited pvt
private pte inc incorporated corp corporation co company plc partners
partner associates associate advocates solicitors
also along alongside together jointly both said
availing avail availed securing secure secured providing provides provided
sanctioning sanctioned obtaining obtain obtained setting up
india indian indias
""".split())
# deal-shape words that carry no identity: two different deals share them
_GENERIC = set("""
acquisition acquire acquires acquired acquiring acquisitions stake stakes
investment invest invests invested investing investor investors transaction
transactions deal deals funding fund funds fundraise fundraising raise raises
raised raising round rounds financing finance financed loan loans facility
facilities merger merge merges merged amalgamation demerger divestment divest
sale sell sells sold buy buys buyout purchase issue issuance offer offering
public initial ipo qip series seed pre bridge equity debt capital group
holdings holding ventures venture business businesses unit units platform
enter enters entered into minority majority shareholding shares share
proposed proposal strategic crore crores cr lakh lakhs million millions mn
billion billions bn inr usd rs eur gbp us dollar dollars rupees
""".split())
_CURRENCY_TOKENS = {"inr", "usd", "rs", "eur", "gbp", "us"}
# Things that tell two otherwise-identical headlines apart: a funding round,
# a fund number, a capacity. A headline that carries one the other lacks, in
# both directions, is a different deal.
# Coarse deal-type classes. Two headlines about the same issuer that name
# DIFFERENT classes (a QIP and a bond issue, an IPO and an acquisition) are
# different deals however many name words they share.
_TYPE_CLASSES = {
    "ipo": re.compile(r"\bipos?\b|initial public offer|\bdrhp\b|\blisting\b|\blisted\b"),
    "qip": re.compile(r"\bqips?\b|qualified institutional|rights issue"),
    "debt": re.compile(r"\bbonds?\b|debentures?|\bncds?\b|\bnotes?\b|\bloans?\b|\becb\b|credit|facilit|refinanc|lend|borrow|sanction"),
    # acquisition, stake sale, investment and fund-raising are one family: the
    # same transaction is described as "stake acquisition" by one outlet and
    # "to invest in" by another
    "equity": re.compile(
        r"acqui|buyout|takeover|purchas|divest|\bsale\b|\bsell|\bsold\b|carve-?out|stake|"
        r"invest|fund-?rais|funding|\bseries [a-z]\b|\bround\b|\braise|subscri|infusion|"
        r"preferential|warrants?"
    ),
}
_MARK_WORDS_RE = re.compile(
    r"\b(series|round|tranche|phase|class|fund|tier|package|batch)\s+([a-z]|[ivx]{1,4}|\d+)\b"
)
_NUMBER_RE = re.compile(r"(?<![a-z0-9.])\d+(?:\.\d+)?(?![a-z0-9])")
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_PUNCT_DASH = re.compile(r"[‐-―]")


def _clean(text):
    text = (text or "").lower().replace("&", " and ").replace("’", "'")
    text = _PUNCT_DASH.sub("-", text)
    return re.sub(r"'s\b", "", text)


def _words(text):
    return _TOKEN_RE.findall(_clean(text))


# ---- small helpers ---------------------------------------------------------
def normalize_url(url):
    p = urlsplit((url or "").strip())
    host = p.netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    return f"{host}{p.path.rstrip('/').lower()}"


def url_host(url):
    host = urlsplit((url or "").strip()).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def url_slug(url):
    return urlsplit((url or "").strip()).path.rstrip("/").rsplit("/", 1)[-1].lower()


def source_label(record):
    """One spelling per outlet. The legacy export wrote both "Bar and Bench"
    and "Bar & Bench", and left 21 blank."""
    host = url_host(record.get("url"))
    for h, label in SOURCE_BY_HOST.items():
        if host == h or host.endswith("." + h):
            return label
    label = (record.get("source") or "").strip()
    if label.lower() in ("bar and bench", "bar & bench", "barandbench"):
        return "Bar & Bench"
    return label


def _shingles(text, n=3):
    w = [x for x in _words(text)]
    if len(w) < n:
        return {tuple(w)} if w else set()
    return {tuple(w[i:i + n]) for i in range(len(w) - n + 1)}


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _overlap(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _parse_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _lede(record):
    """Opening text used for copy detection: the snippet (first ~500 chars of
    body) with the headline's own words not required."""
    return record.get("snippet") or ""


# ---- per-record features ---------------------------------------------------
class Features:
    __slots__ = (
        "rec", "idx", "url", "host", "slug", "firms", "firms_soft", "firm_words", "head_norm",
        "tokens", "rare", "core", "marks", "types", "lede_norm", "lede_shingles", "people",
        "value_inr", "date", "personnel", "label", "n_people",
    )


def _firm_words(firms):
    out = set()
    for f in firms:
        out.update(_words(f))
    return out


def _marks(headline):
    """Identity marks: round/fund labels and plain numbers that are not money
    (money is compared separately, and "INR 30 billion" vs "₹3,000 crore" must
    not look like a mismatch)."""
    text = MONEY_RE.sub(" ", headline or "")
    text = BARE_INR_RE.sub(" ", text)
    c = _clean(text)
    marks = {f"{k}-{v}" for k, v in _MARK_WORDS_RE.findall(c)}
    marks.update(_NUMBER_RE.findall(c.replace(",", "")))
    return marks


def build_features(records, is_personnel_move):
    feats = []
    for i, r in enumerate(records):
        f = Features()
        f.rec, f.idx = r, i
        f.url = normalize_url(r.get("url"))
        f.host = url_host(r.get("url"))
        f.slug = url_slug(r.get("url"))
        firms = set()
        for name in r.get("law_firms") or []:
            c = canonical_firm(name)
            if c:
                firms.add(c)
        f.firms = firms
        # firms named in the opening text too. Used only to MATCH records:
        # an article whose own firm list came out empty ("28MAEI, EY, J Sagar
        # Associates and Solicis Lex advised ...") still has to be comparable.
        f.firms_soft = firms | set(known_firms_in_text(r.get("headline") or "")) | set(
            known_firms_in_text((r.get("snippet") or "")[:500]))
        f.firm_words = _firm_words(firms)
        headline = r.get("headline") or ""
        f.head_norm = " ".join(_words(headline))
        f.lede_norm = " ".join(_words(_lede(r)))[:500]
        f.lede_shingles = _shingles(_lede(r))
        f.people = {
            normalize_name(p["name"]).lower()
            for p in (r.get("people") or []) if p.get("name")
        }
        f.n_people = len(r.get("people") or [])
        value = r.get("deal_value")
        inr = value.get("amount_inr") if isinstance(value, dict) else None
        if isinstance(value, dict) and value.get("source") == "body":
            # a figure lifted from the body is often one tranche of the deal
            # (the fresh-issue part of an IPO, one lender's share), so it can
            # disagree with the headline total without being a different deal
            inr = None
        if not inr:
            hit = parse_money(headline)
            inr = hit["amount_inr"] if hit else None
        f.value_inr = inr
        f.date = _parse_date(r.get("published_at"))
        f.personnel = bool(is_personnel_move(headline) and not r.get("client"))
        f.label = source_label(r)
        f.marks = _marks(headline)
        f.types = {k for k, rx in _TYPE_CLASSES.items() if rx.search(_clean(headline))}
        f.tokens = None  # filled once corpus frequencies are known
        f.rare = None
        f.core = None
        feats.append(f)
    return feats


def _assign_tokens(feats):
    """Significant headline tokens. A token is "rare" (identity-bearing) unless
    it is a stop word, a generic deal word, part of a firm name, or so common
    across the corpus that it cannot identify anything."""
    df = Counter()
    for f in feats:
        df.update(set(_words(f.rec.get("headline"))))
    n = max(len(feats), 1)
    common = {w for w, c in df.items() if n >= 200 and c / n >= 0.04}
    for f in feats:
        toks = set()
        for w in _words(f.rec.get("headline")):
            if w in _STOP or w in f.firm_words or w.isdigit() or w in _CURRENCY_TOKENS:
                continue
            toks.add(w)
        f.tokens = toks
        f.rare = {w for w in toks if w not in _GENERIC and w not in common and len(w) > 1}
        # ordered significant words: equal cores = same sentence once verb
        # tense, filler and firm names are removed ("advises" vs "Advised")
        f.core = tuple(
            w for w in _words(f.rec.get("headline"))
            if w not in _STOP and w not in f.firm_words
            and not w.isdigit() and w not in _CURRENCY_TOKENS
        )


# ---- pair evaluation -------------------------------------------------------
def _value_conflict(a, b):
    if not a.value_inr or not b.value_inr:
        return False
    hi, lo = max(a.value_inr, b.value_inr), min(a.value_inr, b.value_inr)
    return (hi - lo) / hi > VALUE_TOLERANCE


def _date_gap(a, b):
    if not a.date or not b.date:
        return None
    return abs((a.date - b.date).days)


def _firm_compatible(a, b):
    """Hard rules tolerate a missing firm list (a headline-only legacy row);
    they never tolerate two non-empty lists that share nothing."""
    if not a.firms or not b.firms:
        return True
    return bool(a.firms & b.firms)


def compare(a, b):
    """Returns (rule or None, details). rule is a short stable string."""
    detail = {}
    if a.url and a.url == b.url:
        return "same-url", detail
    if a.host == b.host and a.slug and a.slug == b.slug:
        return "same-slug", detail

    gap = _date_gap(a, b)
    detail["date_gap_days"] = gap
    if _value_conflict(a, b):
        detail["veto"] = "value-conflict"
        return None, detail

    if a.marks and b.marks and (a.marks - b.marks) and (b.marks - a.marks):
        detail["veto"] = "mark-mismatch"
        detail["marks"] = [sorted(a.marks), sorted(b.marks)]
        return None, detail

    types_disjoint = bool(a.types and b.types and not (a.types & b.types))

    # --- HARD: same article text --------------------------------------------
    if a.head_norm and a.head_norm == b.head_norm and _firm_compatible(a, b):
        if gap is None or gap <= HEADLINE_DATE_WINDOW_DAYS:
            return "same-headline", detail
    if (a.core and a.core == b.core and a.rare & b.rare and a.firms and b.firms
            and a.firms & b.firms and (gap is None or gap <= DATE_WINDOW_DAYS)):
        return "same-core-headline", detail
    if (a.lede_norm and a.lede_norm == b.lede_norm and len(a.lede_norm) >= 80
            and _firm_compatible(a, b)):
        return "same-lede", detail

    if a.personnel or b.personnel:
        return None, detail

    # --- SOFT: different articles, same deal --------------------------------
    if types_disjoint:
        detail["veto"] = "type-conflict"
        detail["types"] = [sorted(a.types), sorted(b.types)]
        return None, detail
    shared_firms = a.firms_soft & b.firms_soft
    if gap is not None and gap > DATE_WINDOW_DAYS:
        detail["veto"] = "date-gap"
        return None, detail

    shared_rare = a.rare & b.rare
    a_only, b_only = a.rare - b.rare, b.rare - a.rare
    head_sim = _overlap(a.tokens, b.tokens)
    lede_sim = _jaccard(a.lede_shingles, b.lede_shingles)
    shared_people = a.people & b.people
    people_jacc = _jaccard(a.people, b.people)
    detail.update(
        head_sim=round(head_sim, 2), lede_sim=round(lede_sim, 2),
        shared_rare=sorted(shared_rare), a_only=sorted(a_only), b_only=sorted(b_only),
        shared_people=len(shared_people), people_jaccard=round(people_jacc, 2),
    )

    # a name that appears on one side only is a different target / client /
    # round unless the other side simply abbreviates or expands it. Allow a
    # single leftover token per side (an abbreviation or spelling variant),
    # never a pair of full names.
    entity_clash = len(a_only) > 1 and len(b_only) > 1
    # fully nested: one headline's identity tokens are all inside the other's
    nested = bool(shared_rare) and (not a_only or not b_only)

    if entity_clash:
        detail["veto"] = "entity-clash"
        return None, detail

    if not shared_firms:
        # The two sides of one deal are often reported separately by their
        # own counsel (JSA for one JV partner, Khaitan for the other). With no
        # firm in common the headlines must name EXACTLY the same entities.
        if (a.tokens and a.tokens == b.tokens and len(a.tokens) >= 3
                and len(shared_rare) >= 1):
            return "same-entities-different-counsel", detail
        # One headline is the other plus extra description, and they share five
        # or more identity words (Samara Capital / Associated Road Carriers /
        # Calyx Container Terminals). Five specific names do not coincide.
        if nested and len(shared_rare) >= NESTED_ENTITIES_MIN:
            return "nested-entities", detail
        return None, detail

    if lede_sim >= LEDE_COPY_SIM and shared_rare:
        return "copied-text", detail
    # One headline's identity words are all inside the other's, they share
    # at least three, and both name the same kind of deal. Generic words
    # ("proposed", "group") dilute head_sim, so it cannot be the gate here.
    if (nested and len(shared_rare) >= NESTED_SAME_COUNSEL_MIN and a.types & b.types):
        return "nested-same-type", detail
    if (head_sim >= HEADLINE_SIM and len(shared_rare) >= 2
            and (nested or (len(a_only) <= 1 and len(b_only) <= 1))):
        return "headline-entities", detail
    if (len(shared_people) >= PEOPLE_SHARED_MIN and people_jacc >= PEOPLE_JACCARD_MIN
            and head_sim >= HEADLINE_SIM_WITH_PEOPLE and shared_rare):
        return "same-team-and-entities", detail
    return None, detail


# ---- clustering ------------------------------------------------------------
def cluster(feats):
    """Greedy clustering in a deterministic order. Returns clusters, the rule
    log and the near-miss list.

    A candidate joins a cluster when it matches at least one member AND no
    other member contradicts it. A member contradicts it when the pair is
    vetoed (value, date, entity, round) or when the two are by the SAME counsel
    yet failed every rule. Two articles by DIFFERENT counsel that merely do not
    match each other are tolerated: when one deal is covered once by the buyer's
    lawyers and once by the seller's, the only thing connecting those articles
    is the record that lists both firms.
    """
    order = sorted(
        feats,
        key=lambda f: (SOURCE_RANK.get(f.label, 9), f.url, f.idx),
    )
    clusters = []
    log = []
    near = []
    for f in order:
        placed = False
        for c in clusters:
            matches = []
            ok = True
            for m in c:
                rule, detail = compare(f, m)
                if rule is not None:
                    matches.append((rule, m, detail))
                    continue
                same_counsel = bool(f.firms and m.firms and f.firms & m.firms)
                veto = detail.get("veto")
                # An entity-clash is a guess from headline wording ("CPPIB" vs
                # "CPP Investments" looks like two investors). Between two
                # articles by different counsel it is not allowed to veto a
                # link that another member already established; value, date,
                # round and deal-type vetoes are facts and always do.
                if veto and (veto != "entity-clash" or same_counsel):
                    ok = False
                    if detail.get("head_sim", 0) >= 0.6 or detail.get("lede_sim", 0) >= 0.4:
                        near.append((f, m, detail))
                    break
                if same_counsel:
                    ok = False  # same counsel, no rule fired: not provably one deal
                    break
            if ok and matches:
                c.append(f)
                for rule, m, detail in matches:
                    log.append((f, m, rule, detail))
                placed = True
                break
        if not placed:
            clusters.append([f])
    return clusters, log, near


# ---- merging ---------------------------------------------------------------
def _richness(f):
    has_value = 1 if f.value_inr else 0
    return (-f.n_people, -has_value, -len(f.firms), SOURCE_RANK.get(f.label, 9), f.url)


def merge_cluster(members, best_role=None):
    members = sorted(members, key=_richness)
    primary = dict(members[0].rec)
    primary["source"] = members[0].label or primary.get("source", "")
    if len(members) == 1:
        return primary

    firms = []
    for m in members:
        for name in m.rec.get("law_firms") or []:
            c = canonical_firm(name)
            if c and c not in firms:
                firms.append(c)
    primary["law_firms"] = firms

    types = []
    for m in members:
        for t in m.rec.get("transaction_types") or []:
            if t not in types:
                types.append(t)
    primary["transaction_types"] = types

    if not primary.get("client"):
        for m in members[1:]:
            if m.rec.get("client"):
                primary["client"] = m.rec["client"]
                break

    if not primary.get("deal_value"):
        values = [m.rec["deal_value"] for m in members if m.rec.get("deal_value")]
        values.sort(key=lambda v: 0 if v.get("source") == "headline" else 1)
        if values:
            primary["deal_value"] = values[0]

    # people: one entry per person. If any source knows the person's firm, an
    # unattributed copy from another source is dropped rather than kept as a
    # second, firmless row.
    by_name = {}
    order = []
    for m in members:
        for p in m.rec.get("people") or []:
            if not p.get("name"):
                continue
            key = normalize_name(p["name"]).lower()
            cur = by_name.get(key)
            if cur is None:
                by_name[key] = dict(p)
                order.append(key)
                continue
            if not cur.get("firm") and p.get("firm"):
                cur["firm"] = p["firm"]
            if p.get("role"):
                if not cur.get("role"):
                    cur["role"] = p["role"]
                elif best_role and cur["role"] != p["role"]:
                    cur["role"] = best_role(cur["role"], p["role"])
    primary["people"] = [by_name[k] for k in order]

    dates = [m.date for m in members if m.date]
    if dates:
        primary["published_at"] = min(dates).isoformat()
    primary["also_reported_by"] = [
        {"source": m.label, "url": m.rec.get("url"),
         "headline": m.rec.get("headline")}
        for m in members[1:]
    ]
    return primary


def dedupe(records, is_personnel_move, best_role=None):
    """Returns (merged_records, report). Input order does not matter; output is
    sorted by the primary's source rank then URL so builds are reproducible."""
    feats = build_features(records, is_personnel_move)
    _assign_tokens(feats)
    clusters, log, near = cluster(feats)

    merged = [merge_cluster(c, best_role) for c in clusters]
    merged.sort(key=lambda r: (SOURCE_RANK.get(source_label(r), 9), normalize_url(r.get("url"))))

    by_rule = Counter(rule for _, _, rule, _ in log)
    rules_for = {}
    for f_, m_, rule_, _ in log:
        rules_for.setdefault(f_.rec.get("url"), []).append(rule_)
    report = {
        "input_records": len(records),
        "output_records": len(merged),
        "removed_duplicates": len(records) - len(merged),
        "merges_by_rule": dict(by_rule),
        "merged_groups": [
            {
                "kept": {"source": c[0].label, "url": c[0].rec.get("url"),
                         "headline": c[0].rec.get("headline")},
                "duplicates": [
                    {"source": m.label, "url": m.rec.get("url"),
                     "headline": m.rec.get("headline"),
                     "rules": sorted(set(rules_for.get(m.rec.get("url"), [])
                                         + [r for f_, m_, r, _ in log
                                            if m_.rec.get("url") == m.rec.get("url")]))}
                    for m in c[1:]
                ],
            }
            for c in (sorted(cl, key=_richness) for cl in clusters) if len(c) > 1
        ],
        "near_misses": [
            {"a": a.rec.get("headline"), "a_url": a.rec.get("url"),
             "b": b.rec.get("headline"), "b_url": b.rec.get("url"), **d}
            for a, b, d in near
        ],
    }
    return merged, report
