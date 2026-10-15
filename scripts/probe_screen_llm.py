"""Why does the screen's model call not answer? Times a trivial call, then the real one.

    python scripts/probe_screen_llm.py
"""
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tradingagents.default_config import DEFAULT_CONFIG  # noqa: E402
from tradingagents.llm_clients import create_tier_client  # noqa: E402


def timed(llm, prompt, limit=300):
    box = {}

    def run():
        try:
            box["r"] = llm.invoke(prompt)
        except BaseException as exc:
            box["e"] = exc

    t = threading.Thread(target=run, daemon=True)
    start = time.time()
    t.start()
    while t.is_alive() and time.time() - start < limit:
        t.join(5)
        if t.is_alive():
            print(f"  ... still waiting, {time.time() - start:.0f}s", flush=True)
    took = time.time() - start
    if t.is_alive():
        print(f"  NO ANSWER after {took:.0f}s")
        return
    if "e" in box:
        print(f"  ERROR after {took:.0f}s: {type(box['e']).__name__}: {box['e']}")
        return
    r = box["r"]
    reasoning = (getattr(r, "additional_kwargs", {}) or {}).get("reasoning_content") or ""
    usage = getattr(r, "usage_metadata", None)
    print(f"  answered in {took:.1f}s; answer {len(r.content)} chars, reasoning {len(reasoning)} chars; usage {usage}")
    print("  answer starts:", r.content[:300].replace("\n", " "))


c = DEFAULT_CONFIG
print("provider", c.get("llm_provider"), "quick model", c.get("quick_think_llm"),
      "backend", c.get("backend_url"), "max_tokens", c.get("max_tokens"))
print("proxy env:", {k: v for k, v in os.environ.items() if "proxy" in k.lower()})
llm = create_tier_client(c, "quick").get_llm()
print("1) trivial call")
timed(llm, "Reply with the single word OK.", 120)

print("2) the real screen prompt")
from tradingagents import screener  # noqa: E402
from tradingagents.dataflows.config import run_config  # noqa: E402

with run_config(c):
    universe, period = screener.build_universe(c)
industries = sorted(universe["industry"].dropna().astype(str).unique())
prompt = screener._prompt("低估值、ROE高于15%的白酒和家电股", industries, period, universe.attrs.get("missing", []))
print(f"  prompt {len(prompt)} chars, {len(industries)} industries")
timed(llm, prompt, 300)
