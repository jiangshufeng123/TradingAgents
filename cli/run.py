"""Running one analysis from the CLI: build the graph, stream it into the live view, save the report."""

import os
import sys
import time
import webbrowser
from functools import wraps
from pathlib import Path

import typer
from rich.live import Live

from cli.display import (
    ANALYST_ORDER,
    AnalystWallTimeTracker,
    classify_message_type,
    console,
    create_layout,
    display_complete_report,
    message_buffer,
    update_analyst_statuses,
    update_display,
    update_research_team_status,
)
from cli.selections import depth_from_env, get_user_selections, unattended_gaps
from cli.stats_handler import StatsCallbackHandler
from tradingagents.agents.rating import is_review, run_rating
from tradingagents.dataflows.symbols import safe_ticker_component
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.analyst_execution import (
    build_analyst_execution_plan,
)
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.dataflows.config import run_config
from tradingagents.risk import render_markdown as render_sizing
from tradingagents.risk import size_position
from tradingagents.risk import summary_line as sizing_summary
from tradingagents.usage import append_ledger, ledger_row, render_markdown, summarize
from tradingagents.vote import agreement_label, consensus
from tradingagents.vote import render_markdown as render_vote


def _run_directory(config: dict, ticker: str, trade_date: str) -> Path:
    """Where this run writes, with the ticker validated as a path component.

    Every other path that interpolates a ticker checks it first; a value of
    ".." here would place the run outside the results directory.
    """
    return Path(config["results_dir"]) / safe_ticker_component(ticker) / trade_date


def _announce_checkpoint_state(graph, ticker: str, trade_date: str) -> None:
    """Say whether this run resumed a saved one, where the user can see it.

    The graph logs this, but nothing in the CLI configures logging and the live
    view owns the screen, so a resume was invisible.
    """
    if getattr(graph, "_resuming", False):
        message_buffer.add_message(
            "System", f"Resuming the saved run for {ticker} on {trade_date}"
        )
    else:
        message_buffer.add_message("System", f"Starting fresh for {ticker} on {trade_date}")


def _build_run_config(selections: dict, checkpoint: bool | None) -> dict:
    """Assemble the run config from interactive selections, honoring env precedence.

    Round counts and checkpoint follow "explicit env/flag wins": an env-applied
    value on DEFAULT_CONFIG is preserved unless the user overrode it on the CLI.
    """
    config = DEFAULT_CONFIG.copy()
    # Research depth sets both round counts, but an explicit env override
    # (TRADINGAGENTS_MAX_DEBATE_ROUNDS / _MAX_RISK_ROUNDS) wins over the
    # interactive selection — leave the env-applied value in place (#977).
    rounds = (("TRADINGAGENTS_MAX_DEBATE_ROUNDS", "max_debate_rounds"),
              ("TRADINGAGENTS_MAX_RISK_ROUNDS", "max_risk_discuss_rounds"))
    depth_was_asked = not depth_from_env()
    for env_var, key in rounds:
        if not os.environ.get(env_var):
            config[key] = selections["research_depth"]
        elif depth_was_asked:
            # The depth question appeared (it is skipped only when both are
            # set), so say which half of the answer the environment overrode.
            console.print(
                f"[green]✓ {key} from environment:[/green] {config[key]} "
                f"(set by {env_var}, so the research depth you chose does not apply to it)"
            )
    config["quick_think_llm"] = selections["quick_think_llm"]
    config["deep_think_llm"] = selections["deep_think_llm"]
    config["backend_url"] = selections["backend_url"]
    config["llm_provider"] = selections["llm_provider"].lower()
    # Provider-specific thinking configuration
    config["google_thinking_level"] = selections.get("google_thinking_level")
    config["openai_reasoning_effort"] = selections.get("openai_reasoning_effort")
    config["anthropic_effort"] = selections.get("anthropic_effort")
    config["output_language"] = selections.get("output_language", "English")
    # --checkpoint/--no-checkpoint overrides only when explicitly given; omitting
    # the flag preserves TRADINGAGENTS_CHECKPOINT_ENABLED / the default (#976).
    if checkpoint is not None:
        config["checkpoint_enabled"] = checkpoint
    return config


