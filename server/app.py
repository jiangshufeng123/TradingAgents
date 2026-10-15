"""The web API (FastAPI). Start with ``tradingagents server run``; interactive docs at /docs.

Every route under /api except /api/health and /api/auth/login needs a signed-in
user: the ``ta_session`` cookie a browser gets at sign-in, or
``Authorization: Bearer <token>`` for other clients. Requests that change
something and are authenticated by the cookie must carry an ``X-TA-Client``
header, which a page on another site cannot add (cross-site request forgery).

Users see and change only their own watchlist, book, paper account, jobs and
costs. Administrators also manage accounts and data sources. API keys and the
Tushare token stay in .env and are never served or editable here.
"""

from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from server import db
from server.models import Job, Schedule, User
from server.services import auth, costs, jobs, portfolio as portfolio_svc, users, watchlist
from server.tickers import canonical as canonical_ticker, canonicalize_stored

COOKIE = "ta_session"
CLIENT_HEADER = "x-ta-client"
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
TICKER = re.compile(r"^[A-Za-z0-9.\-^=]{1,20}$")


# --- request bodies ---------------------------------------------------------------

class LoginIn(BaseModel):
    username: str
    password: str


class TickersIn(BaseModel):
    tickers: list[str]
    note: str = ""


class CashIn(BaseModel):
    cash: float | None
    currency: str = "CNY"


class HoldingIn(BaseModel):
    quantity: float
    average_price: float | None = None


class AnalysisIn(BaseModel):
    tickers: list[str] = Field(default_factory=list, description="Empty: the whole watchlist")
    runs: int | None = Field(None, ge=1, le=5)
    date: str | None = Field(None, description="YYYY-MM-DD; default today")


class ScreenIn(BaseModel):
    request: str = ""
    preset: str = Field("", description="Rank by this preset (tradingagents/scoring.py); alone, its filters too")
    spec: dict | None = Field(None, description="Edited conditions (as returned on a screen job)")
    where: str | None = None
    limit: int | None = Field(None, ge=1, le=200)


class ReviewIn(BaseModel):
    rows: list[int] = Field(default_factory=list, description="Row numbers (from 1); empty: the top ``top``")
    top: int = Field(5, ge=1, le=10)


class PickIn(BaseModel):
    rows: list[int] = Field(..., description="Row numbers in the screen's result, from 1")


class ScheduleIn(BaseModel):
    enabled: bool
    time_of_day: str = "18:30"
    days: str = "mon-fri"
    runs: int | None = Field(None, ge=1, le=5)        # None: follow TRADINGAGENTS_VOTE_RUNS


class PaperOpenIn(BaseModel):
    cash: float = Field(1_000_000, gt=0, le=1e11)
    currency: str = Field("CNY", pattern="^(CNY|HKD|USD)$")
    restart: bool = Field(False, description="Start over when an account already exists")


class PaperSwitchIn(BaseModel):
    enabled: bool


class PasswordIn(BaseModel):
    old_password: str
    new_password: str


class UserIn(BaseModel):
    username: str
    password: str
    role: str = "user"
    monthly_quota: float | None = None


class UserPatch(BaseModel):
    is_active: bool | None = None
    role: str | None = None
    monthly_quota: float | None = None
    clear_quota: bool = False
    password: str | None = None


# --- helpers ----------------------------------------------------------------------

def _clean_tickers(raw: list[str]) -> list[str]:
    """Split, check and spell each ticker the way the analysis records it (603980.SH -> 603980.SS)."""
    out = []
    for t in raw:
        for part in str(t).replace(";", ",").split(","):
            part = part.strip().upper()
            if not part:
                continue
            if not TICKER.match(part):
                raise HTTPException(422, f"not a ticker: {part!r}")
            out.append(canonical_ticker(part))
    return list(dict.fromkeys(out))


def _iso(value) -> str | None:
    return value.isoformat(timespec="seconds") if isinstance(value, datetime) else value


def _user_out(u: User) -> dict:
    return {"id": u.id, "username": u.username, "display_name": u.display_name, "role": u.role,
            "is_active": u.is_active, "monthly_quota": u.monthly_quota, "created_at": _iso(u.created_at)}


