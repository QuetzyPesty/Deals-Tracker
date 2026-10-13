"""
Source-agnostic extraction engine: firm attribution, "Name (Role)" people,
transaction types and deal size.

Moved verbatim out of scrape_barandbench.py so that every source scraper
(Bar & Bench, Legal Era, LiveLaw) shares one implementation. The rules and
the guards around them (a wrong firm is worse than a missing one) are
unchanged; see the comments on each function for why they exist.
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(Path(__file__).parent))
from build_directory import (  # noqa: E402
    FIRM_ALIASES,
    KNOWN_FIRMS,
    canonical_firm,
    is_personnel_move,
    normalize_name,
)
import ner_supplement  # noqa: E402


# Longest/most-specific phrases first, since regex alternation is
# first-match, not longest-match -- "Associate Partner" must be tried
# before "Partner" and "Associate" or it'd get chopped to just "Partner".
ROLE_WORDS = [
    "Joint Managing Partner",
    "Managing Partner",
    "Senior Partner",
    "Associate Partner",
    "Principal Associate",
    "Senior Associate",
    "Of Counsel",
    "Counsel",
    "Partner",
    "Associate",
]

ROLE_ALTERNATION = "|".join(re.escape(r) for r in ROLE_WORDS)
# Indian/international names commonly include bare middle initials
# ("Ujwala K Adikey", "Aditya J Nair") and lowercase particles ("Chad de
# Souza") -- both must be allowed as inner words, or the whole match breaks
# at that word and only the trailing surname gets captured.
_NAME_FIRST_WORD = r"[A-Z][A-Za-z’'-]*"
_NAME_INNER_WORD = r"(?:[A-Z][A-Za-z’'-]*|de|van|bin|al|la|von|der|del)"
# require >=2 name words -- a single capitalized word before "(Role)" is
# almost always a stray leftover from a nickname-in-parens (see
# _strip_nickname_parens), not a full name.
NAME_PATTERN = rf"{_NAME_FIRST_WORD}(?:\s+{_NAME_INNER_WORD}){{1,4}}"

# Primary, high-confidence extractor: "Name (Role)" or "Name (Role, extra
# title text)" -- the extra-title suffix (e.g. "Partner, Regional Co-Head –
# Capital Markets – West") is discarded, we only keep the matched role word.
PAREN_ROLE_RE = re.compile(
    rf"({NAME_PATTERN})\s*\(({ROLE_ALTERNATION})(?:(?:[,;]|\s+and\s)[^)]*)?\)"
)

# Mid-name nicknames like "Kyungwon (Won) Lee" break paren-role matching --
# strip any single-word parenthetical that isn't one of our role words
# before running extraction.
_NICKNAME_PAREN_RE = re.compile(r"\s\(([A-Za-z]+)\)")


def _strip_nickname_parens(text):
    return _NICKNAME_PAREN_RE.sub(
        lambda m: m.group(0) if m.group(1) in ROLE_WORDS else "", text
    )

# Plural role markers used when several names share one un-parenthesized
# role mention, e.g. "Associates Archit Jain, Arikta Shetty ... and Sajal
# Soni." Matched via plain word scanning below, not a single regex, since
# the name-list length is unbounded and regex would get unreadable fast.
PLURAL_ROLE_MARKERS = {
    "Senior Partners": "Senior Partner",
    "Associate Partners": "Associate Partner",
    "Principal Associates": "Principal Associate",
    "Senior Associates": "Senior Associate",
    "Counsels": "Counsel",
    "Associates": "Associate",
    "Partners": "Partner",
}
# longest marker (by word count) checked first at each position
PLURAL_MARKERS_BY_LENGTH = sorted(
    PLURAL_ROLE_MARKERS, key=lambda k: -len(k.split())
)

# Firm-attribution sentence: "<Firm> advised <client> on ..." /
# "<Firm> acted as ..." / "<Firm> represented ...". Kept deliberately
# simple -- one line, literal keywords -- since it only needs to catch the
# firm name at the *start* of a sentence, which is Bar & Bench's house style.
FIRM_CONTEXT_RE = re.compile(
    r"^([A-Z][\w &.,’'-]*?)\s+"
    r"(advised|advises|is advising|represented|represents|is representing|"
    r"acted for|acted as|acts for|acts as)\b"
)

# Headline verb pattern: "<firms> <verb> <client> <deal desc>"
HEADLINE_VERB = re.compile(
    r"^(?P<firms>.+?)\s+(?:act(?:s)? on|advises|advise|assists|assist|represents|represent)\s+(?P<rest>.+)$",
    re.I,
)

TXN_KEYWORDS = (
    "IPO", "QIP", "Series A", "Series B", "Series C", "Series D", "Series E",
    "acquisition", "stake acquisition", "merger", "amalgamation", "demerger",
    "buyout", "divestment", "financing", "joint venture", "fundraise",
    "investment", "restructuring",
)


def split_firm_list(raw):
    parts = re.split(r",\s*|\s+and\s+", raw.strip())
    firms = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        cname = canonical_firm(p)
        if cname:
            firms.append(cname)
    return firms


def parse_headline(headline):
    m = HEADLINE_VERB.match(headline)
    if not m:
        return {"law_firms": [], "client": None, "transaction_types": []}
    firms_raw = m.group("firms")
    # Some headlines carry a subtitle before the firm list, e.g. "LIC Stake
    # Sale: Dentons Link Legal, Trilegal ..." -- drop everything up to and
    # including the colon or the subtitle leaks in as a fake "firm". BUT at
    # least one real firm name itself contains a colon ("Samvād: Partners"),
    # so only strip when a comma or " and " follows the colon -- that's the
    # subtitle-then-firm-LIST pattern; a single firm name with an internal
    # colon has neither.
    if ":" in firms_raw:
        before, after = firms_raw.rsplit(":", 1)
        if "," in after or " and " in after.lower():
            firms_raw = after.strip()
    firms = split_firm_list(firms_raw)
    rest = m.group("rest")
    client = rest.split(" on ")[0].strip() if " on " in rest else rest.strip()
    # trim trailing deal-size/description noise: "Arboreal ₹230 crore Series
    # A fundraise" -> "Arboreal" (cut at first currency symbol or digit)
    client = re.split(r"[₹$]|\s+\d", client)[0].strip()
    txn_types = [k for k in TXN_KEYWORDS if k.lower() in headline.lower()]
    return {"law_firms": firms, "client": client, "transaction_types": txn_types}


# --- headline shapes used by Legal Era / LiveLaw -------------------------------
# Bar & Bench writes "Khaitan advises Foo on bar". The other outlets write
# Title Case past tense ("Khaitan & Co Advised Foo On Bar"), sometimes with the
# firm as a trailing clause ("A, B Enter Merger Framework; Khaitan & Co Advises
# on Transaction"), and sometimes omit the client ("Khaitan Advised on X's
# divestment"). parse_headline() is left alone so Bar & Bench output cannot
# change; this is the looser sibling.
LOOSE_VERB = (
    r"(?:acted\s+(?:on|for|as)|acts\s+(?:on|for|as)|act\s+on|advis(?:es|ed|e|ing)|"
    r"assist(?:s|ed|ing)?|represent(?:s|ed|ing)?|counsel(?:led|s)?\s+to)"
)
HEADLINE_LOOSE = re.compile(
    rf"^(?P<firms>.+?)\s+{LOOSE_VERB}\s+(?P<rest>.+)$", re.I
)
TRAILING_FIRM_CLAUSE = re.compile(
    rf";\s*(?P<firms>[^;]+?)\s+{LOOSE_VERB}\b", re.I
)
_CLIENT_CUT = re.compile(r"\s+(?:on|in|for|to|through|by|from|of|with|over)\s+|[’']s\s+", re.I)


def _split_firms_loose(raw):
    parts = re.split(r",\s*|\s+and\s+", raw.strip(), flags=re.I)
    out = []
    for p in parts:
        c = canonical_firm(p.strip())
        if c and c not in out:
            out.append(c)
    return out


def parse_headline_loose(headline):
    """Same return shape as parse_headline()."""
    h = " ".join((headline or "").split())
    txn = [k for k in TXN_KEYWORDS if k.lower() in h.lower()]
    m = HEADLINE_LOOSE.match(h)
    if m and ";" not in m.group("firms"):
        firms_raw, rest = m.group("firms"), m.group("rest")
        if ":" in firms_raw:
            before, after = firms_raw.rsplit(":", 1)
            if "," in after or " and " in after.lower():
                firms_raw = after.strip()
        rest = re.sub(r"^(?:on|for|in|as)\s+", "", rest, flags=re.I)
        client = _CLIENT_CUT.split(rest, maxsplit=1)[0]
        client = re.split(r"[₹$]|\s+\d|\s+(?:INR|USD|Rs\.?|EUR|GBP)\b", client)[0].strip(" ,;:-")
        return {"law_firms": _split_firms_loose(firms_raw), "client": client or None,
                "transaction_types": txn}
    t = TRAILING_FIRM_CLAUSE.search(h)
    if t:
        return {"law_firms": _split_firms_loose(t.group("firms")), "client": None,
                "transaction_types": txn}
    return {"law_firms": [], "client": None, "transaction_types": txn}


# Personnel-move headlines don't have a "<firms> act on/advise <client>"
# shape, so parse_headline()'s HEADLINE_VERB never matches them and there's
# no headline-declared firm list to snap body-text mentions onto. These
# patterns pull name/firm/role straight from the headline instead, since
# Bar & Bench's headlines for this category are themselves quite
# structured. Best-effort by design -- a personnel-move headline that
# doesn't match any of these still gets correctly excluded from the deal
# count (via is_personnel_move in build_directory.py); it just won't have
# a firm/role attached, which is a safe (if less useful) fallback rather
# than a wrong one.
PERSONNEL_HEADLINE_PATTERNS = [
    # "X joins Y as Z [in City]"
    re.compile(
        r"^(?P<name>[A-Z][\w .’'-]+?)\s+(?:re-?)?joins?\s+"
        r"(?P<firm>[A-Z][\w &.,’'-]+?)\s+as\s+(?P<role>[A-Za-z ]+?)"
        r"(?:\s+in\s+[A-Za-z ]+)?$",
        re.I,
    ),
    # "X quits Y" / "X resigns from Y"
    re.compile(
        r"^(?P<name>[A-Z][\w .’'-]+?)\s+(?:quits|resigns(?: from)?)\s+"
        r"(?P<firm>[A-Z][\w &.,’'-]+)$",
        re.I,
    ),
    # "X elevated/promoted to Y at Z"
    re.compile(
        r"^(?P<name>[A-Z][\w .’'-]+?)\s+(?:elevated|promoted)\s+(?:to|as)\s+"
        r"(?P<role>[A-Za-z ]+?)\s+at\s+(?P<firm>[A-Z][\w &.,’'-]+)$",
        re.I,
    ),
    # "Former <firm> lawyer X sets up [his/her] [own] chambers"
    re.compile(
        r"^(?:Former\s+(?P<prev_firm>[A-Z][\w &.,’'-]+?)\s+lawyer\s+)?"
        r"(?P<name>[A-Z][\w .’'-]+?)\s+sets?\s+up\s+(?:his|her|their|own)?\s*"
        r"(?:own\s+)?chambers",
        re.I,
    ),
]


# Words that make an otherwise-matched "name" span untrustworthy: quantity
# words ("Two lawyers join...") and the conjunction "and" (a two-person
# headline like "Anchal Dhir and Shubham Rastogi join..." would otherwise
# have both people's names captured as one garbled fake compound name --
# found via adversarial testing, not a hypothetical).
_PERSONNEL_NAME_REJECT_WORDS = {
    "and", "two", "three", "four", "five", "six", "seven", "eight",
    "several", "multiple", "many", "some", "few", "a", "the", "lawyers",
    "partners", "associates", "advocates", "counsels",
}


def _looks_like_real_name(name):
    words = name.split()
    if len(words) < 2:
        return False
    if any(w.lower() in _PERSONNEL_NAME_REJECT_WORDS for w in words):
        return False
    for w in words:
        bare = w.strip(".").replace("-", "")
        if not bare:
            return False
        # a real name's words are capitalized/all-caps, except for known
        # lowercase particles ("de", "van", ...) -- anything else in
        # lowercase (like "lawyers" or "join") means we over-captured a
        # sentence fragment, not an actual name
        if bare[0].islower() and bare.lower() not in _NAME_PARTICLES:
            return False
    return True


def parse_personnel_move_people(headline):
    """Best-effort name/firm/role extraction for a headline already
    classified as a personnel move (see build_directory.is_personnel_move).
    Returns [] if the headline doesn't match a known shape, or if the
    matched "name" fails _looks_like_real_name() -- e.g. a multi-person
    headline ("X and Y join...") or a quantity-word headline ("Two
    lawyers join..."), both of which this best-effort parser isn't
    sophisticated enough to split correctly, so it correctly abstains
    rather than fabricating a single garbled/wrong person."""
    for pat in PERSONNEL_HEADLINE_PATTERNS:
        m = pat.match(headline.strip())
        if not m:
            continue
        gd = m.groupdict()
        name = normalize_name(gd.get("name") or "")
        if not _looks_like_real_name(name):
            continue
        role = (gd.get("role") or "").strip().title() or None
        firm_raw = gd.get("firm")
        if firm_raw:
            firm = canonical_firm(firm_raw)
        elif "chambers" in headline.lower():
            firm = canonical_firm(f"Chambers of {name}")
            role = role or "Advocate"
        else:
            firm = None
        return [{"name": name, "role": role, "firm": firm}]
    return []


def extract_paren_credits(paragraph):
    return [(m.group(1).strip(), m.group(2)) for m in PAREN_ROLE_RE.finditer(paragraph)]


_NAME_PARTICLES = {"de", "van", "bin", "al", "la", "von", "der", "del"}


def _is_name_word(word):
    bare = word.strip(".").replace("-", "")
    if not bare.isalpha():
        return False
    return bare[:1].isupper() or bare.lower() in _NAME_PARTICLES


_SINGULAR_ROLES_BY_LENGTH = sorted(ROLE_WORDS, key=lambda r: -len(r.split()))


def _starts_plural_marker(tokens, i):
    """True at a plural role marker OR a singular role word: either one ends a
    running "Partners A and B" name list ("..., Principal Associate C")."""
    for marker in PLURAL_MARKERS_BY_LENGTH:
        n = len(marker.split())
        if tokens[i:i + n] == marker.split():
            return True
    for role in _SINGULAR_ROLES_BY_LENGTH:
        n = len(role.split())
        if tokens[i:i + n] == role.split() and i + n < len(tokens):
            return True
    return False


def _marker_ends_a_firm_name(tokens, start):
    """True when the plural role word at tokens[start] directly follows a
    capitalised word or an ampersand, i.e. it closes a firm name."""
    if start <= 0:
        return False
    prev = tokens[start - 1]
    if prev == "&":
        return True
    if prev in (",", "and", "of", "by"):
        return False
    bare = prev.strip("()")
    return bare[:1].isupper() and bare.isalpha() and not prev.endswith((".", ":", ";"))


# "Partner Kartick Maheshwari, Principal Associate Debottam Chattopadhyay and
# Senior Associate Shruti Jaju": the role comes first and is singular, with no
# parentheses. LiveLaw's Khaitan reprints use it throughout. A role word must
# stand on its own (not the tail of "J Sagar Associates") and the name must be
# two or more capitalised words ending at punctuation, "and", or a lowercase
# word, so a following firm name or sentence does not leak in.
PREFIX_ROLE_RE = re.compile(
    rf"(?<![A-Za-z&])(?P<role>{ROLE_ALTERNATION})\s+(?P<name>{NAME_PATTERN})"
    r"(?=\s*(?:[,;.)]|$)|\s+(?:and|were|was|led|from|at|of|in|for|who|also|with|along)\b)"
)


def extract_prefix_role_credits(paragraph):
    out = []
    for m in PREFIX_ROLE_RE.finditer(paragraph):
        name = m.group("name").strip()
        words = name.split()
        # a name never contains a role word; this is a run-on like
        # "Partner Senior Associate X" or a firm ("Counsel Lex Mundi")
        if any(w in {r for role in ROLE_WORDS for r in role.split()} for w in words):
            continue
        out.append((name, m.group("role")))
    return out


def extract_plural_list_credits(paragraph):
    """Plain word-by-word scan (no regex) for 'Associates A, B and C' style
    credits, where several names share one plural role marker."""
    tokens = paragraph.replace(",", " , ").split()
    credits = []
    i = 0
    while i < len(tokens):
        matched_marker = None
        for marker in PLURAL_MARKERS_BY_LENGTH:
            marker_words = marker.split()
            if tokens[i:i + len(marker_words)] == marker_words:
                matched_marker = marker
                i += len(marker_words)
                break
        if not matched_marker:
            i += 1
            continue
        if _marker_ends_a_firm_name(tokens, i - len(matched_marker.split())):
            # "J Sagar Associates and Solicis Lex advised ..." / "Fox Mandal &
            # Associates": the word is the tail of a firm name, not a role
            continue
        role = PLURAL_ROLE_MARKERS[matched_marker]
        names = []
        current = []
        while i < len(tokens):
            w = tokens[i]
            if _starts_plural_marker(tokens, i):
                # "Partners A and B, Senior Associates C, D": the next marker
                # ends this list. Without this "Senior Associates C" was read
                # as one long name and D, E inherited the wrong role.
                break
            if w in (",", "and"):
                if current:
                    names.append(" ".join(current))
                    current = []
                i += 1
                continue
            if _is_name_word(w):
                sentence_ended = w.endswith(".")
                current.append(w.rstrip("."))
                i += 1
                if sentence_ended:
                    # e.g. "... and Arshan Kazi." -- stop here, don't let
                    # the next sentence's capitalized first word ("The",
                    # "Trilegal", ...) bleed into this name
                    break
                continue
            break
        if current:
            names.append(" ".join(current))
        for name in names:
            if len(name.split()) >= 2:
                credits.append((name, role))
    return credits



# --- firm hand-over detection ------------------------------------------------
# Bar & Bench credits each firm's team in its own block, and the block opens by
# naming the firm -- but the verb after the name varies from article to article
# ("advised", "acted for", "served as international legal counsel to", "was
# instructed by" ...). Matching a fixed verb list silently lost every firm the
# newer template introduced with a verb we had not listed, so the previous
# firm's label was carried onto the next firm's lawyers. So the hand-over is
# keyed on the thing that never changes -- a firm we KNOW opening the paragraph
# -- and the verb is ignored.
_ALIAS_TEXTS = sorted(
    {a for a in FIRM_ALIASES} | {v.lower() for v in FIRM_ALIASES.values()},
    key=len, reverse=True,
)
# Raw spellings Bar & Bench uses that differ from our aliases only by "and"/"&".
_ALIAS_TEXTS = sorted(
    set(_ALIAS_TEXTS) | {a.replace("&", "and") for a in _ALIAS_TEXTS if "&" in a},
    key=len, reverse=True,
)


_ALIAS_SCAN = re.compile(
    r"(?<![A-Za-z0-9&])(" + "|".join(re.escape(a) for a in _ALIAS_TEXTS) + r")(?![A-Za-z0-9])",
    re.I,
)


def known_firms_in_text(text):
    """Canonical corpus firms named anywhere in `text`. Looser than
    leading_known_firm(): it only feeds de-duplication matching, never the
    firm list a deal is stored with. Short abbreviations (SAM, CAM, JSA, TT&A)
    count only when written as abbreviations."""
    found = []
    for m in _ALIAS_SCAN.finditer(text or ""):
        alias = m.group(1)
        if len(alias) <= 4 and alias != alias.upper():
            continue
        c = canonical_firm(alias)
        if c and c not in found:
            found.append(c)
    return found


def leading_known_firm(paragraph):
    """Canonical firm if the paragraph OPENS with a firm from our corpus."""
    text = paragraph.strip()
    low = text.lower()
    for alias in _ALIAS_TEXTS:
        if not low.startswith(alias):
            continue
        rest = text[len(alias):]
        if rest and (rest[0].isalnum() or rest[0] in "-_"):
            continue  # "sam" inside "Samuel", "cam" inside "Cameron"
        # short abbreviations must be written as abbreviations (SAM, CAM,
        # TT&A), not be ordinary capitalised words at a sentence start
        if len(alias) <= 4 and text[:len(alias)] != text[:len(alias)].upper():
            continue
        return canonical_firm(alias)
    return None


_NOT_A_FIRM_LEAD = {
    "the", "a", "an", "this", "these", "that", "advice", "tax", "team",
    "transaction", "deal", "in", "with", "for", "as", "it", "its", "his", "her",
}
_NOT_A_FIRM_WORDS = {"was", "were", "is", "are", "been", "being", "by", "the", "on", "who"}


def _looks_like_firm_name(raw):
    """A sentence subject that could plausibly be a firm name. Used only to
    decide whether an UNRESOLVED opening subject should end the current
    firm's block. "The tax aspects of the transaction were advised by ..."
    is a sentence about the deal, not a change of firm, and must not wipe the
    firm we are tracking."""
    words = raw.split()
    if not words or len(words) > 7:
        return False
    if words[0].lower() in _NOT_A_FIRM_LEAD:
        return False
    return not any(w.lower() in _NOT_A_FIRM_WORDS for w in words)


def _is_firm_not_person(name):
    """A credited "name" that is really a firm: exact corpus match, opens with
    a corpus firm, or is the truncated front of one ("Duane Morris" for
    "Duane Morris & Selvam"). Wrongly dropping a person is a miss; keeping a
    firm as a person is a hallucination."""
    low = name.lower()
    return (low in KNOWN_FIRMS or bool(leading_known_firm(name))
            or any(k.startswith(low + " ") for k in KNOWN_FIRMS))


def discover_corpus_firms(paragraphs, law_firms):
    """Corpus firms that open a paragraph describing their role in the deal
    but are missing from the deal's firm list (e.g. the international counsel
    a headline leaves out). Returns law_firms extended, order preserved."""
    cue = re.compile(r"\b(advis|act(ed|s|ing)|represent|counsel|instruct|assist)", re.I)
    out = list(law_firms)
    for para in paragraphs:
        f = leading_known_firm(para)
        if f and f not in out and cue.search(para[:200]):
            out.append(f)
    return out


def _drop_fragment_firms(firms):
    """"Cyril" left over from a headline is a prefix of "Cyril Amarchand
    Mangaldas" -- a truncated duplicate, not another firm."""
    return [
        f for f in firms
        if not any(g != f and g.lower().startswith(f.lower() + " ") for g in firms)
    ]