_BOOL_WORDS = {"true": True, "1": True, "yes": True, "y": True, "on": True,
               "false": False, "0": False, "no": False, "n": False, "off": False}


def _as_bool(value):
    """A config answer as True/False, or None when it is unset."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    word = str(value).strip().lower()
    if word not in _BOOL_WORDS:
        raise typer.BadParameter(f"expected true/false, got {value!r}")
    return _BOOL_WORDS[word]


def flags_with_config_defaults(flags: dict, config: dict) -> dict:
    """Command-line flags, with the config (.env) answering any left unset.

    TRADINGAGENTS_SAVE_REPORT / _SHOW_REPORT / _HTML_REPORT / _OPEN_REPORT answer
    the questions after a run, and TRADINGAGENTS_ANALYSTS picks the analysts; a
    flag given on the command line still wins.
    """
    merged = dict(flags)
    for flag, key in (("save", "save_report"), ("show", "show_report"),
                      ("html", "html_report"), ("open", "open_report")):
        if merged.get(flag) is None:
            merged[flag] = _as_bool(config.get(key))
    if merged.get("analysts") is None and config.get("default_analysts"):
        merged["analysts"] = str(config["default_analysts"])
    return merged


def run_analysis(checkpoint: bool | None = None, portfolio=None, flags=None):
    flags = flags_with_config_defaults(flags or {}, DEFAULT_CONFIG)
    # With no terminal nothing can answer a prompt: name every question still
    # open before any model is called, rather than stopping at the first one.
    if not (sys.stdin and sys.stdin.isatty()):
        gaps = unattended_gaps(flags)
        if gaps:
            console.print("[red]No terminal to answer the setup questions. Set:[/red]")
            for gap in gaps:
                console.print(f"  {gap}")
            raise typer.Exit(code=1)

    selections = get_user_selections(flags)

    config = _build_run_config(selections, checkpoint)
    vote_runs = _vote_runs(flags, config)

    stats_handler = StatsCallbackHandler()

    # Normalize analyst selection to predefined order (selection is a 'set', order is fixed)
    selected_set = {analyst.value for analyst in selections["analysts"]}
    selected_analyst_keys = [a for a in ANALYST_ORDER if a in selected_set]
    analyst_execution_plan = build_analyst_execution_plan(selected_analyst_keys)
    analyst_wall_time_tracker = AnalystWallTimeTracker(analyst_execution_plan)

    graph = TradingAgentsGraph(
        selected_analyst_keys,
        config=config,
        debug=True,
        callbacks=[stats_handler],
    )

    message_buffer.init_for_analysis(selected_analyst_keys)

    # Track start time for elapsed display
    start_time = time.time()

    results_dir = _run_directory(config, selections["ticker"], selections["analysis_date"])
    results_dir.mkdir(parents=True, exist_ok=True)
    report_dir = results_dir / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    log_file = results_dir / "message_tool.log"
    log_file.touch(exist_ok=True)

    def save_message_decorator(obj, func_name):
        func = getattr(obj, func_name)

        @wraps(func)
        def wrapper(*args, **kwargs):
            func(*args, **kwargs)
            timestamp, message_type, content = obj.messages[-1]
            content = content.replace("\n", " ")  # Replace newlines with spaces
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"{timestamp} [{message_type}] {content}\n")
        return wrapper

    def save_tool_call_decorator(obj, func_name):
        func = getattr(obj, func_name)

        @wraps(func)
        def wrapper(*args, **kwargs):
            func(*args, **kwargs)
            timestamp, tool_name, args = obj.tool_calls[-1]
            args_str = ", ".join(f"{k}={v}" for k, v in args.items())
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"{timestamp} [Tool Call] {tool_name}({args_str})\n")
        return wrapper

    def save_report_section_decorator(obj, func_name):
        func = getattr(obj, func_name)

        @wraps(func)
        def wrapper(section_name, content):
            func(section_name, content)
            if section_name in obj.report_sections and obj.report_sections[section_name] is not None:
                content = obj.report_sections[section_name]
                if content:
                    file_name = f"{section_name}.md"
                    text = "\n".join(str(item) for item in content) if isinstance(content, list) else content
                    with open(report_dir / file_name, "w", encoding="utf-8") as f:
                        f.write(text)
        return wrapper

    message_buffer.add_message = save_message_decorator(message_buffer, "add_message")
    message_buffer.add_tool_call = save_tool_call_decorator(message_buffer, "add_tool_call")
    message_buffer.update_report_section = save_report_section_decorator(message_buffer, "update_report_section")

    layout = create_layout()

    # The alternate screen keeps a layout taller than the window from redrawing
    # by scrolling; the final report prints after this block, on the normal screen.
    with Live(layout, refresh_per_second=4, screen=True):
        # Initial display
        update_display(layout, stats_handler=stats_handler, start_time=start_time)

        message_buffer.add_message("System", f"Selected ticker: {selections['ticker']}")
        if selections["asset_type"] != "stock":
            message_buffer.add_message("System", f"Detected asset type: {selections['asset_type']}")
        message_buffer.add_message(
            "System", f"Analysis date: {selections['analysis_date']}"
        )
        message_buffer.add_message(
            "System",
            f"Selected analysts: {', '.join(analyst.value for analyst in selections['analysts'])}",
        )
        update_display(layout, stats_handler=stats_handler, start_time=start_time)

        # The analysts start together.
        for spec in analyst_execution_plan.specs:
            message_buffer.update_agent_status(spec.agent_node, "in_progress")
            analyst_wall_time_tracker.mark_started(spec.key)
        update_display(layout, stats_handler=stats_handler, start_time=start_time)

        spinner_text = (
            f"Analyzing {selections['ticker']} on {selections['analysis_date']}..."
        )
        update_display(layout, spinner_text, stats_handler=stats_handler, start_time=start_time)

        # The same initial state propagate() builds, with the resolved
        # instrument identity; the graph's Memory Log step settles past
        # decisions and loads their lessons alongside the analysts.
        init_agent_state = graph.create_run_state(
            selections["ticker"], selections["analysis_date"], selections["asset_type"], portfolio
        )
        # Pass callbacks to graph config for tool execution tracking
        # (LLM tracking is handled separately via LLM constructor)
        args = graph.propagator.get_graph_args(callbacks=[stats_handler])

        # Recompile with a checkpointer and inject the thread_id so --checkpoint
        # actually saves and resumes on the CLI path (#1249); a no-op when
        # checkpointing is disabled. Torn down in the finally below.
        checkpoint_tid = graph.begin_checkpoint(
            selections["ticker"], selections["analysis_date"], selections["asset_type"], portfolio
        )
        if checkpoint_tid is not None:
            args.setdefault("config", {}).setdefault("configurable", {})["thread_id"] = checkpoint_tid
            _announce_checkpoint_state(graph, selections["ticker"], selections["analysis_date"])

        # Stream the analysis. On resume, feed None so LangGraph continues the
        # interrupted run instead of re-appending the initial state (#1249); the
        # try/finally tears the checkpointer down even if the stream raises.
        trace = []
        try:
            for messages, chunk in graph.stream_run(graph.checkpoint_input(init_agent_state), **args):
                for message in messages:
                    msg_id = getattr(message, "id", None)
                    if msg_id is not None:
                        if msg_id in message_buffer._processed_message_ids:
                            continue
                        message_buffer._processed_message_ids.add(msg_id)

                    msg_type, content = classify_message_type(message)
                    if content and content.strip():
                        message_buffer.add_message(msg_type, content)

                    if hasattr(message, "tool_calls") and message.tool_calls:
                        for tool_call in message.tool_calls:
                            if isinstance(tool_call, dict):
                                message_buffer.add_tool_call(tool_call["name"], tool_call["args"])
                            else:
                                message_buffer.add_tool_call(tool_call.name, tool_call.args)

                if chunk is None:   # a step inside an analyst's graph: messages only
                    update_display(layout, stats_handler=stats_handler, start_time=start_time)
                    continue

                update_analyst_statuses(
                    message_buffer,
                    chunk,
                    wall_time_tracker=analyst_wall_time_tracker,
                )

                # Research Team - Handle Investment Debate State
                if chunk.get("investment_debate_state"):
                    debate_state = chunk["investment_debate_state"]
                    bull_hist = debate_state.get("bull_history", "").strip()
                    bear_hist = debate_state.get("bear_history", "").strip()
                    judge = (chunk.get("investment_plan") or "").strip()

                    # Only update status when there's actual content
                    if bull_hist or bear_hist:
                        update_research_team_status("in_progress")
                    if bull_hist:
                        message_buffer.update_report_section(
                            "investment_plan", f"### Bull Researcher Analysis\n{bull_hist}"
                        )
                    if bear_hist:
                        message_buffer.update_report_section(
                            "investment_plan", f"### Bear Researcher Analysis\n{bear_hist}"
                        )
                    if judge:
                        message_buffer.update_report_section(
                            "investment_plan", f"### Research Manager Decision\n{judge}"
                        )
                        update_research_team_status("completed")
                        message_buffer.update_agent_status("Trader", "in_progress")

                # Trading Team
                if chunk.get("trader_investment_plan"):
                    message_buffer.update_report_section(
                        "trader_investment_plan", chunk["trader_investment_plan"]
                    )
                    if message_buffer.agent_status.get("Trader") != "completed":
                        message_buffer.update_agent_status("Trader", "completed")
                        message_buffer.update_agent_status("Aggressive Analyst", "in_progress")

                # Risk Management Team - Handle Risk Debate State
                if chunk.get("risk_debate_state"):
                    risk_state = chunk["risk_debate_state"]
                    agg_hist = risk_state.get("aggressive_history", "").strip()
                    con_hist = risk_state.get("conservative_history", "").strip()
                    neu_hist = risk_state.get("neutral_history", "").strip()
                    judge = (chunk.get("final_trade_decision") or "").strip()

                    if agg_hist:
                        if message_buffer.agent_status.get("Aggressive Analyst") != "completed":
                            message_buffer.update_agent_status("Aggressive Analyst", "in_progress")
                        message_buffer.update_report_section(
                            "final_trade_decision", f"### Aggressive Analyst Analysis\n{agg_hist}"
                        )
                    if con_hist:
                        if message_buffer.agent_status.get("Conservative Analyst") != "completed":
                            message_buffer.update_agent_status("Conservative Analyst", "in_progress")
                        message_buffer.update_report_section(
                            "final_trade_decision", f"### Conservative Analyst Analysis\n{con_hist}"
                        )
                    if neu_hist:
                        if message_buffer.agent_status.get("Neutral Analyst") != "completed":
                            message_buffer.update_agent_status("Neutral Analyst", "in_progress")
                        message_buffer.update_report_section(
                            "final_trade_decision", f"### Neutral Analyst Analysis\n{neu_hist}"
                        )
                    if judge and message_buffer.agent_status.get("Portfolio Manager") != "completed":
                        message_buffer.update_agent_status("Portfolio Manager", "in_progress")
                        message_buffer.update_report_section(
                            "final_trade_decision", f"### Portfolio Manager Decision\n{judge}"
                        )
                        message_buffer.update_agent_status("Aggressive Analyst", "completed")
                        message_buffer.update_agent_status("Conservative Analyst", "completed")
                        message_buffer.update_agent_status("Neutral Analyst", "completed")
                        message_buffer.update_agent_status("Portfolio Manager", "completed")

                update_display(layout, stats_handler=stats_handler, start_time=start_time)

                trace.append(chunk)

            # Streamed chunks are per-node deltas, not full state. Merge them
            # so every report field populated across the run is present.
            final_state = {}
            for chunk in trace:
                final_state.update(chunk)

            # Clean run: log the decision, then drop this run's checkpoint so a
            # later run starts fresh. A mid-stream failure skips both, keeping
            # the checkpoint for resume.
            # With several runs voting, the decision logged is the consensus,
            # recorded once all runs are in.
            if vote_runs == 1:
                graph.record_decision(selections["ticker"], selections["analysis_date"], final_state)
            graph.clear_checkpoint_on_success(
                selections["ticker"], selections["analysis_date"], selections["asset_type"], portfolio
            )
        finally:
            # Always restore the plain uncheckpointed graph, even on failure.
            graph.end_checkpoint()

        for agent in message_buffer.agent_status:
            message_buffer.update_agent_status(agent, "completed")

        message_buffer.add_message(
            "System", f"Completed analysis for {selections['analysis_date']}"
        )
        message_buffer.add_message("System", analyst_wall_time_tracker.format_summary())

        for section in message_buffer.report_sections:
            if section in final_state:
                message_buffer.update_report_section(section, final_state[section])

        update_display(layout, stats_handler=stats_handler, start_time=start_time)

    # Post-analysis prompts (outside Live context for clean interaction)
    console.print("\n[bold cyan]Analysis Complete![/bold cyan]\n")

    # A decision nobody can read is not a position. Say so here rather than
    # leaving the run to look like a normal result.
    if is_review(run_rating(final_state)):
        console.print(
            "[yellow]No rating could be read from the final decision, so this run "
            "is recorded for review rather than as a position. Re-run, or read the "
            "decision text below and judge it yourself.[/yellow]\n"
        )
    console.print(f"[dim]{analyst_wall_time_tracker.format_summary()}[/dim]")

    vote, run_states = None, [final_state]
    if vote_runs > 1:
        run_states += _extra_runs(graph, selections, portfolio, stats_handler, vote_runs)
        vote = consensus([run_rating(state) for state in run_states])
        _print_vote(vote)
        if vote["representative"] is not None:
            final_state = run_states[vote["representative"]]
        if vote["rating"] and vote["rating"] != run_rating(final_state):
            # A split vote: the consensus is a rating no single run gave.
            graph.record_decision(selections["ticker"], selections["analysis_date"], final_state,
                                  rating=vote["rating"])
        else:
            graph.record_decision(selections["ticker"], selections["analysis_date"], final_state)

    rating = (vote or {}).get("rating") or run_rating(final_state)
    sizing = _compute_sizing(graph, selections, rating, portfolio)
    _paper_order(graph, selections, rating)

    usage = summarize(stats_handler.usage, config)
    _print_usage(usage)
    # The run's own duration: the questions below can wait on the user for hours.
    elapsed = time.time() - start_time

    saved_to = _offer_reports(final_state, graph, selections["ticker"],
                              save=flags.get("save"), show=flags.get("show"), html=flags.get("html"),
                              open_page=flags.get("open"),
                              trade_date=selections.get("analysis_date"),
                              usage_markdown=render_markdown(usage),
                              vote_markdown=render_vote(vote) if vote else None,
                              sizing_markdown=render_sizing(sizing) if sizing else None)
    if saved_to and vote:
        _save_run_reports(graph, run_states, selections["ticker"], saved_to)
    _record_cost(usage, graph, config, selections, final_state, saved_to, elapsed, vote, sizing)
    return {"ticker": selections["ticker"], "rating": rating, "sizing": sizing, "report_dir": saved_to}


def _paper_order(graph, selections, rating):
    """Queue the paper account's order for this decision, when paper trading is on."""
    config = getattr(graph, "config", None) or {}
    if not config.get("paper_trading") or not rating or is_review(rating):
        return None
    from tradingagents.paper import PaperAccount

    try:
        with run_config(graph.config):
            account = PaperAccount(graph.config)
            sizing = size_position(selections["ticker"], rating, selections["analysis_date"],
                                   account.portfolio(), graph.config)
            order = account.submit(sizing, selections["analysis_date"])
    except Exception as e:   # the paper account never costs the user the run
        console.print(f"[yellow]Paper order not placed: {e}[/yellow]")
        return None
    if order:
        console.print(f"[bold]Paper order:[/bold] {order['side']} {order['qty']:,.0f} {order['ticker']} "
                      f"at the next session's open (stop {order['stop']:,.4g})")
    else:
        console.print(f"[dim]Paper account: no trade for {selections['ticker']} ({sizing.action}).[/dim]")
    return order


