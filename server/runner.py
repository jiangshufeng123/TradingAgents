"""What the worker does for each kind of job.

* ``analysis``: the CLI's own unattended batch (``tradingagents daily``) in a
  child process, with this user's book, paper account and report folder. A
  child process keeps the CLI's console and globals out of the server, can be
  stopped, and runs exactly the code the scheduled task runs. Its output is
  the job's log; the cost ledger rows it appends are the job's costs.
* ``screen``: a plain-language screen, in process (one quick model call); the
  candidates are kept on the job, and nothing joins a watchlist until the user
  picks rows.
"""

from __future__ import annotations

import csv
import dataclasses
import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

from server import db, procs
from server.models import Job, User
from server.services import paper as paper_svc, portfolio as portfolio_svc

PROJECT_DIR = Path(__file__).resolve().parents[1]


class Cancelled(Exception):
    """The user stopped the job."""


# --- paths --------------------------------------------------------------------------

def reports_base(config: dict) -> Path:
    if config.get("reports_dir"):
        return Path(config["reports_dir"]).expanduser()
    return Path(config["results_dir"]).expanduser() / "reports"


def user_reports_dir(config: dict, user: User) -> Path:
    """Each account's reports and cost ledger: <reports>/users/<username>."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", user.username) or f"user{user.id}"
    return reports_base(config) / "users" / safe


def job_log(job_id: int) -> Path:
    path = db.home() / "jobs" / f"{job_id}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# --- cost ledger --------------------------------------------------------------------

def _canonical(ticker) -> str:
    from server.tickers import canonical

    return canonical(ticker)


def job_rows(rows: list[dict], job_id: int, started: datetime) -> list[dict]:
    """The ledger rows this job wrote. A row names its job (``job_id``); a row from before
    that column existed is the job's when its run began after the job started."""
    out = []
    for r in rows:
        tag = str(r.get("job_id") or "").strip()
        if tag:
            if tag == str(job_id):
                out.append(r)
        elif _began_after(r, started):
            out.append(r)
    return out