def extract_firm_context(paragraph, known_firms=()):
    """Find a firm-attribution sentence and snap it onto one of the deal's
    own headline-declared firms.

    The raw regex match is not trustworthy on its own -- it can include
    trailing junk ("Cyril Amarchand Mangaldas also", "Fox Mandal &
    Associates has") or run on into an unrelated sentence ("JSA ... secured
    the CCI approval for this merger. The Firm"), or pick up something
    that isn't a firm at all ("His chambers", "Independent Chambers", "US
    law firms", a client's in-house team). A headline like "Khaitan & Co,
    JSA act on ..." already gives us the deal's real, clean firm list, so
    body-text mentions are only trusted when they can be matched back onto
    it. If the match is ambiguous (contains more than one known firm, e.g.
    "AZB & Partners and Duane Morris & Selvam" in one sentence) or matches
    none of them, it's discarded rather than stored as a new "firm".
    """
    m = FIRM_CONTEXT_RE.match(paragraph.strip())
    if not m:
        return None
    raw = m.group(1).strip()
    if raw.lower() in ("the firm", "the firm also"):
        return None
    candidate = (canonical_firm(raw) or "").lower()
    if not candidate:
        return None
    matches = {
        hf for hf in known_firms
        if hf.lower() in candidate or candidate in hf.lower()
    }
    if len(matches) == 1:
        return next(iter(matches))
    return None