def _compute_sizing(graph, selections, rating, portfolio):
    """The final rating as a position (tradingagents/risk.py); None when it cannot be computed."""
    if not rating or is_review(rating):
        return None
    try:
        with run_config(graph.config):
            sizing = size_position(selections["ticker"], rating, selections["analysis_date"],
                                   portfolio, graph.config)
    except Exception as e:   # sizing is an aid; it never costs the user the run
        console.print(f"[yellow]Could not compute the position size: {e}[/yellow]")
        return None
    console.print(f"\n[bold]Position sizing:[/bold] {sizing_summary(sizing)}")
    for warning in sizing.warnings:
        console.print(f"  [yellow]•[/yellow] {warning}")
    return sizing


def _vote_runs(flags: dict, config: dict) -> int:
    """How many runs vote on this analysis: --runs, else vote_runs from the config."""
    value = flags.get("runs") or config.get("vote_runs") or 1
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        raise typer.BadParameter(f"vote_runs must be a whole number, got {value!r}") from None


def _extra_runs(graph, selections, portfolio, stats_handler, total: int) -> list[dict]:
    """Runs 2..total of the same analysis, without the live display.

    A run that fails is reported and left out of the vote rather than ending it.
    """
    from tradingagents.dataflows.config import run_config

    states = []
    for n in range(2, total + 1):
        with console.status(f"[cyan]Run {n}/{total}: {selections['ticker']} {selections['analysis_date']}…[/cyan]"):
            try:
                with run_config(graph.config):
                    init = graph.create_run_state(selections["ticker"], selections["analysis_date"],
                                                  selections["asset_type"], portfolio)
                    state = graph.graph.invoke(init, **graph.propagator.get_graph_args(callbacks=[stats_handler]))
            except Exception as e:   # one failed run must not cost the others
                console.print(f"[yellow]Run {n}/{total} failed and is left out of the vote: {e}[/yellow]")
                continue
        states.append(state)
        console.print(f"  Run {n}/{total}: [bold]{run_rating(state)}[/bold]")
    return states


