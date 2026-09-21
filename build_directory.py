import json
import re
import sqlite3
from pathlib import Path

BASE = Path(__file__).parent
SRC = BASE / "structured_deals.json"
SCRAPED_SRC = BASE / "barandbench_deals.json"
DB_PATH = BASE / "legal_directory.db"
JSON_PATH = BASE / "legal_directory.json"
VERIFICATION_PATH = BASE / "firm_history_verification.json"

# Canonicalize firm name variants that refer to the same firm.
FIRM_ALIASES = {
    "jsa": "JSA Advocates & Solicitors",
    "jsa advocates & solicitors": "JSA Advocates & Solicitors",
    "sam": "Shardul Amarchand Mangaldas & Co",
    "shardul amarchand mangaldas": "Shardul Amarchand Mangaldas & Co",
    "shardul amarchand mangaldas & co": "Shardul Amarchand Mangaldas & Co",
    "khaitan": "Khaitan & Co",
    "khaitan & co": "Khaitan & Co",
    "azb": "AZB & Partners",
    "azb & partners": "AZB & Partners",
    "tt&a": "Talwar Thakore & Associates",
    "talwar thakore & associates": "Talwar Thakore & Associates",
    "sullivan": "Sullivan & Cromwell",
    "sullivan & cromwell": "Sullivan & Cromwell",
    "cyril amarchand mangaldas": "Cyril Amarchand Mangaldas",
    "trilegal": "Trilegal",
    "white & case": "White & Case",
    "linklaters": "Linklaters",
    "l&l partners": "L&L Partners",
    "induslaw": "IndusLaw",
    "saraf and partners": "Saraf and Partners",
    "argus partners": "Argus Partners",
    "luthra and luthra": "Luthra and Luthra",
    "s&r associates": "S&R Associates",
    "wadia ghandy": "Wadia Ghandy",
    "dsk legal": "DSK Legal",
    "j. sagar associates": "J. Sagar Associates",
    "nishith desai associates": "Nishith Desai Associates",
    "desai & diwanji": "Desai & Diwanji",
    "kirkland & ellis": "Kirkland & Ellis",
    "latham & watkins": "Latham & Watkins",
    "clifford chance": "Clifford Chance",
    "allen & overy": "Allen & Overy",
    "ashurst": "Ashurst",
    "baker mckenzie": "Baker McKenzie",
    "herbert smith freehills": "Herbert Smith Freehills",
    "freshfields": "Freshfields",
    "skadden": "Skadden",
    "simpson thacher": "Simpson Thacher",
    "ropes & gray": "Ropes & Gray",
    "weil gotshal": "Weil Gotshal",
    "paul weiss": "Paul Weiss",
    "paul, weiss": "Paul Weiss",
    "davis polk": "Davis Polk",
    "sidley austin": "Sidley Austin",
    "jsa advocates and solicitors": "JSA Advocates & Solicitors",
    "cms induslaw": "CMS INDUSLAW",

    # short-form / abbreviation aliases that were producing near-duplicate
    # firm entries (e.g. "Cam" and "Elp" as their own separate firms)
    "cam": "Cyril Amarchand Mangaldas",
    "elp": "Economic Laws Practice",
    "economic laws practice (elp)": "Economic Laws Practice",
    "economic laws practice": "Economic Laws Practice",
    "bharucha": "Bharucha & Partners",
    "bharucha & partners": "Bharucha & Partners",
    "fox & mandal": "Fox Mandal & Associates",
    "fox mandal & associates": "Fox Mandal & Associates",
    "fox mandal associates": "Fox Mandal & Associates",
    "sequitur": "Sequitur Advocates & Legal Consultants",
    "sequitur advocates & legal consultants": "Sequitur Advocates & Legal Consultants",
    "duane morris & selvam": "Duane Morris & Selvam",
    "duane morris selvam": "Duane Morris & Selvam",
    "hogan lovells": "Hogan Lovells",
    "hogan lovells cadwalader": "Hogan Lovells Cadwalader",
    "mcdermott will & schulte": "McDermott Will & Schulte",
    "mcdermott will & emery": "McDermott Will & Emery",
    "k law": "K Law",
    "k&l gates": "K&L Gates",
    "knm & partners": "KNM & Partners",
    "chandhiok & mahajan": "Chandhiok & Mahajan",
    "chandhiok mahajan": "Chandhiok & Mahajan",
    "dentons link legal": "Dentons Link Legal",
    "kochhar & co": "Kochhar & Co",
    "kochhar and co": "Kochhar & Co",
    "rajani associates": "Rajani Associates",
    "almt legal": "ALMT Legal",
    "vertices partners": "Vertices Partners",
    "veritas legal": "Veritas Legal",
    "phoenix legal": "Phoenix Legal",
    "ikigai law": "Ikigai Law",
    "novojuris legal": "NovoJuris Legal",
    "touchstone partners": "Touchstone Partners",
    "samvad partners": "Samvād: Partners",
    "samvād: partners": "Samvād: Partners",
    "triumvir law": "Triumvir Law",
    "anagram partners": "Anagram Partners",
    "krishnamurthy & co": "Krishnamurthy & Co",
    "obhan & associates": "Obhan & Associates",
    "gibson dunn": "Gibson Dunn",
    "gibson dunn & crutcher": "Gibson Dunn",
    "wilson sonsini goodrich & rosati": "Wilson Sonsini Goodrich & Rosati",
    "wilson sonsini": "Wilson Sonsini Goodrich & Rosati",
    "squire patton boggs": "Squire Patton Boggs",
    "norton rose fulbright": "Norton Rose Fulbright",
    "k&l gates llp": "K&L Gates",
    "morgan lewis": "Morgan Lewis",
    "morgan lewis & bockius": "Morgan Lewis",
    "goodwin": "Goodwin",
    "goodwin procter": "Goodwin",
    "avisen legal": "Avisen Legal",
    "legacy law offices": "Legacy Law Offices",
    "dhir & dhir associates": "Dhir & Dhir Associates",
    "troutman pepper locke": "Troutman Pepper Locke",
    "a&o shearman": "A&O Shearman",
    "allen & overy shearman": "A&O Shearman",
}

