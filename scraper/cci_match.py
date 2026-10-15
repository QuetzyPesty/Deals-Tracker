"""
Attach CCI combination filings to the deals we hold.

The join is by NAME, because a news story and a filing share nothing else: no
common id, and the filing names no law firm. Names are the weakest possible
key, so this module is built to prefer a missed match over a wrong one (a wrong
filing on a deal tells a reader something false about it, a missing one tells
them nothing):

  * deal-type gate  - only acquisitions, mergers, JVs and stake purchases can
                      be notified to the CCI; an IPO, QIP or loan cannot.
  * name evidence   - shared multi-word NAME PHRASES between the deal's text
                      and the filing's parties, never loose single tokens.
                      HIGH needs two distinct matched entities (acquirer AND
                      target), or one full rare multi-word name.
  * date window     - the filing's notification date must sit near the date
                      the deal was reported. Undated deals need two entities.
  * ambiguity       - if two filings are equally good the deal gets neither;
                      the candidates are listed in the report for a person.

Nothing here changes a deal. A match is an annotation, and every decision with
its evidence is written to cci_report.json.
"""
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import dedup  # noqa: E402  (word lists, tokenisation, type classes)

# Calibrated on the real register; see tests/test_cci.py and STATE.md.
# Measured on 40 two-party name matches and 286 candidate pairs (STATE.md):
# true matches have the filing notified shortly BEFORE the deal is reported
# (news often runs at completion, after approval); outside this band, name hits
# are the same companies' unrelated filings from years earlier.
#
# Hand-labelled review of every candidate in -300..+120 days: all confirmed
# matches fall between about -125 and +35; the wrong ones (a common word
# shared with an unrelated party) sat at -165 to -240. Small sample, so the
# band is deliberately not wider than the evidence.
DAYS_BEFORE = 130   # filing may be notified up to this long before the deal was reported
DAYS_AFTER = 60     # ... or this long after
MIN_PARTY_COVERAGE = 0.66  # a shared phrase must cover this much of the filing party's own name
RARE_PHRASE_DF = 3  # a phrase in more filings than this is too common to identify anything
RARE_TOKEN_DF = 4
MIN_SINGLE_NAME_LEN = 6   # a lone word shorter than this is an English word more often than a company
MAX_NGRAM = 5

# What a CCI filing can be about. IPO/QIP/debt-only deals never are.
_NOTIFIABLE = re.compile(
    r"acqui|merg|amalgam|demerg|joint venture|\bjv\b|takeover|buyout|stake|"
    r"divest|carve-?out|invest|\bsale of\b|\bsells?\b|\bsold\b|purchas|scheme of arrangement"
)
_NOT_NOTIFIABLE_ONLY = re.compile(r"\bipos?\b|initial public offer|\bqips?\b|\bbonds?\b|debenture|\bncds?\b|\becb\b|\bloan\b")

_PARTY_NOISE = set("""
mumbai delhi new maharashtra gujarat karnataka haryana tamil nadu telangana kolkata chennai bengaluru
bangalore hyderabad pune gurugram gurgaon noida singapore mauritius japan germany france london
jointly acquirer target notice proposed given regard section combination pursuant respect
llc llp pte plc gmbh ltd limited private pvt inc corp corporation company co sa nv bv ag
""".split())


# Industry / structure words: they name a SECTOR, not a party. A filing party
# "Shreyas Shipping" sharing "shipping" with a headline about a "shipping pool"
# is not evidence the deal involves that party.
_SECTOR_WORDS = set("""
shipping logistics power energy steel cement cements pharma pharmaceutical pharmaceuticals
healthcare health hospital hospitals infra infrastructure telecom motors motor finance
financial insurance solar renewable renewables airlines airline textiles foods food media
software technologies technology systems solutions industries industry enterprises services
ventures resources minerals mining chemicals agro retail realty properties estate housing
transport transmission hospitality hotels hotel education digital networks petroleum oil gas
""".split())


def _words(text):
    return dedup._words(text)


