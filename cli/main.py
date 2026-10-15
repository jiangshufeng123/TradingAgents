import sys

import typer

from cli.display import console
from cli.models import AnalystType, AssetType
from cli.prompts import filter_analysts_for_asset_type, parse_analysts
from cli.run import flags_with_config_defaults, run_analysis
from cli.selections import unattended_gaps
from tradingagents.backtest import iter_grid, run_backtest, summarize
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.portfolio import load_portfolio
from tradingagents.risk import summary_line as sizing_summary

# prompt_toolkit's win32 output module is importable only on Windows (it asserts
# the platform at import time), so gate on the platform rather than catching the
# failure — that way a genuinely broken prompt_toolkit on Windows still surfaces
# instead of silently disabling the handler below. Off Windows this stays an
# empty tuple, which `except` accepts and never matches (#1138).
if sys.platform == "win32":  # pragma: no cover - platform dependent
    from prompt_toolkit.output.win32 import NoConsoleScreenBufferError

    _NO_CONSOLE_ERRORS: tuple[type[BaseException], ...] = (NoConsoleScreenBufferError,)
else:
    _NO_CONSOLE_ERRORS = ()

app = typer.Typer(
    name="TradingAgents",
    help="TradingAgents CLI: Multi-Agents LLM Financial Trading Framework",
    add_completion=True,  # Enable shell completion
)


@app.callback(invoke_without_command=True)
def analyze(
    ctx: typer.Context,
    checkpoint: bool | None = typer.Option(
        None,
        "--checkpoint/--no-checkpoint",
        help="Enable/disable checkpoint-resume (save state after each node so a "
        "crashed run can resume). Omit to honor TRADINGAGENTS_CHECKPOINT_ENABLED.",
    ),
    clear_checkpoints: bool = typer.Option(
        False,
        "--clear-checkpoints",
        help="Delete all saved checkpoints before running (force fresh start).",
    ),
    portfolio: str = typer.Option(
        None,
        "--portfolio",
        help="JSON file with current holdings and cash, so the trader, risk and "
        "portfolio agents size against your actual position (default: TRADINGAGENTS_PORTFOLIO).",
    ),
    ticker: str = typer.Option(None, "--ticker", help="Ticker to analyze, e.g. NVDA or 0700.HK; skips the prompt"),
    date: str = typer.Option(None, "--date", help="Analysis date, YYYY-MM-DD; skips the prompt"),
    analysts: str = typer.Option(
        None, "--analysts", help="Comma-separated analysts, e.g. market,news; skips the prompt"
    ),
    save: bool | None = typer.Option(
        None, "--save/--no-save", help="Save the report under results_dir without asking"
    ),
    show: bool | None = typer.Option(
        None, "--show/--no-show", help="Show the full report at the end without asking"
    ),
    html: bool | None = typer.Option(
        None, "--html/--no-html",
        help="Save the report as one HTML page too, complete_report.html, without asking (default: yes)",
    ),
    runs: int | None = typer.Option(
        None, "--runs", min=1,
        help="Run the analysis N times and take the majority rating (default: TRADINGAGENTS_VOTE_RUNS, else 1)",
    ),
):
    """Run an analysis. This is what a bare `tradingagents` does.

    Flags answer their questions; with provider, models, depth and language also
    set through TRADINGAGENTS_* variables, the run asks nothing.
    """
    if ctx.invoked_subcommand is not None:
        return
    if clear_checkpoints:
        from tradingagents.graph.checkpointer import clear_all_checkpoints
        n = clear_all_checkpoints(DEFAULT_CONFIG["data_cache_dir"])
        console.print(f"[yellow]Cleared {n} checkpoint(s).[/yellow]")
    portfolio_context = _portfolio(portfolio)

    try:
        flags = {"ticker": ticker, "date": date, "analysts": analysts, "save": save, "show": show, "html": html,
                 "runs": runs}
        run_analysis(checkpoint=checkpoint, portfolio=portfolio_context, flags=flags)
    except _NO_CONSOLE_ERRORS:
        # A terminal with no console buffer cannot host the interactive prompts.
        # Emit one actionable line on stderr instead of a prompt_toolkit
        # traceback; plain text, since rich may not render here either (#1138).
        typer.echo(
            "Error: no Windows console available. The interactive CLI needs a real "
            "console buffer — run it from Windows Terminal, PowerShell, or cmd.exe "
            "rather than a piped or embedded terminal.",
            err=True,
        )
        raise typer.Exit(code=1) from None


@app.command()
def backtest(
    tickers: str = typer.Argument(..., help="Comma-separated tickers, e.g. NVDA,AAPL"),
    start: str = typer.Option(..., "--start", help="First analysis date, YYYY-MM-DD"),
    end: str = typer.Option(..., "--end", help="Last analysis date, YYYY-MM-DD"),
    every: int = typer.Option(7, "--every", help="Days between analysis dates"),
    analysts: str = typer.Option(
        None, "--analysts", help="Comma-separated analysts to run: market, sentiment, news, fundamentals; omit for all the asset type allows"
    ),
    asset_type: str = typer.Option("stock", "--asset-type", help="stock or crypto"),
    portfolio: str = typer.Option(
        None, "--portfolio", help="JSON file with holdings and cash, held constant across the grid"
    ),
    run_id: str = typer.Option(
        None, "--run-id", help="Continue an earlier sweep: its cells are skipped and its log reused"
    ),
):
    """Score past decisions over a grid of tickers and dates."""

    try:
        dates = iter_grid(start, end, every)
        book = load_portfolio(portfolio) if portfolio else None
        kind = AssetType(asset_type.strip().lower())
        # The analysts are named and checked as for an analysis; without a
        # choice, every analyst the asset type allows runs.
        chosen = (parse_analysts(analysts, kind) if analysts
                  else filter_analysts_for_asset_type(list(AnalystType), kind))
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    names = [t.strip() for t in tickers.split(",") if t.strip()]
    if not names:
        console.print("[red]No ticker to analyze; pass them comma-separated, e.g. NVDA,AAPL[/red]")
        raise typer.Exit(code=1)

    def show_progress(done, total, ticker, date):
        console.print(f"[dim][{done}/{total}] {ticker} {date}[/dim]")

    kwargs = {"asset_type": kind.value, "portfolio": book, "run_id": run_id, "progress": show_progress,
              "selected_analysts": [a.value for a in chosen]}

    try:
        result = run_backtest(names, dates, DEFAULT_CONFIG, **kwargs)
    except Exception as exc:  # a missing key or an unknown analyst is a setup error
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    console.print(summarize(result).render())
    console.print(f"\nRan {result.cells_run} cells, skipped {result.skipped}. Log: {result.log_path}")
    console.print(f"Continue or settle this sweep: --run-id {result.run_id}")
    for ticker, date, reason in result.failures:
        console.print(f"[yellow]failed:[/yellow] {ticker} {date}: {reason}")
    for ticker, reason in result.settlement_failures:
        console.print(f"[yellow]unsettled:[/yellow] {ticker}: {reason}")


