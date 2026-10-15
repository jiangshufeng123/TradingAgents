"""Remove pending decisions from the memory log so a day can be analyzed again.

The memory log keeps the first decision per ticker and date and ignores later
runs of the same day. When a run was made on bad data, drop its entry first:

    python scripts/drop_log_entries.py --date 2026-10-15
    python scripts/drop_log_entries.py --date 2026-10-15 --ticker 6616.HK --ticker NVDA

Only pending entries are removed (settled ones already count in the scoreboard).
A backup is written next to the log first.
"""
import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tradingagents.default_config import DEFAULT_CONFIG  # noqa: E402
from tradingagents.memory.log import TradingMemoryLog  # noqa: E402

SEPARATOR = TradingMemoryLog._SEPARATOR


def drop(path: Path, date: str, tickers: set[str]) -> list[str]:
    text = path.read_text(encoding="utf-8")
    kept, dropped = [], []
    for chunk in text.split(SEPARATOR):
        head = chunk.strip().splitlines()[0] if chunk.strip() else ""
        parts = [p.strip() for p in head.strip("[]").split("|")] if head.startswith("[") else []
        if (len(parts) == 4 and parts[0] == date and parts[3] == "pending"
                and (not tickers or parts[1] in tickers)):
            dropped.append(head)
            continue
        kept.append(chunk)
    if dropped:
        shutil.copyfile(path, path.with_suffix(path.suffix + ".bak"))
        path.write_text(SEPARATOR.join(kept), encoding="utf-8")
    return dropped


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", required=True, help="trade date, YYYY-MM-DD")
    ap.add_argument("--ticker", action="append", default=[], help="only this ticker (repeatable)")
    ap.add_argument("--log", default=DEFAULT_CONFIG["memory_log_path"])
    args = ap.parse_args(argv)
    path = Path(args.log).expanduser()
    if not path.exists():
        print(f"No memory log at {path}")
        return 1
    dropped = drop(path, args.date, set(args.ticker))
    if not dropped:
        print(f"No pending entries for {args.date} in {path}")
        return 0
    print(f"Removed {len(dropped)} pending entr{'y' if len(dropped) == 1 else 'ies'} "
          f"(backup: {path.name}.bak):")
    for head in dropped:
        print("  " + head)
    return 0


if __name__ == "__main__":
    sys.exit(main())