# Known-firm corpus: names we've confirmed are real law firms active in
# Indian (or India-linked cross-border) M&A coverage, used to sanity-check
# firm-like strings pulled from free text (headlines/body copy) before
# they're accepted as a firm rather than discarded as noise.
KNOWN_FIRMS = {v.lower() for v in FIRM_ALIASES.values()}

CITY_SUFFIXES = {
    "bangalore", "bengaluru", "mumbai", "delhi", "new delhi", "gurugram",
    "gurgaon", "chennai", "hyderabad", "pune", "kolkata", "noida",
}

_SMALL_WORDS = {"&", "of", "and", "the"}

# Generic/non-specific references that show up in source text where no
# actual firm is named (e.g. a headline saying "US law firms" instead of
# naming them, or body text referring to "his chambers" / an in-house
# team) -- these are not firms and must never become one.
NON_FIRM_PHRASES = {
    "us law firms", "international law firms", "foreign counsel",
    "international counsel", "local counsel", "his chambers",
    "her chambers", "independent chambers", "independent counsel",
    "in-house", "in-house counsel", "the firm", "the firm also",
}


def _clean_firm_string(name):
    name = re.sub(r"\s+", " ", name).strip()
    # drop "(formerly ...)" annotations
    name = re.sub(r"\s*\(formerly[^)]*\)", "", name, flags=re.I).strip()
    # drop trailing ", <City>" qualifiers
    m = re.match(r"^(.*?),\s*([A-Za-z][A-Za-z ]*)$", name)
    if m and m.group(2).strip().lower() in CITY_SUFFIXES:
        name = m.group(1).strip()
    # drop trailing LLP/LLC suffix (keep the firm's base name for dedup)
    name = re.sub(r"\s+(LLP|LLC)\.?$", "", name, flags=re.I).strip()
    # drop a stray trailing sentence-ending period/comma left over from
    # extracting the firm name out of surrounding prose -- otherwise
    # "Trilegal." and "Trilegal" register as two different firms
    name = re.sub(r"[.,]+$", "", name).strip()
    return name


def _smart_title(name):
    if not name.isupper():
        return name
    words = name.split(" ")
    out = []
    for w in words:
        out.append(w if w == "&" else (w.lower() if w.lower() in _SMALL_WORDS else w.capitalize()))
    return " ".join(out)