def _print_vote(vote: dict) -> None:
    ratings = ", ".join(f"{i}:{r}" for i, r in enumerate(vote["ratings"], 1))
    console.print(f"\n[bold]Consensus over {vote['runs']} runs: {vote['rating'] or 'REVIEW'}[/bold] "
                  f"({agreement_label(vote)}, agreement {vote['agreement']:.0%})")
    console.print(f"[dim]Ratings by run: {ratings}[/dim]")


def _save_run_reports(graph, run_states, ticker, saved_to) -> None:
    """Every run's own report under runs/run_N, next to the consensus report."""
    from tradingagents.reporting import write_report_tree

    for n, state in enumerate(run_states, 1):
        try:
            write_report_tree(state, ticker, Path(saved_to) / "runs" / f"run_{n}",
                              settings=graph.run_settings(), html=False)
        except Exception as e:
            console.print(f"[yellow]Could not save run {n}'s report: {e}[/yellow]")


def _print_usage(usage: dict) -> None:
    """Token usage and cost by role, after the run."""
    from rich.table import Table

    cur = usage["currency"]
    table = Table(title="Token usage and cost", show_lines=False)
    for name, justify in (("Role", "left"), ("Model", "left"), ("Calls", "right"),
                          ("Input", "right"), ("Output", "right"), ("Cost", "right")):
        table.add_column(name, justify=justify)

    def money(value):
        return "—" if value is None else f"{cur}{value:.4f}"

    for r in usage["rows"]:
        table.add_row(r["role"], r["model"], str(r["calls"]), f"{r['input_tokens']:,}",
                      f"{r['output_tokens']:,}", money(r["cost"]))
    t = usage["total"]
    table.add_row("[bold]Total[/bold]", "", f"[bold]{t['calls']}[/bold]", f"[bold]{t['input_tokens']:,}[/bold]",
                  f"[bold]{t['output_tokens']:,}[/bold]", f"[bold]{money(usage['cost'])}[/bold]")
    console.print(table)
    if not usage["priced"]:
        console.print("[dim]Set TRADINGAGENTS_MODEL_PRICES in .env to see costs.[/dim]")


