"""Stock screening in plain language: an LLM writes the filter, code applies it.

``tradingagents screen "市盈率低于20、ROE超过15%的消费股，按市值排序"`` turns the
request into a filter specification (fields, comparisons, industries, sort,
limit), prints that specification for the user to check, and only then runs it
over every A-share. The model never sees or ranks the stocks: it only writes
the conditions, so a misread request shows up as a wrong condition on screen
rather than as a plausible-looking list.

The universe is built from two AkShare tables and cached for the day:

* East Money's real-time quote list (``stock_zh_a_spot_em``): price, change,
  turnover, dynamic PE, PB, market caps, 60-day and year-to-date change.
* East Money's quarterly results (``stock_yjbb_em``) for the latest period whose
  filing deadline has passed: revenue and profit growth, ROE, gross margin,
  EPS, operating cash flow per share and the company's industry.

Screening reads today's market; it is for picking what to analyze now, not for
backtests (the list would be today's survivors).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

# field -> (description for the model, unit)
FIELDS = {
    "price": ("latest price", "元"),
    "chg_pct": ("today's change", "%"),
    "turnover_pct": ("turnover rate today", "%"),
    "volume_ratio": ("volume ratio (today vs 5-day average)", ""),
    "amount": ("turnover value today", "亿元"),
    "pe": ("dynamic P/E (negative = loss-making)", "x"),
    "pb": ("price to book", "x"),
    "dividend_yield": ("dividend yield, trailing 12 months (Tushare quotes only)", "%"),
    "mcap": ("total market cap", "亿元"),
    "float_mcap": ("free-float market cap", "亿元"),
    "chg_60d": ("price change over 60 days", "%"),
    "chg_ytd": ("price change year to date", "%"),
    "revenue_yoy": ("revenue growth, year on year, latest reported period", "%"),
    "profit_yoy": ("net profit growth, year on year, latest reported period", "%"),
    "roe": ("ROE for the reported period so far (not annualized)", "%"),
    "roe_annualized": ("ROE annualized from the reported period", "%"),
    "gross_margin": ("gross margin", "%"),
    "eps": ("EPS for the reported period so far", "元"),
    "ocf_per_share": ("operating cash flow per share, period so far", "元"),
}
OPS = {"<", "<=", ">", ">=", "==", "between"}


# --- the universe ------------------------------------------------------------------

def latest_complete_period(today: date | None = None) -> str:
    """The latest report period whose filing deadline has passed, as YYYYMMDD."""
    today = today or date.today()
    y = today.year
    deadlines = [  # (period end, deadline)
        (date(y - 1, 9, 30), date(y - 1, 10, 31)), (date(y - 1, 12, 31), date(y, 4, 30)),
        (date(y, 3, 31), date(y, 4, 30)), (date(y, 6, 30), date(y, 8, 31)), (date(y, 9, 30), date(y, 10, 31)),
    ]
    done = [end for end, deadline in deadlines if deadline < today]
    # Q1 and the annual report share a deadline: Q1 is the later period.
    return max(done).strftime("%Y%m%d")


def yahoo_symbol(code: str) -> str | None:
    code = str(code).zfill(6)
    if code.startswith(("60", "68", "90")):
        return f"{code}.SS"
    if code.startswith(("00", "30", "20")):
        return f"{code}.SZ"
    return None          # Beijing exchange: Yahoo has no prices for it


def _cache_dir(config: dict) -> Path:
    return Path(config.get("data_cache_dir") or ".").expanduser() / "screen"


def _cached(path: Path, max_age: float | None, fetch) -> pd.DataFrame:
    if path.exists() and (max_age is None or time.time() - path.stat().st_mtime < max_age):
        return pd.read_csv(path, dtype={"代码": str, "股票代码": str}, encoding="utf-8")
    df = fetch()
    if df is not None and not df.empty:
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(path, index=False, encoding="utf-8")
    return df


SINA_URL = "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
SINA_COUNT_URL = ("http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                  "Market_Center.getHQNodeStockCount?node=hs_a")


def spot_sina(pause: float = 0.3, timeout: float = 15) -> pd.DataFrame:
    """Every A-share's quote from Sina's market centre, in East Money's column names.

    AkShare's own ``stock_zh_a_spot`` drops Sina's P/E, P/B, market caps and
    turnover, so the pages are read here. Sina has no 60-day or year-to-date
    change and no volume ratio; those columns stay empty. Sina throttles
    repeated full downloads, so the result is cached for the day by the caller.
    """
    import math
    import re as _re

    import requests

    count = int(_re.findall(r"\d+", requests.get(SINA_COUNT_URL, timeout=timeout).text)[0])
    frames = []
    for page in range(1, math.ceil(count / 80) + 1):
        params = {"page": str(page), "num": "80", "sort": "symbol", "asc": "1", "node": "hs_a",
                  "symbol": "", "_s_r_a": "page"}
        text = requests.get(SINA_URL, params=params, timeout=timeout).text
        try:
            rows = json.loads(text)
        except ValueError:
            from akshare.utils import demjson   # Sina sometimes answers with unquoted keys

            rows = demjson.decode(text)
        frames.append(pd.DataFrame(rows))
        time.sleep(pause)
    raw = pd.concat(frames, ignore_index=True)

    def num(col):
        return pd.to_numeric(raw.get(col), errors="coerce")

    return pd.DataFrame({
        "代码": raw["code"].astype(str).str.zfill(6), "名称": raw["name"],
        "最新价": num("trade"), "涨跌幅": num("changepercent"), "换手率": num("turnoverratio"),
        "量比": float("nan"), "成交额": num("amount"),
        "市盈率-动态": num("per"), "市净率": num("pb"),
        "总市值": num("mktcap") * 1e4, "流通市值": num("nmc") * 1e4,      # Sina reports 万元
        "60日涨跌幅": float("nan"), "年初至今涨跌幅": float("nan"),
    })


RESULTS_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
# East Money's field names for the columns kept, with AkShare's Chinese names.
RESULT_FIELDS = {
    "SECURITY_CODE": "股票代码", "SECURITY_NAME_ABBR": "股票简称", "BASIC_EPS": "每股收益",
    "TOTAL_OPERATE_INCOME": "营业总收入-营业总收入", "PARENT_NETPROFIT": "净利润-净利润",
    "WEIGHTAVG_ROE": "净资产收益率", "YSTZ": "营业总收入-同比增长", "SJLTZ": "净利润-同比增长",
    "BPS": "每股净资产", "MGJYXJJE": "每股经营现金流量", "XSMLL": "销售毛利率", "PUBLISHNAME": "所处行业",
}


def results_paged(period: str, cache: Path, pause: float = 1.0, timeout: float = 30,
                  retries: int = 4) -> pd.DataFrame:
    """East Money's quarterly results for every listing, one page at a time.

    The table runs to some 23 pages, and East Money cuts off clients that pull
    them back to back, so each page is fetched with its own retries and a pause,
    and kept on disk: a run that is cut off resumes from the page it reached.
    """
    import requests

    pages = cache / f"results_{period}_pages"
    pages.mkdir(parents=True, exist_ok=True)
    params = {"sortColumns": "UPDATE_DATE,SECURITY_CODE", "sortTypes": "-1,-1", "pageSize": "500",
              "reportName": "RPT_LICO_FN_CPD", "columns": "ALL",
              "filter": f"(REPORTDATE='{period[:4]}-{period[4:6]}-{period[6:]}')"}
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0"

    def page(n: int) -> dict:
        path = pages / f"{n}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        last = None
        for attempt in range(retries):
            try:
                resp = session.get(RESULTS_URL, params={**params, "pageNumber": str(n)}, timeout=timeout)
                resp.raise_for_status()
                result = resp.json()["result"]
                path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
                time.sleep(pause)
                return result
            except Exception as exc:     # throttled or cut off: back off and ask again
                last = exc
                time.sleep(pause * (2 + 3 * attempt))
        raise RuntimeError(f"results page {n} failed: {type(last).__name__}: {last}")

    first = page(1)
    rows = list(first["data"])
    for n in range(2, int(first["pages"]) + 1):
        rows += page(n)["data"]
    raw = pd.DataFrame(rows)
    return raw[[c for c in RESULT_FIELDS if c in raw.columns]].rename(columns=RESULT_FIELDS)


def _spot(config: dict, max_age: float) -> tuple[pd.DataFrame, str]:
    """Every A-share's quote, from the data gateway (Tushare, East Money, Sina;
    a good answer is reused for ``max_age`` seconds). Returns (frame, source)."""
    from tradingagents.datagw import GatewayNoDataError, GatewayUnavailableError, gateway

    try:
        result = gateway().fetch("cn_spot", ttl=max_age, cache_dir=config.get("data_cache_dir"),
                                 day=f"{date.today():%Y-%m-%d}")
    except (GatewayNoDataError, GatewayUnavailableError) as exc:
        raise RuntimeError(f"no A-share quote source answered ({exc})") from exc
    df, source = result.data, result.source
    as_of = df.attrs.get("as_of")
    return df, f"{source} ({as_of} close)" if as_of else source


def build_universe(config: dict, spot_max_age: float = 3600) -> tuple[pd.DataFrame, str]:
    """Every A-share with quote and latest-results fields; (frame, report period).

    ``frame.attrs["source"]`` names the quote source and ``frame.attrs["missing"]``
    the fields it could not supply.
    """
    from tradingagents.dataflows.vendors.akshare_cn import call

    cache = _cache_dir(config)
    spot, source = _spot(config, spot_max_age)
    period = latest_complete_period()
    try:
        # ~11,000 rows in 23 pages: one long attempt, cached for the period once it lands.
        results = _cached(cache / f"results_{period}.csv", None, lambda: results_paged(period, cache))
    except Exception as exc:
        logger.warning("Quarterly results unavailable (%s); screening on quotes only", exc)
        results = None
    q = {"0331": 1, "0630": 2, "0930": 3, "1231": 4}[period[4:]]

    df = pd.DataFrame({
        "code": spot["代码"].astype(str).str.zfill(6),
        "name": spot["名称"],
        "price": pd.to_numeric(spot["最新价"], errors="coerce"),
        "chg_pct": pd.to_numeric(spot["涨跌幅"], errors="coerce"),
        "turnover_pct": pd.to_numeric(spot["换手率"], errors="coerce"),
        "volume_ratio": pd.to_numeric(spot["量比"], errors="coerce"),
        "amount": pd.to_numeric(spot["成交额"], errors="coerce") / 1e8,
        "pe": pd.to_numeric(spot["市盈率-动态"], errors="coerce"),
        "pb": pd.to_numeric(spot["市净率"], errors="coerce"),
        "mcap": pd.to_numeric(spot["总市值"], errors="coerce") / 1e8,
        "float_mcap": pd.to_numeric(spot["流通市值"], errors="coerce") / 1e8,
        "chg_60d": pd.to_numeric(spot["60日涨跌幅"], errors="coerce"),
        "chg_ytd": pd.to_numeric(spot["年初至今涨跌幅"], errors="coerce"),
        "dividend_yield": (pd.to_numeric(spot["股息率"], errors="coerce") if "股息率" in spot.columns
                           else float("nan")),
    })
    if results is not None and not results.empty:
        r = pd.DataFrame({
            "code": results["股票代码"].astype(str).str.zfill(6),
            "industry": results["所处行业"],
            "revenue_yoy": pd.to_numeric(results["营业总收入-同比增长"], errors="coerce"),
            "profit_yoy": pd.to_numeric(results["净利润-同比增长"], errors="coerce"),
            "roe": pd.to_numeric(results["净资产收益率"], errors="coerce"),
            "gross_margin": pd.to_numeric(results["销售毛利率"], errors="coerce"),
            "eps": pd.to_numeric(results["每股收益"], errors="coerce"),
            "ocf_per_share": pd.to_numeric(results["每股经营现金流量"], errors="coerce"),
        }).drop_duplicates("code")
        r["roe_annualized"] = r["roe"] * 4 / q
        df = df.merge(r, on="code", how="left")
    for name in FIELDS:              # a field today's sources lack is empty: it matches nothing
        if name not in df.columns:
            df[name] = float("nan")
    if "industry" not in df.columns or df["industry"].isna().all():
        # without quarterly results, the quote source's own industry (Tushare's) if it has one
        df["industry"] = spot["行业"].values if "行业" in spot.columns else None
    df["ticker"] = df["code"].map(yahoo_symbol)
    df["st"] = df["name"].astype(str).str.upper().str.contains("ST")
    df.attrs["source"] = source
    df.attrs["as_of"] = spot.attrs.get("as_of") or date.today().isoformat()   # the quotes' trading day
    df.attrs["missing"] = [f for f in FIELDS if df[f].isna().all()]
    return df, period


# --- the filter specification -------------------------------------------------------

@dataclass
class Spec:
    filters: list[dict] = field(default_factory=list)
    industries: list[str] = field(default_factory=list)
    exclude_industries: list[str] = field(default_factory=list)
    exclude_st: bool = True
    sort: list[dict] = field(default_factory=list)
    limit: int = 20
    explanation: str = ""
    # Ranking when no sort is given: a preset (tradingagents/scoring.py) or factor weights.
    preset: str = ""
    weights: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "Spec":
        spec = cls(
            filters=[f for f in data.get("filters") or [] if _valid_filter(f)],
            industries=[str(x) for x in data.get("industries") or []],
            exclude_industries=[str(x) for x in data.get("exclude_industries") or []],
            exclude_st=bool(data.get("exclude_st", True)),
            sort=[s for s in data.get("sort") or [] if s.get("field") in FIELDS],
            limit=max(1, min(int(data.get("limit") or 20), 200)),
            explanation=str(data.get("explanation") or ""),
            preset=str(data.get("preset") or "") if str(data.get("preset") or "") in _presets() else "",
            weights=_clean_weights(data.get("weights")),
        )
        return spec

    @classmethod
    def from_preset(cls, key: str, limit: int = 20) -> "Spec":
        """A preset's own filters and weights, for a screen with no request to interpret."""
        from tradingagents.scoring import PRESETS

        p = PRESETS[key]
        return cls(filters=[dict(f) for f in p.filters], preset=key, limit=limit,
                   explanation=f"预设策略「{p.name}」：{p.summary}")

    def ranking(self) -> dict:
        """The factor weights this spec ranks by (empty when an explicit sort decides)."""
        from tradingagents.scoring import weights_for

        return {} if self.sort else weights_for(self.preset, self.weights)

    def describe(self) -> str:
        lines = []
        for f in self.filters:
            desc, unit = FIELDS[f["field"]]
            value = f"{f['value'][0]}–{f['value'][1]}" if f["op"] == "between" else f["value"]
            lines.append(f"- {f['field']} ({desc}) {f['op']} {value}{(' ' + unit) if unit else ''}")
        if self.industries:
            lines.append(f"- industry in: {', '.join(self.industries)}")
        if self.exclude_industries:
            lines.append(f"- industry not in: {', '.join(self.exclude_industries)}")
        lines.append(f"- {'excluding' if self.exclude_st else 'including'} ST stocks")
        if self.sort:
            lines.append("- sort by: " + ", ".join(f"{s['field']} {'desc' if s.get('desc', True) else 'asc'}"
                                                  for s in self.sort))
        else:
            from tradingagents.scoring import FACTORS, PRESETS

            w = self.ranking()
            label = PRESETS[self.preset].name if self.preset and not self.weights else "custom weights"
            lines.append(f"- ranked by factor score ({label}): " +
                         ", ".join(f"{FACTORS[k]} {v:g}" for k, v in w.items()))
        lines.append(f"- top {self.limit}")
        return "\n".join(lines)


