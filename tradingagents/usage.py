"""Token usage and cost per role and per model, for the report and a running cost ledger.

The CLI's stats callback feeds every chat-model call into a :class:`UsageRecorder`.
Each call is attributed to the graph node (role) it ran in — an analyst's inner
"agent"/"wrap_up" nodes count toward that analyst — and to the model that served it.
Costs are only shown when prices are configured (``model_prices`` /
TRADINGAGENTS_MODEL_PRICES), because provider prices change and differ by
account; token counts are always recorded.
"""

from __future__ import annotations

import csv
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# Roles in the order the graph runs them; anything else (e.g. a reflection made
# outside the graph) is listed after these.
ROLE_ORDER = (
    "Market Analyst", "Social Analyst", "Sentiment Analyst", "News Analyst", "Fundamentals Analyst",
    "Memory Log",
    "Bull Researcher", "Bear Researcher", "Style Panel", "Research Manager",
    "Trader",
    "Aggressive Analyst", "Conservative Analyst", "Neutral Analyst",
    "Portfolio Manager",
)
OTHER = "Other"

LEDGER_FIELDS = (
    "run_time", "ticker", "trade_date", "rating", "analysts", "quick_model", "deep_model",
    "llm_calls", "input_tokens", "output_tokens", "cached_input_tokens",
    "quick_input_tokens", "quick_output_tokens", "deep_input_tokens", "deep_output_tokens",
    "cost", "currency", "elapsed_seconds", "report_dir",
    "vote_runs", "vote_ratings", "agreement",
    "action", "target_weight", "stop",
)


def role_from_metadata(metadata: dict | None) -> str:
    """The graph node a call ran in; a nested analyst graph counts as its analyst."""
    metadata = metadata or {}
    ns = metadata.get("langgraph_checkpoint_ns") or metadata.get("checkpoint_ns") or ""
    if ns:
        outer = ns.split("|", 1)[0].split(":", 1)[0]
        if outer:
            return outer
    return metadata.get("langgraph_node") or OTHER


def model_from_params(params: dict | None, serialized: dict | None = None) -> str:
    """The model name a chat-model call was made with."""
    for source in (params or {}, (serialized or {}).get("kwargs") or {}):
        for key in ("model", "model_name", "model_id", "deployment_name"):
            value = source.get(key)
            if isinstance(value, str) and value:
                return value
    return "unknown"


@dataclass
class _Tally:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0

    def add(self, other: "_Tally") -> None:
        self.calls += other.calls
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cached_input_tokens += other.cached_input_tokens


@dataclass
class UsageRecorder:
    """Thread-safe tallies keyed by (role, model)."""

    _pending: dict = field(default_factory=dict)
    _tallies: dict = field(default_factory=lambda: defaultdict(_Tally))
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def start(self, run_id, metadata: dict | None, params: dict | None, serialized: dict | None = None) -> None:
        model = model_from_params(params, serialized)
        if model == "unknown" and (metadata or {}).get("ls_model_name"):
            model = metadata["ls_model_name"]
        key = (role_from_metadata(metadata), model)
        with self._lock:
            self._pending[run_id] = key
            self._tallies[key].calls += 1

    def end(self, run_id, usage: dict | None, raw_usage: dict | None = None) -> None:
        """Add a finished call's tokens.

        ``raw_usage`` is the provider's own usage block (``response_metadata
        ["token_usage"]``), read for cache hits a provider reports under its own
        name, such as DeepSeek's ``prompt_cache_hit_tokens``.
        """
        with self._lock:
            key = self._pending.pop(run_id, None)
            if key is None or not usage:
                return
            tally = self._tallies[key]
            tally.input_tokens += int(usage.get("input_tokens") or 0)
            tally.output_tokens += int(usage.get("output_tokens") or 0)
            details = usage.get("input_token_details") or {}
            cached = int(details.get("cache_read") or 0)
            if not cached and raw_usage:
                cached = int(raw_usage.get("prompt_cache_hit_tokens") or 0)
            tally.cached_input_tokens += min(cached, int(usage.get("input_tokens") or 0))

    def rows(self) -> list[tuple[str, str, _Tally]]:
        with self._lock:
            items = [(role, model, _Tally(**vars(t))) for (role, model), t in self._tallies.items()]
        rank = {name: i for i, name in enumerate(ROLE_ORDER)}
        return sorted(items, key=lambda r: (rank.get(r[0], len(rank)), r[0], r[1]))