def _record_cost(usage, graph, config, selections, final_state, saved_to, elapsed, vote=None,
                 sizing=None) -> None:
    """Append this run to the cost ledger next to the reports; a failure only warns."""
    from datetime import datetime

    try:
        if saved_to:
            base = Path(saved_to).parent.parent
        elif config.get("reports_dir"):
            base = Path(config["reports_dir"]).expanduser()
        else:
            base = Path(config["results_dir"]) / "reports"
        path = append_ledger(base / "cost_ledger.csv", ledger_row(
            usage,
            run_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ticker=selections["ticker"],
            trade_date=selections.get("analysis_date", ""),
            rating=(vote or {}).get("rating") or run_rating(final_state),
            sizing=sizing,
            analysts=[getattr(a, "value", a) for a in selections.get("analysts", [])],
            config=config,
            elapsed_seconds=elapsed,
            report_dir=saved_to or "",
            vote=vote,
        ))
        console.print(f"[dim]Cost ledger:[/dim] {path.resolve()}")
    except Exception as e:   # the ledger must never cost the user the run
        console.print(f"[yellow]Could not update the cost ledger: {e}[/yellow]")


def _yes(question: str) -> bool:
    return typer.prompt(question, default="Y").strip().upper() in ("Y", "YES", "")


def _graphical_browser():
    """A browser that opens a page in its own window on this machine, or None.

    Over SSH the page sits on the remote machine, and a terminal browser (lynx,
    w3m, elinks) would take over the terminal, so neither is offered.
    """
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return None
    try:
        browser = webbrowser.get()
    except webbrowser.Error:
        return None
    if type(browser) is webbrowser.GenericBrowser or isinstance(browser, webbrowser.Elinks):
        return None
    return browser