def extract_people(paragraphs, fallback_firm, known_firms=()):
    seen = set()
    people = []
    para_firms = []  # (cleaned_paragraph, firm_in_effect) -- reused below
    current_firm = fallback_firm
    for raw_para in paragraphs:
        para = _strip_nickname_parens(raw_para)
        lead_firm = leading_known_firm(para)
        context_firm = lead_firm or extract_firm_context(para, known_firms)
        if context_firm:
            current_firm = context_firm
        else:
            m = FIRM_CONTEXT_RE.match(para.strip())
            if (m and not para.strip().lower().startswith("the firm")
                    and _looks_like_firm_name(m.group(1).strip())):
                # This paragraph hands over to a firm we could not resolve. Carrying
                # the previous firm forward is how TT&A's three lawyers ended up
                # labelled JSA: a confident wrong answer, which is worse than none.
                # Drop the context instead and let them come out unattributed.
                current_firm = None
        para_firms.append((para, current_firm))

        credits = (extract_paren_credits(para) + extract_plural_list_credits(para)
                   + extract_prefix_role_credits(para))
        for name, role in credits:
            # a firm name sitting next to a role word ("Cyril Amarchand
            # Mangaldas (Partner)", crediting the firm itself rather than
            # a lawyer at it) would otherwise be captured as a fake person
            # -- reject anything that matches our own known-firm corpus
            if _is_firm_not_person(name):
                continue
            key = (name.lower(), role, current_firm)
            if key in seen:
                continue
            seen.add(key)
            people.append({"name": name, "role": role, "firm": current_firm})

    # Secondary, precision-gated NER pass (see ner_supplement.py) -- only
    # ever fills genuine gaps the regex above missed. Every candidate must
    # independently satisfy: spaCy PERSON tag, a full "(Role)" match, a
    # firm that resolves to one of this deal's own headline firms, and not
    # be a fragment of a name already found above. Silently contributes
    # nothing if spaCy isn't installed.
    known_names_lower = {p["name"].lower() for p in people}
    known_firms_set = set(known_firms)
    for para, firm in para_firms:
        for extra in ner_supplement.supplement_credits(
            para, firm, known_firms_set, ROLE_WORDS, known_names_lower
        ):
            key = (extra["name"].lower(), extra["role"], extra["firm"])
            if key in seen:
                continue
            seen.add(key)
            known_names_lower.add(extra["name"].lower())
            people.append(extra)

    return people


