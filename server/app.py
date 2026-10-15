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
    spec: dict | None = Field(None, description="Edited conditions (as returned on a screen job)")
    where: str | None = None
    limit: int | None = Field(None, ge=1, le=200)


class PickIn(BaseModel):
    rows: list[int] = Field(..., description="Row numbers in the screen's result, from 1")


class ScheduleIn(BaseModel):
    enabled: bool
    time_of_day: str = "18:30"
    days: str = "mon-fri"
    runs: int = Field(2, ge=1, le=5)


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
    out = []
    for t in raw:
        for part in str(t).replace(";", ",").split(","):
            part = part.strip().upper()
            if not part:
                continue
            if not TICKER.match(part):
                raise HTTPException(422, f"not a ticker: {part!r}")
            out.append(part)
    return list(dict.fromkeys(out))


def _iso(value) -> str | None:
    return value.isoformat(timespec="seconds") if isinstance(value, datetime) else value


def _user_out(u: User) -> dict:
    return {"id": u.id, "username": u.username, "display_name": u.display_name, "role": u.role,
            "is_active": u.is_active, "monthly_quota": u.monthly_quota, "created_at": _iso(u.created_at)}


def _job_out(j: Job, detail: bool = False) -> dict:
    out = {"id": j.id, "kind": j.kind, "status": j.status, "params": j.params,
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

    @app.get("/api/health")
    def health():
        return {"ok": True, "time": _iso(datetime.now())}

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
            from sqlalchemy import select

            from server.models import CostEntry

            decisions = [{"ticker": c.ticker, "rating": c.rating, "trade_date": c.trade_date,
                          "run_time": _iso(c.run_time), "job_id": c.job_id, "cost": c.cost}
                         for c in s.scalars(select(CostEntry).where(CostEntry.user_id == user.id)
                                            .order_by(CostEntry.run_time.desc()).limit(10))]
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
        return llm_balance.balance(provider)

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
        from server.services import paper as paper_svc
        from tradingagents.dataflows.config import run_config
        from tradingagents.paper import render_status

        account = paper_svc.account(config, user.id)
        if not account.path.exists():
            return {"enabled": bool(config.get("paper_trading")), "exists": False}
        with run_config(config):
            text = render_status(account, date.today().isoformat())
        return {"enabled": bool(config.get("paper_trading")), "exists": True, "status_markdown": text}

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
        from server.runner import reports_base

        with db.session() as s:
            job = _own_job(s, job_id, user)
            runs = (job.result or {}).get("runs") or []
        if not 0 <= index < len(runs) or not runs[index].get("report_dir"):
            raise HTTPException(404, "no report for that run")
        name = "complete_report.html" if fmt == "html" else "complete_report.md"
        path = (Path(runs[index]["report_dir"]) / name).resolve()
        base = reports_base(config).resolve()
        if base not in path.parents or not path.exists():
            raise HTTPException(404, "report file not found")
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

    @app.post("/api/analyses/estimate")
    def analysis_estimate(body: AnalysisIn, user: User = Depends(current_user)):
        from server.runner import estimate

        params = _analysis_params(body, user)
        est = estimate(config, user, params["tickers"], params["runs"])
        with db.session() as s:
            ok, remaining = costs.within_quota(s, s.get(User, user.id), est or 0.0)
        return {**params, "estimate": est, "currency": config.get("price_currency") or "",
                "within_quota": ok, "quota_remaining": remaining}

    @app.post("/api/analyses")
    def analysis_create(body: AnalysisIn, user: User = Depends(current_user)):
        from server.runner import estimate

        params = _analysis_params(body, user)
        est = estimate(config, user, params["tickers"], params["runs"])
        with db.session() as s:
            ok, remaining = costs.within_quota(s, s.get(User, user.id), est or 0.0)
            if not ok:
                raise HTTPException(402, f"estimated cost {est:.2f} exceeds this month's remaining quota "
                                         f"{remaining:.2f}")
            job = jobs.create(s, user.id, "analysis", {**params, "estimate": est})
            out = _job_out(job)
        _wake()
        return out

    # --- screens ----------------------------------------------------------------------

    @app.post("/api/screens")
    def screen_create(body: ScreenIn, user: User = Depends(current_user)):
        if not (body.request.strip() or body.spec or body.where):
            raise HTTPException(422, "describe the stocks you want, or give conditions")
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
            sched = s.get(Schedule, user.id) or Schedule(user_id=user.id)
            out = {"enabled": bool(sched.enabled), "time_of_day": sched.time_of_day or "18:30",
                   "days": sched.days or "mon-fri", "runs": sched.runs or 2}
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
                body.enabled, body.time_of_day, body.days, body.runs
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

    app.state.wake = _wake
    return app