def _presets() -> dict:
    from tradingagents.scoring import PRESETS

    return PRESETS


def _clean_weights(raw) -> dict:
    from tradingagents.scoring import FACTORS

    out = {}
    for k, v in (raw or {}).items() if isinstance(raw, dict) else []:
        try:
            if k in FACTORS and 0 < float(v) <= 10:
                out[k] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def _valid_filter(f: dict) -> bool:
    if not isinstance(f, dict) or f.get("field") not in FIELDS or f.get("op") not in OPS:
        return False
    if f["op"] == "between":
        return isinstance(f.get("value"), (list, tuple)) and len(f["value"]) == 2
    return isinstance(f.get("value"), (int, float))


_COND = re.compile(r"^\s*([a-z_0-9]+)\s*(<=|>=|==|<|>)\s*(-?[\d.]+)\s*$")


def parse_where(text: str) -> Spec:
    """``"pe<20, roe_annualized>15, mcap>=100"`` -> a Spec; unknown fields raise."""
    filters = []
    for part in [p for p in re.split(r"[,，;；]", text) if p.strip()]:
        m = _COND.match(part)
        if not m or m.group(1) not in FIELDS:
            raise ValueError(f"cannot read condition {part.strip()!r}; fields: {', '.join(FIELDS)}")
        filters.append({"field": m.group(1), "op": m.group(2), "value": float(m.group(3))})
    return Spec(filters=filters)


