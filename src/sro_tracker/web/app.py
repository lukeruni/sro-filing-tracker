"""The dashboard: a local, server-rendered Flask app.

No build step, no bundler, no client framework. Jinja renders HTML and a small
amount of vanilla JS handles the refresh button. That choice is deliberate: this
has to still start in three years on a machine where nobody has run npm.

Only ``/api/refresh`` mutates anything, and it is POST-only.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading

from flask import Flask, jsonify, render_template, request, send_file

from .. import exports, registry, report as report_module
from ..config import Config
from ..models import COLUMNS
from ..store import Store

log = logging.getLogger(__name__)

# Guards against a second refresh starting while one is in flight. A refresh
# takes tens of seconds, and an impatient double-click should be a no-op rather
# than two crawls racing to commit.
_refresh_lock = threading.Lock()


def _store(cfg: Config) -> Store:
    return Store(cfg.db_path)


def _filters(args) -> dict[str, object]:
    year = args.get("year", type=int)
    return {
        "search": (args.get("q") or "").strip(),
        "sro": args.get("sro") or "",
        "family": args.get("family") or "",
        "status": args.get("status") or "",
        "year": year,
    }


def create_app(cfg: Config) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["CFG"] = cfg
    cfg.ensure_dirs()

    @app.get("/healthz")
    def healthz():
        """Readiness probe. The launcher polls this before opening a browser."""
        try:
            with _store(cfg) as store:
                count = store.count()
                last = store.last_run()
            return jsonify(
                ok=True,
                records=count,
                last_run=dict(last) if last else None,
                version=cfg.user_agent,
            )
        except Exception as exc:  # noqa: BLE001
            return jsonify(ok=False, error=str(exc)), 503

    @app.get("/")
    def index():
        filters = _filters(request.args)
        page = max(request.args.get("page", 1, type=int), 1)
        per_page = min(request.args.get("per_page", 100, type=int), 500)

        with _store(cfg) as store:
            total = store.count_matching(**filters)
            rows = store.query(**filters, limit=per_page, offset=(page - 1) * per_page)
            last_run = store.last_run()
            health = store.source_health(last_run["id"]) if last_run else []
            facets = {
                "sro": store.distinct("sro"),
                "family": store.distinct("sro_family"),
                "status": store.distinct("status"),
                "year": store.distinct("filing_year"),
            }
            by_family = store.summary_by("sro_family")
            by_status = store.summary_by("status")
            grand_total = store.count()

            # Week-over-week, measured by filing date so it means the same
            # thing regardless of when the scraper last ran.
            end = dt.date.today() + dt.timedelta(days=1)
            week_start = end - dt.timedelta(days=7)
            prior_start = week_start - dt.timedelta(days=7)
            this_week = store.count_in_period(week_start, end)
            last_week = store.count_in_period(prior_start, week_start)

        period = {
            "this_week": this_week,
            "last_week": last_week,
            "delta": this_week - last_week,
        }

        return render_template(
            "index.html",
            rows=rows,
            total=total,
            grand_total=grand_total,
            page=page,
            per_page=per_page,
            pages=max((total + per_page - 1) // per_page, 1),
            filters=filters,
            facets=facets,
            by_family=by_family,
            by_status=by_status,
            period=period,
            last_run=last_run,
            health=health,
            cfg=cfg,
            args=request.args,
        )

    @app.get("/filing/<path:filing_no>")
    def filing_detail(filing_no: str):
        with _store(cfg) as store:
            row = store.get(filing_no)
            if row is None:
                return render_template("not_found.html", filing_no=filing_no, cfg=cfg), 404
            history = store.history_for(filing_no)
        return render_template("detail.html", row=row, history=history, cfg=cfg)

    @app.get("/sources")
    def sources_page():
        from ..sources import exchange as edge

        with _store(cfg) as store:
            last_run = store.last_run()
            health = store.source_health(last_run["id"]) if last_run else []
            runs = store.recent_runs(15)
        return render_template(
            "sources.html",
            health=health,
            runs=runs,
            last_run=last_run,
            sros=registry.resolve(cfg.sros),
            edge_coverage=edge.coverage(),
            cfg=cfg,
        )

    # ---- API ----------------------------------------------------------

    @app.get("/api/filings")
    def api_filings():
        filters = _filters(request.args)
        limit = min(request.args.get("limit", 500, type=int), 5000)
        with _store(cfg) as store:
            rows = store.query(**filters, limit=limit)
        return jsonify(
            count=len(rows),
            columns=list(COLUMNS),
            filings=[{c: r[c] for c in COLUMNS if c in r.keys()} for r in rows],
        )

    @app.post("/api/refresh")
    def api_refresh():
        from .. import pipeline

        if not _refresh_lock.acquire(blocking=False):
            return jsonify(ok=False, error="A refresh is already running."), 409
        try:
            with _store(cfg) as store:
                result = pipeline.refresh(cfg, store)
            return jsonify(
                ok=result.ok,
                run_id=result.run_id,
                verdict=result.verdict.verdict,
                summary=result.verdict.summary(),
                added=result.added,
                changed=result.changed,
                committed=result.committed,
            )
        finally:
            _refresh_lock.release()

    @app.get("/export/<kind>")
    def export(kind: str):
        if kind not in {"csv", "xlsx"}:
            return jsonify(error="kind must be csv or xlsx"), 400
        filters = _filters(request.args)
        with _store(cfg) as store:
            rows = store.query(**filters)

        scope = filters["sro"] or filters["family"] or "all"
        stem = f"sro-filings-{str(scope).lower().replace(' ', '-')}"
        if kind == "csv":
            path = exports.to_csv(rows, exports.timestamped(cfg.export_dir, stem, ".csv"))
        else:
            path = exports.to_excel(rows, exports.timestamped(cfg.export_dir, stem, ".xlsx"))
        return send_file(path, as_attachment=True, download_name=path.name)

    @app.get("/report")
    def weekly_report():
        days = request.args.get("days", 7, type=int)
        with _store(cfg) as store:
            built = report_module.build(store, days=days)
        return report_module.render_html(built, cfg=cfg)

    # ---- template helpers ---------------------------------------------

    @app.template_filter("nicedate")
    def nicedate(value: object) -> str:
        if not value:
            return "—"
        text = str(value)
        try:
            return dt.date.fromisoformat(text[:10]).strftime("%d %b %Y")
        except ValueError:
            return text

    @app.template_filter("statusclass")
    def statusclass(value: object) -> str:
        return "s-" + str(value or "unknown").lower().replace(" ", "-")

    return app
