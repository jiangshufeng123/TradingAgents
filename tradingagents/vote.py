"""Several runs of the same analysis, and the rating they agree on.

One run of an LLM pipeline is one sample: the same ticker and date can come back
Buy one time and Hold the next. Running it N times and taking the majority makes
the call steadier and shows how much the runs disagree. ``vote_runs``
(TRADINGAGENTS_VOTE_RUNS, or ``--runs`` on the CLI) sets N.

The consensus is the most common rating. A tie goes to the tied rating nearest
the runs' average score, and a remaining tie to the more cautious one (nearer
Hold). The run reported in full is the first run that gave the consensus rating,
so the report's reasoning matches its rating.
"""

from __future__ import annotations

from collections import Counter

from tradingagents.agents.rating import RATINGS_5_TIER

SCORES = {"Buy": 2, "Overweight": 1, "Hold": 0, "Underweight": -1, "Sell": -2}


def consensus(ratings: list[str]) -> dict:
    """The vote over ``ratings`` (one per run, in run order)."""
    valid = [r for r in ratings if r in SCORES]
    result = {
        "ratings": list(ratings),
        "runs": len(ratings),
        "valid": len(valid),
        "counts": {r: valid.count(r) for r in RATINGS_5_TIER if r in valid},
        "rating": None,
        "representative": None,
        "agreement": 0.0,
        "mean_score": None,
        "spread": None,
    }
    if not valid:
        return result
    counts = Counter(valid)
    top = max(counts.values())
    tied = [r for r in RATINGS_5_TIER if counts.get(r) == top]
    mean = sum(SCORES[r] for r in valid) / len(valid)
    rating = min(tied, key=lambda r: (abs(SCORES[r] - mean), abs(SCORES[r])))
    scores = [SCORES[r] for r in valid]
    result.update(
        rating=rating,
        representative=ratings.index(rating),
        agreement=top / len(valid),
        mean_score=mean,
        spread=max(scores) - min(scores),
    )
    return result


def agreement_label(vote: dict) -> str:
    """How firmly the runs agree, in words."""
    if not vote["valid"]:
        return "no usable rating"
    if vote["agreement"] == 1:
        return "unanimous"
    if vote["agreement"] >= 0.6 and vote["spread"] <= 2:
        return "clear majority"
    if vote["spread"] >= 3:
        return "split: runs point in opposite directions"
    return "weak majority"


def render_markdown(vote: dict) -> str:
    """The vote section for the saved report."""
    lines = [
        f"- Consensus rating: **{vote['rating'] or 'REVIEW'}** "
        f"({agreement_label(vote)}; {vote['counts'].get(vote['rating'], 0)} of {vote['valid']} runs)",
    ]
    if vote["mean_score"] is not None:
        lines.append(f"- Average score: {vote['mean_score']:+.2f} "
                     f"(Sell -2, Underweight -1, Hold 0, Overweight +1, Buy +2); "
                     f"spread {vote['spread']}")
    if vote["representative"] is not None:
        lines.append(f"- The full report below is run {vote['representative'] + 1}, the first run "
                     f"that gave the consensus rating; every run is saved under runs/.")
    lines += ["", "| Run | Rating |", "|---:|---|"]
    lines += [f"| {i} | {r} |" for i, r in enumerate(vote["ratings"], 1)]
    lines += ["", "| Rating | Runs |", "|---|---:|"]
    lines += [f"| {r} | {n} |" for r, n in vote["counts"].items()]
    return "\n".join(lines)