def entity_runs(text, extra_stop=frozenset()):
    """Runs of consecutive identity words, split at stop/generic words.
    "Kotak Special Situations Fund, Gayatri Highways" -> [(kotak, special,
    situations), (gayatri, highways)]. The same function runs on deal text and
    on filing parties, so a name reduces to the same tuple on both sides."""
    runs, cur = [], []
    drop = dedup._STOP | dedup._GENERIC | dedup._CURRENCY_TOKENS | _PARTY_NOISE | set(extra_stop)
    for w in _words(text):
        if w in drop or w.isdigit() or len(w) < 2:
            if cur:
                runs.append(tuple(cur))
                cur = []
        else:
            cur.append(w)
    if cur:
        runs.append(tuple(cur))
    return runs


def _ngrams(runs, nmax=MAX_NGRAM):
    out = set()
    for run in runs:
        for n in range(1, min(len(run), nmax) + 1):
            for i in range(len(run) - n + 1):
                out.add(run[i:i + n])
    return out


class FilingIndex:
    def __init__(self, filings):
        self.filings = filings
        self.runs = [entity_runs(f.get("parties", "")) for f in filings]
        self.index = defaultdict(set)      # gram -> {(filing, run)}
        self.filing_df = defaultdict(set)  # gram -> {filing}
        self.token_df = Counter()
        for i, runs in enumerate(self.runs):
            for j, run in enumerate(runs):
                for g in _ngrams([run]):
                    self.index[g].add((i, j))
                    self.filing_df[g].add(i)
        for g, fs in self.filing_df.items():
            if len(g) == 1:
                self.token_df[g[0]] = len(fs)

    def df(self, gram):
        """Number of distinct FILINGS whose parties contain this phrase."""
        return len(self.filing_df.get(gram, ()))


def deal_text(deal):
    """Headline, client and the headlines other outlets used for the same deal:
    more spellings of the same names, which is what a name join wants."""
    bits = [deal.get("headline") or "", deal.get("client") or ""]
    bits += [o.get("headline") or "" for o in deal.get("also_reported_by") or []]
    return " . ".join(b for b in bits if b)


def deal_is_notifiable(deal):
    text = dedup._clean(deal_text(deal))
    if not _NOTIFIABLE.search(text):
        return False
    # "advises on IPO ... and acquisition of stake" is rare; an IPO/QIP/debt
    # headline with no acquisition-type word never reaches here because
    # _NOTIFIABLE already failed. If BOTH appear, trust the notifiable word.
    return True


def _date(v):
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def _firm_words(deal):
    words = set()
    for f in deal.get("law_firms") or []:
        words.update(_words(f))
    return words


def strong_entities(deal, idx):
    """Per candidate filing: the distinct filing PARTIES the deal's text names,
    each with the phrase that matched. Returns
    {filing_index: [(run_index, phrase), ...]} -- one entry per party, so two
    words of the same party name are one piece of evidence, not two.

    A phrase counts only when it covers most of the party's own name
    (MIN_PARTY_COVERAGE). Sharing one word of "Thriveni Earthmovers and Infra"
    with a story about "Thriveni Pellets" names a corporate group, not the
    party; sharing "Emirates NBD" with "Emirates NBD Bank" does."""
    runs = entity_runs(deal_text(deal), _firm_words(deal))
    dgrams = _ngrams(runs)
    hits = defaultdict(dict)  # filing -> run -> best (longest) phrase
    for g in dgrams:
        specific = [t for t in g if t not in _SECTOR_WORDS and idx.token_df[t] <= RARE_TOKEN_DF]
        if not specific:
            continue
        if len(g) == 1 and (len(g[0]) < MIN_SINGLE_NAME_LEN or idx.df(g) > RARE_PHRASE_DF):
            continue
        if len(g) >= 2 and idx.df(g) > RARE_PHRASE_DF * 3:
            continue
        shared_names = [t for t in g if t not in _SECTOR_WORDS]
        for fi, rj in idx.index.get(g, ()):
            # coverage is measured on NAME words: "Jindal Power" covers all of
            # "Jindal Power Limited"; "Green Power" covers half of "AMG Green
            # Power" because "power" is a sector word, not part of the name.
            party_names = [t for t in idx.runs[fi][rj] if t not in _SECTOR_WORDS] or list(idx.runs[fi][rj])
            if len(shared_names) / len(party_names) < MIN_PARTY_COVERAGE:
                continue
            cur = hits[fi].get(rj)
            if cur is None or len(g) > len(cur):
                hits[fi][rj] = g
    return {fi: sorted(runs_.items()) for fi, runs_ in hits.items()}


