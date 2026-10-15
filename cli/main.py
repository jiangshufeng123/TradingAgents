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


def _watchlist(tickers: str | None) -> list[str]:
    text = tickers or DEFAULT_CONFIG.get("watchlist") or ""
    return list(dict.fromkeys(t.strip().upper() for t in str(text).replace(";", ",").split(",") if t.strip()))


@app.command()
def daily(
    tickers: str = typer.Argument(
        None, help="Comma-separated tickers; omit to use TRADINGAGENTS_WATCHLIST from .env"),
    date: str = typer.Option(None, "--date", help="Analysis date, YYYY-MM-DD; default today"),
    runs: int = typer.Option(None, "--runs", min=1, help="Runs per ticker (majority vote); default TRADINGAGENTS_VOTE_RUNS"),
):
    """Analyze every ticker on the watchlist, unattended, and log each decision for the scoreboard."""
    from datetime import date as _date

    names = _watchlist(tickers)
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

    book = _portfolio(None)
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
    for result in results:
        sizing = result.get("sizing")
        if sizing is not None:
            console.print(f"  {sizing_summary(sizing)}")
        elif result.get("ticker"):
            console.print(f"  {result['ticker']}: {result.get('rating') or 'REVIEW'}")
    if book is None:
        console.print("[dim]No portfolio set (TRADINGAGENTS_PORTFOLIO): sizes are weights of equity, "
                      "not share counts.[/dim]")
    for ticker, reason in failed:
        console.print(f"[yellow]failed:[/yellow] {ticker}: {reason}")
    console.print("Decisions are in the memory log; run [bold]tradingagents scoreboard[/bold] once they have settled.")


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


if __name__ == "__main__":
    app()
