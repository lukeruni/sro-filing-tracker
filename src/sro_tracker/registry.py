"""The SRO registry: the single source of truth for *what* we track.

Every SRO is addressed on sec.gov in one of two ways, both of which render the
identical filings table:

  * most exchanges  - ``.../national-securities-exchanges/all-years?sro_organization=<id>``
  * FINRA and peers - a dedicated path such as ``.../self-regulatory-organization-rulemaking/finra``

The numeric ids are stable Drupal term ids and were read off the SEC's own index
page. ``code`` is the token that appears inside a filing number
(``SR-<code>-<year>-<seq>``); it is what lets a filing scraped from an exchange
site be matched back to its registry entry.

Adding an SRO is a one-line change here. No parser edits, no new module.
"""

from __future__ import annotations

import dataclasses

# Families group SROs that share an operator, which is how regulation staff
# actually think about them and how the dashboard is organised.
FAMILY_NYSE = "NYSE"
FAMILY_NASDAQ = "Nasdaq"
FAMILY_CBOE = "Cboe"
FAMILY_MIAX = "MIAX"
FAMILY_FINRA = "FINRA"
FAMILY_INDEPENDENT = "Independent"

SEC_BASE = "https://www.sec.gov/rules-regulations/self-regulatory-organization-rulemaking"
SEC_EXCHANGES_PATH = f"{SEC_BASE}/national-securities-exchanges/all-years"


@dataclasses.dataclass(frozen=True, slots=True)
class Sro:
    key: str
    """Stable short slug used in config, CLI flags and URLs."""

    name: str
    """Display name."""

    code: str
    """Token inside SR-<code>-<year>-<seq>."""

    family: str

    sec_org_id: int | None = None
    """Drupal term id for the exchanges listing."""

    sec_path: str | None = None
    """Dedicated SEC path, for SROs not on the exchanges listing."""

    aliases: tuple[str, ...] = ()
    """Other codes that have referred to this SRO, including former names."""

    core: bool = True
    """Part of the default tracking scope."""

    def listing_url(self, year: int | str = "All") -> str:
        """URL for this SRO's filings, optionally scoped to one year."""
        if self.sec_path:
            return f"{self.sec_path}?year={year}&month=All"
        return (
            f"{SEC_EXCHANGES_PATH}?sro_organization={self.sec_org_id}"
            f"&year={year}&month=All"
        )

    @property
    def match_codes(self) -> tuple[str, ...]:
        return (self.code.upper(),) + tuple(a.upper() for a in self.aliases)


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------
# `core=True` marks the working scope for US equities/options rule filings.
# Everything else is registered but off by default, so widening scope is a
# config change rather than a code change.

SROS: tuple[Sro, ...] = (
    # ---- NYSE ----------------------------------------------------------
    Sro("nyse",        "New York Stock Exchange",  "NYSE",     FAMILY_NYSE, 192816),
    Sro("nyse-arca",   "NYSE Arca",                "NYSEARCA", FAMILY_NYSE, 192821),
    Sro("nyse-amer",   "NYSE American",            "NYSEAMER", FAMILY_NYSE, 192826,
        aliases=("NYSEAmer", "NYSEMKT", "Amex")),
    Sro("nyse-natl",   "NYSE National",            "NYSENAT",  FAMILY_NYSE, 193066),
    Sro("nyse-chx",    "NYSE Chicago",             "NYSECHX",  FAMILY_NYSE, 192831, aliases=("CHX",)),
    Sro("nyse-texas",  "NYSE Texas",               "NYSETEX",  FAMILY_NYSE, 344836),

    # ---- Nasdaq --------------------------------------------------------
    Sro("nasdaq",      "The Nasdaq Stock Market",  "NASDAQ",   FAMILY_NASDAQ, 192811),
    Sro("nasdaq-bx",   "Nasdaq BX",                "BX",       FAMILY_NASDAQ, 192786, aliases=("NASDAQBX",)),
    Sro("nasdaq-phlx", "Nasdaq PHLX",              "Phlx",     FAMILY_NASDAQ, 192806, aliases=("PHLX", "NASDAQPHLX")),
    Sro("nasdaq-ise",  "Nasdaq ISE",               "ISE",      FAMILY_NASDAQ, 192796),
    Sro("nasdaq-gemx", "Nasdaq GEMX",              "GEMX",     FAMILY_NASDAQ, 192791),
    Sro("nasdaq-mrx",  "Nasdaq MRX",               "MRX",      FAMILY_NASDAQ, 192801),
    Sro("nasdaq-texas", "Nasdaq Texas",            "NASDAQTX", FAMILY_NASDAQ, 354086, core=False),

    # ---- Cboe ----------------------------------------------------------
    Sro("cboe",        "Cboe Exchange (Options)",  "CBOE",     FAMILY_CBOE, 192751),
    Sro("cboe-c2",     "Cboe C2 Exchange",         "C2",       FAMILY_CBOE, 192736, aliases=("CboeC2",)),
    Sro("cboe-bzx",    "Cboe BZX Exchange",        "CboeBZX",  FAMILY_CBOE, 192731, aliases=("BATS", "BZX")),
    Sro("cboe-byx",    "Cboe BYX Exchange",        "CboeBYX",  FAMILY_CBOE, 192726, aliases=("BYX",)),
    Sro("cboe-edga",   "Cboe EDGA Exchange",       "CboeEDGA", FAMILY_CBOE, 192741, aliases=("EDGA",)),
    Sro("cboe-edgx",   "Cboe EDGX Exchange",       "CboeEDGX", FAMILY_CBOE, 192746, aliases=("EDGX",)),

    # ---- MIAX ----------------------------------------------------------
    Sro("miax",        "MIAX Options",             "MIAX",     FAMILY_MIAX, 192771),
    Sro("miax-pearl",  "MIAX Pearl",               "PEARL",    FAMILY_MIAX, 192781, aliases=("MIAXPEARL",)),
    Sro("miax-emerald", "MIAX Emerald",            "EMERALD",  FAMILY_MIAX, 192776, aliases=("MIAXEMERALD",)),
    Sro("miax-sapphire", "MIAX Sapphire",          "SAPPHIRE", FAMILY_MIAX, 335656, aliases=("MIAXSAPPHIRE",)),

    # ---- Independent ---------------------------------------------------
    Sro("memx",        "MEMX",                     "MEMX",     FAMILY_INDEPENDENT, 192766),
    Sro("ltse",        "Long-Term Stock Exchange", "LTSE",     FAMILY_INDEPENDENT, 192761),
    Sro("iex",         "Investors Exchange",       "IEX",      FAMILY_INDEPENDENT, 192756),
    Sro("txse",        "Texas Stock Exchange",     "TXSE",     FAMILY_INDEPENDENT, 350456),
    Sro("gix",         "Green Impact Exchange",    "GIX",      FAMILY_INDEPENDENT, 345326),
    Sro("box",         "BOX Exchange",             "BOX",      FAMILY_INDEPENDENT, 192721),
    Sro("24x",         "24X National Exchange",    "24X",      FAMILY_INDEPENDENT, 344566, core=False),
    Sro("mx2",         "MX2",                      "MX2",      FAMILY_INDEPENDENT, 344571, core=False),

    # ---- FINRA (dedicated path) ---------------------------------------
    Sro("finra",       "FINRA",                    "FINRA",    FAMILY_FINRA,
        sec_path=f"{SEC_BASE}/finra", aliases=("NASD",)),
)