def _prompt(request: str, industries: list[str], period: str, missing=()) -> str:
    from tradingagents.scoring import FACTORS, PRESETS

    fields = "\n".join(f"- {name}: {desc}" + (f" [{unit}]" if unit else "")
                       for name, (desc, unit) in FIELDS.items() if name not in missing)
    presets = "\n".join(f"- {p.key}: {p.name}，{p.summary}" for p in PRESETS.values())
    factors = ", ".join(f"{k} ({v})" for k, v in FACTORS.items())
    return f"""Translate a stock-screening request for China A-shares into a JSON filter specification.

Fields you may filter or sort on (results fields come from the {period[:4]}-{period[4:6]}-{period[6:]} report):
{fields}

Industry names that exist in the data (use these exact strings, choosing every one that fits the request):
{", ".join(industries)}

Ranking: unless the request asks to sort by one field, leave "sort" empty and the stocks that pass are
ranked by a factor score. Pick the preset that fits the request's intent (or "balanced"), and set
"weights" only when the request states a mix the presets do not have:
{presets}
Factors for weights: {factors}

Return only JSON, with this shape:
{{"filters": [{{"field": "pe", "op": "<", "value": 20}}, {{"field": "mcap", "op": "between", "value": [50, 500]}}],
 "industries": ["..."], "exclude_industries": [], "exclude_st": true,
 "sort": [], "preset": "quality_growth", "weights": {{}}, "limit": 20,
 "explanation": "one sentence in Chinese restating the conditions and how they are ranked"}}

Rules: op is one of < <= > >= == between. Percent fields take plain numbers (15 means 15%). Market caps are
in 亿元. "Low valuation" without a number means pe between 0 and 20 and pb < 2; "profitable" means pe > 0;
"large cap" means mcap > 500, "small cap" mcap < 100. ROE named without qualification means roe_annualized
(roe is only the reported period so far). Only add conditions the request asks for or clearly
implies; leave out what it does not say. Default limit 20 and exclude ST unless asked.

Request: {request}"""