def canonical_firm(name):
    if not name:
        return None
    cleaned = _clean_firm_string(name)
    if not cleaned:
        return None
    key = cleaned.lower()
    if key in NON_FIRM_PHRASES or re.match(r"^(his|her)\s+chambers\b", key):
        return None
    # a client's own internal legal team, e.g. "Oravel Stays Limited
    # (in-house)" -- that's the client, not an external law firm
    if re.search(r"\(\s*in-?house\b", key):
        return None
    if key in FIRM_ALIASES:
        return FIRM_ALIASES[key]
    return _smart_title(cleaned)


def normalize_name(name):
    name = re.sub(r"\s+", " ", name).strip()
    # strip stray leading/trailing punctuation artifacts from truncated snippets
    name = name.strip(" .,;:-")
    return name


ROLE_RANK = {
    "Managing Partner": 6,
    "Joint Managing Partner": 6,
    "Senior Partner": 5,
    "Partner": 4,
    "Associate Partner": 3,
    "Principal Associate": 3,
    "Counsel": 3,
    "Of Counsel": 3,
    "Senior Associate": 2,
    "Associate": 1,
}


def best_role(existing, new):
    if not existing:
        return new
    if not new:
        return existing
    return existing if ROLE_RANK.get(existing, 0) >= ROLE_RANK.get(new, 0) else new


# Simplified role taxonomy for filtering/browsing. The raw `role` field is
# full of one-off practice-area-qualified variants ("Partner (Head - Media,
# Education & Gaming)", "Associate (SEBI matters)", "Senior Associate,
# Corporate law team") -- 285 distinct strings across the dataset, 210 of
# them appearing for exactly one person. That's unusable as a filter list.
# role_bucket() maps each raw title down to one of a small, fixed set of
# seniority categories; the full original title is kept in `role` and
# still shown on the person's own detail page -- only the filter/list
# view uses the simplified bucket.
#
# Patterns are checked in order, most-specific-first, since e.g. "Founding
# & Managing Partner" and "Associate Partner" both contain the substring
# "partner" and must be caught by their own rule before the generic
# Partner/Associate catch-alls would misfile them.
ROLE_BUCKET_PATTERNS = [
    ("Managing Partner", re.compile(r"managing partner", re.I)),
    ("Senior Partner", re.compile(r"senior partner", re.I)),
    ("Founder", re.compile(r"founder|founding", re.I)),
    ("Associate Partner", re.compile(r"associate partner", re.I)),
    ("Partner", re.compile(r"partner", re.I)),
    ("Counsel", re.compile(r"counsel", re.I)),
    ("Principal Associate", re.compile(r"principal associate", re.I)),
    ("Senior Associate", re.compile(r"senior associate", re.I)),
    ("Associate", re.compile(r"associate", re.I)),
    ("Senior Advocate", re.compile(r"senior advocate", re.I)),
    ("Advocate", re.compile(r"advocate", re.I)),
    ("Consultant", re.compile(r"consultant", re.I)),
]


def role_bucket(role):
    if not role:
        return None
    for bucket, pattern in ROLE_BUCKET_PATTERNS:
        if pattern.search(role):
            return bucket
    # no known pattern matched (Company Secretary, Paralegal, Executive
    # Chairman, "Head of India Practice" with no rank word, etc.) -- these
    # don't sit on the standard associate/partner ladder at all, so they
    # go in one "Other" bucket rather than each becoming its own filter
    # chip. Nothing is lost: the full original title is still shown on
    # that person's own profile regardless of which bucket they're filed
    # under here.
    return "Other"


def shorten_snapshot(headline):
    h = re.sub(r"\s+", " ", headline).strip()
    h = h.rstrip(" -")
    if len(h) > 110:
        h = h[:107].rsplit(" ", 1)[0] + "..."
    return h