def _open_page(page: Path, ask: bool = True) -> None:
    """Open the saved page in a browser window (asking first unless ``ask`` is False);
    say where it is if opening fails."""
    browser = _graphical_browser()
    if browser is None or (ask and not _yes("Open it in your browser?")):
        return
    try:
        opened = browser.open(page.as_uri())
    except (webbrowser.Error, OSError):
        opened = False
    if not opened:
        console.print(f"  [dim]Could not open a browser; the page is at:[/dim] {page}")


def _offer_reports(final_state, graph, ticker, save=None, show=None, html=None, trade_date=None,
                   usage_markdown=None, vote_markdown=None, open_page=None, sizing_markdown=None):
    """Save the report tree and show it; ``save``/``show``/``html`` answer the questions when given.

    Returns the folder the report was saved in, or None when it was not saved.

    A saved report includes the HTML page unless ``html`` is False. Someone
    answering the save question at the prompt is also asked about the page and
    offered to open it; a run whose flags answer the save question asks neither.
    """
    asked = save is None
    saved_to = None
    if asked:
        save = typer.prompt("Save report?", default="Y").strip().upper() in ("Y", "YES", "")
    if save:
        # Under results_dir, not the working directory: in Docker the working
        # directory is inside the container and the report goes with it, while
        # results_dir is the mounted volume the rest of the run already writes to.
        # Every run gets its own <TICKER>/<date>_<time> subfolder under the base
        # folder, including a base typed at the prompt, so runs never overwrite
        # each other.
        trade_date = trade_date or final_state.get("trade_date")
        base = graph.reports_base_dir()
        if asked:   # someone at the prompt may pick another base folder
            base = Path(typer.prompt(
                "Report folder (each run is saved in its own TICKER/date_time subfolder; "
                "press Enter for default)", default=str(base)
            ).strip().strip('"'))
        save_path = graph.default_report_path(ticker, trade_date, base=base)
        if html is None:
            html = _yes("Also save it as an HTML page?") if asked else True
        saved = False
        try:
            report_file = graph.save_reports(final_state, ticker, save_path, html=html,
                                             usage_markdown=usage_markdown, vote_markdown=vote_markdown,
                                             sizing_markdown=sizing_markdown)
            saved = True
            saved_to = save_path
            console.print(f"\n[green]✓ Report saved to:[/green] {save_path.resolve()}")
            console.print(f"  [dim]Complete report:[/dim] {report_file.name}")
        except Exception as e:
            console.print(f"[red]Error saving report: {e}[/red]")
        if saved and html:
            page = (save_path / "complete_report.html").resolve()
            console.print(f"  [dim]HTML report:[/dim] {page.name}")
            if open_page:
                _open_page(page, ask=False)
            elif open_page is None and asked:
                _open_page(page)

    if show is None:
        show = typer.prompt("\nDisplay full report on screen?", default="Y").strip().upper() in ("Y", "YES", "")
    if show:
        display_complete_report(final_state)
    return saved_to