BY_KEY: dict[str, Sro] = {s.key: s for s in SROS}

_BY_CODE: dict[str, Sro] = {}
for _sro in SROS:
    for _code in _sro.match_codes:
        _BY_CODE.setdefault(_code, _sro)


def all_sros() -> tuple[Sro, ...]:
    return SROS


def core_sros() -> tuple[Sro, ...]:
    return tuple(s for s in SROS if s.core)


def get(key: str) -> Sro:
    try:
        return BY_KEY[key]
    except KeyError:
        raise KeyError(
            f"unknown SRO {key!r}. Known keys: {', '.join(sorted(BY_KEY))}"
        ) from None


def by_code(code: str) -> Sro | None:
    """Resolve the token from a filing number back to a registry entry."""
    return _BY_CODE.get(code.upper())


def resolve(keys: tuple[str, ...] | list[str] | None) -> tuple[Sro, ...]:
    """Select SROs from config. Empty means the core scope.

    Accepts SRO keys and family names, so ``--sro Cboe`` selects all six Cboe
    markets.
    """
    if not keys:
        return core_sros()
    selected: list[Sro] = []
    seen: set[str] = set()
    families = {f.lower(): f for f in
                (FAMILY_NYSE, FAMILY_NASDAQ, FAMILY_CBOE, FAMILY_MIAX,
                 FAMILY_FINRA, FAMILY_INDEPENDENT)}
    for raw in keys:
        token = raw.strip()
        if not token:
            continue
        low = token.lower()
        if low == "all":
            return SROS

        # An explicit family request, e.g. "family:NYSE". Needed because some
        # names are both a key and a family: "nyse" is the New York Stock
        # Exchange itself *and* the label for its six markets. A bare name means
        # the specific SRO; only this prefix means the whole family.
        if low.startswith("family:"):
            wanted = low.split(":", 1)[1]
            if wanted not in families:
                raise KeyError(
                    f"unknown family {wanted!r}. Known families: "
                    f"{', '.join(sorted(families.values()))}"
                )
            for s in SROS:
                if s.family == families[wanted] and s.key not in seen:
                    seen.add(s.key)
                    selected.append(s)
            continue

        # Exact SRO keys take precedence over family names.
        sro = BY_KEY.get(low) or by_code(token)
        if sro is None and low in families:
            for s in SROS:
                if s.family == families[low] and s.key not in seen:
                    seen.add(s.key)
                    selected.append(s)
            continue
        if sro is None:
            raise KeyError(
                f"unknown SRO or family {raw!r}. "
                f"Known keys: {', '.join(sorted(BY_KEY))}"
            )
        if sro.key not in seen:
            seen.add(sro.key)
            selected.append(sro)
    return tuple(selected)