# Transaction type -> one or more practice areas. A deal/person can span multiple.
PRACTICE_AREA_MAP = {
    "IPO": ["Capital Markets"],
    "QIP": ["Capital Markets"],
    "Series A": ["Private Equity & Venture Capital"],
    "Series B": ["Private Equity & Venture Capital"],
    "Series C": ["Private Equity & Venture Capital"],
    "Series D": ["Private Equity & Venture Capital"],
    "Series E": ["Private Equity & Venture Capital"],
    "fundraise": ["Private Equity & Venture Capital"],
    "investment": ["Private Equity & Venture Capital"],
    "acquisition": ["Mergers & Acquisitions"],
    "stake acquisition": ["Mergers & Acquisitions"],
    "merger": ["Mergers & Acquisitions"],
    "amalgamation": ["Mergers & Acquisitions"],
    "demerger": ["Mergers & Acquisitions"],
    "buyout": ["Mergers & Acquisitions"],
    "divestment": ["Mergers & Acquisitions"],
    "financing": ["Banking & Finance"],
    "joint venture": ["Corporate & Commercial"],
    "joint development agreement": ["Corporate & Commercial"],
    "restructuring": ["Restructuring & Insolvency"],
}


def practice_areas_for(types):
    areas = set()
    for t in types:
        areas.update(PRACTICE_AREA_MAP.get(t, []))
    return sorted(areas)


# Headlines about a lawyer's career (joining/leaving a firm, being
# promoted, setting up an independent practice) aren't M&A/transaction
# deals -- they carry no client, no transaction type, and nothing that
# should count as a "matter" on that person's record. But the person/firm/
# role information in them is still real and worth keeping, so these are
# excluded from the deals table (and deal counts) while the people
# mentioned are still upserted with their current firm/role -- just with
# no deal attached. Applied uniformly to every source (mbox-derived and
# scraped) at this single consolidation point.
PERSONNEL_MOVE_RE = re.compile(
    r"\b(re-?joins?(?!\s+hands)|joins?(?!\s+hands)|quits?|resigns?|steps?\s+down|"
    r"elevated\s+(to|as)|"
    r"promoted\s+(to|as)|appointed\s+as|"
    r"sets?\s+up\s+(his|her|their|own)?\s*(own\s+)?chambers|"
    r"launches?\s+(his|her|their)?\s*own\s+(practice|firm|chambers)|"
    r"starts?\s+(his|her|their)?\s*own\s+(practice|firm|chambers)|"
    r"moves?\s+to\s+.+\s+as\s|named\s+partner|elevated\s+as\s+partner|"
    r"hires?\s+.+\s+as\s+partner)\b",
    re.I,
)


def is_personnel_move(headline):
    return bool(PERSONNEL_MOVE_RE.search(headline))


# Roles that legitimately have no firm: a Senior Advocate or a retired law
# officer gets named in a deal write-up without being employed by anyone. Their
# name matching a firm's lawyer is a coincidence of co-mention, not evidence of
# employment, so they are never merged.
NON_FIRM_ROLES = {
    "senior advocate", "advocate", "former attorney general", "attorney general",
    "solicitor general", "additional solicitor general", "general counsel",
    "judge", "justice", "arbitrator", "amicus curiae", "counsel to the bench",
}


def _deal_value_columns(v):
    """Flatten the scraper's deal_value dict into the deals table's columns.

    Older entries in structured_deals.json predate size extraction and simply
    have no deal_value, which is why every column is nullable: a missing size
    is normal, not a defect, and must not be stored as zero.
    """
    if not isinstance(v, dict):
        return (None, None, None, None, 0, None)
    return (
        v.get("raw"),
        v.get("currency"),
        v.get("amount"),
        v.get("amount_inr"),
        1 if v.get("approx") else 0,
        v.get("source"),
    )


def merge_unaffiliated_people(cur):
    """Fold a firm-less person row into the row that already carries their firm.

    `people` is UNIQUE(name, firm_id), so a deal that failed to attribute
    someone creates a SECOND row for them with firm_id NULL rather than
    updating the first. The same lawyer then appears twice: once correctly
    placed at their firm, once floating. This folds the floating row into the
    real one, moving its deals across.

    Deliberately conservative. A merge happens only when the firm-less row's
    name matches rows at EXACTLY ONE firm -- two candidate firms means either a
    lateral move or two different people, and the data cannot tell which, so
    those are left alone. Roles that have no firm by nature are never merged.

    This lives here, not in a migration, because build_directory.py drops and
    recreates every table on each run: a fix applied to the database would be
    gone within one scheduled build.
    """
    rows = cur.execute(
        "SELECT id, name, role FROM people WHERE firm_id IS NULL"
    ).fetchall()
    merged = 0
    for pid, name, role in rows:
        if (role or "").strip().lower() in NON_FIRM_ROLES:
            continue
        targets = cur.execute(
            "SELECT id, firm_id, role FROM people "
            "WHERE name = ? AND firm_id IS NOT NULL",
            (name,),
        ).fetchall()
        if len({t[1] for t in targets}) != 1:
            continue
        keep_id, _, keep_role = targets[0]
        cur.execute(
            "UPDATE OR IGNORE person_deals SET person_id = ? WHERE person_id = ?",
            (keep_id, pid),
        )
        # UPDATE OR IGNORE drops rows that would collide on the composite
        # primary key -- the person was already linked to that deal under the
        # firmed row. Clear whatever is left so nothing dangles.
        cur.execute("DELETE FROM person_deals WHERE person_id = ?", (pid,))
        cur.execute(
            "UPDATE people SET role = ? WHERE id = ?",
            (best_role(keep_role, role), keep_id),
        )
        cur.execute("DELETE FROM people WHERE id = ?", (pid,))
        merged += 1
    return merged