ALL_ANALYSTS = "market,sentiment,news,fundamentals"


def _portfolio(path):
    """The book from --portfolio, else TRADINGAGENTS_PORTFOLIO; None when neither is set."""
    path = path or DEFAULT_CONFIG.get("portfolio_path")
    if not path:
        return None
    try:
        return load_portfolio(path)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None


def _screen_picks(request: str, top: int) -> list[str]:
    """The top picks of a plain-language screen, for an unattended daily run; [] on failure."""
    from tradingagents import screener
    from tradingagents.dataflows.config import run_config
    from tradingagents.llm_clients import create_tier_client

    try:
        with run_config(DEFAULT_CONFIG):
            universe, period = screener.build_universe(DEFAULT_CONFIG)
        industries = sorted(universe["industry"].dropna().astype(str).unique()) if "industry" in universe else []
        spec = screener.interpret(request, create_tier_client(DEFAULT_CONFIG, "quick").get_llm(),
                                  industries, period, universe.attrs.get("missing", []))
        spec.limit = max(spec.limit, top)
        with run_config(DEFAULT_CONFIG):
            result = screener.apply(spec, universe)
        screener.save(result, spec, request, DEFAULT_CONFIG)
    except Exception as exc:   # the watchlist still runs
        console.print(f"[yellow]Daily screen failed ({exc}); analyzing the watchlist only.[/yellow]")
        return []
    picks = list(result["ticker"].dropna().head(top))
    console.print(f"[bold]Daily screen[/bold] ({request}): {spec.explanation or ''}\n{spec.describe()}\n"
                  f"→ {len(result)} candidate(s) saved, none added to the watchlist. "
                  f"Review and pick with: tradingagents watchlist pick")
    return picks


def _tickers_from_file(path: str) -> list[str]:
    """Tickers from a text file (one per line) or a CSV with a ``ticker`` column."""
    from pathlib import Path

    text = Path(path).expanduser().read_text(encoding="utf-8-sig")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    if lines and "," in lines[0]:
        header = [h.strip().lower() for h in lines[0].split(",")]
        if "ticker" in header:
            i = header.index("ticker")
            return [row.split(",")[i].strip().upper() for row in lines[1:] if row.split(",")[i].strip()]
    return [ln.split(",")[0].strip().upper() for ln in lines]


def _watchlist(tickers: str | None) -> list[str]:
    """The tickers given, else .env's TRADINGAGENTS_WATCHLIST plus watchlist.txt."""
    if tickers:
        return list(dict.fromkeys(t.strip().upper() for t in str(tickers).replace(";", ",").split(",") if t.strip()))
    from tradingagents import watchlist as wl

    return wl.tickers(DEFAULT_CONFIG)