# --- firm extraction from the page's own entity tags -------------------------
# Bar & Bench tags each article with /topic/ links -- an editorially curated
# entity list. It is strictly better than the headline: the Adani article tags
# /topic/tta even though TT&A never appears in the headline, which is exactly
# the firm the headline-only approach lost.
#
# The list mixes firms, lawyers and clients, so it cannot be used raw. A topic
# is treated as a firm only when it ALSO opens a body attribution sentence
# ("X advised Y on this transaction") -- two independent signals agreeing.
# Clients never open one; nor do individual lawyers. Across five test articles
# that rule found 16 of 16 firms with no false positives, and it needs no
# whitelist, which is what stops this breaking again the next time a boutique
# nobody has heard of appears.

TOPIC_HREF_RE = re.compile(r'href="/topic/([^"/]+)"')
# Trailing filler the attribution regex drags in: "River Law also advised ...".
FIRM_TAIL_RE = re.compile(r"\s+(also|has|have|had|is|was|were|and)$", re.I)


def topic_slugs(html):
    return sorted(set(TOPIC_HREF_RE.findall(html)))


def _slugify_firm(name):
    """Match Bar & Bench's own slug convention.

    Theirs DROPS the ampersand rather than expanding it: "Khaitan & Co" is
    khaitan-co and "TT&A" is tta. Expanding "&" to "and" here silently breaks
    the match for every firm with an ampersand in its name, which is most of
    the big ones.
    """
    return re.sub(r"[^a-z0-9]+", "-", name.lower().replace("&", "")).strip("-")