def main():
    deals = json.loads(SRC.read_text())
    if SCRAPED_SRC.exists():
        deals = deals + json.loads(SCRAPED_SRC.read_text())
    verification = json.loads(VERIFICATION_PATH.read_text()) if VERIFICATION_PATH.exists() else {}
    verification.pop("_readme", None)

    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE firms (
            id INTEGER PRIMARY KEY,
            name TEXT UNIQUE NOT NULL
        );
        CREATE TABLE people (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            firm_id INTEGER REFERENCES firms(id),
            role TEXT,
            UNIQUE(name, firm_id)
        );
        CREATE TABLE deals (
            id INTEGER PRIMARY KEY,
            headline TEXT NOT NULL,
            client TEXT,
            source TEXT,
            url TEXT UNIQUE,
            snippet TEXT,
            -- Reported deal size. value_amount is in base units of
            -- value_currency (rupees, dollars); value_inr is the same figure
            -- converted at a fixed dated rate and exists only so deals in
            -- different currencies can be ordered and range-filtered against
            -- each other. Display value_raw, never value_inr.
            value_raw TEXT,
            value_currency TEXT,
            value_amount INTEGER,
            value_inr INTEGER,
            value_approx INTEGER DEFAULT 0,
            value_source TEXT
        );
        CREATE TABLE deal_types (
            deal_id INTEGER REFERENCES deals(id),
            type TEXT NOT NULL
        );
        CREATE TABLE deal_practice_areas (
            deal_id INTEGER REFERENCES deals(id),
            area TEXT NOT NULL
        );
        CREATE TABLE deal_firms (
            deal_id INTEGER REFERENCES deals(id),
            firm_id INTEGER REFERENCES firms(id)
        );
        CREATE TABLE person_deals (
            person_id INTEGER REFERENCES people(id),
            deal_id INTEGER REFERENCES deals(id),
            client_override TEXT,
            PRIMARY KEY (person_id, deal_id)
        );
        """
    )

    firm_ids = {}

    def get_firm_id(name):
        cname = canonical_firm(name)
        if cname is None:
            return None
        if cname not in firm_ids:
            cur.execute(
                "INSERT OR IGNORE INTO firms(name) VALUES (?)", (cname,)
            )
            cur.execute("SELECT id FROM firms WHERE name = ?", (cname,))
            firm_ids[cname] = cur.fetchone()[0]
        return firm_ids[cname]

    person_ids = {}  # (norm_name_lower, firm_id) -> person_id

    def get_person_id(name, firm_id, role):
        norm = normalize_name(name)
        key = (norm.lower(), firm_id)
        if key not in person_ids:
            cur.execute(
                "INSERT OR IGNORE INTO people(name, firm_id, role) VALUES (?, ?, ?)",
                (norm, firm_id, role),
            )
            cur.execute(
                "SELECT id, role FROM people WHERE name = ? AND firm_id IS ?",
                (norm, firm_id),
            )
            row = cur.fetchone()
            person_ids[key] = row[0]
        else:
            pid = person_ids[key]
            cur.execute("SELECT role FROM people WHERE id = ?", (pid,))
            existing_role = cur.fetchone()[0]
            new_role = best_role(existing_role, role)
            if new_role != existing_role:
                cur.execute(
                    "UPDATE people SET role = ? WHERE id = ?", (new_role, pid)
                )
        return person_ids[key]

    skipped_people = 0

    skipped_personnel_moves = 0

    for d in deals:
        # A headline can be genuinely deal-shaped ("<Firm> advises <Client>
        # on ...") while still containing a personnel-move trigger word
        # incidentally (e.g. "... advises promoter as CEO steps down amid
        # succession planning") -- found via adversarial testing. A
        # populated client name is strong evidence this really is a deal,
        # so it overrides the keyword match rather than the reverse.
        personnel_move = is_personnel_move(d["headline"]) and not d.get("client")
        if personnel_move:
            skipped_personnel_moves += 1
            deal_id = None
        else:
            cur.execute(
                "INSERT OR IGNORE INTO deals(headline, client, source, url, snippet, "
                "value_raw, value_currency, value_amount, value_inr, value_approx, value_source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (d["headline"], d.get("client"), d.get("source"), d.get("url"), d.get("snippet"),
                 *_deal_value_columns(d.get("deal_value"))),
            )
            cur.execute("SELECT id FROM deals WHERE url = ?", (d.get("url"),))
            row = cur.fetchone()
            if row is None:
                # url was null/duplicate-less path fallback
                cur.execute(
                    "SELECT id FROM deals WHERE headline = ? AND snippet IS ?",
                    (d["headline"], d.get("snippet")),
                )
                row = cur.fetchone()
            deal_id = row[0]

            types = d.get("transaction_types", [])
            for t in types:
                cur.execute(
                    "INSERT INTO deal_types(deal_id, type) VALUES (?, ?)", (deal_id, t)
                )
            for area in practice_areas_for(types):
                cur.execute(
                    "INSERT INTO deal_practice_areas(deal_id, area) VALUES (?, ?)",
                    (deal_id, area),
                )

        deal_firm_ids = []
        for f in d.get("law_firms", []):
            fid = get_firm_id(f)
            if fid is not None:
                deal_firm_ids.append(fid)
                if not personnel_move:
                    cur.execute(
                        "INSERT INTO deal_firms(deal_id, firm_id) VALUES (?, ?)",
                        (deal_id, fid),
                    )

        primary_firm_id = deal_firm_ids[0] if deal_firm_ids else None

        for p in d.get("people", []):
            pname = p.get("name")
            if not pname or len(pname.strip()) < 2:
                skipped_people += 1
                continue
            # A person's own firm/client (confirmed from the source article) takes
            # priority over the deal-level guess of "first firm listed".
            person_firm_id = get_firm_id(p["firm"]) if p.get("firm") else primary_firm_id
            pid = get_person_id(pname, person_firm_id, p.get("role"))
            # a personnel-move article isn't a matter -- record the
            # person's (now-current) firm/role above, but don't link them
            # to a "deal" that doesn't exist
            if not personnel_move:
                cur.execute(
                    "INSERT OR IGNORE INTO person_deals(person_id, deal_id, client_override) VALUES (?, ?, ?)",
                    (pid, deal_id, p.get("client")),
                )

    conn.commit()

    merged = merge_unaffiliated_people(cur)
    conn.commit()
    if merged:
        print(f"merged {merged} unaffiliated duplicates into their firmed record")

    # ---- Build JSON export (aggregated per person) ----
    people_rows = cur.execute(
        """
        SELECT p.id, p.name, p.role, f.name
        FROM people p LEFT JOIN firms f ON p.firm_id = f.id
        ORDER BY p.name
        """
    ).fetchall()

    people_out = []
    for pid, name, role, firm in people_rows:
        deal_rows = cur.execute(
            """
            SELECT d.id, d.headline, d.client, d.url, d.source, pd.client_override,
                   d.value_raw, d.value_currency, d.value_amount, d.value_inr, d.value_approx
            FROM deals d
            JOIN person_deals pd ON pd.deal_id = d.id
            WHERE pd.person_id = ?
            """,
            (pid,),
        ).fetchall()

        clients = []
        deals_list = []
        txn_types = set()
        practice_areas = set()
        for (did, headline, client, url, source, client_override,
             v_raw, v_cur, v_amt, v_inr, v_approx) in deal_rows:
            effective_client = client_override or client
            if effective_client and effective_client not in clients:
                clients.append(effective_client)
            types = [
                r[0]
                for r in cur.execute(
                    "SELECT type FROM deal_types WHERE deal_id = ?", (did,)
                ).fetchall()
            ]
            areas = [
                r[0]
                for r in cur.execute(
                    "SELECT area FROM deal_practice_areas WHERE deal_id = ?", (did,)
                ).fetchall()
            ]
            txn_types.update(types)
            practice_areas.update(areas)
            deals_list.append(
                {
                    "headline": headline,
                    "client": effective_client,
                    "url": url,
                    "source": source,
                    "types": types,
                    "practice_areas": areas,
                    "value": (
                        {
                            "raw": v_raw, "currency": v_cur, "amount": v_amt,
                            "amount_inr": v_inr, "approx": bool(v_approx),
                        }
                        if v_raw
                        else None
                    ),
                }
            )

        snapshot = shorten_snapshot(deals_list[0]["headline"]) if deals_list else None

        people_out.append(
            {
                "id": pid,
                "name": name,
                "firm": firm,
                "role": role,
                "role_bucket": role_bucket(role),
                "transaction_types": sorted(txn_types),
                "practice_areas": sorted(practice_areas),
                "clients": clients,
                "deals": deals_list,
                "snapshot": snapshot,
            }
        )

    # ---- Same-name-elsewhere cross-reference ----
    # We have no reliable date signal (the mbox "Date" header is the Google
    # Alert send date, not the deal date, and cannot be used to infer whether
    # a name change between firms is a real career move or two different
    # people who share a name). So we do NOT merge same-name records and do
    # NOT claim an ordering or a "likely a firm change" story from dates
    # alone. Where available, we attach a manually researched verdict from
    # firm_history_verification.json (LinkedIn / firm bios / legal press,
    # gathered and fact-checked by hand) — otherwise the record stays
    # explicitly unverified rather than assumed.
    by_name = {}
    for p in people_out:
        by_name.setdefault(p["name"].lower(), []).append(p)

    for name_key, group in by_name.items():
        if len(group) < 2:
            continue
        same_name = [
            {
                "id": p["id"],
                "firm": p["firm"],
                "role": p["role"],
                "deal_count": len(p["deals"]),
            }
            for p in group
        ]
        v = verification.get(group[0]["name"])
        for p in group:
            p["same_name_elsewhere"] = [s for s in same_name if s["id"] != p["id"]]
            p["firm_verification"] = v if v else {"verdict": "UNVERIFIED", "evidence": "Not yet researched."}

    firms_out = [
        r[0] for r in cur.execute("SELECT name FROM firms ORDER BY name").fetchall()
    ]

    # Firm hierarchy: firm -> practice area -> people, purely derived from
    # each person's aggregated practice areas (no extra data needed).
    firm_hierarchy = []
    for firm_name in firms_out:
        firm_people = [p for p in people_out if p["firm"] == firm_name]
        area_buckets = {}
        for p in firm_people:
            areas = p["practice_areas"] or ["Unclassified"]
            for area in areas:
                area_buckets.setdefault(area, []).append(p["id"])
        practice_group_list = [
            {"area": area, "person_ids": ids}
            for area, ids in sorted(area_buckets.items(), key=lambda kv: -len(kv[1]))
        ]
        firm_hierarchy.append(
            {
                "firm": firm_name,
                "people_count": len(firm_people),
                "practice_groups": practice_group_list,
            }
        )

    all_practice_areas = sorted(
        {a for p in people_out for a in p["practice_areas"]}
    )

    stats = {
        "people": len(people_out),
        "firms": len(firms_out),
        "deals": cur.execute("SELECT COUNT(*) FROM deals").fetchone()[0],
        "practice_areas": len(all_practice_areas),
    }

    JSON_PATH.write_text(
        json.dumps(
            {
                "stats": stats,
                "firms": firms_out,
                "practice_areas": all_practice_areas,
                "firm_hierarchy": firm_hierarchy,
                "people": people_out,
            },
            indent=2,
            ensure_ascii=False,
        )
    )

    conn.close()

    print(f"people: {stats['people']}  firms: {stats['firms']}  deals: {stats['deals']}")
    print(f"skipped malformed person names: {skipped_people}")
    print(f"personnel-move articles excluded from deal count: {skipped_personnel_moves}")
    print(f"wrote {DB_PATH.name} and {JSON_PATH.name}")


if __name__ == "__main__":
    main()