@app.command()
def daily(
    tickers: str = typer.Argument(
        None, help="Comma-separated tickers; omit to use TRADINGAGENTS_WATCHLIST from .env"),
    date: str = typer.Option(None, "--date", help="Analysis date, YYYY-MM-DD; default today"),
    runs: int = typer.Option(None, "--runs", min=1, help="Runs per ticker (majority vote); default TRADINGAGENTS_VOTE_RUNS"),
    from_file: str = typer.Option(
        None, "--from-file",
        help="Also analyze the tickers in this file (one per line, or a CSV with a ticker column), "
             "e.g. the screens/latest.txt that `tradingagents screen` writes"),
    portfolio: str = typer.Option(None, "--portfolio", help="Portfolio JSON for sizing; default TRADINGAGENTS_PORTFOLIO"),
    no_portfolio: bool = typer.Option(False, "--no-portfolio", help="Size as weights of equity, ignoring "
                                                                    "TRADINGAGENTS_PORTFOLIO"),
):
    """Analyze every ticker on the watchlist, unattended, and log each decision for the scoreboard."""
    from datetime import date as _date

    names = _watchlist(tickers)
    if from_file:
        try:
            names = list(dict.fromkeys(names + _tickers_from_file(from_file)))
        except OSError as exc:
            console.print(f"[red]Cannot read {from_file}: {exc}[/red]")
            raise typer.Exit(code=1) from None
    if not names:
        console.print("[red]No tickers: pass them (e.g. NVDA,600519.SS) or set TRADINGAGENTS_WATCHLIST in .env[/red]")
        raise typer.Exit(code=1)
    base = flags_with_config_defaults(
        {"date": date or _date.today().isoformat(), "save": True, "show": False, "html": True,
         "open": False, "runs": runs}, DEFAULT_CONFIG)
    base["analysts"] = base.get("analysts") or ALL_ANALYSTS
    # Nobody is there to answer a question, so everything a question would ask must be set.
    gaps = unattended_gaps({**base, "ticker": names[0]})
    if gaps:
        console.print("[red]An unattended run needs these set in .env (or as flags):[/red]")
        for gap in gaps:
            console.print(f"  {gap}")
        raise typer.Exit(code=1)

    if DEFAULT_CONFIG.get("daily_screen") and not tickers:
        # Candidates only: nothing joins the watchlist until you pick it
        # (tradingagents watchlist pick).
        _screen_picks(DEFAULT_CONFIG["daily_screen"], int(DEFAULT_CONFIG.get("daily_screen_top") or 20))
    book = None if no_portfolio else _portfolio(portfolio)
    if DEFAULT_CONFIG.get("paper_trading"):
        # Yesterday's orders fill at today's open before today's analyses size against the book.
        try:
            _, fills = _paper_account()
            if fills:
                console.print("[bold]Paper account fills:[/bold]")
                _print_fills(fills)
        except Exception as exc:
            console.print(f"[yellow]Paper account update failed: {exc}[/yellow]")
    done, failed, results = [], [], []
    for index, ticker in enumerate(names, 1):
        console.rule(f"[bold cyan]{index}/{len(names)} {ticker} {base['date']}")
        try:
            results.append(run_analysis(portfolio=book, flags={**base, "ticker": ticker}) or {})
            done.append(ticker)
        except (Exception, typer.Exit) as exc:   # one ticker must not end the watchlist
            failed.append((ticker, f"{type(exc).__name__}: {exc}"))
            console.print(f"[yellow]{ticker} failed: {exc}[/yellow]")
    console.rule("[bold]Daily run finished")
    console.print(f"Analyzed {len(done)}/{len(names)}: {', '.join(done) or '-'}")
    from tradingagents.names import name_of

    for result in results:
        sizing, ticker = result.get("sizing"), result.get("ticker")
        name = name_of(ticker) if ticker else None
        if sizing is not None:
            line = sizing_summary(sizing)
            console.print(f"  {line.replace(sizing.ticker, f'{sizing.ticker} {name}', 1) if name else line}")
        elif ticker:
            console.print(f"  {ticker}{' ' + name if name else ''}: {result.get('rating') or 'REVIEW'}")
    if book is None:
        console.print("[dim]No portfolio set (TRADINGAGENTS_PORTFOLIO): sizes are weights of equity, "
                      "not share counts.[/dim]")
    if DEFAULT_CONFIG.get("paper_trading"):
        try:
            from datetime import date as _d

            from tradingagents.dataflows.config import run_config
            from tradingagents.paper import render_status

            account, _ = _paper_account(update=False)
            with run_config(DEFAULT_CONFIG):
                console.print("\n" + render_status(account, _d.today().isoformat()))
        except Exception as exc:
            console.print(f"[yellow]Paper account summary failed: {exc}[/yellow]")
    for ticker, reason in failed:
        console.print(f"[yellow]failed:[/yellow] {ticker}: {reason}")
    _print_balance()
    console.print("Decisions are in the memory log; run [bold]tradingagents scoreboard[/bold] once they have settled.")


def _print_balance() -> None:
    """The model account's remaining balance, when the provider publishes one (never fails a run)."""
    try:
        from tradingagents import llm_balance

        provider = (DEFAULT_CONFIG.get("quick_think_provider") or DEFAULT_CONFIG.get("llm_provider") or "").lower()
        if llm_balance.supported(provider):
            console.print(llm_balance.summary_line(llm_balance.balance(provider, max_age=0)))
    except Exception:
        pass


@app.command()
def scoreboard(
    settle: bool = typer.Option(
        True, "--settle/--no-settle",
        help="First settle decisions whose holding window has traded (fetches prices and writes "
             "a short LLM reflection for each); --no-settle only reads the log"),
):
    """Score the live decisions: direction hit rate and alpha, by rating and by ticker."""
    from datetime import date as _date
    from pathlib import Path

    from tradingagents import scoreboard as sb

    config = DEFAULT_CONFIG
    if settle:
        try:
            from tradingagents.graph.trading_graph import TradingAgentsGraph

            with console.status("[cyan]Settling decisions whose holding window has traded…[/cyan]"):
                graph = TradingAgentsGraph(config=config)
                outcome = graph.settle_all_pending()
                revised = graph.update_beliefs()
            if getattr(outcome, "failed", None):
                console.print(f"[yellow]{len(outcome.failed)} decision(s) could not be settled now.[/yellow]")
            if revised:
                console.print(f"[green]Beliefs revised from {revised['based_on']} settled decisions:[/green]\n"
                              f"{revised['beliefs']}\n")
        except Exception as exc:   # still show what is already settled
            console.print(f"[yellow]Settling failed ({exc}); showing what is already settled.[/yellow]")
    board = sb.build(config["memory_log_path"])
    console.print(sb.render_markdown(board, _date.today().isoformat()))
    base = Path(config.get("reports_dir") or Path(config["results_dir"]) / "reports").expanduser()
    md, csv_path = sb.write(board, base, _date.today().isoformat())
    console.print(f"\n[dim]Saved:[/dim] {md}\n[dim]       [/dim] {csv_path}")


@app.command()
def risk(
    tickers: str = typer.Argument(
        None, help="Comma-separated tickers; omit to use TRADINGAGENTS_WATCHLIST from .env"),
    rating: str = typer.Option("Buy", "--rating", help="Rating to size for: Buy/Overweight/Hold/Underweight/Sell"),
    date: str = typer.Option(None, "--date", help="As-of date, YYYY-MM-DD (default: today)"),
    portfolio: str = typer.Option(None, "--portfolio", help="Holdings JSON (default: TRADINGAGENTS_PORTFOLIO)"),
):
    """Risk figures and position size for tickers, computed in code; no model is called."""
    from datetime import date as _date

    from tradingagents import risk as rk
    from tradingagents.dataflows.config import run_config

    names = _watchlist(tickers)
    if not names:
        console.print("[red]No tickers: pass them or set TRADINGAGENTS_WATCHLIST in .env[/red]")
        raise typer.Exit(code=1)
    book = _portfolio(portfolio)
    as_of = date or _date.today().isoformat()
    rating = rating.strip().capitalize()
    with run_config(DEFAULT_CONFIG):
        for ticker in names:
            console.rule(f"[bold cyan]{ticker} {as_of}")
            block = rk.context_block(ticker, as_of, book, DEFAULT_CONFIG)
            console.print(block or "[yellow]No price history: risk figures unavailable.[/yellow]")
            if not block:
                continue
            try:
                sizing = rk.size_position(ticker, rating, as_of, book, DEFAULT_CONFIG)
            except Exception as exc:
                console.print(f"[yellow]Could not size {ticker}: {exc}[/yellow]")
                continue
            console.print(f"\n[bold]If the rating were {rating}:[/bold] {rk.summary_line(sizing)}")
            for warning in sizing.warnings:
                console.print(f"  [yellow]•[/yellow] {warning}")


