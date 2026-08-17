"""The weekly report: what changed, not what exists.

A full dump of every filing is not a report - nobody reads it. What regulation
staff need on a Monday is the delta: what appeared, and what moved. The
append-only change log makes that exact rather than approximate; the report
asks the store what changed, instead of re-deriving it by comparing snapshots.

Output is a self-contained HTML document with inline styles, because every mail
client strips <style> blocks and none of them fetch external CSS.
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

_STATUS_COLOURS = {
    "Approved": "#1a7f37",
    "Immediately Effective": "#0969da",
    "Notice": "#6639ba",
    "Amended": "#8250df",
    "Period Extended": "#9a6700",
    "Proceedings Instituted": "#bc4c00",
    "Suspended": "#bc4c00",
    "Withdrawn": "#6e7781",
    "Disapproved": "#cf222e",
    "Unknown": "#6e7781",
}


# A filing discovered this week but dated well before it is a backfill, not
# news - the first run of a fresh install discovers thousands at once. Anything
# dated within this many days of the window still counts as news, because a
# filing the team has not seen before is worth reporting even if the SEC posted
# it a fortnight ago.
BACKFILL_GRACE_DAYS = 21


@dataclass(slots=True)
class WeeklyReport:
    since: dt.datetime
    until: dt.datetime
    added: list[sqlite3.Row] = field(default_factory=list)
    status_changes: list[tuple[sqlite3.Row, str, str]] = field(default_factory=list)
    other_changes: list[sqlite3.Row] = field(default_factory=list)
    backfilled: int = 0
    """Older filings first recorded in this window - counted, not listed."""
    total_tracked: int = 0

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.status_changes or self.other_changes)

    @property
    def subject(self) -> str:
        window = self.since.strftime("%d %b") + " - " + self.until.strftime("%d %b %Y")
        if self.is_empty:
            return f"SRO rule filings - no changes ({window})"
        bits = []
        if self.added:
            bits.append(f"{len(self.added)} new")
        if self.status_changes:
            bits.append(f"{len(self.status_changes)} status change"
                        f"{'s' if len(self.status_changes) != 1 else ''}")
        return f"SRO rule filings - {', '.join(bits)} ({window})"


def build(store: Store, *, days: int = 7) -> WeeklyReport:
    """Assemble the report from the change log."""
    until = dt.datetime.now()
    since = until - dt.timedelta(days=days)

    report = WeeklyReport(since=since, until=until, total_tracked=store.count())
    seen_added: set[str] = set()
    seen_changed: set[str] = set()

    news_floor = (since - dt.timedelta(days=BACKFILL_GRACE_DAYS)).date()

    for row in store.changes_since(since):
        if row["change_type"] == "added":
            if row["filing_no"] in seen_added:
                continue
            seen_added.add(row["filing_no"])
            filed = row["filing_date"]
            try:
                is_news = not filed or dt.date.fromisoformat(str(filed)[:10]) >= news_floor
            except ValueError:
                is_news = True
            if is_news:
                report.added.append(row)
            else:
                report.backfilled += 1
            continue

        if row["filing_no"] in seen_added or row["filing_no"] in seen_changed:
            continue
        try:
            changes = json.loads(row["changes"])
        except (TypeError, ValueError):
            changes = {}
        seen_changed.add(row["filing_no"])
        if "status" in changes:
            before, after = changes["status"]
            report.status_changes.append((row, str(before or ""), str(after or "")))
        else:
            report.other_changes.append(row)

    report.added.sort(key=lambda r: (r["filing_date"] or "", r["filing_no"]), reverse=True)
    return report


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _esc(value: object) -> str:
    return html.escape(str(value or ""))


def _badge(status: str) -> str:
    colour = _STATUS_COLOURS.get(status, "#6e7781")
    return (
        f'<span style="display:inline-block;padding:2px 8px;border-radius:10px;'
        f'background:{colour};color:#fff;font-size:11px;font-weight:600;'
        f'white-space:nowrap">{_esc(status)}</span>'
    )


def _link(row: sqlite3.Row) -> str:
    url = row["filing_url"] if "filing_url" in row.keys() else ""
    label = _esc(row["filing_no"])
    if not url:
        return f"<strong>{label}</strong>"
    return (f'<a href="{_esc(url)}" style="color:#0969da;text-decoration:none;'
            f'font-weight:600">{label}</a>')


def _table(rows_html: list[str], headers: list[str]) -> str:
    head = "".join(
        f'<th style="text-align:left;padding:8px 10px;border-bottom:2px solid #d0d7de;'
        f'font-size:12px;color:#57606a;text-transform:uppercase;letter-spacing:.03em">'
        f"{_esc(h)}</th>"
        for h in headers
    )
    return (
        '<table cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;'
        f'margin:0 0 26px"><thead><tr>{head}</tr></thead><tbody>'
        + "".join(rows_html)
        + "</tbody></table>"
    )


_CELL = "padding:9px 10px;border-bottom:1px solid #eaeef2;font-size:13px;vertical-align:top"


def render_html(report: WeeklyReport, *, cfg: Config | None = None) -> str:
    window = (f"{report.since.strftime('%d %B %Y')} to "
              f"{report.until.strftime('%d %B %Y')}")
    parts = [
        '<!doctype html><html><head><meta charset="utf-8">',
        f"<title>{_esc(report.subject)}</title></head>",
        '<body style="margin:0;padding:24px;background:#f6f8fa;'
        'font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#1f2328">',
        '<div style="max-width:940px;margin:0 auto;background:#fff;border:1px solid #d0d7de;'
        'border-radius:8px;padding:28px">',
        '<h1 style="margin:0 0 4px;font-size:21px">SRO Rule Filings - Weekly Summary</h1>',
        f'<p style="margin:0 0 22px;color:#57606a;font-size:13px">{_esc(window)} '
        f"&nbsp;·&nbsp; {report.total_tracked:,} filings tracked</p>",
    ]

    if report.is_empty:
        parts.append(
            '<p style="padding:22px;background:#f6f8fa;border-radius:6px;color:#57606a;'
            'margin:0">No new filings or status changes in this period.</p>'
        )

    if report.added:
        parts.append(f'<h2 style="font-size:15px;margin:0 0 10px">New filings '
                     f'({len(report.added)})</h2>')
        rows = [
            f'<tr><td style="{_CELL}white-space:nowrap">{_link(row)}</td>'
            f'<td style="{_CELL}white-space:nowrap">{_esc(row["sro"])}</td>'
            f'<td style="{_CELL}white-space:nowrap">{_esc(row["filing_date"])}</td>'
            f'<td style="{_CELL}">{_badge(row["status"])}</td>'
            f'<td style="{_CELL}">{_esc(row["summary"])}</td></tr>'
            for row in report.added
        ]
        parts.append(_table(rows, ["Filing", "SRO", "Date", "Status", "Summary"]))

    if report.status_changes:
        parts.append(f'<h2 style="font-size:15px;margin:0 0 10px">Status changes '
                     f'({len(report.status_changes)})</h2>')
        rows = [
            f'<tr><td style="{_CELL}white-space:nowrap">{_link(row)}</td>'
            f'<td style="{_CELL}white-space:nowrap">{_esc(row["sro"])}</td>'
            f'<td style="{_CELL}white-space:nowrap">{_badge(before)}'
            f'<span style="color:#8c959f;margin:0 6px">&rarr;</span>{_badge(after)}</td>'
            f'<td style="{_CELL}">{_esc(row["summary"])}</td></tr>'
            for row, before, after in report.status_changes
        ]
        parts.append(_table(rows, ["Filing", "SRO", "Change", "Summary"]))

    if report.other_changes:
        parts.append(f'<h2 style="font-size:15px;margin:0 0 10px">Amended details '
                     f'({len(report.other_changes)})</h2>')
        rows = [
            f'<tr><td style="{_CELL}white-space:nowrap">{_link(row)}</td>'
            f'<td style="{_CELL}white-space:nowrap">{_esc(row["sro"])}</td>'
            f'<td style="{_CELL}">{_esc(row["summary"])}</td></tr>'
            for row in report.other_changes
        ]
        parts.append(_table(rows, ["Filing", "SRO", "Summary"]))

    if report.backfilled:
        parts.append(
            f'<p style="margin:0 0 22px;padding:11px 14px;background:#f6f8fa;'
            f'border-radius:6px;color:#57606a;font-size:12.5px">'
            f"{report.backfilled:,} older filing(s) were also recorded this period "
            f"while backfilling history. They are excluded above to keep this "
            f"summary to genuine activity.</p>"
        )

    dashboard = f"http://{cfg.host}:{cfg.port}/" if cfg else ""
    parts.append(
        '<hr style="border:0;border-top:1px solid #eaeef2;margin:26px 0 14px">'
        '<p style="margin:0;color:#8c959f;font-size:11px">'
        "Generated from SEC self-regulatory organization rulemaking listings"
        + (f' &nbsp;·&nbsp; <a href="{_esc(dashboard)}" style="color:#57606a">{_esc(dashboard)}</a>'
           if dashboard else "")
        + "</p></div></body></html>"
    )
    return "".join(parts)


def render_text(report: WeeklyReport) -> str:
    """Plain-text alternative, for clients that refuse HTML."""
    lines = [
        report.subject,
        "=" * len(report.subject),
        f"{report.since:%d %b %Y} to {report.until:%d %b %Y} | "
        f"{report.total_tracked:,} filings tracked",
        "",
    ]
    if report.is_empty:
        lines.append("No new filings or status changes in this period.")
    if report.added:
        lines.append(f"NEW FILINGS ({len(report.added)})")
        for row in report.added:
            lines.append(f"  {row['filing_no']:<22} {row['filing_date'] or '':<12} "
                         f"{row['status']:<22} {row['sro']}")
            lines.append(f"      {row['summary'][:110]}")
        lines.append("")
    if report.status_changes:
        lines.append(f"STATUS CHANGES ({len(report.status_changes)})")
        for row, before, after in report.status_changes:
            lines.append(f"  {row['filing_no']:<22} {before} -> {after}   {row['sro']}")
        lines.append("")
    if report.backfilled:
        lines.append(f"({report.backfilled:,} older filings also recorded while "
                     f"backfilling history; excluded above.)")
    return "\n".join(lines)


def write(report: WeeklyReport, cfg: Config) -> Path:
    """Write the HTML report to the export directory and return its path."""
    cfg.ensure_dirs()
    name = f"weekly-report-{report.until:%Y%m%d}.html"
    path = cfg.export_dir / name
    path.write_text(render_html(report, cfg=cfg), encoding="utf-8")
    return path
