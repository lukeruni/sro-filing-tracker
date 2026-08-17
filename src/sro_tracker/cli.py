"""Command line interface.

One entry point for every context - interactive use, the scheduled task, and
CI - so the thing you test is the thing that runs at 6am.

Exit codes are meaningful, because a scheduler can only see the number:

    0  success
    1  a run was rejected, or a command failed
    2  success with warnings (degraded sources)
    3  configuration is invalid; nothing was attempted
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from . import config, exports, mail, pipeline, quality, registry, report as report_module
from .config import APP_NAME, VERSION, Config
from .store import Store

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_WARN = 2
EXIT_CONFIG = 3


def _log_setup(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def _load(args: argparse.Namespace) -> Config:
    overrides: dict[str, object] = {}
    if getattr(args, "contact", None):
        overrides["contact"] = args.contact
    if getattr(args, "years", None):
        overrides["years"] = tuple(args.years)
    if getattr(args, "sro", None):
        overrides["sros"] = tuple(args.sro)
    if getattr(args, "port", None):
        overrides["port"] = args.port
    if getattr(args, "no_edge", False):
        overrides["enable_exchange_sources"] = False
    return config.load(getattr(args, "config", None), **overrides)


def _require_valid(cfg: Config) -> None:
    problems = cfg.problems()
    if problems:
        print("Configuration is not usable:\n", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print("\nRun 'sro-tracker doctor' for detail.", file=sys.stderr)
        raise SystemExit(EXIT_CONFIG)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = _load(args)
    print(config.describe(cfg))
    print()

    problems = cfg.problems()
    warnings = cfg.warnings()

    sros = registry.resolve(cfg.sros)
    from .sources import exchange as edge

    coverage = edge.coverage()
    with_feed = sum(1 for s in sros if coverage.get(s.key))
    print(f"  scope         {len(sros)} SRO(s); {with_feed} with a freshness feed, "
          f"{len(sros) - with_feed} authoritative-only")

    if cfg.db_path.exists():
        with Store(cfg.db_path) as store:
            last = store.last_run()
            print(f"  database      {store.count():,} filings")
            if last:
                print(f"  last run      #{last['id']} {last['started_at']} -> {last['outcome']}")
    else:
        print("  database      not created yet (run 'sro-tracker refresh')")

    print()
    if not problems and not args.offline:
        print("Checking connectivity to sec.gov …")
        from .http import Client, FetchError

        try:
            with Client(cfg) as client:
                response = client.get(registry.get("nyse").listing_url(2026))
            print(f"  OK  reachable, HTTP {response.status}, {response.elapsed:.1f}s")
        except FetchError as exc:
            print(f"  FAIL {exc}")
            problems = list(problems) + ["sec.gov is not reachable with this configuration."]

    if warnings:
        print("\nWarnings:")
        for warning in warnings:
            print(f"  ! {warning}")
    if problems:
        print("\nProblems:")
        for problem in problems:
            print(f"  x {problem}")
        return EXIT_CONFIG
    print("\nAll checks passed.")
    return EXIT_WARN if warnings else EXIT_OK


def cmd_refresh(args: argparse.Namespace) -> int:
    cfg = _load(args)
    _require_valid(cfg)

    def progress(result) -> None:
        mark = {"ok": "  ok", "degraded": "  ~~", "failed": "  XX"}.get(result.status, "  ??")
        print(f"{mark} {result.source:<24} {len(result.filings):>4} rec  {result.duration:5.1f}s")

    with Store(cfg.db_path) as store:
        report = pipeline.refresh(cfg, store, progress=progress if not args.quiet else None)

    print()
    print(report.render())

    if not report.ok:
        return EXIT_FAIL
    return EXIT_WARN if report.verdict.verdict == quality.VERDICT_PASS_WITH_WARNINGS else EXIT_OK


def cmd_serve(args: argparse.Namespace) -> int:
    cfg = _load(args)
    _require_valid(cfg)
    from .web.app import create_app

    app = create_app(cfg)
    url = f"http://{cfg.host}:{cfg.port}/"

    if args.open:
        # Open the browser only once the server answers /healthz. Opening it
        # first is what produces a spurious "connection refused" page and a
        # support ticket that says the app is broken.
        threading.Thread(target=_open_when_ready, args=(url,), daemon=True).start()

    print(f"{APP_NAME} {VERSION}")
    print(f"  serving  {url}")
    print(f"  database {cfg.db_path}")
    print("  Ctrl+C to stop")
    app.run(host=cfg.host, port=cfg.port, debug=False, use_reloader=False, threaded=True)
    return EXIT_OK


def _open_when_ready(url: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    probe = url.rstrip("/") + "/healthz"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(probe, timeout=2) as response:
                if response.status == 200:
                    webbrowser.open(url)
                    return
        except (urllib.error.URLError, OSError):
            time.sleep(0.3)
    print(f"  (server did not become ready within {timeout:.0f}s; open {url} manually)")


def cmd_export(args: argparse.Namespace) -> int:
    cfg = _load(args)
    with Store(cfg.db_path) as store:
        if store.count() == 0:
            print("No data to export. Run 'sro-tracker refresh' first.", file=sys.stderr)
            return EXIT_FAIL
        rows = store.query(
            search=args.search or "",
            sro=args.sro_filter or "",
            family=args.family or "",
            status=args.status or "",
            year=args.year,
        )

    if not rows:
        print("No filings matched those filters.", file=sys.stderr)
        return EXIT_FAIL

    stem = "sro-filings"
    if args.output:
        path = Path(args.output)
        writer = exports.to_excel if path.suffix.lower() == ".xlsx" else exports.to_csv
    elif args.format == "xlsx":
        path = exports.timestamped(cfg.export_dir, stem, ".xlsx")
        writer = exports.to_excel
    else:
        path = exports.timestamped(cfg.export_dir, stem, ".csv")
        writer = exports.to_csv

    written = writer(rows, path)
    print(f"Wrote {len(rows):,} filings to {written}")
    return EXIT_OK


def cmd_report(args: argparse.Namespace) -> int:
    cfg = _load(args)
    with Store(cfg.db_path) as store:
        built = report_module.build(store, days=args.days)

    print(report_module.render_text(built))
    path = report_module.write(built, cfg)
    print(f"HTML report: {path}")

    attachment = None
    if args.attach:
        with Store(cfg.db_path) as store:
            rows = store.query(since=built.since.date())
        if rows:
            attachment = exports.to_excel(
                rows, exports.timestamped(cfg.export_dir, "weekly-filings", ".xlsx"))
            print(f"Attachment:  {attachment}")

    if args.send and not cfg.mail_to:
        print("Refusing to send: mail_to is empty.", file=sys.stderr)
        return EXIT_FAIL

    try:
        delivery = mail.deliver(
            cfg,
            subject=built.subject,
            html_body=report_module.render_html(built, cfg=cfg),
            text_body=report_module.render_text(built),
            attachment=attachment,
            send=args.send,
        )
    except mail.MailError as exc:
        print(f"Delivery failed: {exc}", file=sys.stderr)
        return EXIT_FAIL

    print(delivery.render())
    return EXIT_OK


def cmd_sources(args: argparse.Namespace) -> int:
    cfg = _load(args)
    from .sources import exchange as edge

    coverage = edge.coverage()
    sros = registry.resolve(cfg.sros) if not args.all else registry.all_sros()
    print(f"{'KEY':<16} {'CODE':<12} {'FAMILY':<14} {'FEED':<6} NAME")
    print("-" * 78)
    for sro in sros:
        feed = "yes" if coverage.get(sro.key) else "-"
        print(f"{sro.key:<16} {sro.code:<12} {sro.family:<14} {feed:<6} {sro.name}")
    print(f"\n{len(sros)} SRO(s). All are covered authoritatively by the SEC source; "
          f"'feed' marks those with an additional same-day exchange feed.")
    return EXIT_OK


def cmd_validate(args: argparse.Namespace) -> int:
    """Post-refresh sanity checks, suitable for a release gate."""
    cfg = _load(args)
    failures: list[str] = []
    checks: list[tuple[str, str]] = []

    if not cfg.db_path.exists():
        print("No database. Run 'sro-tracker refresh' first.", file=sys.stderr)
        return EXIT_FAIL

    with Store(cfg.db_path) as store:
        count = store.count()
        checks.append(("database has records", f"{count:,}"))
        if count < cfg.min_records:
            failures.append(f"only {count} records, below min_records={cfg.min_records}")

        last = store.last_run()
        checks.append(("last run outcome", last["outcome"] if last else "none"))
        if not last or last["outcome"] == "rejected":
            failures.append("the most recent run did not commit")

        undated = len(store.query(search="")) - len(
            [r for r in store.query() if r["filing_date"]])
        checks.append(("records with a date", f"{count - undated:,} of {count:,}"))
        if count and undated / count > 0.05:
            failures.append(f"{undated} records have no filing date")

        unknown = dict(store.summary_by("status")).get("Unknown", 0)
        checks.append(("status derived", f"{count - unknown:,} of {count:,}"))
        if count and unknown / count > 0.25:
            failures.append(f"{unknown} records have an Unknown status")

        families = store.summary_by("sro_family")
        checks.append(("families represented", ", ".join(f"{f} {n}" for f, n in families)))

    width = max(len(name) for name, _ in checks)
    for name, value in checks:
        print(f"  {name:<{width}}  {value}")

    if failures:
        print("\nFAILED:")
        for failure in failures:
            print(f"  x {failure}")
        return EXIT_FAIL
    print("\nAll validation checks passed.")
    return EXIT_OK


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sro-tracker",
        description="Track SEC self-regulatory organization rule filings.",
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--config", metavar="PATH", help="path to config.toml")
    parser.add_argument("--contact", metavar="EMAIL",
                        help="contact address for the User-Agent (required by the SEC)")

    subs = parser.add_subparsers(dest="command", required=True)

    p = subs.add_parser("doctor", help="check configuration and connectivity")
    p.add_argument("--offline", action="store_true", help="skip the network check")
    p.set_defaults(func=cmd_doctor)

    p = subs.add_parser("refresh", help="fetch, reconcile and commit filings")
    p.add_argument("--years", nargs="+", type=int, metavar="YEAR")
    p.add_argument("--sro", nargs="+", metavar="KEY",
                   help="SRO keys or 'family:NYSE'; default is the core scope")
    p.add_argument("--no-edge", action="store_true",
                   help="authoritative sources only; skip exchange feeds")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_refresh)

    p = subs.add_parser("serve", help="run the local dashboard")
    p.add_argument("--port", type=int)
    p.add_argument("--open", action="store_true",
                   help="open a browser once the server is ready")
    p.set_defaults(func=cmd_serve)

    p = subs.add_parser("export", help="write filings to CSV or Excel")
    p.add_argument("--format", choices=("csv", "xlsx"), default="xlsx")
    p.add_argument("--output", "-o", metavar="PATH")
    p.add_argument("--search", metavar="TEXT")
    p.add_argument("--sro-filter", metavar="NAME")
    p.add_argument("--family", metavar="NAME")
    p.add_argument("--status", metavar="STATUS")
    p.add_argument("--year", type=int)
    p.set_defaults(func=cmd_export)

    p = subs.add_parser("report", help="build the weekly change report")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--attach", action="store_true", help="build an Excel attachment")
    p.add_argument("--send", action="store_true",
                   help="deliver via the configured transport (default is preview only)")
    p.set_defaults(func=cmd_report)

    p = subs.add_parser("sources", help="list configured SROs and coverage")
    p.add_argument("--all", action="store_true", help="include out-of-scope SROs")
    p.set_defaults(func=cmd_sources)

    p = subs.add_parser("validate", help="post-refresh sanity checks")
    p.set_defaults(func=cmd_validate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _log_setup(args.verbose)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return EXIT_FAIL
    except KeyError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_CONFIG


if __name__ == "__main__":
    raise SystemExit(main())