@app.command()
def screen(
    request: str = typer.Argument(None, help='What to look for, in plain language, e.g. "市盈率低于20、ROE超过15%的消费股"'),
    where: str = typer.Option(None, "--where", help='Conditions without the model, e.g. "pe<20, roe_annualized>15, mcap>100"'),
    limit: int = typer.Option(None, "--limit", min=1, max=200, help="How many to keep (default 20)"),
    yes: bool = typer.Option(False, "--yes", help="Run the interpreted conditions without asking"),
    add: str = typer.Option(None, "--add", help='Rows to add to the watchlist without the picker, e.g. "1,3" or "1-3" or "all"'),
    no_pick: bool = typer.Option(False, "--no-pick", help="Only show and save the results; add nothing"),
):
    """Screen every A-share: plain language becomes conditions you check, then code filters the market."""
    from tradingagents import screener
    from tradingagents.dataflows.config import run_config

    if not request and not where:
        console.print('[red]Say what to look for, e.g. tradingagents screen "低估值高ROE的白酒股", or use --where.[/red]')
        raise typer.Exit(code=1)
    with run_config(DEFAULT_CONFIG):
        with console.status("[cyan]Loading every A-share's quote and latest results…[/cyan]"):
            universe, period = screener.build_universe(DEFAULT_CONFIG)
    if where:
        spec = screener.parse_where(where)
    else:
        # Asked outside any spinner or run scope, the way scripts/probe_screen_llm.py
        # does: a call made under the spinner was seen never to return on Windows.
        from tradingagents.llm_clients import create_tier_client

        industries = sorted(universe["industry"].dropna().astype(str).unique()) if "industry" in universe else []
        llm = create_tier_client(DEFAULT_CONFIG, "quick").get_llm()
        console.print(f"Asking {DEFAULT_CONFIG.get('quick_think_llm')} to turn the request into conditions "
                      f"(usually 10-30 s)…")
        spec = screener.interpret(request, llm, industries, period, universe.attrs.get("missing", []),
                                  progress=lambda s: console.print(f"  … {s:.0f}s"))
    with run_config(DEFAULT_CONFIG):
        if limit:
            spec.limit = limit
        console.print(f"[dim]Quotes from {universe.attrs.get('source', '?')}; "
                      f"{len(universe):,} A-shares; results from the {period} report.[/dim]")
        gone = screener.unavailable(spec, universe)
        if gone:
            console.print(f"[yellow]Not available from today's source, so these match nothing: "
                          f"{', '.join(gone)}[/yellow]")
        console.print("[bold]Conditions:[/bold]")
        if spec.explanation:
            console.print(f"  {spec.explanation}")
        console.print(spec.describe())
        if not where and not yes and sys.stdin.isatty() and not typer.confirm("Run these conditions?", default=True):
            raise typer.Exit()
        result = screener.apply(spec, universe)
    console.print("\n" + screener.render(result, spec, period))
    if result.empty:
        return
    csv_path, latest = screener.save(result, spec, request or where, DEFAULT_CONFIG)
    console.print(f"\n[dim]Saved:[/dim] {csv_path}")
    if not no_pick:
        _pick_into_watchlist(result, note=f"screen: {(request or where)[:40]}", rows=add)


def _pick_into_watchlist(result, note: str, rows: str | None = None) -> list[str]:
    """Ask which screened rows join the watchlist; add only those. Returns the tickers added."""
    from tradingagents import watchlist as wl

    if result is None or result.empty:
        return []
    labels = [f"{i}. {r['ticker']} {r.get('name', '')} | {r.get('industry') or '—'} | "
              f"PE {_num(r.get('pe'))} | ROE {_num(r.get('roe_annualized'))}%"
              for i, (_, r) in enumerate(result.iterrows(), 1)]
    picks: list[int] = []
    if rows is not None:
        picks = wl.parse_picks(rows, len(result))
    elif sys.stdin.isatty():
        try:
            import questionary

            chosen = questionary.checkbox(
                "Add which to the watchlist? (Space to tick, a = all, Enter to finish; none ticked adds nothing)",
                choices=[questionary.Choice(label, value=i) for i, label in enumerate(labels)],
            ).ask()
            picks = list(chosen or [])
        except Exception:   # no full console (e.g. some IDE terminals): type row numbers instead
            while True:
                text = typer.prompt("Add which rows to the watchlist? (e.g. 1,3 or 1-3, all; Enter adds none)",
                                    default="", show_default=False)
                try:
                    picks = wl.parse_picks(text, len(result))
                    break
                except ValueError as exc:
                    console.print(f"[yellow]{exc}[/yellow]")
    if not picks:
        console.print("[dim]Nothing added to the watchlist. Pick later with: tradingagents watchlist pick[/dim]")
        return []
    chosen = [result["ticker"].iloc[i] for i in picks]
    added = wl.add(DEFAULT_CONFIG, chosen, note)
    already = [t for t in chosen if t not in added]
    if added:
        console.print(f"[green]Added to the watchlist:[/green] {', '.join(added)}  ({wl.path(DEFAULT_CONFIG)})")
    if already:
        console.print(f"[dim]Already watched: {', '.join(already)}[/dim]")
    if added and sys.stdin.isatty() and typer.confirm(
            f"Analyze the {len(added)} added now? (about ¥2.5 each with 2 runs; otherwise the next daily run does it)",
            default=False):
        daily(tickers=",".join(added), date=None, runs=None, from_file=None)
    return added


def _num(v) -> str:
    try:
        return "—" if v is None or v != v else f"{float(v):.1f}"
    except (TypeError, ValueError):
        return "—"