def _invoke_with_timeout(llm, prompt: str, seconds: float, progress=None) -> str:
    """The model's answer, or TimeoutError after ``seconds``.

    The call runs on a daemon thread and the caller waits in short slices, so a
    request that never returns cannot hold the command, and Ctrl+C still works
    (on Windows a blocked network read does not see Ctrl+C).
    """
    import threading

    box: dict = {}

    def run():
        try:
            box["text"] = llm.invoke(prompt).content
        except BaseException as exc:   # handed back below
            box["error"] = exc

    worker = threading.Thread(target=run, daemon=True, name="screen-interpret")
    worker.start()
    start = time.time()
    deadline = start + seconds
    next_report = start + 10
    while worker.is_alive() and time.time() < deadline:
        worker.join(0.5)
        if progress and worker.is_alive() and time.time() >= next_report:
            progress(time.time() - start)
            next_report += 10
    if worker.is_alive():
        raise TimeoutError(f"the model did not answer within {seconds:.0f}s; "
                           f"write the conditions with --where instead (fields: {', '.join(FIELDS)})")
    if "error" in box:
        raise box["error"]
    return box.get("text", "")


def interpret(request: str, llm, industries: list[str], period: str, missing=(), timeout: float = 120,
              progress=None) -> Spec:
    text = _invoke_with_timeout(llm, _prompt(request, industries, period, missing), timeout, progress)
    match = re.search(r"\{.*\}", str(text), re.DOTALL)
    if not match:
        raise ValueError(f"the model did not return a filter specification: {str(text)[:200]}")
    return Spec.from_dict(json.loads(match.group(0)))