def _price(values) -> tuple[float, float, float | None]:
    values = [float(v) for v in values]
    if len(values) not in (2, 3):
        raise ValueError("expected input/output or input/output/cached-input")
    return values[0], values[1], (values[2] if len(values) == 3 else None)


def parse_prices(spec) -> dict[str, tuple[float, float, float | None]]:
    """Prices per million tokens, ``{model: (input, output, cached_input_or_None)}``.

    Accepts a dict, or a string such as
    ``"deepseek-v4-pro=9/27/0.3, deepseek-flash=2/8/0.04"``: input / output /
    optional cache-hit input, each per 1M tokens. Without a cache-hit price,
    cached input is charged at the input price.
    """
    if not spec:
        return {}
    if isinstance(spec, dict):
        return {str(k): _price(v) for k, v in spec.items()}
    prices = {}
    for item in str(spec).replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            model, values = item.split("=", 1)
            prices[model.strip()] = _price(values.split("/"))
        except ValueError as exc:
            raise ValueError(
                f"model_prices entry {item!r} is not model=input/output[/cached] (per 1M tokens)"
            ) from exc
    return prices


def _cost(tally: _Tally, price) -> float | None:
    if price is None:
        return None
    price_in, price_out, price_cached = price
    if price_cached is None:
        price_cached = price_in
    fresh = tally.input_tokens - tally.cached_input_tokens
    return (fresh * price_in + tally.cached_input_tokens * price_cached
            + tally.output_tokens * price_out) / 1_000_000


def tier_of(role: str, model: str, config: dict) -> str:
    """'deep' or 'quick': by model when the tiers differ, else by role."""
    deep, quick = config.get("deep_think_llm"), config.get("quick_think_llm")
    if deep != quick:
        if model == deep:
            return "deep"
        if model == quick:
            return "quick"
    return "deep" if role in ("Research Manager", "Portfolio Manager") else "quick"


def summarize(recorder: UsageRecorder, config: dict) -> dict:
    """Usage by role, by tier and in total, with costs where prices are known."""
    prices = parse_prices(config.get("model_prices"))
    rows, tiers, total = [], defaultdict(_Tally), _Tally()
    total_cost, priced_all = 0.0, True
    for role, model, tally in recorder.rows():
        cost = _cost(tally, prices.get(model))
        if cost is None and (tally.input_tokens or tally.output_tokens):
            priced_all = False
        total_cost += cost or 0.0
        tiers[tier_of(role, model, config)].add(tally)
        total.add(tally)
        rows.append({"role": role, "model": model, "tier": tier_of(role, model, config),
                     **vars(tally), "cost": cost})
    return {
        "rows": rows,
        "tiers": {name: vars(t) for name, t in tiers.items()},
        "total": vars(total),
        "cost": total_cost if prices else None,
        "cost_complete": bool(prices) and priced_all,
        "currency": config.get("price_currency") or "",
        "priced": bool(prices),
    }


def _n(value: int) -> str:
    return f"{value:,}"


def _money(value: float | None, currency: str) -> str:
    if value is None:
        return "—"
    return f"{currency}{value:.4f}" if value < 1 else f"{currency}{value:.2f}"