watch_app = typer.Typer(help="The watchlist daily analyzes: .env's TRADINGAGENTS_WATCHLIST plus watchlist.txt.")
app.add_typer(watch_app, name="watchlist")


@watch_app.command("list")
def watchlist_list():
    """Show every watched ticker and where it came from."""
    from tradingagents import watchlist as wl

    for t in wl.from_env(DEFAULT_CONFIG):
        console.print(f"  {t}  [dim](.env TRADINGAGENTS_WATCHLIST)[/dim]")
    for t, note in wl.entries(DEFAULT_CONFIG):
        console.print(f"  {t}  [dim]({note or 'watchlist.txt'})[/dim]")
    console.print(f"[dim]File: {wl.path(DEFAULT_CONFIG)}[/dim]")


@watch_app.command("add")
def watchlist_add(tickers: str = typer.Argument(..., help="Comma-separated tickers")):
    """Add tickers to watchlist.txt."""
    from tradingagents import watchlist as wl

    added = wl.add(DEFAULT_CONFIG, [t for t in tickers.replace(";", ",").split(",") if t.strip()], "added by hand")
    console.print(f"Added: {', '.join(added) or 'nothing new'}")


@watch_app.command("remove")
def watchlist_remove(tickers: str = typer.Argument(..., help="Comma-separated tickers")):
    """Remove tickers from watchlist.txt (names in .env stay; edit .env for those)."""
    from tradingagents import watchlist as wl

    gone = wl.remove(DEFAULT_CONFIG, [t for t in tickers.replace(";", ",").split(",") if t.strip()])
    console.print(f"Removed: {', '.join(gone) or 'none of those are in watchlist.txt'}")


@watch_app.command("pick")
def watchlist_pick(
    file: str = typer.Option(None, "--file", help="A saved screen CSV (default: the latest in <reports>/screens)"),
    add: str = typer.Option(None, "--add", help='Rows to add without the picker, e.g. "1,3"'),
):
    """Choose from a saved screen's results which tickers join the watchlist."""
    from pathlib import Path

    import pandas as pd

    from tradingagents import screener

    if file:
        path = Path(file)
    else:
        base = Path(DEFAULT_CONFIG.get("reports_dir") or Path(DEFAULT_CONFIG["results_dir"]) / "reports").expanduser()
        saved = sorted((base / "screens").glob("screen_*.csv"))
        if not saved:
            console.print("[yellow]No saved screen yet. Run: tradingagents screen \"…\"[/yellow]")
            raise typer.Exit(code=1)
        path = saved[-1]
    result = pd.read_csv(path, encoding="utf-8-sig", dtype={"code": str})
    console.print(f"[dim]{path}[/dim]\n")
    console.print(screener.render(result, screener.Spec(), "saved"))
    _pick_into_watchlist(result, note=f"screen {path.stem[7:]}", rows=add)


server_app = typer.Typer(help="The system service: database, accounts, and the web API.")
app.add_typer(server_app, name="server")


@server_app.command("init")
def server_init(
    admin: str = typer.Option(..., "--admin", help="Administrator username"),
    import_files: bool = typer.Option(True, "--import/--no-import",
                                      help="Bring this computer's watchlist, portfolio, paper account and "
                                           "cost ledger into the administrator's account"),
):
    """Create the database and the administrator account (asks for the password)."""
    from server import db
    from server.importer import import_all
    from server.services import users

    db.init_db()
    password = typer.prompt("Administrator password (at least 8 characters)", hide_input=True,
                            confirmation_prompt=True)
    with db.session() as s:
        if users.by_name(s, admin):
            console.print(f"[yellow]User {admin!r} already exists; nothing created.[/yellow]")
            raise typer.Exit(code=1)
        user = users.create_user(s, admin, password, role="admin")
        summary = import_all(s, user.id, DEFAULT_CONFIG) if import_files else {}
    console.print(f"[green]Database ready:[/green] {db.default_url()}")
    console.print(f"[green]Administrator:[/green] {admin}")
    if summary:
        console.print(f"Imported: watchlist {len(summary['watchlist'])}, holdings {summary['holdings']}, "
                      f"cost entries {summary['costs']}, paper account {'yes' if summary['paper'] else 'no'}")


@server_app.command("adduser")
def server_adduser(
    username: str = typer.Argument(..., help="New username"),
    admin: bool = typer.Option(False, "--admin", help="Give administrator rights"),
    quota: float = typer.Option(None, "--quota", help="Monthly model spending cap (price currency); omit for none"),
):
    """Add an account (asks for its password)."""
    from server import db
    from server.services import users

    db.init_db()
    password = typer.prompt("Password (at least 8 characters)", hide_input=True, confirmation_prompt=True)
    with db.session() as s:
        user = users.create_user(s, username, password, role="admin" if admin else "user")
        user.monthly_quota = quota
    console.print(f"[green]Created[/green] {username} ({'admin' if admin else 'user'})")


@server_app.command("run")
def server_run(
    host: str = typer.Option("127.0.0.1", "--host", help="Address to listen on. 127.0.0.1: this computer only; "
                                                        "0.0.0.0: every network (LAN / Tailscale)"),
    port: int = typer.Option(8000, "--port"),
    worker: bool = typer.Option(True, "--worker/--no-worker", help="Run queued jobs in this process"),
    scheduler: bool = typer.Option(True, "--scheduler/--no-scheduler", help="Queue each user's daily analysis"),
):
    """Start the web service (API; interactive docs at /docs)."""
    import uvicorn

    from server import db
    from server.app import create_app
    from server.services import users

    db.init_db()
    with db.session() as s:
        if not users.admins(s):
            console.print("[red]No administrator yet: run `tradingagents server init --admin <name>` first.[/red]")
            raise typer.Exit(code=1)
    from server.access import allowed_networks, local_addresses

    try:
        nets = allowed_networks()
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    console.print(f"[green]TradingAgents service[/green] on this computer: http://127.0.0.1:{port}  "
                  f"(API docs at /docs); database {db.default_url()}")
    if host not in ("127.0.0.1", "localhost", "::1"):
        names = {"tailscale": "Tailscale (phone, laptop anywhere)", "lan": "home network", "other": "other"}
        for addr, kind in local_addresses():
            console.print(f"  {names[kind]:<36} http://{addr}:{port}")
        console.print(f"[dim]Accepted from: {', '.join(str(n) for n in nets)} "
                      "(TRADINGAGENTS_ALLOWED_NETS). Use strong passwords.[/dim]")
    uvicorn.run(create_app(DEFAULT_CONFIG, start_worker=worker, start_scheduler=scheduler),
                host=host, port=port, log_level="info")


