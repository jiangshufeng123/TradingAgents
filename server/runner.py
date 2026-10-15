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
from datetime import datetime
from pathlib import Path

from server import db
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
        "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "COLUMNS": "120", "NO_COLOR": "1",
    })
    return env


PROGRESS = re.compile(r"(\d+)/(\d+) (\S+) \d{4}-\d{2}-\d{2}")


def progress(log_text: str) -> dict | None:
    """Which ticker of the batch is running, from the batch's section rules."""
    found = PROGRESS.findall(log_text)
    if not found:
        return None
    index, total, ticker = found[-1]
    return {"index": int(index), "total": int(total), "ticker": ticker}


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
        proc = subprocess.Popen(analysis_command(job.params, portfolio_file), cwd=PROJECT_DIR,
                                env=analysis_env(config, user), stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL)
        while proc.poll() is None:
            if should_stop():
                proc.terminate()
                try:
                    proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise Cancelled()
            time.sleep(poll)
    rows = ledger_rows(reports / "cost_ledger.csv", since=started)
    runs = [{"ticker": r.get("ticker"), "rating": r.get("rating"), "action": r.get("action"),
             "target_weight": _float(r.get("target_weight")), "stop": _float(r.get("stop")),
             "cost": _float(r.get("cost")), "currency": r.get("currency"),
             "report_dir": r.get("report_dir"), "trade_date": r.get("trade_date"),
             "vote_ratings": r.get("vote_ratings"), "llm_calls": int(_float(r.get("llm_calls")) or 0),
             "input_tokens": int(_float(r.get("input_tokens")) or 0),
             "output_tokens": int(_float(r.get("output_tokens")) or 0),
             "run_time": r.get("run_time")} for r in rows]
    if proc.returncode != 0 and not runs:
        raise RuntimeError(f"the batch exited with code {proc.returncode}; see the log")
    done = {r["ticker"] for r in runs}
    missing = [t for t in job.params["tickers"] if t not in done]
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
    if job.params.get("spec"):                     # conditions the user edited
        spec = screener.Spec.from_dict(job.params["spec"])
    elif job.params.get("where"):
        spec = screener.parse_where(job.params["where"])
    else:
        industries = sorted(universe["industry"].dropna().astype(str).unique())
        spec = screener.interpret(request, create_tier_client(config, "quick").get_llm(), industries,
                                  period, universe.attrs.get("missing", []),
                                  progress=lambda secs: note(f"waiting for the model ({secs:.0f}s)"))
    if job.params.get("limit"):
        spec.limit = int(job.params["limit"])
    note(f"conditions: {spec.describe()}")
    with run_config(config):
        result = screener.apply(spec, universe)
    screener.save(result, spec, request, config)
    note(f"{len(result)} candidate(s)")
    keep = [c for c in screener.SHOW if c in result.columns]
    rows = json.loads(result[keep].to_json(orient="records", force_ascii=False))
    return {"request": request, "conditions": spec.describe(), "explanation": spec.explanation or "",
            "spec": dataclasses.asdict(spec),
            "unavailable": screener.unavailable(spec, universe), "period": period,
            "source": universe.attrs.get("source"), "rows": rows}


RUNNERS = {"analysis": run_analysis, "screen": run_screen}
