"""Role-class and US-location matching for job titles.

Kept deliberately regex-based and dependency-free: this is Stage 03 (Filter) of the
pipeline, which RESEARCH.md assigns to `code`, not to a model. Every decision here
must be inspectable and reproducible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# --------------------------------------------------------------------------
# Seniority exclusions
# --------------------------------------------------------------------------
# Applied to every role class. The Simplify feed is new-grad-curated but not
# perfectly so -- "Senior Staff Applications Engineer" appears in live rows.
# "Member of Technical Staff" is an entry-level title at Anthropic/OpenAI/Nutanix,
# so `staff` must not fire there -- hence the negative lookbehind.
_SENIOR = re.compile(
    r"""(?<!technical\s)\b(
        senior | sr\.? | staff | principal | lead | leader | director | manager
      | head\s+of | vp | vice\s+president | architect | fellow | distinguished
      | experienced | mid[\s-]?level | expert
    )\b""",
    re.I | re.X,
)

# Level suffixes: "Engineer II", "Engineer 3", "MTS 2" are not new grad.
# "Engineer I" IS entry level, so roman-numeral I alone must NOT match.
_LEVELED = re.compile(r"(?:\b(?:II|III|IV|V|VI)\b|\b[2-9]\b|\bL[3-9]\b)")

# Internships are a different product than new-grad full-time.
_INTERN = re.compile(r"\b(intern|internship|co[\s-]?op|apprentice|trainee|summer\s+20\d\d)\b", re.I)

# --------------------------------------------------------------------------
# Role class: SWE / SDE
# --------------------------------------------------------------------------
_SWE = re.compile(
    r"""(
        \bsoftware\s+(engineer|developer|development\s+engineer|engineering)
      | \bsde\b | \bswe\b
      | \bmember\s+of\s+technical\s+staff\b | \bmts\b
      | \b(back[\s-]?end|front[\s-]?end|full[\s-]?stack|web|mobile|ios|android
          |platform|infrastructure|cloud|systems?|embedded|distributed
          |application|product)\s+(engineer|developer)\b
      | \bprogrammer\b
      | \bdeveloper\s+(i\b|program|associate)
      | \bsoftware\s+development\b
      | \b(associate|junior|jr|graduate|entry[\s-]?level)\s*[-,]?\s*developer\b
      | \bdeveloper\b\s*[-,]\s*(information\s+technology|cyber|software)
    )""",
    re.I | re.X,
)

# --------------------------------------------------------------------------
# Role class: ML / AI / Data Science
# --------------------------------------------------------------------------
# Two-part match: a domain token AND a role noun. Avoids "AI" matching
# incidental words and avoids "Data Analyst" sliding in as "Data Scientist".
_ML_DOMAIN = re.compile(
    r"""\b(
        machine\s+learning | \bml\b | \bai\b | artificial\s+intelligence
      | deep\s+learning | neural | \bnlp\b | natural\s+language
      | computer\s+vision | \bcv\b | \bllm\b | generative\s+ai | gen\s?ai
      | reinforcement\s+learning | ml\s?ops | perception | robotics\s+learning
      | recommend(er|ation) | search\s+relevance | speech | forecasting
    )\b""",
    re.I | re.X,
)
_ROLE_NOUN = re.compile(r"\b(engineer|scientist|developer|researcher|engineering|research)\b", re.I)

# Strong standalone phrases that don't need the two-part test.
_DS_PHRASE = re.compile(
    r"""\b(
        data\s+scien(ce|tist) | applied\s+scien(ce|tist) | research\s+scien(ce|tist)
      | decision\s+scien(ce|tist) | quantitative\s+research(er)?
      | data\s+science\s+(engineer|analyst)
    )\b""",
    re.I | re.X,
)

# Explicitly NOT the ML/DS class even though they contain "data".
_DATA_NEGATIVE = re.compile(
    r"\b(data\s+(analyst|engineer|architect|entry|center|centre|governance|steward|quality)"
    r"|business\s+(analyst|intelligence)|mission\s+data|tax\s+technology)\b",
    re.I,
)


@dataclass(frozen=True)
class RoleMatch:
    swe: bool
    ml: bool
    excluded_by: str | None

    @property
    def any(self) -> bool:
        return (self.swe or self.ml) and self.excluded_by is None

    @property
    def classes(self) -> list[str]:
        if self.excluded_by:
            return []
        out = []
        if self.swe:
            out.append("swe")
        if self.ml:
            out.append("ml")
        return out


def classify_title(title: str) -> RoleMatch:
    """Classify a job title into the two target role classes."""
    t = (title or "").strip()
    if not t:
        return RoleMatch(False, False, "empty")
    # "Associate Software (Full-Stack) Developer" -- a parenthetical breaks the
    # adjacency of "software ... developer", so match the flattened form too.
    t_flat = re.sub(r"\s*\([^)]*\)\s*", " ", t).strip()

    if _INTERN.search(t):
        return RoleMatch(False, False, "intern")
    if _SENIOR.search(t):
        return RoleMatch(False, False, "senior")
    if _LEVELED.search(t):
        return RoleMatch(False, False, "leveled")

    swe = bool(_SWE.search(t) or _SWE.search(t_flat))

    ml = False
    if _DS_PHRASE.search(t) or _DS_PHRASE.search(t_flat):
        ml = True
    elif _ML_DOMAIN.search(t_flat) and _ROLE_NOUN.search(t_flat):
        ml = True
    if ml and _DATA_NEGATIVE.search(t) and not _DS_PHRASE.search(t):
        ml = False

    return RoleMatch(swe, ml, None)


# --------------------------------------------------------------------------
# US location matching
# --------------------------------------------------------------------------
# `locations` is unnormalised free text (RESEARCH.md: "No country field").
# 710 distinct strings across the live feed, in at least six shapes:
#   "San Jose, CA" | "NYC" | "California" | "Remote in USA" | "United States"
#   | "Toronto, ON, Canada"

_US_STATE_ABBR = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA",
    "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
    "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT",
    "VA","WA","WV","WI","WY","DC","PR",
}

_US_STATE_NAME = {
    "alabama","alaska","arizona","arkansas","california","colorado","connecticut",
    "delaware","florida","georgia","hawaii","idaho","illinois","indiana","iowa",
    "kansas","kentucky","louisiana","maine","maryland","massachusetts","michigan",
    "minnesota","mississippi","missouri","montana","nebraska","nevada",
    "new hampshire","new jersey","new mexico","new york","north carolina",
    "north dakota","ohio","oklahoma","oregon","pennsylvania","rhode island",
    "south carolina","south dakota","tennessee","texas","utah","vermont",
    "virginia","washington","west virginia","wisconsin","wyoming",
    "district of columbia","puerto rico",
}

# Bare city strings that appear without a state in the feed.
_US_CITY_BARE = {
    "nyc","sf","la","dc","san francisco","new york","new york city","los angeles",
    "bay area","silicon valley","seattle","boston","chicago","austin","denver",
    "atlanta","dallas","houston","miami","philadelphia","phoenix","portland",
    "san diego","san jose","pittsburgh","detroit","minneapolis","nashville",
    "charlotte","washington dc","washington d.c.","socal","bellevue","redmond",
    "sunnyvale","mountain view","palo alto","santa clara","cupertino","menlo park",
    "south sf","south san francisco","san mateo","santa monica","boulder","raleigh",
    "durham","columbus","indianapolis","kansas city","salt lake city","st louis",
    "tampa","orlando","sacramento","san antonio","las vegas","milwaukee","cleveland",
}

_NON_US = re.compile(
    r"""\b(
        canada | ontario | quebec | british\s+columbia | alberta | manitoba
      | saskatchewan | nova\s+scotia | toronto | vancouver | montreal | ottawa
      | calgary | waterloo,?\s*on | burnaby | mississauga | edmonton | winnipeg
      | united\s+kingdom | \buk\b | england | scotland | wales | london | cambridge,\s*uk
      | manchester | edinburgh | glasgow | bristol | dublin | ireland
      | india | bangalore | bengaluru | hyderabad | pune | chennai | mumbai
      | delhi | noida | gurgaon | gurugram | kolkata | ahmedabad
      | germany | berlin | munich | m\u00fcnchen | hamburg | frankfurt | cologne
      | france | paris | lyon | toulouse | italy | milan | rome | spain | madrid
      | barcelona | netherlands | amsterdam | eindhoven | belgium | brussels
      | poland | warsaw | krakow | krak\u00f3w | wroclaw | portugal | lisbon | porto
      | switzerland | zurich | z\u00fcrich | geneva | austria | vienna
      | sweden | stockholm | denmark | copenhagen | norway | oslo | finland | helsinki
      | czech | prague | romania | bucharest | hungary | budapest | bulgaria
      | greece | athens | turkey | istanbul | ukraine | serbia | croatia | estonia
      | lithuania | latvia | slovakia | slovenia | luxembourg | iceland | malta
      | israel | tel\s?aviv | haifa | jerusalem
      | uae | dubai | abu\s+dhabi | qatar | doha | saudi | riyadh | egypt | cairo
      | singapore | japan | tokyo | osaka | china | beijing | shanghai | shenzhen
      | hong\s?kong | taiwan | taipei | korea | seoul | vietnam | hanoi
      | philippines | manila | thailand | bangkok | malaysia | kuala\s+lumpur
      | indonesia | jakarta | australia | sydney | melbourne | brisbane | perth
      | new\s+zealand | auckland | wellington
      | brazil | sao\s+paulo | s\u00e3o\s+paulo | mexico | guadalajara | argentina
      | buenos\s+aires | chile | santiago | colombia | bogota | bogot\u00e1 | peru | lima
      | costa\s+rica | panama | uruguay
      | south\s+africa | cape\s+town | johannesburg | nigeria | lagos | kenya | nairobi
      | morocco | tunisia | ghana | pakistan | karachi | lahore | islamabad
      | bangladesh | dhaka | sri\s+lanka | nepal | russia | moscow
      | europe | emea | apac | latam | anz | \bglobal\b | worldwide
      | \bon\b,?\s*canada | ,\s*(ON|BC|AB|QC|MB|SK|NS|NB|NL|PE)\s*$
    )\b""",
    re.I | re.X,
)

_US_EXPLICIT = re.compile(
    r"\b(united\s+states(\s+of\s+america)?|u\.?s\.?a\.?|\bus\b|stateside|nationwide)\b",
    re.I,
)

_COMMA_STATE = re.compile(r",\s*([A-Z]{2})\b\s*$")


def is_us_location(loc: str) -> bool | None:
    """True = US, False = definitely not US, None = unrecognised."""
    s = (loc or "").strip()
    if not s:
        return None

    if _NON_US.search(s):
        return False
    if _US_EXPLICIT.search(s):
        return True

    m = _COMMA_STATE.search(s)
    if m and m.group(1).upper() in _US_STATE_ABBR:
        return True

    norm = re.sub(r"^remote\s+(in|-)?\s*", "", s, flags=re.I).strip().strip(",").lower()
    norm = re.sub(r"[.\u200b]", "", norm)
    if norm in _US_STATE_NAME or norm in _US_CITY_BARE:
        return True
    if len(norm) == 2 and norm.upper() in _US_STATE_ABBR:
        return True
    # "Austin, TX, USA" style trailing country already caught by _US_EXPLICIT.
    for part in (p.strip().lower() for p in s.split(",")):
        if part in _US_STATE_NAME or part in _US_CITY_BARE:
            return True
    return None


def listing_in_us(locations: list[str] | None) -> bool:
    """A listing counts as US if any of its locations resolves to the US."""
    for loc in locations or []:
        if is_us_location(loc) is True:
            return True
    return False