SERVER_TASK = "TradingAgents Server"
service_app = typer.Typer(help="Run the web service by itself after you sign in to Windows.")
server_app.add_typer(service_app, name="service")


def _startup_launcher() -> "Path":
    """The launcher in the user's Startup folder (runs at sign-in; no administrator rights needed)."""
    import os
    from pathlib import Path

    base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / f"{SERVER_TASK}.vbs"


@service_app.command("install")
def service_install(
    host: str = typer.Option("0.0.0.0", "--host", help="0.0.0.0: reachable from the home network and Tailscale; "
                                                      "127.0.0.1: this computer only"),
    port: int = typer.Option(8000, "--port"),
    start: bool = typer.Option(True, "--start/--no-start", help="Start it now as well"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Restart even if an analysis is running"),
):
    """Run the service in the background each time you sign in to Windows (scripts/run_server.ps1).

    A small launcher goes in your Startup folder, so no administrator rights are needed.
    """
    import subprocess

    script = _project_dir() / "scripts" / "run_server.ps1"
    if not _on_windows():
        console.print(f"Not Windows: run `tradingagents server run --host {host} --port {port}` under systemd "
                      "or a process manager instead.")
        return
    if start and not _confirm_interrupt(yes):
        raise typer.Exit(code=1)
    command = (f'powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""{script}"" '
               f"-BindHost {host} -Port {port}")
    launcher = _startup_launcher()
    launcher.parent.mkdir(parents=True, exist_ok=True)
    # Window style 0: no console window at all; False: do not wait for it.
    launcher.write_text(f'CreateObject("WScript.Shell").Run "{command}", 0, False\r\n', encoding="utf-16")
    console.print(f"[green]Installed:[/green] {launcher}")
    if start:
        _stop_service_processes()          # a previous copy would hold the port
        subprocess.Popen(["wscript.exe", str(launcher)])
        console.print("Started in the background; it answers in about 10 seconds "
                      "(check with: tradingagents server service status).")
    console.print(f"The service starts each time you sign in to Windows and restarts itself if it stops. "
                  f"Logs: {_project_dir() / 'logs'}\\server_<date>.log")
    console.print("Keep the computer awake (Settings > System > Power: never sleep when plugged in), or the "
                  "service and the daily schedule pause with it.")
    if host not in ("127.0.0.1", "localhost"):
        console.print("Other devices also need the firewall opened once, from an administrator window: "
                      "[bold]tradingagents server firewall[/bold]")


def _running_jobs() -> list:
    """Jobs the service is running now (a restart would cut them off)."""
    try:
        from server import db
        from server.models import Job
        from server.services import jobs

        db.init_db()
        with db.session() as s:
            return [(j.id, j.kind, j.params) for j in jobs.active(s) if j.status in ("running", "cancelling")]
    except Exception:
        return []


def _confirm_interrupt(yes: bool) -> bool:
    running = _running_jobs()
    if not running or yes:
        return True
    for jid, kind, params in running:
        what = ", ".join(params.get("tickers") or []) if kind == "analysis" else params.get("request", "")
        console.print(f"[yellow]Job {jid} ({kind}: {what}) is running; restarting the service stops it "
                      f"(it is marked failed and can be run again).[/yellow]")
    return typer.confirm("Restart anyway?", default=False)


@service_app.command("remove")
def service_remove(yes: bool = typer.Option(False, "--yes", "-y", help="Stop even if an analysis is running")):
    """Stop the service and stop it starting at sign-in."""
    if not _on_windows():
        console.print("Not Windows: nothing to remove.")
        return
    if not _confirm_interrupt(yes):
        raise typer.Exit(code=1)
    launcher = _startup_launcher()
    if launcher.exists():
        launcher.unlink()
        console.print(f"Removed {launcher}")
    code, _ = _schtasks("/Query", "/TN", SERVER_TASK)       # an older install used a scheduled task
    if code == 0:
        _schtasks("/Delete", "/F", "/TN", SERVER_TASK)
    _stop_service_processes()
    console.print("[green]The service is stopped and will not start at sign-in.[/green]")


def _stop_service_processes() -> None:
    """End the hidden PowerShell loop and the server it started."""
    import subprocess

    # The loop, the server, and any analysis the server started (`python -m cli.main daily`;
    # a `tradingagents daily` you run yourself is left alone).
    script = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'run_server.ps1' -or "
              "$_.CommandLine -match 'tradingagents.*server run' -or $_.CommandLine -match '-m cli.main daily' } | "
              "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True)


@service_app.command("status")
def service_status(port: int = typer.Option(8000, "--port")):
    """Is it set to start at sign-in, and does the service answer?"""
    import urllib.request

    if _on_windows():
        launcher = _startup_launcher()
        if launcher.exists():
            console.print(f"Starts at sign-in: yes ({launcher})")
        else:
            console.print("[yellow]Starts at sign-in: no (install with `tradingagents server service install`)[/yellow]")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3) as resp:
            console.print(f"[green]The service answers on port {port}.[/green] {resp.read().decode()[:80]}")
    except Exception as exc:
        console.print(f"[yellow]Nothing answers on port {port}: {exc}[/yellow]")


