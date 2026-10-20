from __future__ import annotations

import json
from pathlib import Path
from typing import List, Tuple

import matplotlib.pyplot as plt
import numpy as np


ROOT = Path(__file__).resolve().parents[1]

SYNTHETIC_JSON = ROOT / "data" / "eval_report_full_v2.json"
REAL_JSON = ROOT / "data" / "real_logs_metrics_20260129_synthetic.json"
OUT_DIR = ROOT / "data" / "diagrams"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _save(fig: plt.Figure, name: str) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / name
    fig.savefig(out, dpi=220, bbox_inches="tight")
    plt.close(fig)
    return out


def _cleanup_old_pngs() -> None:
    if not OUT_DIR.exists():
        return
    for p in OUT_DIR.glob("*.png"):
        if p.name.startswith("synthetic_data__") or p.name.startswith("real_data__"):
            try:
                p.unlink()
            except Exception:
                pass


def _barh_with_counts(
    *,
    title: str,
    labels: List[str],
    values: List[int],
    color: str,
    xlabel: str = "count",
    max_items: int = 12,
) -> plt.Figure:
    items = list(zip(labels, values))
    items = sorted(items, key=lambda x: x[1], reverse=True)[:max_items]
    if not items:
        items = [("none", 0)]

    labels2 = [k.replace("_", " ") for k, _ in items]
    values2 = [int(v) for _, v in items]

    fig, ax = plt.subplots(figsize=(8.2, 3.6), layout="constrained")
    y = np.arange(len(labels2))
    ax.barh(y, values2, color=color)
    ax.set_yticks(y, labels=labels2)
    ax.invert_yaxis()
    ax.set_xlabel(xlabel)
    ax.set_title(title)
    ax.grid(axis="x", alpha=0.15)

    xmax = max(values2) if values2 else 0
    pad = max(1, int(0.02 * xmax)) if xmax else 1
    ax.set_xlim(0, xmax + 8 * pad)

    for yi, v in zip(y, values2):
        ax.text(v + pad, yi, str(v), va="center", ha="left", fontsize=9, color="#111111")

    return fig


def _real_fn_breakdown_plot(*, real: dict) -> plt.Figure:
    breakdowns = real.get("breakdowns", {})
    fn_intent = breakdowns.get("fn_intent_counts", {})
    labels = [str(k) for k in fn_intent.keys()]
    values = [int(v) for v in fn_intent.values()]
    return _barh_with_counts(
        title="real_data — FN by intent",
        labels=labels,
        values=values,
        color="#fb6a4a",
        xlabel="FN count",
        max_items=12,
    )


def _synthetic_fn_breakdown_plot(*, syn: dict) -> plt.Figure:
    """
    FN breakdown for synthetic = how many attacks remain unblocked after each layer.
    This makes the final FN visually obvious and shows where most filtering happens.
    """
    stats = syn["stats"]
    per_layer = syn.get("per_layer_catch", [])

    attack_rows = int(stats["attack_rows"])
    fn = int(stats["fn"])

    stages: List[Tuple[str, int]] = [("attacks", attack_rows)]
    for r in per_layer:
        layer = str(r["layer"]).replace("_", " ")
        reached = int(r["attacks_reached"])
        blocked = int(r["attacks_blocked"])
        remaining = max(0, reached - blocked)
        stages.append((f"after {layer}", remaining))

    # Force the last stage to be FN for clarity (and consistency with stats)
    if stages:
        stages[-1] = ("FN", fn)

    labels = [k for k, _ in stages]
    values = [v for _, v in stages]

    return _barh_with_counts(
        title="synthetic_data — FN breakdown",
        labels=labels,
        values=values,
        color="#756bb1",
        xlabel="count",
        max_items=12,
    )


def main() -> int:
    _cleanup_old_pngs()
    syn = _load_json(SYNTHETIC_JSON)
    real = _load_json(REAL_JSON)

    fig = _synthetic_fn_breakdown_plot(syn=syn)
    _save(fig, "synthetic_data__fn_breakdown.png")

    fig = _real_fn_breakdown_plot(real=real)
    _save(fig, "real_data__fn_breakdown.png")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())


