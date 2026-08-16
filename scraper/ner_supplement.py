"""
Secondary, precision-first name detector for scrape_barandbench.py.

Only ever used to recover people the primary regex/plain-token extractors
missed -- e.g. because a name's characters didn't fit NAME_PATTERN's
character classes. This is deliberately narrow, not a general NER sweep:
a candidate is accepted only when ALL of the following hold.

  1. spaCy tags it as a PERSON entity. A bare Indian-names word-list match
     (first/last token lookup) was tried and measured to produce far more
     false positives -- it tagged things like "Fox Mandal" (a firm name)
     and "Puravankara Limited" (a client) as people. Dropped from the
     production path for that reason; it stays in local_tools/ as an
     exploratory tool only.
  2. The entity sits immediately before one of our known role words in
     parentheses, with a FULL match -- the role must be followed by "," or
     ")", not just a word boundary. A word-boundary-only check previously
     let "Senior Associate" wrongly match inside "(Senior Associate
     Designate)", mislabeling someone's actual (different, more senior)
     title.
  3. Its firm resolves to one of the deal's own headline-declared firms --
     same rule extract_firm_context() already enforces for the primary
     extractor. No match, no addition.
  4. It is not a substring of, or a superset containing, any name the
     primary extractor already found in this article. This is what stops
     "Jose Vayttaden" from being added as a bogus duplicate alongside the
     already-correctly-captured "Shishir Jose Vayttaden".

Missing a name here is an acceptable, expected outcome. Adding a wrong or
duplicate one is not -- when in doubt, this module returns nothing.
"""
import re

try:
    import spacy
    NLP = spacy.load("en_core_web_sm")
except Exception:
    # spaCy/model not installed, or failed to load for any reason -- NER
    # supplementation is skipped entirely rather than breaking the scrape.
    NLP = None

ROLE_LOOKUP_WINDOW = 40


def _full_role_after(text, end_char, role_words):
    tail = text[end_char: end_char + ROLE_LOOKUP_WINDOW]
    for r in role_words:
        if re.match(rf"\s*\(\s*{re.escape(r)}\s*[,)]", tail):
            return r
    return None


def _is_fragment_of_known_name(candidate, known_names_lower):
    c = candidate.lower()
    for known in known_names_lower:
        if c == known or c in known or known in c:
            return True
    return False


def supplement_credits(paragraph, current_firm, known_firms, role_words, known_names_lower):
    """Returns a list of {"name","role","firm","source"} dicts for people
    found in `paragraph` that survive every guard above. Returns []
    whenever spaCy isn't available, current_firm isn't a validated
    headline firm, or nothing passes the checks."""
    if NLP is None or not current_firm or current_firm not in known_firms:
        return []

    matches = []
    doc = NLP(paragraph)
    for ent in doc.ents:
        if ent.label_ != "PERSON":
            continue
        name = ent.text.strip()
        if len(name.split()) < 2:
            continue
        if _is_fragment_of_known_name(name, known_names_lower):
            continue
        role = _full_role_after(paragraph, ent.end_char, role_words)
        if not role:
            continue
        matches.append({"name": name, "role": role, "firm": current_firm, "source": "ner"})
    return matches