def _job_out(j: Job, detail: bool = False) -> dict:
    out = {"id": j.id, "serial": j.serial, "kind": j.kind, "status": j.status, "params": j.params,
           "created_at": _iso(j.created_at), "started_at": _iso(j.started_at),
           "finished_at": _iso(j.finished_at), "cost": j.cost,
           "error": (j.error or "").split("\n\n")[0] or None}
    if j.status in ("running", "cancelling") and j.log_path:
        from server.runner import progress

        try:
            out["progress"] = progress(Path(j.log_path).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            out["progress"] = None
    if detail:
        out["result"] = j.result
    return out


def _norm(ticker: str) -> str:
    from tradingagents.dataflows.symbols import normalize_symbol

    try:
        return str(normalize_symbol((ticker or "").strip())).upper()
    except Exception:
        return (ticker or "").strip().upper()


def create_app(config: dict | None = None, *, start_worker: bool = True, start_scheduler: bool = True,
               cookie_secure: bool | None = None) -> FastAPI:
    from tradingagents.default_config import DEFAULT_CONFIG

    config = config or DEFAULT_CONFIG
    secure = cookie_secure if cookie_secure is not None else \
        os.environ.get("TRADINGAGENTS_COOKIE_SECURE", "").lower() in ("1", "true", "yes")
    throttle = auth.Throttle()
    state: dict[str, Any] = {"worker": None, "scheduler": None}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.init_db()
        with db.session() as s:
            auth.purge_expired(s)
            canonicalize_stored(s)
        if start_worker:
            from server.worker import Worker

            state["worker"] = Worker(config)
            state["worker"].start()
        if start_scheduler:
            from server.scheduler import DailyScheduler

            state["scheduler"] = DailyScheduler(on_queued=_wake)
            state["scheduler"].start()
        yield
        if state["scheduler"]:
            state["scheduler"].stop()
        if state["worker"]:
            state["worker"].stop()

    def _wake() -> None:
        if state["worker"]:
            state["worker"].wake()

    app = FastAPI(title="TradingAgents", version="0.1", lifespan=lifespan)
    app.state.ta = state

    from fastapi.responses import PlainTextResponse as _Text

    from server.access import allowed_networks, is_allowed

    nets = allowed_networks()

    @app.middleware("http")
    async def only_allowed_networks(request: Request, call_next):
        """Refuse clients outside this computer, the home network and Tailscale (see server/access.py)."""
        if not is_allowed(request.client.host if request.client else None, nets):
            return _Text("403: this service only accepts this computer, the home network and Tailscale",
                         status_code=403)
        return await call_next(request)

    # --- authentication --------------------------------------------------------------

    def current_user(request: Request) -> User:
        header = request.headers.get("authorization", "")
        bearer = header[7:].strip() if header.lower().startswith("bearer ") else None
        token = bearer or request.cookies.get(COOKIE)
        if bearer is None and token and request.method in MUTATING and not request.headers.get(CLIENT_HEADER):
            raise HTTPException(403, f"missing {CLIENT_HEADER} header")
        with db.session() as s:
            user = auth.resolve(s, token)
        if user is None:
            raise HTTPException(401, "not signed in")
        return user

    def admin_user(user: User = Depends(current_user)) -> User:
        if user.role != "admin":
            raise HTTPException(403, "administrators only")
        return user

    def _decisions(s, user: User, limit: int = 30) -> list[dict]:
        """Each stock's latest decision, newest first: from this user's cost ledger (every run the
        CLI or the web made for them; an administrator also sees the pre-web ledger), linked to the
        web job that produced it when there is one. One row per listing (600519.SS = 600519.SH)."""
        from sqlalchemy import select

        from server.runner import ledger_rows, reports_base, user_reports_dir

        paths = [user_reports_dir(config, user) / "cost_ledger.csv"]
        if user.role == "admin":
            paths.append(reports_base(config) / "cost_ledger.csv")
        rows = [r for path in paths for r in ledger_rows(path)]
        job_of = {}
        for j in s.scalars(select(Job).where(Job.user_id == user.id, Job.kind == "analysis", Job.status == "done")
                           .order_by(Job.id.desc()).limit(100)):
            for i, r in enumerate((j.result or {}).get("runs") or []):
                job_of[(_norm(r.get("ticker")), r.get("run_time"))] = (j.id, i)

        def num(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        out, seen = [], set()
        for r in sorted(rows, key=lambda r: r.get("run_time") or "", reverse=True):
            key = _norm(r.get("ticker"))
            if not key or key in seen:
                continue
            seen.add(key)
            job = job_of.get((key, r.get("run_time")))
            out.append({"ticker": r.get("ticker"), "rating": r.get("rating"), "action": r.get("action") or None,
                        "target_weight": num(r.get("target_weight")), "stop": num(r.get("stop")),
                        "vote_ratings": r.get("vote_ratings"), "trade_date": r.get("trade_date"),
                        "run_time": r.get("run_time"), "cost": num(r.get("cost")),
                        "job_id": job[0] if job else None, "index": job[1] if job else None,
                        "report_dir": r.get("report_dir") or None})
            if len(out) >= limit:
                break
        return out

    def _report_path(user: User, report_dir: str, fmt: str) -> Path:
        from server.runner import reports_base, user_reports_dir

        base = (reports_base(config) if user.role == "admin" else user_reports_dir(config, user)).resolve()
        name = "complete_report.html" if fmt == "html" else "complete_report.md"
        path = (Path(report_dir) / name).resolve()
        if base not in path.parents or not path.exists():
            raise HTTPException(404, "report file not found")
        return path

    @app.get("/api/report")
    def report_by_dir(dir: str, fmt: str = "html", user: User = Depends(current_user)):
        """A saved report by its folder (decisions made outside a web job carry only the folder)."""
        path = _report_path(user, dir, fmt)
        return FileResponse(path, media_type="text/html" if fmt == "html" else "text/markdown")

    from server.version import fingerprint, settings

    started = {"at": _iso(datetime.now()), "code": fingerprint(), "settings": settings(config)}

    @app.get("/api/health")
    def health():
        """Also which code and settings this service started with (`tradingagents server selfcheck`)."""
        return {"ok": True, "time": _iso(datetime.now()), "started": started["at"],
                "code": started["code"], "settings": started["settings"]}

    @app.post("/api/auth/login")
    def login(body: LoginIn, request: Request, response: Response):
        ip = request.client.host if request.client else "?"
        keys = (f"user:{body.username.strip().lower()}", f"ip:{ip}")
        wait = throttle.blocked(*keys)
        if wait:
            raise HTTPException(429, f"too many failed sign-ins; try again in {wait / 60:.0f} min")
        with db.session() as s:
            user = users.authenticate(s, body.username, body.password)
            if user is None:
                throttle.failed(*keys)
                raise HTTPException(401, "wrong username or password")
            throttle.succeeded(*keys)
            token = auth.create(s, user, request.headers.get("user-agent", ""))
        response.set_cookie(COOKIE, token, httponly=True, samesite="lax", secure=secure,
                            max_age=auth.SESSION_DAYS * 86400)
        return {"user": _user_out(user), "token": token}

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response, user: User = Depends(current_user)):
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else request.cookies.get(COOKIE)
        with db.session() as s:
            auth.revoke(s, token)
        response.delete_cookie(COOKIE)
        return {"ok": True}

    @app.post("/api/auth/password")
    def change_password(body: PasswordIn, user: User = Depends(current_user)):
        with db.session() as s:
            me = s.get(User, user.id)
            if users.authenticate(s, me.username, body.old_password) is None:
                raise HTTPException(400, "the current password is wrong")
            try:
                users.set_password(s, me, body.new_password)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
            auth.revoke_user(s, me.id)
        return {"ok": True, "note": "signed out everywhere; sign in again"}

    @app.get("/api/me")
    def me(user: User = Depends(current_user)):
        return _user_out(user)

    # --- overview ---------------------------------------------------------------------

    @app.get("/api/overview")
    def overview(user: User = Depends(current_user)):
        with db.session() as s:
            recent = [_job_out(j) for j in jobs.recent(s, user.id, 10)]
            active = [_job_out(j) for j in jobs.active(s, user.id)]
            spend = costs.month_spend(s, user.id)
            n_watch = len(watchlist.tickers(s, user.id))
            sched = s.get(Schedule, user.id)
            decisions = _decisions(s, user)
        nxt = state["scheduler"].next_run(user.id) if state["scheduler"] else None
        return {
            "user": _user_out(user), "watchlist_count": n_watch, "active_jobs": active, "recent_jobs": recent,
            "month_spend": spend, "monthly_quota": user.monthly_quota, "price_currency":
                config.get("price_currency") or "", "decisions": decisions,
            "schedule": {"enabled": bool(sched and sched.enabled), "time_of_day": sched.time_of_day if sched else None,
                         "days": sched.days if sched else None, "next_run": _iso(nxt.replace(tzinfo=None)) if nxt else None},
            "worker": {"running": state["worker"] is not None, "current_job": getattr(state["worker"], "current", None)},
        }

    @app.get("/api/balance")
    def balance(user: User = Depends(current_user)):
        from tradingagents import llm_balance

        provider = (config.get("quick_think_provider") or config.get("llm_provider") or "").lower()
        if not llm_balance.supported(provider):
            return {"provider": provider, "ok": False, "error": "this provider publishes no balance"}
        out = llm_balance.balance(provider)
        # Below the alert line (TRADINGAGENTS_BALANCE_ALERT), every page reminds to top up.
        try:
            alert = float(config.get("balance_alert") or 0)
        except (TypeError, ValueError):
            alert = 0.0
        totals = [b.get("total") for b in (out.get("balances") or []) if b.get("total") is not None]
        out["alert_at"] = alert or None
        out["low"] = bool(out.get("ok") and alert > 0 and totals and max(totals) < alert)
        return out

    @app.get("/api/names")
    def security_names(t: str = "", user: User = Depends(current_user)):
        """Names for up to 60 comma-separated tickers: {ticker: name or null}."""
        from tradingagents.dataflows.config import run_config
        from tradingagents.names import names_for

        tickers = [x.strip().upper() for x in t.split(",") if x.strip()][:60]
        if any(not TICKER.match(x) for x in tickers):
            raise HTTPException(422, "not a ticker list")
        with run_config(config):
            return names_for(tickers)

    @app.get("/api/datasources")
    def datasources(user: User = Depends(current_user)):
        from tradingagents.datagw import gateway

        return {"sources": gateway(config).status()}

    @app.post("/api/datasources/reset")
    def datasources_reset(user: User = Depends(admin_user)):
        from tradingagents.datagw import gateway

        gateway(config).health.reset()
        return {"ok": True}

    # --- watchlist --------------------------------------------------------------------

    @app.get("/api/watchlist")
    def watchlist_get(user: User = Depends(current_user)):
        with db.session() as s:
            return [{"ticker": i.ticker, "note": i.note, "source": i.source, "added": _iso(i.created_at)}
                    for i in watchlist.items(s, user.id)]

    @app.post("/api/watchlist")
    def watchlist_add(body: TickersIn, user: User = Depends(current_user)):
        with db.session() as s:
            added = watchlist.add(s, user.id, _clean_tickers(body.tickers), note=body.note)
        return {"added": added}

    @app.delete("/api/watchlist/{ticker}")
    def watchlist_remove(ticker: str, user: User = Depends(current_user)):
        with db.session() as s:
            gone = watchlist.remove(s, user.id, _clean_tickers([ticker]))
        if not gone:
            raise HTTPException(404, "not on the watchlist")
        return {"removed": gone}

    # --- real book --------------------------------------------------------------------

    @app.get("/api/portfolio")
    def portfolio_get(user: User = Depends(current_user)):
        from server.models import PortfolioCash

        with db.session() as s:
            cash = s.get(PortfolioCash, user.id)
            return {"cash": cash.cash if cash else None, "currency": cash.currency if cash else "CNY",
                    "holdings": [{"ticker": h.ticker, "quantity": h.quantity, "average_price": h.average_price}
                                 for h in portfolio_svc.holdings(s, user.id)]}

    @app.put("/api/portfolio/cash")
    def portfolio_cash(body: CashIn, user: User = Depends(current_user)):
        with db.session() as s:
            portfolio_svc.set_cash(s, user.id, body.cash, body.currency)
        return {"ok": True}

    @app.put("/api/portfolio/holdings/{ticker}")
    def portfolio_holding(ticker: str, body: HoldingIn, user: User = Depends(current_user)):
        if body.quantity < 0:
            raise HTTPException(422, "quantity cannot be negative")
        with db.session() as s:
            portfolio_svc.set_holding(s, user.id, _clean_tickers([ticker])[0], body.quantity, body.average_price)
        return {"ok": True}

    @app.delete("/api/portfolio/holdings/{ticker}")
    def portfolio_holding_delete(ticker: str, user: User = Depends(current_user)):
        with db.session() as s:
            portfolio_svc.set_holding(s, user.id, _clean_tickers([ticker])[0], 0)
        return {"ok": True}

    @app.get("/api/paper")
    def paper(user: User = Depends(current_user)):
        """The user's paper account: valuation, daily equity against the benchmark, recent trades."""
        import csv as _csv

        from server.services import paper as paper_svc
        from tradingagents.dataflows.config import run_config

        account = paper_svc.account(config, user.id)
        out = {"exists": account.path.exists(), "orders_on": paper_svc.orders_on(config, user.id),
               "benchmark": config.get("paper_benchmark") or "",
               "currency": account.state.get("base") or config.get("paper_currency") or "CNY"}
        if not out["exists"]:
            return out
        out["start_date"] = account.state.get("start_date")
        out["last_update"] = account.state.get("last_update")

        def rows(name):
            path = account.dir / name
            if not path.exists():
                return []
            with open(path, encoding="utf-8-sig", newline="") as f:
                return list(_csv.DictReader(f))

        def num(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        try:
            with run_config(config):
                v = account.valuation(date.today().isoformat())
        except Exception as exc:      # prices unavailable: still show the history
            v = {"error": f"{type(exc).__name__}: {exc}"}
        out["valuation"] = v
        out["start_cash"] = account.state.get("start_cash")
        out["pending"] = account.state.get("pending") or []
        out["equity"] = [{"date": r["date"], "equity": num(r.get("equity")), "return": num(r.get("return")),
                          "benchmark_return": num(r.get("benchmark_return"))} for r in rows("equity.csv")]
        out["trades"] = list(reversed(rows("trades.csv")))[:50]
        return out

    @app.post("/api/paper/open")
    def paper_open(body: PaperOpenIn, user: User = Depends(current_user)):
        """Open the paper account (or start it over with ``restart``)."""
        from server.services import paper as paper_svc

        if paper_svc.exists(config, user.id) and not body.restart:
            raise HTTPException(409, "the paper account already exists; start over to reset it")
        paper_svc.open_account(config, user.id, body.cash, body.currency)
        return paper(user)

    @app.put("/api/paper/switch")
    def paper_switch(body: PaperSwitchIn, user: User = Depends(current_user)):
        """Turn automatic paper orders on or off for this user's analyses."""
        from server.services import paper as paper_svc

        if not paper_svc.exists(config, user.id):
            raise HTTPException(409, "open the paper account first")
        paper_svc.set_enabled(config, user.id, body.enabled)
        return {"orders_on": paper_svc.orders_on(config, user.id)}

    # --- jobs -------------------------------------------------------------------------

    def _own_job(s, job_id: int, user: User) -> Job:
        job = s.get(Job, job_id)
        if job is None or (job.user_id != user.id and user.role != "admin"):
            raise HTTPException(404, "no such job")
        return job

    @app.get("/api/jobs")
    def jobs_list(limit: int = 30, user: User = Depends(current_user)):
        with db.session() as s:
            return [_job_out(j) for j in jobs.recent(s, user.id, min(limit, 200))]

    @app.get("/api/jobs/{job_id}")
    def job_get(job_id: int, user: User = Depends(current_user)):
        with db.session() as s:
            return _job_out(_own_job(s, job_id, user), detail=True)

    @app.get("/api/jobs/{job_id}/log", response_class=PlainTextResponse)
    def job_log(job_id: int, offset: int = 0, response: Response = None, user: User = Depends(current_user)):
        """The job's log from byte ``offset``; the next offset is in the X-Log-Offset header."""
        with db.session() as s:
            job = _own_job(s, job_id, user)
            path = Path(job.log_path) if job.log_path else None
        if path is None or not path.exists():
            response.headers["X-Log-Offset"] = "0"
            return ""
        with open(path, "rb") as f:
            f.seek(max(offset, 0))
            data = f.read(512_000)
        response.headers["X-Log-Offset"] = str(max(offset, 0) + len(data))
        return data.decode("utf-8", errors="replace")

    @app.post("/api/jobs/{job_id}/cancel")
    def job_cancel(job_id: int, user: User = Depends(current_user)):
        with db.session() as s:
            job = _own_job(s, job_id, user)
            if not jobs.cancel(s, job):
                raise HTTPException(409, f"the job is already {job.status}")
            return {"status": job.status}

    @app.get("/api/jobs/{job_id}/report/{index}")
    def job_report(job_id: int, index: int, fmt: str = "html", user: User = Depends(current_user)):
        """The saved report of the job's ``index``-th run (html or md)."""

        with db.session() as s:
            job = _own_job(s, job_id, user)
            runs = (job.result or {}).get("runs") or []
        if not 0 <= index < len(runs) or not runs[index].get("report_dir"):
            raise HTTPException(404, "no report for that run")
        path = _report_path(user, runs[index]["report_dir"], fmt)
        return FileResponse(path, media_type="text/html" if fmt == "html" else "text/markdown")

    # --- analyses ---------------------------------------------------------------------

    def _analysis_params(body: AnalysisIn, user: User) -> dict:
        with db.session() as s:
            tickers = _clean_tickers(body.tickers) if body.tickers else watchlist.tickers(s, user.id)
        if not tickers:
            raise HTTPException(422, "no tickers: give some, or add to the watchlist")
        if body.date:
            try:
                date.fromisoformat(body.date)
            except ValueError:
                raise HTTPException(422, "date must be YYYY-MM-DD") from None
        runs = body.runs or int(config.get("vote_runs") or 1)
        return {"tickers": tickers, "runs": runs, "date": body.date}

    @app.get("/api/scoreboard")
    def scoreboard_get(user: User = Depends(current_user)):
        """How the settled decisions have done (shared by all accounts, like the memory log)."""
        from server.services import scoreboard as scoreboard_svc, screen_track

        out = scoreboard_svc.payload(config)
        with db.session() as s:
            out["screens"] = screen_track.summary(s, user.id)
        return out

    @app.get("/api/analyses/defaults")
    def analysis_defaults(user: User = Depends(current_user)):
        """What an analysis uses when the form leaves a field empty (from .env at service start)."""
        return {"runs": int(config.get("vote_runs") or 1)}

    @app.post("/api/analyses/estimate")
    def analysis_estimate(body: AnalysisIn, user: User = Depends(current_user)):
        from server.runner import estimate

        params = _analysis_params(body, user)
        est = estimate(config, user, params["tickers"], params["runs"])
        with db.session() as s:
            ok, remaining, why = costs.check_budget(s, s.get(User, user.id), est or 0.0, config)
        return {**params, "estimate": est, "currency": config.get("price_currency") or "",
                "within_quota": ok, "quota_remaining": remaining, "over_budget": why}

    @app.post("/api/analyses")
    def analysis_create(body: AnalysisIn, user: User = Depends(current_user)):
        from server.runner import estimate

        params = _analysis_params(body, user)
        est = estimate(config, user, params["tickers"], params["runs"])
        with db.session() as s:
            ok, remaining, why = costs.check_budget(s, s.get(User, user.id), est or 0.0, config)
            if not ok:
                raise HTTPException(402, why)
            job = jobs.create(s, user.id, "analysis", {**params, "estimate": est})
            out = _job_out(job)
        _wake()
        return out

    # --- screens ----------------------------------------------------------------------

    @app.get("/api/screens/presets")
    def screen_presets(user: User = Depends(current_user)):
        """The ranking presets and the factors, for the screen page."""
        from tradingagents.scoring import FACTORS, PRESETS

        return {"factors": FACTORS, "presets": [{"key": p.key, "name": p.name, "summary": p.summary,
                                                 "weights": p.weights, "filters": list(p.filters)}
                                                for p in PRESETS.values()]}

    @app.post("/api/screens")
    def screen_create(body: ScreenIn, user: User = Depends(current_user)):
        from tradingagents.scoring import PRESETS

        if body.preset and body.preset not in PRESETS:
            raise HTTPException(422, f"no preset {body.preset!r}")
        if not (body.request.strip() or body.spec or body.where or body.preset):
            raise HTTPException(422, "describe the stocks you want, choose a preset, or give conditions")
        if body.spec is not None:
            from tradingagents.screener import Spec

            try:
                Spec.from_dict(body.spec)
            except Exception as exc:
                raise HTTPException(422, f"conditions not usable: {exc}") from None
        with db.session() as s:
            job = jobs.create(s, user.id, "screen", body.model_dump())
            out = _job_out(job)
        _wake()
        return out

    @app.post("/api/screens/{job_id}/review")
    def screen_review(job_id: int, body: ReviewIn, user: User = Depends(current_user)):
        """A quick second look at a finished screen's leaders: one quick-model call each."""
        from server.runner import review_rows

        with db.session() as s:
            screen = _own_job(s, job_id, user)
            if screen.kind != "screen" or screen.status != "done":
                raise HTTPException(409, "not a finished screen")
            rows = review_rows(screen, body.rows[:10], body.top)
            if not rows:
                raise HTTPException(422, "no reviewable stock in those rows")
            est = _review_estimate(len(rows))
            ok, _, why = costs.check_budget(s, s.get(User, user.id), est or 0.0, config)
            if not ok:
                raise HTTPException(402, why)
            job = jobs.create(s, user.id, "review", {"screen_job": job_id, "rows": body.rows[:10], "top": body.top,
                                                     "tickers": [r["ticker"] for r in rows], "estimate": est})
            out = _job_out(job)
        _wake()
        return out

    @app.get("/api/screens/{job_id}/reviews")
    def screen_reviews(job_id: int, user: User = Depends(current_user)):
        """The reviews of a screen, newest first (each a review job, finished or not)."""
        with db.session() as s:
            _own_job(s, job_id, user)
            found = [j for j in jobs.recent(s, user.id, 200) if j.kind == "review"
                     and int((j.params or {}).get("screen_job") or 0) == job_id]
            return [{**_job_out(j), "result": j.result} for j in found]

    def _review_estimate(n: int) -> float | None:
        """About 3,000 tokens in and 800 out per stock, at the quick model's price."""
        from tradingagents.usage import parse_prices

        price = parse_prices(config.get("model_prices")).get(config.get("quick_think_llm") or "")
        return None if price is None else round(n * (3000 * price[0] + 800 * price[1]) / 1_000_000, 4)

    @app.post("/api/screens/{job_id}/pick")
    def screen_pick(job_id: int, body: PickIn, user: User = Depends(current_user)):
        """Add the chosen rows of a finished screen to the watchlist (nothing is added otherwise)."""
        with db.session() as s:
            job = _own_job(s, job_id, user)
            if job.kind != "screen" or job.status != "done":
                raise HTTPException(409, "not a finished screen")
            rows = (job.result or {}).get("rows") or []
            bad = [n for n in body.rows if not 1 <= n <= len(rows)]
            if bad:
                raise HTTPException(422, f"no such rows: {bad}")
            picked = [rows[n - 1] for n in body.rows]
            no_symbol = [r.get("name") or r.get("code") for r in picked if not r.get("ticker")]
            tickers = [r["ticker"] for r in picked if r.get("ticker")]
            added = watchlist.add(s, user.id, tickers, note=f"screen #{job_id}: {job.params.get('request', '')}"[:200],
                                  source="screen")
        return {"added": added, "already": [t for t in tickers if t not in added], "no_symbol": no_symbol}

    # --- schedule ---------------------------------------------------------------------

    @app.get("/api/schedule")
    def schedule_get(user: User = Depends(current_user)):
        with db.session() as s:
            sched = s.get(Schedule, user.id)
            # runs None: the account follows TRADINGAGENTS_VOTE_RUNS (default_runs), as the
            # analysis page does; a number is one the user chose for the schedule.
            out = {"enabled": bool(sched and sched.enabled),
                   "time_of_day": (sched and sched.time_of_day) or "18:30",
                   "days": (sched and sched.days) or "mon-fri",
                   "runs": (sched.runs or None) if sched else None,
                   "default_runs": int(config.get("vote_runs") or 1)}
        nxt = state["scheduler"].next_run(user.id) if state["scheduler"] else None
        out["next_run"] = _iso(nxt.replace(tzinfo=None)) if nxt else None
        return out

    @app.put("/api/schedule")
    def schedule_put(body: ScheduleIn, user: User = Depends(current_user)):
        from server.scheduler import valid_time

        try:
            valid_time(body.time_of_day)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        if not re.fullmatch(r"[a-z*,\-]{1,32}", body.days):
            raise HTTPException(422, "days: e.g. mon-fri, mon,wed,fri or *")
        with db.session() as s:
            sched = s.get(Schedule, user.id)
            if sched is None:
                sched = Schedule(user_id=user.id)
                s.add(sched)
            sched.enabled, sched.time_of_day, sched.days, sched.runs = \
                body.enabled, body.time_of_day, body.days, body.runs or 0
        if state["scheduler"]:
            state["scheduler"].reload()
        return schedule_get(user)

    # --- costs ------------------------------------------------------------------------

    @app.get("/api/costs")
    def cost_list(user: User = Depends(current_user)):
        from sqlalchemy import select

        from server.models import CostEntry

        with db.session() as s:
            rows = s.scalars(select(CostEntry).where(CostEntry.user_id == user.id)
                             .order_by(CostEntry.run_time.desc()).limit(200))
            entries = [{"run_time": _iso(c.run_time), "ticker": c.ticker, "trade_date": c.trade_date,
                        "rating": c.rating, "cost": c.cost, "currency": c.currency, "job_id": c.job_id,
                        "input_tokens": c.input_tokens, "output_tokens": c.output_tokens} for c in rows]
            return {"month_spend": costs.month_spend(s, user.id), "monthly_quota": user.monthly_quota,
                    "entries": entries}

    # --- administration ---------------------------------------------------------------

    @app.get("/api/admin/users")
    def admin_users(user: User = Depends(admin_user)):
        from sqlalchemy import select

        with db.session() as s:
            return [{**_user_out(u), "month_spend": costs.month_spend(s, u.id)}
                    for u in s.scalars(select(User).order_by(User.id))]

    @app.post("/api/admin/users")
    def admin_user_create(body: UserIn, user: User = Depends(admin_user)):
        if body.role not in ("admin", "user"):
            raise HTTPException(422, "role is admin or user")
        with db.session() as s:
            try:
                new = users.create_user(s, body.username, body.password, role=body.role)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
            new.monthly_quota = body.monthly_quota
            return _user_out(new)

    @app.patch("/api/admin/users/{user_id}")
    def admin_user_patch(user_id: int, body: UserPatch, user: User = Depends(admin_user)):
        with db.session() as s:
            target = s.get(User, user_id)
            if target is None:
                raise HTTPException(404, "no such user")
            if target.id == user.id and (body.is_active is False or body.role == "user"):
                raise HTTPException(409, "you cannot disable or demote yourself")
            if body.role is not None:
                if body.role not in ("admin", "user"):
                    raise HTTPException(422, "role is admin or user")
                target.role = body.role
            if body.is_active is not None:
                target.is_active = body.is_active
                if not body.is_active:
                    auth.revoke_user(s, target.id)
            if body.clear_quota:
                target.monthly_quota = None
            elif body.monthly_quota is not None:
                target.monthly_quota = body.monthly_quota
            if body.password:
                try:
                    users.set_password(s, target, body.password)
                except ValueError as exc:
                    raise HTTPException(422, str(exc)) from None
                auth.revoke_user(s, target.id)
            return _user_out(target)

    static = Path(__file__).resolve().parent / "static"
    if static.exists():
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=static, html=True), name="pages")
    app.state.wake = _wake
    return app