# --- running it ---------------------------------------------------------------------

def unavailable(spec: Spec, universe: pd.DataFrame) -> list[str]:
    """Fields the spec uses that today's data cannot supply (they would match nothing)."""
    missing = set(universe.attrs.get("missing", []))
    return sorted({f["field"] for f in spec.filters + spec.sort} & missing)


def price_loader(config: dict):
    """``load(ticker, as_of)`` -> daily bars for the leaders' price metrics. Each call sets the
    config itself: the scoring fetches on worker threads, which do not inherit it."""
    from tradingagents.dataflows.config import run_config
    from tradingagents.dataflows.vendors.yahoo.ohlcv import load_ohlcv

    def load(ticker: str, as_of: str):
        with run_config(config):
            return load_ohlcv(ticker, as_of)
    return load


def apply(spec: Spec, universe: pd.DataFrame, as_of: str | None = None, load=None,
          tech_top: int = 60) -> pd.DataFrame:
    """The listings that pass ``spec``'s filters, best first: by the requested sort, else by
    factor score (tradingagents/scoring.py; ``load(ticker, as_of)`` gives the daily bars for
    the leaders' price metrics, None to rank on quotes and results alone). Every row carries
    the factor scores either way."""
    df = universe[universe["ticker"].notna()].copy()
    if spec.exclude_st:
        df = df[~df["st"]]
    for f in spec.filters:
        col = df[f["field"]]
        if f["op"] == "between":
            lo, hi = sorted(float(v) for v in f["value"])
            df = df[(col >= lo) & (col <= hi)]
        else:
            df = df[{"<": col < f["value"], "<=": col <= f["value"], ">": col > f["value"],
                     ">=": col >= f["value"], "==": col == f["value"]}[f["op"]]]
    if "industry" in df.columns:
        industry = df["industry"].fillna("").astype(str)
        if spec.industries:
            df = df[industry.map(lambda x: any(k in x for k in spec.industries)).astype(bool)]
            industry = df["industry"].fillna("").astype(str)
        if spec.exclude_industries:
            df = df[~industry.map(lambda x: any(k in x for k in spec.exclude_industries)).astype(bool)]
    from tradingagents import scoring

    if spec.sort:
        df = scoring.score(df, scoring.weights_for(spec.preset, spec.weights)) if not df.empty else df
        df = df.sort_values([s["field"] for s in spec.sort],
                            ascending=[not s.get("desc", True) for s in spec.sort], na_position="last")
        return df.head(spec.limit).reset_index(drop=True)
    return scoring.rank(df, spec.ranking(), spec.limit, as_of or date.today().isoformat(), load, tech_top)