@server_app.command("firewall")
def server_firewall(
    port: int = typer.Option(8000, "--port"),
    lan: bool = typer.Option(True, "--lan/--no-lan", help="Also allow the home network, not only Tailscale"),
):
    """Open the port in Windows Firewall for Tailscale (and the home network). Needs an administrator window."""
    import subprocess

    remote = "100.64.0.0/10" + (",LocalSubnet" if lan else "")
    args = ["netsh", "advfirewall", "firewall", "add", "rule", "name=TradingAgents web service", "dir=in",
            "action=allow", "protocol=TCP", f"localport={port}", f"remoteip={remote}", "profile=any"]
    if not _on_windows():
        console.print("Not Windows: allow the port in your firewall for 100.64.0.0/10 (Tailscale) and your LAN.")
        return
    subprocess.run(["netsh", "advfirewall", "firewall", "delete", "rule", "name=TradingAgents web service"],
                   capture_output=True, text=True)
    done = subprocess.run(args, capture_output=True, text=True)
    if done.returncode != 0:
        console.print("[yellow]Windows refused (this needs administrator rights). Open PowerShell with "
                      "\"Run as administrator\" and run:[/yellow]")
        console.print("  " + " ".join(a if " " not in a else f'"{a}"' for a in args))
        raise typer.Exit(code=1)
    console.print(f"[green]Firewall opened[/green] for port {port}: Tailscale"
                  f"{' and the home network' if lan else ''} only.")


@server_app.command("backup")
def server_backup():
    """Copy the database now (the running service also does this every night at 03:00, keeping 14)."""
    from server.backup import backup

    try:
        path = backup()
    except Exception as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    console.print(f"[green]Backed up[/green] to {path}")


@server_app.command("users")
def server_users():
    """List the accounts."""
    from sqlalchemy import select

    from server import db
    from server.models import User
    from server.services import costs

    db.init_db()
    with db.session() as s:
        for u in s.scalars(select(User).order_by(User.id)):
            quota = "no cap" if u.monthly_quota is None else f"cap {u.monthly_quota:,.2f}"
            console.print(f"  {u.id:>3}  {u.username:<16} {u.role:<6} {'active' if u.is_active else 'disabled':<8} "
                          f"this month {costs.month_spend(s, u.id):,.2f} ({quota})")


data_app = typer.Typer(help="The data gateway: which sources serve A-share/HK data, and how they are doing.")
app.add_typer(data_app, name="datasource")

_STATE_STYLE = {"ok": "green", "unused": "dim", "failing": "yellow", "circuit open": "red",
                "not configured": "dim", "empty": "cyan", "bad data": "red", "failed": "red"}


def _ago(stamp) -> str:
    import time as _time

    if not stamp:
        return "-"
    secs = _time.time() - stamp
    if secs < 120:
        return f"{secs:.0f}s ago"
    if secs < 7200:
        return f"{secs / 60:.0f}m ago"
    if secs < 172800:
        return f"{secs / 3600:.0f}h ago"
    return f"{secs / 86400:.0f}d ago"


@data_app.command("status")
def datasource_status():
    """Each capability's sources in priority order, with their record and circuit state."""
    from rich.table import Table

    from tradingagents.datagw import gateway

    table = Table(show_lines=False)
    for col in ("Data", "#", "Source", "State", "OK", "Fail", "Last OK", "Latency", "Last error / note"):
        table.add_column(col)
    for r in gateway(DEFAULT_CONFIG).status():
        style = _STATE_STYLE.get(r["state"], "")
        note = r["note"] or (r["last_error"] or "" if r["state"] != "ok" else "")
        if r["open_until"]:
            import time as _time

            note = f"skipped for {(r['open_until'] - _time.time()) / 60:.0f} more min; " + note
        table.add_row(r["title"], str(r["rank"]), r["label"], f"[{style}]{r['state']}[/{style}]",
                      str(r["ok"]), str(r["fail"]), _ago(r["last_ok"]),
                      f"{r['latency_ms']} ms" if r["latency_ms"] is not None else "-", note[:70])
    console.print(table)


@data_app.command("check")
def datasource_check(
    ticker: str = typer.Option("600519.SS", "--ticker", help="A-share used for the checks"),
    hk: str = typer.Option("0700.HK", "--hk", help="Hong Kong listing used for the bar check"),
    spot: bool = typer.Option(False, "--spot", help="Also download the whole-market quote table from each "
                                                    "source (slow: Sina takes about a minute)"),
):
    """Ask every source for a small sample now and show which answer (no model is called)."""
    from datetime import date, timedelta

    import pandas as pd

    from tradingagents.datagw import gateway

    gw = gateway(DEFAULT_CONFIG)
    end = pd.Timestamp(date.today())
    probes = [
        ("daily_bars", {"ticker": ticker, "start": end - timedelta(days=14), "end": end}, ticker),
        ("daily_bars", {"ticker": hk, "start": end - timedelta(days=14), "end": end}, hk),
        ("lhb", {"ticker": ticker, "start": end - timedelta(days=10), "end": end}, ticker),
        ("margin", {"ticker": ticker, "day": end - timedelta(days=1 if end.weekday() else 3)}, ticker),
    ]
    if spot:
        probes.append(("cn_spot", {"day": f"{end:%Y-%m-%d}"}, "all A-shares"))
    for cap_name, kwargs, what in probes:
        cap = gw.capabilities[cap_name]
        console.print(f"[bold]{cap.title}[/bold] ({what})")
        for p in gw.providers(cap_name):
            state, detail, took = gw.probe(cap_name, p.name, **kwargs)
            style = _STATE_STYLE.get(state, "")
            console.print(f"  {p.label:<12} [{style}]{state:<15}[/{style}] {took:5.1f}s  {detail[:90]}")
    if not spot:
        console.print("[dim]Whole-market quotes not checked; add --spot to include them.[/dim]")


@data_app.command("reset")
def datasource_reset():
    """Clear every source's record and close all circuits (after fixing a network problem)."""
    from tradingagents.datagw import gateway

    gateway(DEFAULT_CONFIG).health.reset()
    console.print("[green]Source records cleared; every source will be tried again.[/green]")


@app.command()
def balance():
    """The model provider's remaining account balance (DeepSeek), read with the key in .env."""
    from tradingagents import llm_balance

    provider = (DEFAULT_CONFIG.get("quick_think_provider") or DEFAULT_CONFIG.get("llm_provider") or "").lower()
    if not llm_balance.supported(provider):
        provider = "deepseek"
    info = llm_balance.balance(provider, max_age=0)
    console.print(llm_balance.summary_line(info), style="green" if info.get("ok") else "yellow")


