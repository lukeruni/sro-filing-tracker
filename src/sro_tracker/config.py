"""Configuration: file, then environment, then defaults.

No identity, contact address, credential or corporate path is ever written into
source. Everything an operator must supply lives here and is validated by
``sro-tracker doctor`` before a run is allowed to touch the network.

Precedence (highest first):
    1. environment variables  (SRO_TRACKER_*)
    2. config file            (config.toml, or --config PATH)
    3. built-in defaults
"""

from __future__ import annotations

import dataclasses
import os
import sys
import tomllib
from pathlib import Path

APP_NAME = "sro-filing-tracker"
VERSION = "1.0.0"

# The SEC's automated-access policy requires a declared User-Agent carrying a
# real contact address. Requests without one are refused (HTTP 403), which is
# exactly what we saw during design. There is deliberately no default value:
# a placeholder would get the operator rate-limited under someone else's name.
CONTACT_ENV = "SRO_TRACKER_CONTACT"


def _project_root() -> Path:
    """Repository root when run from a clone, else the current directory."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd()


@dataclasses.dataclass(slots=True)
class Config:
    # --- identity -------------------------------------------------------
    contact: str = ""
    """Contact address embedded in the User-Agent. Required. See CONTACT_ENV."""

    # --- paths ----------------------------------------------------------
    root: Path = dataclasses.field(default_factory=_project_root)
    data_dir: Path | None = None
    export_dir: Path | None = None
    log_dir: Path | None = None

    # --- network --------------------------------------------------------
    request_timeout: float = 30.0
    max_retries: int = 4
    backoff_base: float = 0.8
    rate_limit_per_sec: float = 5.0
    """SEC fair-access allows 10 req/s. Half that is polite and still fast."""

    ca_bundle: str = ""
    """Path to a corporate root CA bundle. Empty means use certifi defaults.

    This exists so a TLS-inspecting proxy can be trusted *properly*. Certificate
    verification is never disabled; see http.py.
    """
    proxy: str = ""

    # --- scope ----------------------------------------------------------
    years: tuple[int, ...] = ()
    """Filing years to track. Empty means 'current and previous year'."""

    sros: tuple[str, ...] = ()
    """SRO keys to track. Empty means every SRO in the registry."""

    enable_exchange_sources: bool = True
    """Tier-2 edge adapters. Disable for a spine-only run."""

    # --- quality gate ---------------------------------------------------
    min_records: int = 50
    max_shrink_ratio: float = 0.10
    """Refuse a commit that drops more than this fraction of known records."""

    # --- web ------------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 5057
    """Deliberately not 5000/5001, so this can never collide with an older app."""

    # --- reporting ------------------------------------------------------
    mail_transport: str = "file"       # file | smtp | outlook
    mail_from: str = ""
    mail_to: tuple[str, ...] = ()
    smtp_host: str = ""
    smtp_port: int = 25
    smtp_use_tls: bool = True

    # ---- derived paths -------------------------------------------------

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.data_dir = Path(self.data_dir) if self.data_dir else self.root / "data"
        self.export_dir = Path(self.export_dir) if self.export_dir else self.root / "exports"
        self.log_dir = Path(self.log_dir) if self.log_dir else self.root / "logs"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "filings.db"

    @property
    def user_agent(self) -> str:
        return f"{APP_NAME}/{VERSION} ({self.contact})"

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.export_dir, self.log_dir):
            path.mkdir(parents=True, exist_ok=True)

    def target_years(self) -> tuple[int, ...]:
        if self.years:
            return tuple(sorted(set(self.years)))
        import datetime as _dt

        current = _dt.date.today().year
        return (current - 1, current)

    # ---- validation ----------------------------------------------------

    def problems(self) -> list[str]:
        """Blocking issues. A non-empty list means we must not hit the network."""
        issues: list[str] = []
        if not self.contact.strip():
            issues.append(
                "No contact address set. The SEC requires a User-Agent with a real "
                f"contact. Set {CONTACT_ENV} or 'contact' in config.toml."
            )
        elif "@" not in self.contact and "http" not in self.contact.lower():
            issues.append(
                f"contact={self.contact!r} does not look like an email address or URL."
            )
        if self.ca_bundle and not Path(self.ca_bundle).exists():
            issues.append(f"ca_bundle path does not exist: {self.ca_bundle}")
        if self.mail_transport not in {"file", "smtp", "outlook"}:
            issues.append(f"mail_transport must be file|smtp|outlook, got {self.mail_transport!r}")
        if self.mail_transport == "smtp" and not self.smtp_host:
            issues.append("mail_transport=smtp but smtp_host is empty.")
        if not 0 < self.max_shrink_ratio < 1:
            issues.append("max_shrink_ratio must be between 0 and 1.")
        return issues

    def warnings(self) -> list[str]:
        """Non-blocking advisories."""
        out: list[str] = []
        if self.host not in {"127.0.0.1", "localhost"}:
            out.append(
                f"host={self.host!r} binds beyond loopback. The bundled server is for "
                "local use only; put a real server in front before exposing it."
            )
        if self.rate_limit_per_sec > 8:
            out.append("rate_limit_per_sec above 8 risks tripping SEC fair-access limits.")
        return out


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _coerce(raw: str, field: dataclasses.Field) -> object:
    t = field.type
    if t is bool or t == "bool":
        low = raw.strip().lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise ValueError(f"{field.name}: expected a boolean, got {raw!r}")
    if t is int or t == "int":
        return int(raw)
    if t is float or t == "float":
        return float(raw)
    if "tuple[int" in str(t):
        return tuple(int(p) for p in raw.replace(",", " ").split())
    if "tuple[str" in str(t):
        return tuple(p.strip() for p in raw.split(",") if p.strip())
    return raw


def load(config_path: str | Path | None = None, **overrides: object) -> Config:
    """Build a Config from file + environment + explicit overrides."""
    values: dict[str, object] = {}
    fields = {f.name: f for f in dataclasses.fields(Config)}

    # 1. file
    path = Path(config_path) if config_path else _project_root() / "config.toml"
    if path.exists():
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
        for section in raw.values():
            if isinstance(section, dict):
                for key, value in section.items():
                    if key in fields:
                        values[key] = value
        for key, value in raw.items():
            if key in fields and not isinstance(value, dict):
                values[key] = value

    # 2. environment
    for name, field in fields.items():
        env_key = f"SRO_TRACKER_{name.upper()}"
        if env_key in os.environ:
            values[name] = _coerce(os.environ[env_key], field)
    # Convenience aliases for the two most commonly set values.
    if CONTACT_ENV in os.environ:
        values["contact"] = os.environ[CONTACT_ENV]
    if "REQUESTS_CA_BUNDLE" in os.environ and "ca_bundle" not in values:
        values["ca_bundle"] = os.environ["REQUESTS_CA_BUNDLE"]

    # 3. explicit
    for key, value in overrides.items():
        if value is not None and key in fields:
            values[key] = value

    # Normalize list-ish values that TOML gives us as lists.
    for key in ("years", "sros", "mail_to"):
        if key in values and isinstance(values[key], list):
            values[key] = tuple(values[key])

    return Config(**values)  # type: ignore[arg-type]


def describe(cfg: Config) -> str:
    """Human-readable summary for `doctor`, with nothing sensitive echoed."""
    lines = [
        f"{APP_NAME} {VERSION}",
        f"  python        {sys.version.split()[0]}",
        f"  root          {cfg.root}",
        f"  database      {cfg.db_path}",
        f"  exports       {cfg.export_dir}",
        f"  contact       {cfg.contact or '(UNSET)'}",
        f"  user-agent    {cfg.user_agent}",
        f"  years         {', '.join(str(y) for y in cfg.target_years())}",
        f"  sros          {', '.join(cfg.sros) if cfg.sros else '(all)'}",
        f"  edge sources  {'enabled' if cfg.enable_exchange_sources else 'disabled'}",
        f"  ca bundle     {cfg.ca_bundle or '(system default)'}",
        f"  proxy         {cfg.proxy or '(none)'}",
        f"  server        http://{cfg.host}:{cfg.port}/",
        f"  mail          {cfg.mail_transport}",
    ]
    return "\n".join(lines)