def _began_after(row: dict, started: datetime, slack: float = 5.0) -> bool:
    """Whether the ledger row's run began after this job started. A row is written when its
    run ends; one that ended during this job but began before it belongs to another process
    (an analysis left running by an earlier service), not to this job."""
    elapsed = _float(row.get("elapsed_seconds"))
    try:
        ended = datetime.strptime(str(row.get("run_time") or ""), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return True
    if elapsed is None:
        return True
    return (ended - started).total_seconds() - elapsed >= -slack


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def ledger_rows(path: Path, since: datetime | None = None) -> list[dict]:
    """The ledger's rows (run on or after ``since``), oldest first."""
    if not path.exists():
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if since is None:
        return rows
    out = []
    for r in rows:
        try:
            when = datetime.strptime(r.get("run_time", ""), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if when >= since.replace(microsecond=0):
            out.append(r)
    return out


def cost_per_run(config: dict, user: User | None = None) -> float | None:
    """Average model cost of one run of one ticker, from this user's ledger, else the
    shared one (the CLI's); None before any priced run."""
    paths = ([user_reports_dir(config, user) / "cost_ledger.csv"] if user else []) + \
        [reports_base(config) / "cost_ledger.csv"]
    for path in paths:
        per_run = []
        for r in ledger_rows(path)[-30:]:
            cost, runs = _float(r.get("cost")), _float(r.get("vote_runs")) or 1
            if cost is not None and cost > 0:
                per_run.append(cost / runs)
        if per_run:
            return sum(per_run) / len(per_run)
    return None


def estimate(config: dict, user: User, tickers: list[str], runs: int) -> float | None:
    per_run = cost_per_run(config, user)
    return None if per_run is None else round(per_run * len(tickers) * max(runs, 1), 4)


# --- analysis -----------------------------------------------------------------------

def analysis_command(params: dict, portfolio_file: Path | None) -> list[str]:
    cmd = [sys.executable, "-m", "cli.main", "daily", ",".join(params["tickers"])]
    if params.get("runs"):
        cmd += ["--runs", str(int(params["runs"]))]
    if params.get("date"):
        cmd += ["--date", str(params["date"])]
    cmd += ["--portfolio", str(portfolio_file)] if portfolio_file else ["--no-portfolio"]
    return cmd


def analysis_env(config: dict, user: User) -> dict:
    env = dict(os.environ)
    env.update({
        "TRADINGAGENTS_REPORTS_DIR": str(user_reports_dir(config, user)),
        "TRADINGAGENTS_PAPER_DIR": str(paper_svc.user_dir(config, user.id)),
        "TRADINGAGENTS_DAILY_SCREEN": "",          # the web screens on request, never inside a batch
        # Each user's own switch (opened and on in the web page), not the shared .env setting.
        "TRADINGAGENTS_PAPER_TRADING": "true" if paper_svc.orders_on(config, user.id) else "false",
        "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "COLUMNS": "120", "NO_COLOR": "1",
        # Write each line as it comes, and print a line per step (no terminal display to draw).
        "PYTHONUNBUFFERED": "1", "TRADINGAGENTS_PLAIN_PROGRESS": "1",
    })
    return env


PROGRESS = re.compile(r"(\d+)/(\d+) (\S+) \d{4}-\d{2}-\d{2}")
STEP = re.compile(r"^(\d\d:\d\d:\d\d) \[进度 (\d+)/(\d+)\] (?:第 (\d+)/(\d+) 次运行 )?(.+)$", re.M)
STEP_LINE = re.compile(r"^\d\d:\d\d:\d\d \[(?:进度 \d+/\d+|工具|运行|出错)\] .*$", re.M)


def progress(log_text: str) -> dict | None:
    """Where the batch is: which ticker, and (from the plain progress lines) which step of
    which vote run, with the fraction of the batch done."""
    found = PROGRESS.findall(log_text)
    if not found:
        return None
    index, total, ticker = found[-1]
    out = {"index": int(index), "total": int(total), "ticker": ticker}
    # Steps only count after this ticker's rule.
    tail = log_text[log_text.rfind(f"{index}/{total} {ticker}"):]
    steps = STEP.findall(tail)
    lines = STEP_LINE.findall(tail)
    within = 0.0
    if steps:
        _, done, of, run, runs, text = steps[-1]
        run, runs = int(run or 1), int(runs or 1)
        out.update(step=text.strip(), step_done=int(done), step_total=int(of), run=run, runs=runs)
        within = ((run - 1) + int(done) / max(int(of), 1)) / max(runs, 1)
    if lines:
        out["last_line"] = lines[-1][:200]
    out["fraction"] = round(((int(index) - 1) + min(within, 0.99)) / max(int(total), 1), 4)
    return out


def run_analysis(job: Job, user: User, config: dict, should_stop, poll: float = 2.0) -> dict:
    """Run the batch; returns the job result. Raises Cancelled or RuntimeError."""
    log = job_log(job.id)
    work = log.with_suffix("")
    work.mkdir(parents=True, exist_ok=True)
    with db.session() as s:
        book = portfolio_svc.context(s, user.id)
    portfolio_file = None
    if book is not None:
        portfolio_file = work / "portfolio.json"
        portfolio_file.write_text(book.model_dump_json(), encoding="utf-8")
    reports = user_reports_dir(config, user)
    reports.mkdir(parents=True, exist_ok=True)
    started = datetime.now()
    with open(log, "w", encoding="utf-8") as out:
        out.write(f"$ tradingagents daily {','.join(job.params['tickers'])} "
                  f"(runs {job.params.get('runs') or 'default'})\n")
        out.flush()
        env = analysis_env(config, user)
        env["TRADINGAGENTS_JOB_ID"] = str(job.id)              # each ledger row it writes names this job
        proc = subprocess.Popen(analysis_command(job.params, portfolio_file), cwd=PROJECT_DIR,
                                env=env, stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL)
        handle = procs.contain(proc)                            # ends with the service, however it stops
        procs.remember(work, proc.pid)
        try:
            while proc.poll() is None:
                if should_stop():
                    proc.terminate()
                    try:
                        proc.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    raise Cancelled()
                time.sleep(poll)
        finally:
            procs.release(handle)
            procs.forget(work)
    rows = job_rows(ledger_rows(reports / "cost_ledger.csv", since=started), job.id, started)
    runs = [{"ticker": r.get("ticker"), "rating": r.get("rating"), "action": r.get("action"),
             "target_weight": _float(r.get("target_weight")), "stop": _float(r.get("stop")),
             "cost": _float(r.get("cost")), "currency": r.get("currency"),
             "report_dir": r.get("report_dir"), "trade_date": r.get("trade_date"),
             "vote_ratings": r.get("vote_ratings"), "llm_calls": int(_float(r.get("llm_calls")) or 0),
             "input_tokens": int(_float(r.get("input_tokens")) or 0),
             "output_tokens": int(_float(r.get("output_tokens")) or 0),
             "run_time": r.get("run_time")} for r in rows]
    if not runs:
        # Nothing analysed (every ticker failed, or the batch did not start): a failed job,
        # not a finished one with an empty result.
        raise RuntimeError(f"no ticker produced a result (exit code {proc.returncode}); see the log")
    # The run writes the canonical symbol (603980.SH is recorded as 603980.SS): compare those.
    done = {_canonical(r["ticker"]) for r in runs}
    missing = [t for t in job.params["tickers"] if _canonical(t) not in done]
    return {"runs": runs, "missing": missing, "exit_code": proc.returncode}


# --- screen -------------------------------------------------------------------------

def run_screen(job: Job, user: User, config: dict, should_stop) -> dict:
    from tradingagents import screener
    from tradingagents.dataflows.config import run_config
    from tradingagents.llm_clients import create_tier_client

    log = job_log(job.id)
    request = job.params["request"]

    def note(msg: str) -> None:
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%H:%M:%S} {msg}\n")

    note(f"screen: {request}")
    with run_config(config):
        universe, period = screener.build_universe(config)
    note(f"universe: {len(universe)} listings, quotes from {universe.attrs.get('source')}, results {period}")
    if should_stop():
        raise Cancelled()
    preset = job.params.get("preset") or ""
    if job.params.get("spec"):                     # conditions the user edited
        spec = screener.Spec.from_dict(job.params["spec"])
    elif job.params.get("where"):
        spec = screener.parse_where(job.params["where"])
    elif preset and not request.strip():           # a preset alone: no model needed
        spec = screener.Spec.from_preset(preset)
    else:
        industries = sorted(universe["industry"].dropna().astype(str).unique())
        spec = screener.interpret(request, create_tier_client(config, "quick").get_llm(), industries,
                                  period, universe.attrs.get("missing", []),
                                  progress=lambda secs: note(f"waiting for the model ({secs:.0f}s)"))
    if preset and not job.params.get("spec"):     # the user's choice of ranking wins over the model's
        spec.preset, spec.weights, spec.sort = preset, {}, []
    if job.params.get("limit"):
        spec.limit = int(job.params["limit"])
    note(f"conditions: {spec.describe()}")
    note("scoring: quotes and results for every listing that passes, daily bars for the leaders")
    with run_config(config):
        result = screener.apply(spec, universe, load=screener.price_loader(config))
    screener.save(result, spec, request, config)
    note(f"{len(result)} candidate(s)")
    try:                                           # the top picks, checked against the index later
        from server.services import screen_track

        with db.session() as s:
            kept = screen_track.record(s, job.id, user.id, result, spec, universe.attrs.get("as_of"),
                                       label=request[:60] if request.strip() else "")
        if kept:
            note(f"kept the top {kept} for the forward check (5 and 20 trading days)")
    except Exception as exc:
        note(f"forward check not recorded: {exc}")
    keep = [c for c in screener.SHOW if c in result.columns]
    rows = json.loads(result[keep].to_json(orient="records", force_ascii=False))
    return {"request": request, "conditions": spec.describe(), "explanation": spec.explanation or "",
            "spec": dataclasses.asdict(spec), "ranking": spec.ranking(),
            "unavailable": screener.unavailable(spec, universe), "period": period,
            "source": universe.attrs.get("source"), "rows": rows}


def review_rows(screen: Job, rows: list[int] | None, top: int | None) -> list[dict]:
    """The screen's rows to review: the row numbers given (from 1), else its top ``top``."""
    found = ((screen.result or {}).get("rows") or [])
    if rows:
        return [found[n - 1] for n in rows if 1 <= n <= len(found) and found[n - 1].get("ticker")]
    return [r for r in found if r.get("ticker")][:max(1, min(int(top or 5), 10))]


def run_review(job: Job, user: User, config: dict, should_stop) -> dict:
    """A quick second look at a screen's leaders (tradingagents/review.py)."""
    from tradingagents import review
    from tradingagents.dataflows.config import run_config
    from tradingagents.dataflows.vendors import akshare_cn
    from tradingagents.llm_clients import create_tier_client
    from tradingagents.usage import parse_prices

    log = job_log(job.id)

    def note(msg: str) -> None:
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%H:%M:%S} {msg}\n")

    with db.session() as s:
        screen = s.get(Job, int(job.params["screen_job"]))
        if screen is None or screen.user_id != user.id or screen.kind != "screen" or screen.status != "done":
            raise RuntimeError("the screen to review is gone or not finished")
        rows = review_rows(screen, job.params.get("rows"), job.params.get("top"))
        result = screen.result or {}
    if not rows:
        raise RuntimeError("no stock to review")
    note(f"reviewing {len(rows)}: " + ", ".join(f"{r['ticker']} {r.get('name') or ''}" for r in rows))
    if should_stop():
        raise Cancelled()
    model = config.get("quick_think_llm") or ""
    price = parse_prices(config.get("model_prices")).get(model)

    def news(ticker, start, end):
        with run_config(config):
            return akshare_cn.get_news(ticker, start, end)

    ranking = result.get("conditions", "").splitlines()[-2:] if result.get("conditions") else []
    with run_config(config):
        llm = create_tier_client(config, "quick").get_llm()
    reviews = review.review(rows, llm, news=news, ranking="；".join(ranking), price=price)
    for r in reviews:
        note(f"{r['ticker']}: {r.get('verdict') or r.get('error')}")
    currency = config.get("price_currency") or ""
    runs = [{"ticker": r["ticker"], "rating": r.get("verdict") or "", "cost": r.get("cost"), "currency": currency,
             "llm_calls": 1 if "verdict" in r else 0, "input_tokens": r.get("input_tokens") or 0,
             "output_tokens": r.get("output_tokens") or 0, "trade_date": date.today().isoformat()}
            for r in reviews]
    return {"screen_job": int(job.params["screen_job"]), "model": model, "reviews": reviews, "runs": runs}


RUNNERS = {"analysis": run_analysis, "screen": run_screen, "review": run_review}