def render_markdown(summary: dict) -> str:
    """The usage section for the saved report."""
    cur = summary["currency"]
    lines = [
        "| Role | Model | Tier | Calls | Input tokens | Output tokens | Cost |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for r in summary["rows"]:
        lines.append(f"| {r['role']} | {r['model']} | {r['tier']} | {r['calls']} | "
                     f"{_n(r['input_tokens'])} | {_n(r['output_tokens'])} | {_money(r['cost'], cur)} |")
    t = summary["total"]
    lines.append(f"| **Total** | | | **{t['calls']}** | **{_n(t['input_tokens'])}** | "
                 f"**{_n(t['output_tokens'])}** | **{_money(summary['cost'], cur)}** |")
    notes = []
    for name in ("quick", "deep"):
        tier = summary["tiers"].get(name)
        if tier:
            notes.append(f"{name} tier: {_n(tier['input_tokens'])} in / {_n(tier['output_tokens'])} out")
    if t["cached_input_tokens"]:
        share = t["cached_input_tokens"] / max(t["input_tokens"], 1)
        notes.append(f"cache-hit input: {_n(t['cached_input_tokens'])} ({share:.0%} of input)")
    if not summary["priced"]:
        notes.append("no prices configured: set TRADINGAGENTS_MODEL_PRICES to see costs")
    elif not summary["cost_complete"]:
        notes.append("some models have no configured price, so the total is a lower bound")
    if notes:
        lines += ["", "- " + "\n- ".join(notes)]
    return "\n".join(lines)


def append_ledger(path, row: dict) -> Path:
    """Append one run to the CSV cost ledger, writing the header for a new file.

    UTF-8 with BOM so Excel opens it with Chinese text intact.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _upgrade_header(path)
    new = not path.exists() or path.stat().st_size == 0
    with open(path, "a", newline="", encoding="utf-8-sig" if new else "utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LEDGER_FIELDS, extrasaction="ignore")
        if new:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in LEDGER_FIELDS})
    return path


def _upgrade_header(path: Path) -> None:
    """Rewrite a ledger written with fewer columns under the current header."""
    if not path.exists() or path.stat().st_size == 0:
        return
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if tuple(reader.fieldnames or ()) == LEDGER_FIELDS:
            return
        rows = list(reader)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=LEDGER_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in LEDGER_FIELDS})


def ledger_row(summary: dict, *, run_time: str, ticker: str, trade_date: str, rating: str,
               analysts, config: dict, elapsed_seconds: float, report_dir, vote: dict | None = None,
               sizing=None) -> dict:
    total, tiers = summary["total"], summary["tiers"]
    quick, deep = tiers.get("quick", {}), tiers.get("deep", {})
    cost = summary["cost"]
    return {
        "run_time": run_time, "ticker": ticker, "trade_date": trade_date, "rating": rating,
        "analysts": " ".join(analysts or []),
        "quick_model": config.get("quick_think_llm") or "", "deep_model": config.get("deep_think_llm") or "",
        "llm_calls": total["calls"], "input_tokens": total["input_tokens"],
        "output_tokens": total["output_tokens"], "cached_input_tokens": total["cached_input_tokens"],
        "quick_input_tokens": quick.get("input_tokens", 0), "quick_output_tokens": quick.get("output_tokens", 0),
        "deep_input_tokens": deep.get("input_tokens", 0), "deep_output_tokens": deep.get("output_tokens", 0),
        "cost": "" if cost is None else round(cost, 6), "currency": summary["currency"],
        "elapsed_seconds": round(elapsed_seconds, 1), "report_dir": str(report_dir or ""),
        "vote_runs": vote["runs"] if vote else 1,
        "vote_ratings": " ".join(vote["ratings"]) if vote else rating,
        "agreement": round(vote["agreement"], 2) if vote else 1,
        "action": sizing.action if sizing else "",
        "target_weight": "" if sizing is None or sizing.target_weight is None else round(sizing.target_weight, 4),
        "stop": round(sizing.stop, 4) if sizing else "",
    }