def _topic_match(name, topics):
    """The topic slug that names this firm, if any.

    Matched loosely in both directions because the body and the tag disagree
    on formality: the body says "Pinac", the tag says
    pinac-advocates-and-solicitors; the body says "JSA Advocates and
    Solicitors", the tag says jsa.
    """
    sl = _slugify_firm(name)
    if not sl:
        return None
    sq = sl.replace("-", "")
    for t in topics:
        tq = t.replace("-", "")
        if t == sl or t.startswith(sl + "-") or sl.startswith(t + "-"):
            return t
        if tq == sq or tq.startswith(sq) or sq.startswith(tq):
            return t
    return None


def body_attribution_firms(paragraphs):
    """Names that open a firm-attribution sentence, in document order."""
    out = []
    for para in paragraphs:
        m = FIRM_CONTEXT_RE.match(para.strip())
        if not m:
            continue
        raw = FIRM_TAIL_RE.sub("", m.group(1).strip())
        if not raw or raw.lower().startswith("the firm"):
            continue
        if raw not in out:
            out.append(raw)
    return out


def firms_from_names(names, paragraphs):
    """firms_from_topics() for sources that expose an entity list as plain
    names (JSON-LD `keywords`) rather than /topic/ links. Same rule: a firm
    counts only when the curated list AND a body attribution sentence agree."""
    topics = sorted({_slugify_firm(n) for n in names if _slugify_firm(n)})
    if not topics:
        return []
    firms = []
    for raw in body_attribution_firms(paragraphs):
        if not _topic_match(raw, topics):
            continue
        cname = canonical_firm(raw)
        if cname and cname not in firms:
            firms.append(cname)
    return firms