def _contains(big, small):
    n = len(small)
    return any(big[i:i + n] == small for i in range(len(big) - n + 1))


def match_deal(deal, idx):
    """-> (match or None, info). `info` carries the reason either way."""
    if not deal_is_notifiable(deal):
        return None, {"reason": "type-gate"}
    cands = strong_entities(deal, idx)
    if not cands:
        return None, {"reason": "no-name-evidence"}
    reported = _date(deal.get("published_at"))
    scored = []
    for fi, ents in cands.items():
        f = idx.filings[fi]
        notified = _date(f.get("notified"))
        phrases = [g for _, g in ents]
        n_ent = len(ents)
        gap = None
        if reported and notified:
            gap = (notified - reported).days
            if gap < -DAYS_BEFORE or gap > DAYS_AFTER:
                continue
        elif n_ent < 2:
            # undated on either side: one named party is not enough
            continue
        scored.append((n_ent, -(abs(gap) if gap is not None else 10**6), fi, ents, gap))
    if not scored:
        return None, {"reason": "evidence-below-threshold", "candidates": len(cands)}
    scored.sort(reverse=True)
    top = scored[0]
    # Two filings equally good for one deal: attach neither. There is no
    # "nearest in time wins" tie-break; Cube Highways alone has three filings
    # in one year and nearness would be a coin flip presented as a fact.
    if len(scored) > 1 and scored[1][0] == top[0]:
        return None, {
            "reason": "ambiguous",
            "candidates": [idx.filings[sc[2]]["combination_no"] for sc in scored[:4]],
        }
    n_ent, _, fi, ents, gap = top
    phrases = [g for _, g in ents]
    f = idx.filings[fi]
    return {
        "combination_no": f["combination_no"],
        "status": f.get("status"),
        "notified": f.get("notified"),
        "decided": f.get("decided"),
        "summary_url": f.get("summary_url"),
        "order_url": f.get("order_url"),
        "parties": f.get("parties"),
        "confidence": "high",
        "entities": [" ".join(g) for g in phrases],
        "gap_days": gap,
    }, {"reason": "matched"}


def attach(deals, filings):
    """Annotate deals in place with `cci`. Returns a report dict."""
    idx = FilingIndex(filings)
    report = {"filings": len(filings), "deals": len(deals), "matched": [], "ambiguous": [],
              "reasons": Counter()}
    for d in deals:
        m, info = match_deal(d, idx)
        report["reasons"][info["reason"]] += 1
        if m:
            d["cci"] = m
            report["matched"].append({
                "headline": d.get("headline"), "url": d.get("url"),
                "reported": d.get("published_at"), **m})
        elif info["reason"] == "ambiguous":
            report["ambiguous"].append({"headline": d.get("headline"), "url": d.get("url"),
                                        "candidates": info["candidates"]})
    report["reasons"] = dict(report["reasons"])
    # One filing matched by two DIFFERENT deals means the names fit more than
    # one transaction (SMBC buying into YES Bank and, separately, selling
    # Kotak shares). A name cannot say which; attach it to neither.
    per_filing = Counter(m["combination_no"] for m in report["matched"])
    shared = {k for k, v in per_filing.items() if v > 1}
    report["filing_claimed_by_several_deals"] = {}
    if shared:
        keep = []
        for m in report["matched"]:
            if m["combination_no"] in shared:
                report["filing_claimed_by_several_deals"].setdefault(m["combination_no"], []).append(m["headline"])
            else:
                keep.append(m)
        report["matched"] = keep
        for d in deals:
            if d.get("cci", {}).get("combination_no") in shared:
                del d["cci"]
    return report