paper_app = typer.Typer(help="The paper-trading account: simulated fills of the system's calls.")
app.add_typer(paper_app, name="paper")


def _paper_account(update: bool = True):
    from tradingagents.dataflows.config import run_config
    from tradingagents.paper import PaperAccount

    with run_config(DEFAULT_CONFIG):
        account = PaperAccount(DEFAULT_CONFIG)
        fills = account.update() if update else []
    return account, fills


def _print_fills(fills) -> None:
    for f in fills:
        colour = "green" if f.status == "filled" else "yellow"
        console.print(f"  [{colour}]{f.date} {f.side} {f.qty:,.0f} {f.ticker} @ {f.price:,.4g} "
                      f"({f.status}: {f.reason})[/{colour}]")


@paper_app.command("status")
def paper_status():
    """Fill due orders, run the stops, and show the account."""
    from datetime import date as _date

    from tradingagents.dataflows.config import run_config
    from tradingagents.paper import render_status

    account, fills = _paper_account()
    if fills:
        console.print("[bold]New fills:[/bold]")
        _print_fills(fills)
    with run_config(DEFAULT_CONFIG):
        console.print(render_status(account, _date.today().isoformat()))
    console.print(f"\n[dim]Files:[/dim] {account.dir}")


@paper_app.command("reset")
def paper_reset(
    cash: float = typer.Option(None, "--cash", help="Starting cash (default: TRADINGAGENTS_PAPER_CASH)"),
    currency: str = typer.Option(None, "--currency", help="Account currency (default: CNY)"),
    yes: bool = typer.Option(False, "--yes", help="Do not ask for confirmation"),
):
    """Start the paper account over (old trades and equity files are kept, renamed)."""
    from tradingagents.paper import PaperAccount

    account = PaperAccount(DEFAULT_CONFIG)
    cash = cash or float(DEFAULT_CONFIG.get("paper_cash") or 1_000_000)
    currency = currency or DEFAULT_CONFIG.get("paper_currency") or "CNY"
    if not yes and not typer.confirm(f"Reset the paper account to {cash:,.0f} {currency}?"):
        raise typer.Exit()
    account.reset(cash, currency)
    console.print(f"Paper account reset: {cash:,.0f} {currency.upper()} in {account.dir}")


schedule_app = typer.Typer(help="Run the daily analysis on a schedule (Windows Task Scheduler).")
app.add_typer(schedule_app, name="schedule")

TASK_NAME = "TradingAgents Daily"


def _project_dir():
    from pathlib import Path

    return Path(__file__).resolve().parents[1]


def _schtasks(*args) -> tuple[int, str]:
    import subprocess

    done = subprocess.run(["schtasks", *args], capture_output=True, text=True)
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def _on_windows() -> bool:
    return sys.platform == "win32"


@schedule_app.command("install")
def schedule_install(
    time_of_day: str = typer.Option("18:30", "--time", help="Local time, HH:MM"),
    days: str = typer.Option("MON,TUE,WED,THU,FRI", "--days", help="Days, e.g. MON,TUE,WED,THU,FRI"),
    name: str = typer.Option(TASK_NAME, "--name", help="Task name"),
):
    """Create (or replace) the task that runs scripts/run_daily.ps1: paper fills, daily analysis, scoreboard."""
    script = _project_dir() / "scripts" / "run_daily.ps1"
    command = f'powershell -NoProfile -ExecutionPolicy Bypass -File "{script}"'
    if not _on_windows():
        hh, mm = time_of_day.split(":")
        console.print("Not Windows: add this line with `crontab -e` instead:\n"
                      f"{int(mm)} {int(hh)} * * {days} cd {_project_dir()} && tradingagents daily && tradingagents scoreboard")
        return
    code, out = _schtasks("/Create", "/F", "/TN", name, "/SC", "WEEKLY", "/D", days.upper(), "/ST", time_of_day,
                          "/TR", command)
    console.print(out.strip())
    if code != 0:
        raise typer.Exit(code=code)
    console.print(f"[green]Scheduled:[/green] {name}, {days.upper()} at {time_of_day}. Logs go to "
                  f"{_project_dir() / 'logs'}. The computer must be on and you logged in at that time.")


@schedule_app.command("list")
def schedule_list(name: str = typer.Option(TASK_NAME, "--name", help="Task name")):
    """Show the task's next and last run and its result (0 means success)."""
    if not _on_windows():
        console.print("Not Windows: see `crontab -l`.")
        return
    code, out = _schtasks("/Query", "/TN", name, "/V", "/FO", "LIST")
    if code != 0:
        console.print(f"[yellow]No task named {name!r}.[/yellow] Create it with: tradingagents schedule install")
        raise typer.Exit(code=1)
    keep = ("TaskName", "Next Run Time", "Last Run Time", "Last Result", "Status", "Task To Run", "Days",
            "Start Time", "任务名", "下次运行时间", "上次运行时间", "上次结果", "状态", "要运行的任务", "天", "开始时间")
    lines = [ln for ln in out.splitlines() if ln.split(":", 1)[0].strip() in keep]
    console.print("\n".join(lines) or out)


@schedule_app.command("remove")
def schedule_remove(name: str = typer.Option(TASK_NAME, "--name", help="Task name")):
    """Delete the scheduled task."""
    if not _on_windows():
        console.print("Not Windows: remove the line with `crontab -e`.")
        return
    code, out = _schtasks("/Delete", "/TN", name, "/F")
    console.print(out.strip())
    raise typer.Exit(code=code)


@schedule_app.command("run")
def schedule_run(name: str = typer.Option(TASK_NAME, "--name", help="Task name")):
    """Start the scheduled task now (it runs in the background; see logs/)."""
    if not _on_windows():
        console.print("Not Windows: run scripts/run_daily.ps1 or `tradingagents daily` directly.")
        return
    code, out = _schtasks("/Run", "/TN", name)
    console.print(out.strip())
    raise typer.Exit(code=code)


if __name__ == "__main__":
    app()