def firms_from_topics(html, paragraphs):
    """Firms confirmed by BOTH the page's topic tags and a body attribution
    sentence. Returns canonical names, in the order the article introduces
    them, so firms_from_topics()[0] is the lead firm."""
    topics = topic_slugs(html)
    if not topics:
        return []
    firms = []
    for raw in body_attribution_firms(paragraphs):
        if not _topic_match(raw, topics):
            continue
        cname = canonical_firm(raw)
        if cname and cname not in firms:
            firms.append(cname)
    return firms


# --- deal size ---------------------------------------------------------------
# 59% of Dealstreet articles state a figure, nearly always in the headline
# ("... on ₹9,825 crore ...", "... for $206 million"). Three currencies and
# four magnitude words cover essentially all of it.

CURRENCY_SYMBOL = {
    "₹": "INR", "$": "USD", "€": "EUR", "£": "GBP",
    # written-out codes: Legal Era and LiveLaw write "INR 30 Billion" and
    # "USD 100 Million" where Bar & Bench uses symbols
    "INR": "INR", "RS": "INR", "RS.": "INR", "USD": "USD", "US$": "USD",
    "EUR": "EUR", "GBP": "GBP",
}
MAGNITUDE = {
    "crore": 10_000_000, "crores": 10_000_000, "cr": 10_000_000,
    "lakh": 100_000, "lakhs": 100_000, "lac": 100_000, "lacs": 100_000,
    "million": 1_000_000, "mn": 1_000_000,
    "billion": 1_000_000_000, "bn": 1_000_000_000,
}

