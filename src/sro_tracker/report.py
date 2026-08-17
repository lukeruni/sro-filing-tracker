"""The weekly report: this week's filings, and how the week compared.

Two design decisions carry this module.

**Periods are measured by filing date, not by when we scraped.** The change log
knows when *we* learned something, which makes week-over-week comparison
meaningless after a backfill, a missed run, or a first install. Filing dates
belong to the filings, so "last week versus the week before" means the same
thing however often the scraper ran. Status transitions are the one exception -
a 2025 filing approved this morning is genuinely this week's news - so those are
read from the change log.

**The HTML is built for Outlook.** Desktop Outlook renders mail with Microsoft
Word, which ignores flexbox, grid, float, border-radius, background images and
anything in a <style> block. So the layout is nested tables with inline styles
and ``bgcolor`` attributes, fixed pixel widths, and padding on cells rather than
margins. Square corners are not a compromise here; they suit the typeface.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .store import Store

# ---------------------------------------------------------------------------
# Palette - shared with the dashboard and the workbook
# ---------------------------------------------------------------------------

INK = "#0B1F2A"
INK_SOFT = "#16323F"
PAPER = "#F4F7F9"
RULE = "#D7DEE6"
MUTED = "#5A6B7A"
FAINT = "#8A97A3"
ACCENT = "#0A66C2"
SIGNAL = "#00C2A8"
UP = "#1A7F37"
DOWN = "#CF222E"

STATUS_COLOURS = {
    "Filed": "#06806F",
    "Approved": "#1A7F37",
    "Immediately Effective": "#0969DA",
    "Notice": "#6639BA",
    "Amended": "#8250DF",
    "Period Extended": "#9A6700",
    "Proceedings Instituted": "#BC4C00",
    "Suspended": "#BC4C00",
    "Withdrawn": "#6E7781",
    "Disapproved": "#CF222E",
    "Unknown": "#6E7781",
}

MONO = "Consolas, 'Cascadia Mono', 'Courier New', monospace"
SANS = "'Segoe UI', -apple-system, Helvetica, Arial, sans-serif"

WIDTH = 860


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Snapshot:
    """One period of filing activity."""

    start: dt.date
    end: dt.date
    total: int = 0
    by_family: dict[str, int] = field(default_factory=dict)
    by_status: dict[str, int] = field(default_factory=dict)
    by_sro: dict[str, int] = field(default_factory=dict)
    filings: list[sqlite3.Row] = field(default_factory=list)

    @property
    def label(self) -> str:
        last = self.end - dt.timedelta(days=1)
        if self.start.year == last.year and self.start.month == last.month:
            return f"{self.start:%d}–{last:%d %b %Y}"
        return f"{self.start:%d %b} – {last:%d %b %Y}"

    @property
    def short_label(self) -> str:
        last = self.end - dt.timedelta(days=1)
        return f"{self.start:%d %b} – {last:%d %b}"


@dataclass(slots=True)
class WeeklyReport:
    current: Snapshot
    previous: Snapshot
    status_changes: list[tuple[sqlite3.Row, str, str]] = field(default_factory=list)
    total_tracked: int = 0
    generated_at: dt.datetime = field(default_factory=dt.datetime.now)
    days: int = 7

    # ---- derived ----

    @property
    def delta(self) -> int:
        return self.current.total - self.previous.total

    @property
    def delta_pct(self) -> float | None:
        if not self.previous.total:
            return None
        return (self.delta / self.previous.total) * 100

    @property
    def is_quiet(self) -> bool:
        return not self.current.filings and not self.status_changes

    @property
    def families(self) -> list[tuple[str, int, int]]:
        names = set(self.current.by_family) | set(self.previous.by_family)
        rows = [(n, self.current.by_family.get(n, 0), self.previous.by_family.get(n, 0))
                for n in names]
        return sorted(rows, key=lambda r: (-r[1], r[0]))

    @property
    def statuses(self) -> list[tuple[str, int, int]]:
        names = set(self.current.by_status) | set(self.previous.by_status)
        rows = [(n, self.current.by_status.get(n, 0), self.previous.by_status.get(n, 0))
                for n in names]
        return sorted(rows, key=lambda r: (-r[1], r[0]))

    @property
    def busiest(self) -> tuple[str, int] | None:
        if not self.current.by_sro:
            return None
        name, count = max(self.current.by_sro.items(), key=lambda kv: kv[1])
        return name, count

    @property
    def subject(self) -> str:
        window = self.current.short_label
        if self.is_quiet:
            return f"SRO rule filings — no activity ({window})"
        bits = [f"{self.current.total} new"]
        if self.delta:
            bits.append(f"{self.delta:+d} vs prior week")
        if self.status_changes:
            n = len(self.status_changes)
            bits.append(f"{n} status change{'' if n == 1 else 's'}")
        return f"SRO rule filings — {', '.join(bits)} ({window})"


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def _snapshot(store: Store, start: dt.date, end: dt.date, *, with_rows: bool) -> Snapshot:
    return Snapshot(
        start=start,
        end=end,
        total=store.count_in_period(start, end),
        by_family=store.breakdown_in_period(start, end, "sro_family"),
        by_status=store.breakdown_in_period(start, end, "status"),
        by_sro=store.breakdown_in_period(start, end, "sro"),
        filings=store.filings_in_period(start, end) if with_rows else [],
    )


def build(store: Store, *, days: int = 7, end: dt.date | None = None) -> WeeklyReport:
    """Assemble the current period and the one immediately before it.

    ``end`` is exclusive and defaults to tomorrow, so a report run at any hour
    today still includes filings dated today.
    """
    end = end or (dt.date.today() + dt.timedelta(days=1))
    current_start = end - dt.timedelta(days=days)
    previous_start = current_start - dt.timedelta(days=days)

    current = _snapshot(store, current_start, end, with_rows=True)
    previous = _snapshot(store, previous_start, current_start, with_rows=False)

    changes: list[tuple[sqlite3.Row, str, str]] = []
    seen: set[str] = set()
    window_start = dt.datetime.combine(current_start, dt.time.min)
    new_in_period = {r["filing_no"] for r in current.filings}

    for row in store.status_changes_in_window(window_start):
        if row["filing_no"] in seen or row["filing_no"] in new_in_period:
            # A filing that is already listed as new this week does not also
            # need a "status changed" line.
            continue
        try:
            payload = json.loads(row["changes"])
        except (TypeError, ValueError):
            continue
        if "status" not in payload:
            continue
        seen.add(row["filing_no"])
        before, after = payload["status"]
        changes.append((row, str(before or "—"), str(after or "—")))

    return WeeklyReport(
        current=current,
        previous=previous,
        status_changes=changes,
        total_tracked=store.count(),
        days=days,
    )


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------


def _esc(value: object) -> str:
    return html.escape(str(value or ""))


def _fmt_date(value: object) -> str:
    if not value:
        return "—"
    try:
        return dt.date.fromisoformat(str(value)[:10]).strftime("%d %b")
    except ValueError:
        return str(value)


def _chip(status: str) -> str:
    """Square status chip. Word ignores border-radius, so square it is."""
    colour = STATUS_COLOURS.get(status, FAINT)
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'style="display:inline-table"><tr>'
        f'<td bgcolor="{colour}" style="background:{colour};padding:3px 8px;'
        f'font-family:{SANS};font-size:10px;font-weight:700;color:#ffffff;'
        f'letter-spacing:.4px;white-space:nowrap;text-transform:uppercase">'
        f"{_esc(status)}</td></tr></table>"
    )


def _delta_cell(delta: int) -> str:
    if delta > 0:
        return f'<span style="color:{UP};font-weight:700">&#9650; +{delta}</span>'
    if delta < 0:
        return f'<span style="color:{DOWN};font-weight:700">&#9660; {delta}</span>'
    return f'<span style="color:{FAINT}">&mdash;</span>'


def _stat_tile(value: str, label: str, *, colour: str = "#FFFFFF",
               sub: str = "") -> str:
    return (
        f'<td width="25%" valign="top" style="padding:0 6px">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'bgcolor="{INK_SOFT}" style="background:{INK_SOFT}"><tr>'
        f'<td style="padding:16px 14px;border-left:3px solid {SIGNAL}">'
        f'<div style="font-family:{SANS};font-size:30px;line-height:1.05;'
        f'font-weight:700;color:{colour}">{value}</div>'
        f'<div style="font-family:{SANS};font-size:10px;letter-spacing:1.2px;'
        f'text-transform:uppercase;color:#8FA3B0;padding-top:6px">{_esc(label)}</div>'
        + (f'<div style="font-family:{SANS};font-size:11px;color:#B7C6D0;'
           f'padding-top:4px">{sub}</div>' if sub else "")
        + "</td></tr></table></td>"
    )


def _section(title: str, note: str = "") -> str:
    return (
        f'<tr><td style="padding:30px 0 10px">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td style="font-family:{SANS};font-size:12px;font-weight:700;color:{INK};'
        f'letter-spacing:1.6px;text-transform:uppercase;border-bottom:2px solid {INK};'
        f'padding-bottom:7px">{_esc(title)}</td>'
        + (f'<td align="right" style="font-family:{SANS};font-size:11px;color:{FAINT};'
           f'border-bottom:2px solid {INK};padding-bottom:7px">{_esc(note)}</td>'
           if note else "")
        + "</tr></table></td></tr>"
    )


_TH = (f"font-family:{SANS};font-size:10px;font-weight:700;color:{MUTED};"
       f"letter-spacing:1px;text-transform:uppercase;padding:9px 10px;"
       f"border-bottom:1px solid {RULE};text-align:left")
_TD = (f"font-family:{SANS};font-size:13px;color:{INK};padding:11px 10px;"
       f"border-bottom:1px solid {RULE};vertical-align:top")


def _comparison_table(report: WeeklyReport, rows: list[tuple[str, int, int]],
                      heading: str, *, colour_names: bool = False) -> str:
    if not rows:
        return ""
    head = (
        f'<tr><th style="{_TH}">{_esc(heading)}</th>'
        f'<th style="{_TH};text-align:center">{_esc(report.current.short_label)}</th>'
        f'<th style="{_TH};text-align:center">{_esc(report.previous.short_label)}</th>'
        f'<th style="{_TH};text-align:center">Change</th></tr>'
    )
    body = []
    for name, current, previous in rows:
        colour = STATUS_COLOURS.get(name, INK) if colour_names else INK
        weight = "700" if colour_names and name in STATUS_COLOURS else "400"
        body.append(
            f'<tr><td style="{_TD};color:{colour};font-weight:{weight}">{_esc(name)}</td>'
            f'<td style="{_TD};text-align:center;font-weight:700">{current}</td>'
            f'<td style="{_TD};text-align:center;color:{MUTED}">{previous}</td>'
            f'<td style="{_TD};text-align:center">{_delta_cell(current - previous)}</td></tr>'
        )
    return (
        '<tr><td><table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0">{head}{"".join(body)}</table></td></tr>'
    )


def _filings_table(rows: list[sqlite3.Row]) -> str:
    head = (
        f'<tr><th style="{_TH}">Filing</th><th style="{_TH}">SRO</th>'
        f'<th style="{_TH}">Date</th><th style="{_TH}">Status</th>'
        f'<th style="{_TH}">Summary</th></tr>'
    )
    body = []
    for row in rows:
        keys = row.keys()
        url = row["filing_url"] if "filing_url" in keys else ""
        number = _esc(row["filing_no"])
        link = (f'<a href="{_esc(url)}" style="color:{ACCENT};text-decoration:none;'
                f'font-weight:700">{number}</a>') if url else f"<strong>{number}</strong>"
        body.append(
            f'<tr><td style="{_TD};font-family:{MONO};font-size:12px;white-space:nowrap">{link}</td>'
            f'<td style="{_TD};white-space:nowrap">{_esc(row["sro"])}'
            f'<div style="font-size:11px;color:{FAINT}">{_esc(row["sro_family"])}</div></td>'
            f'<td style="{_TD};white-space:nowrap;color:{MUTED}">'
            f'{_esc(_fmt_date(row["filing_date"]))}</td>'
            f'<td style="{_TD}">{_chip(str(row["status"]))}</td>'
            f'<td style="{_TD};line-height:1.45">{_esc(row["summary"])}</td></tr>'
        )
    return ('<tr><td><table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0" border="0">{head}{"".join(body)}</table></td></tr>')


def render_html(report: WeeklyReport, *, cfg: Config | None = None,
                max_rows: int = 60) -> str:
    current, previous = report.current, report.previous
    pct = report.delta_pct
    trend = ""
    if pct is not None and previous.total:
        trend = f"{pct:+.0f}% vs prior week"
    elif previous.total == 0 and current.total:
        trend = "no activity in prior week"

    busiest = report.busiest
    approved = current.by_status.get("Approved", 0)

    parts: list[str] = [
        '<!doctype html><html><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>{_esc(report.subject)}</title></head>",
        f'<body style="margin:0;padding:0;background:{PAPER}">',
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'bgcolor="{PAPER}" style="background:{PAPER}"><tr><td align="center" '
        f'style="padding:26px 12px">',
        f'<table role="presentation" width="{WIDTH}" cellpadding="0" cellspacing="0" '
        f'border="0" style="width:{WIDTH}px;max-width:100%">',

        # ---- masthead ----
        f'<tr><td bgcolor="{INK}" style="background:{INK};padding:24px 26px 22px;'
        f'border-top:3px solid {SIGNAL}">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td style="font-family:{SANS};font-size:19px;font-weight:700;color:#ffffff;'
        f'letter-spacing:.3px">SRO RULE FILINGS</td>'
        f'<td align="right" style="font-family:{MONO};font-size:11px;color:{SIGNAL};'
        f'letter-spacing:1px">WEEKLY BRIEF</td></tr>'
        f'<tr><td colspan="2" style="font-family:{SANS};font-size:12.5px;color:#8FA3B0;'
        f'padding-top:6px">{_esc(current.label)} &nbsp;&middot;&nbsp; '
        f'{report.total_tracked:,} filings tracked</td></tr></table></td></tr>',

        # ---- tiles ----
        f'<tr><td bgcolor="{INK}" style="background:{INK};padding:0 20px 22px">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        + _stat_tile(str(current.total), "New this week", sub=trend)
        + _stat_tile(
            ("+" if report.delta > 0 else "") + str(report.delta) if report.delta else "0",
            "Vs prior week",
            colour=UP if report.delta > 0 else (DOWN if report.delta < 0 else "#FFFFFF"),
            sub=f"{previous.total} last week")
        + _stat_tile(str(approved), "Approved", sub="orders granted")
        + _stat_tile(str(len(report.status_changes)), "Status moves",
                     sub="on earlier filings")
        + "</tr></table></td></tr>",

        '<tr><td bgcolor="#FFFFFF" style="background:#ffffff;padding:4px 26px 30px">',
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">',
    ]

    if report.is_quiet:
        parts.append(
            f'<tr><td style="padding:34px 0;text-align:center;font-family:{SANS};'
            f'font-size:14px;color:{MUTED}">No filings were issued and no statuses '
            f"changed in this period.</td></tr>"
        )

    if busiest:
        parts.append(
            f'<tr><td style="padding:20px 0 0;font-family:{SANS};font-size:13px;'
            f'color:{MUTED};line-height:1.6">'
            f'Busiest SRO was <strong style="color:{INK}">{_esc(busiest[0])}</strong> '
            f"with {busiest[1]} filing{'' if busiest[1] == 1 else 's'}."
            f"</td></tr>"
        )

    if report.families:
        parts.append(_section("Week over week", "by SRO family"))
        parts.append(_comparison_table(report, report.families, "Family"))

    if report.statuses:
        parts.append(_section("Week over week", "by status"))
        parts.append(_comparison_table(report, report.statuses, "Status", colour_names=True))

    if current.filings:
        shown = current.filings[:max_rows]
        note = (f"{len(current.filings)} total"
                + (f", showing first {max_rows}" if len(current.filings) > max_rows else ""))
        parts.append(_section("New filings", note))
        parts.append(_filings_table(shown))
        if len(current.filings) > max_rows:
            parts.append(
                f'<tr><td style="padding:12px 10px;font-family:{SANS};font-size:12px;'
                f'color:{MUTED}">{len(current.filings) - max_rows} further filing(s) are in '
                f"the attached workbook.</td></tr>"
            )

    if report.status_changes:
        parts.append(_section("Status changes", "on filings from earlier periods"))
        head = (f'<tr><th style="{_TH}">Filing</th><th style="{_TH}">SRO</th>'
                f'<th style="{_TH}">Change</th><th style="{_TH}">Summary</th></tr>')
        body = []
        for row, before, after in report.status_changes:
            keys = row.keys()
            url = row["filing_url"] if "filing_url" in keys else ""
            number = _esc(row["filing_no"])
            link = (f'<a href="{_esc(url)}" style="color:{ACCENT};text-decoration:none;'
                    f'font-weight:700">{number}</a>') if url else f"<strong>{number}</strong>"
            body.append(
                f'<tr><td style="{_TD};font-family:{MONO};font-size:12px;'
                f'white-space:nowrap">{link}</td>'
                f'<td style="{_TD};white-space:nowrap">{_esc(row["sro"])}</td>'
                f'<td style="{_TD};white-space:nowrap">{_chip(before)}'
                f'<span style="color:{FAINT};padding:0 6px">&rarr;</span>{_chip(after)}</td>'
                f'<td style="{_TD};line-height:1.45">{_esc(row["summary"])}</td></tr>'
            )
        parts.append('<tr><td><table role="presentation" width="100%" cellpadding="0" '
                     f'cellspacing="0" border="0">{head}{"".join(body)}</table></td></tr>')

    dashboard = f"http://{cfg.host}:{cfg.port}/" if cfg else ""
    parts += [
        "</table></td></tr>",
        f'<tr><td bgcolor="{INK}" style="background:{INK};padding:16px 26px">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td style="font-family:{SANS};font-size:10.5px;color:#7E93A1;line-height:1.6">'
        f"Generated {report.generated_at:%d %b %Y at %H:%M} from SEC "
        f"Self-Regulatory Organization Rulemaking listings and exchange feeds."
        + (f'<br><a href="{_esc(dashboard)}" style="color:{SIGNAL};'
           f'text-decoration:none">{_esc(dashboard)}</a>' if dashboard else "")
        + "</td></tr></table></td></tr>",
        "</table></td></tr></table></body></html>",
    ]
    return "".join(parts)


# ---------------------------------------------------------------------------
# Plain text
# ---------------------------------------------------------------------------


def render_text(report: WeeklyReport) -> str:
    current, previous = report.current, report.previous
    lines = [
        report.subject,
        "=" * min(len(report.subject), 78),
        f"{current.label}  |  {report.total_tracked:,} filings tracked",
        "",
        f"  New this week   {current.total}",
        f"  Prior week      {previous.total}",
        f"  Change          {report.delta:+d}",
        f"  Status moves    {len(report.status_changes)}",
        "",
    ]

    if report.families:
        lines.append(f"{'BY FAMILY':<24}{'THIS':>7}{'PRIOR':>7}{'CHG':>7}")
        for name, now, before in report.families:
            lines.append(f"  {name:<22}{now:>7}{before:>7}{now - before:>+7}")
        lines.append("")

    if report.statuses:
        lines.append(f"{'BY STATUS':<24}{'THIS':>7}{'PRIOR':>7}{'CHG':>7}")
        for name, now, before in report.statuses:
            lines.append(f"  {name:<22}{now:>7}{before:>7}{now - before:>+7}")
        lines.append("")

    if current.filings:
        lines.append(f"NEW FILINGS ({len(current.filings)})")
        for row in current.filings:
            lines.append(f"  {row['filing_no']:<22} {_fmt_date(row['filing_date']):<8} "
                         f"{row['status']:<22} {row['sro']}")
            lines.append(f"      {str(row['summary'])[:104]}")
        lines.append("")

    if report.status_changes:
        lines.append(f"STATUS CHANGES ({len(report.status_changes)})")
        for row, before, after in report.status_changes:
            lines.append(f"  {row['filing_no']:<22} {before} -> {after}   {row['sro']}")
        lines.append("")

    if report.is_quiet:
        lines.append("No filings were issued and no statuses changed in this period.")

    return "\n".join(lines)


def write(report: WeeklyReport, cfg: Config) -> Path:
    """Write the HTML report to the export directory and return its path."""
    cfg.ensure_dirs()
    end = report.current.end - dt.timedelta(days=1)
    path = cfg.export_dir / f"weekly-report-{end:%Y%m%d}.html"
    path.write_text(render_html(report, cfg=cfg), encoding="utf-8")
    return path


def comparison_for_export(report: WeeklyReport):
    """Adapt the report into the workbook's Activity sheet model."""
    from .exports import PeriodComparison

    return PeriodComparison(
        current_label=report.current.short_label,
        previous_label=report.previous.short_label,
        current_total=report.current.total,
        previous_total=report.previous.total,
        by_family={n: (c, p) for n, c, p in report.families},
        by_status={n: (c, p) for n, c, p in report.statuses},
    )
