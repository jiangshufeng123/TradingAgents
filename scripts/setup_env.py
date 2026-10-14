"""Add or update the TradingAgents settings in .env, keeping everything else (API keys) as is.

Run from the project folder:  python scripts/setup_env.py
A copy of the old file is kept as .env.bak. Values are printed for the settings
below only; API keys are never printed.
"""
import os
import shutil
import sys

# Set (overwriting an existing value).
SET = {
    "TRADINGAGENTS_LLM_PROVIDER": "deepseek",
    "TRADINGAGENTS_QUICK_THINK_LLM": "deepseek-flash",
    "TRADINGAGENTS_DEEP_THINK_LLM": "deepseek-v4-pro",
    "TRADINGAGENTS_OUTPUT_LANGUAGE": "Chinese",
    "TRADINGAGENTS_MAX_DEBATE_ROUNDS": "1",
    "TRADINGAGENTS_MAX_RISK_ROUNDS": "1",
    "TRADINGAGENTS_WATCHLIST": "NVDA,600519.SS,603980.SS,0700.HK",
    "TRADINGAGENTS_ANALYSTS": "market,sentiment,news,fundamentals",
    "TRADINGAGENTS_SAVE_REPORT": "true",
    "TRADINGAGENTS_SHOW_REPORT": "false",
    "TRADINGAGENTS_HTML_REPORT": "true",
    "TRADINGAGENTS_OPEN_REPORT": "false",
}
# Set only when not already present (keeps your own choice).
DEFAULTS = {
    "TRADINGAGENTS_REPORTS_DIR": r"D:\tradingagents_reports",
    "TRADINGAGENTS_VOTE_RUNS": "2",
    "TRADINGAGENTS_MODEL_PRICES": "deepseek-v4-pro=9/27/0.3, deepseek-flash=2/8/0.04",
    "TRADINGAGENTS_PRICE_CURRENCY": "¥",
}
# Removed: Alpha Vantage's news endpoint is premium-only, so this chain only wastes a call.
REMOVE = {"TRADINGAGENTS_VENDOR_NEWS_DATA"}

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
path = os.path.join(root, ".env")
if not os.path.exists(path):
    sys.exit(f"No .env at {path}; copy .env.example to .env and add your API key first.")

with open(path, encoding="utf-8-sig") as f:
    lines = f.read().splitlines()
shutil.copyfile(path, path + ".bak")


def key_of(line):
    text = line.strip()
    if not text or text.startswith("#") or "=" not in text:
        return None
    return text.split("=", 1)[0].strip()


present = {key_of(l) for l in lines} - {None}
out, changed = [], []
for line in lines:
    key = key_of(line)
    if key in REMOVE:
        changed.append(f"removed  {key}")
        continue
    if key in SET:
        new = f"{key}={SET[key]}"
        if line.strip() != new:
            changed.append(f"updated  {new}")
        out.append(new)
        continue
    out.append(line)

added = [f"{k}={v}" for k, v in SET.items() if k not in present]
added += [f"{k}={v}" for k, v in DEFAULTS.items() if k not in present]
if added:
    out += ["", "# --- TradingAgents settings (scripts/setup_env.py) ---"] + added
    changed += [f"added    {a}" for a in added]

with open(path, "w", encoding="utf-8", newline="\n") as f:
    f.write("\n".join(out).rstrip("\n") + "\n")

print(f"Updated {path} (backup: .env.bak)")
for c in changed or ["nothing to change"]:
    print("  " + c)
kept = sorted(k for k in present if k not in SET and k not in REMOVE)
print(f"Kept {len(kept)} other setting(s) unchanged (API keys not shown).")