# Indicative rates, deliberately hard-coded rather than fetched. A scheduled
# job that looked up live FX would silently restate the value of every deal
# already recorded, and two runs of the same article would disagree. These
# exist ONLY to order deals against each other and to answer range filters --
# the figure shown to a reader is always the original currency, unconverted.
FX_TO_INR_ASOF = "2026-09-01"
FX_TO_INR = {"INR": 1.0, "USD": 88.0, "EUR": 96.0, "GBP": 112.0}

_APPROX = r"(?:~|about|approx\w*|around|over|up\s?to|more\s+than|nearly|almost|upwards\s+of)\s*"
MONEY_RE = re.compile(
    rf"(?P<approx>{_APPROX})?"
    r"(?P<cur>[₹$€£]|(?<![A-Za-z])(?:INR|Rs\.?|USD|US\$|EUR|GBP)(?![A-Za-z]))"
    r"\s?(?P<amt>\d[\d,]*(?:\.\d+)?)(?!\d)\s*"
    r"(?P<unit>crores?|cr|lakhs?|lacs?|million|mn|billion|bn)?(?![A-Za-z])"
    r"(?P<plus>\s*\+)?",
    re.I,
)
# "1,940 crores" with no currency mark is still rupees: crore and lakh are
# Indian units. Million/billion without a currency are ambiguous and ignored.
BARE_INR_RE = re.compile(
    rf"(?P<approx>{_APPROX})?"
    r"(?<![\d,.])(?P<amt>\d[\d,]*(?:\.\d+)?)(?!\d)\s*(?P<unit>crores?|cr|lakhs?|lacs?)(?![A-Za-z])"
    r"(?P<plus>\s*\+)?",
    re.I,
)