SHOW = ["ticker", "name", "industry", "score", "price", "chg_pct", "pe", "pb", "dividend_yield", "mcap",
        "roe_annualized", "revenue_yoy", "profit_yoy", "chg_60d",
        "value_score", "quality_score", "growth_score", "momentum_score", "low_vol_score", "liquidity_score",
        "mom_120_20", "vol_60", "drawdown_60"]


def render(result: pd.DataFrame, spec: Spec, period: str) -> str:
    cols = [c for c in SHOW if c in result.columns and not c.endswith("_score")
            and c not in ("mom_120_20", "vol_60", "drawdown_60", "dividend_yield")]
    for f in spec.filters + spec.sort:
        if f["field"] not in cols and f["field"] in result.columns:
            cols.append(f["field"])
    head = "| # | " + " | ".join(cols) + " |"
    rows = [head, "|---|" + "---|" * len(cols)]
    for i, (_, r) in enumerate(result.iterrows(), 1):
        rows.append(f"| {i} | " + " | ".join(_fmt(r[c]) for c in cols) + " |")
    return f"{len(result)} stock(s); results fields from the {period} report.\n\n" + "\n".join(rows)


def _fmt(v) -> str:
    if isinstance(v, float):
        return "—" if pd.isna(v) else f"{v:,.2f}"
    return "—" if v is None else str(v)


def save(result: pd.DataFrame, spec: Spec, request: str, config: dict) -> tuple[Path, Path]:
    """The result as CSV plus ``latest.txt`` (one ticker per line, for ``daily --from-file``)."""
    base = Path(config.get("reports_dir") or os.path.join(config.get("results_dir", "."), "reports")).expanduser()
    out = base / "screens"
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    csv_path = out / f"screen_{stamp}.csv"
    result.to_csv(csv_path, index=False, encoding="utf-8-sig")
    (out / f"screen_{stamp}.json").write_text(json.dumps(
        {"request": request, "spec": spec.__dict__}, ensure_ascii=False, indent=1), encoding="utf-8")
    latest = out / "latest.txt"
    latest.write_text("\n".join(result["ticker"].dropna()) + "\n", encoding="utf-8")
    return csv_path, latest