# A body figure is only trusted when its sentence is about the transaction --
# otherwise it can be a valuation, a revenue line or last year's raise.
DEAL_SENTENCE = re.compile(
    r"\b(acqui|invest|rais|fundrais|fund-rais|issu|subscri|sell|sale|sold|buy|purchas|"
    r"merg|divest|stake|transact|deal|IPO|QIP|placement|financ|loan|facilit)",
    re.I,
)


def parse_money(text):
    """First money-shaped figure in `text`, normalised. None if there isn't one."""
    text = text or ""
    m = MONEY_RE.search(text)
    bare = BARE_INR_RE.search(text)
    if bare and (m is None or bare.start() < m.start()):
        m, currency = bare, "INR"
    elif m:
        currency = CURRENCY_SYMBOL.get(m.group("cur").upper().replace(" ", ""))
    else:
        return None
    if not currency:
        return None
    try:
        amount = float(m.group("amt").replace(",", ""))
    except ValueError:
        return None
    unit = (m.group("unit") or "").lower()
    amount *= MAGNITUDE.get(unit, 1)
    if amount <= 0:
        return None
    return {
        "raw": m.group(0).strip(),
        "currency": currency,
        # base units of the stated currency: rupees, dollars, euros
        "amount": int(round(amount)),
        # a single comparable number, for ordering only -- see FX_TO_INR
        "amount_inr": int(round(amount * FX_TO_INR.get(currency, 1.0))),
        "approx": bool(m.group("approx") or m.group("plus")),
        "fx_asof": FX_TO_INR_ASOF if currency != "INR" else None,
    }


def extract_deal_value(headline, paragraphs):
    """Deal size, preferring the headline.

    The headline figure is the one an editor chose to describe the deal, and
    it is unambiguous. The body is only consulted when the headline has none,
    and then only from a sentence that is actually about the transaction --
    the Adani article, for instance, restates its ₹9,825 crore headline figure
    as "(~$1 billion)" one paragraph later, and taking both would record the
    same deal twice at two different values.
    """
    from_headline = parse_money(headline)
    if from_headline:
        from_headline["source"] = "headline"
        return from_headline
    for para in paragraphs[:4]:
        if not DEAL_SENTENCE.search(para):
            continue
        hit = parse_money(para)
        if hit:
            hit["source"] = "body"
            return hit
    return None
