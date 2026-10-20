from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import pandas as pd


@dataclass(frozen=True)
class Metrics:
    name: str
    n: int
    tp: int
    fn: int
    recall: float


def _norm_action(x: object) -> Optional[str]:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    s = str(x).strip().lower()
    if s in {"allow", "ok", "pass"}:
        return "allow"
    if s in {"block", "blocked", "deny", "reject"}:
        return "block"
    return None


def _norm_status(x: object) -> Optional[str]:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    s = str(x).strip().lower()
    if s == "ok":
        return "allow"
    if s == "blocked":
        return "block"
    return None


def _safe_div(a: float, b: float) -> float:
    return float(a) / float(b) if b else 0.0


def _recall(df: pd.DataFrame, name: str) -> Metrics:
    """
    Recall по expected_action=block:
      TP = pred=block
      FN = pred=allow
    """
    n = len(df)
    tp = int((df["pred_action"] == "block").sum())
    fn = int((df["pred_action"] == "allow").sum())
    return Metrics(name=name, n=n, tp=tp, fn=fn, recall=_safe_div(tp, tp + fn))


def _fmt_pct(x: float) -> str:
    return f"{x*100:.1f}%"

def _per_layer_catch_rate(
    df: pd.DataFrame,
    *,
    layer_order: list[str],
    name: str,
) -> list[dict]:
    """
    Per-Layer Catch Rate для block-worthy строк (expected_action=block).

    Определения:
    - blocked_layer берём из reason_norm для строк pred_action=block
      (для pred_action=allow ставим blocked_layer='none')
    - reached(layer) = сколько строк дошло до слоя (т.е. не было остановлено раньше):
        blocked_layer в {layer, любые более поздние, 'none'}
    - blocked(layer) = blocked_layer == layer
    """
    d = df.copy()
    d["blocked_layer"] = d["reason_norm"]
    d.loc[d["pred_action"] == "allow", "blocked_layer"] = "none"

    idx = {layer: i for i, layer in enumerate(layer_order)}

    def layer_index(x: str) -> int:
        if x == "none":
            return len(layer_order)  # прошёл все слои
        return idx.get(x, len(layer_order) + 1)  # неизвестный слой -> после всех (чтобы не ломать reached)

    d["blocked_layer_idx"] = d["blocked_layer"].map(layer_index)

    out: list[dict] = []
    for i, layer in enumerate(layer_order):
        reached = int((d["blocked_layer_idx"] >= i).sum())
        blocked = int((d["blocked_layer"] == layer).sum())
        out.append(
            {
                "table": name,
                "layer": layer,
                "attacks_reached": reached,
                "attacks_blocked": blocked,
                "catch_rate": _safe_div(blocked, reached),
            }
        )
    return out


def main():
    ap = argparse.ArgumentParser(description="Metrics on labeled real logs (Excel)")
    ap.add_argument("--xlsx", type=str, required=True)
    ap.add_argument("--sheet", type=str, default=None)
    ap.add_argument("--json-out", type=str, default="")
    ap.add_argument(
        "--exclude-intent-from-blockworthy",
        type=str,
        default="",
        help=(
            "Comma-separated intent_label values (lowercase after normalization) to exclude from "
            "block-worthy subset (expected_action=block) when computing recall/per-layer metrics. "
            "Example: --exclude-intent-from-blockworthy benign"
        ),
    )
    args = ap.parse_args()

    df = pd.read_excel(args.xlsx, sheet_name=args.sheet)
    # pandas возвращает dict листов, если sheet_name=None и в файле несколько листов
    if isinstance(df, dict):
        if args.sheet:
            raise SystemExit(f"Sheet {args.sheet!r} not found. Available: {list(df.keys())}")
        # берём первый лист
        first = next(iter(df.keys()))
        df = df[first]

    required = {"status", "reason", "intent_label", "expected_action"}
    missing = required - set(df.columns)
    if missing:
        raise SystemExit(f"Missing columns in xlsx: {sorted(missing)}")

    df["pred_action"] = df["status"].map(_norm_status)
    df["expected_action_norm"] = df["expected_action"].map(_norm_action)
    df["intent_label_norm"] = df["intent_label"].astype(str).str.strip().str.lower()
    df["reason_norm"] = df["reason"].fillna("none").astype(str).str.strip().str.lower()

    bad_pred = df["pred_action"].isna().sum()
    bad_expected = df["expected_action_norm"].isna().sum()
    if bad_pred or bad_expected:
        print(f"WARNING: bad pred_action rows={bad_pred}, bad expected_action rows={bad_expected} (will be dropped)")
        df = df[df["pred_action"].notna() & df["expected_action_norm"].notna()].copy()

    # --- Overall confusion matrix (allow/block) ---
    tp = int(((df["pred_action"] == "block") & (df["expected_action_norm"] == "block")).sum())
    fp = int(((df["pred_action"] == "block") & (df["expected_action_norm"] == "allow")).sum())
    tn = int(((df["pred_action"] == "allow") & (df["expected_action_norm"] == "allow")).sum())
    fn = int(((df["pred_action"] == "allow") & (df["expected_action_norm"] == "block")).sum())

    # FPR = FP / (FP + TN) по expected_action=allow
    fpr = _safe_div(fp, fp + tn)
    tpr_block = _safe_div(tp, tp + fn)  # recall for block-worthy
    tnr_allow = _safe_div(tn, tn + fp)  # recall for allow (specificity)
    balanced_accuracy = 0.5 * (tpr_block + tnr_allow)

    print("\n=== Overall (allow/block) ===")
    print(f"Rows: {len(df)}")
    print(f"TP(block correct): {tp}")
    print(f"FP(overblock): {fp}")
    print(f"TN(allow correct): {tn}")
    print(f"FN(underblock): {fn}")
    print(f"False Positive Rate (FPR): {_fmt_pct(fpr)}  # доля ЛЕГИТИМНЫХ allow, ошибочно заблокированных")
    print(f"Balanced Accuracy: {_fmt_pct(balanced_accuracy)}")

    # --- Recalls (block-worthy) ---
    blockworthy = df[df["expected_action_norm"] == "block"].copy()
    if args.exclude_intent_from_blockworthy:
        excluded = {s.strip().lower() for s in args.exclude_intent_from_blockworthy.split(",") if s.strip()}
        if excluded:
            blockworthy = blockworthy[~blockworthy["intent_label_norm"].isin(excluded)].copy()
    print("\n=== Recall (block-worthy) ===")
    m_all = _recall(blockworthy, "all_expected_block")
    print(f"{m_all.name}: n={m_all.n}, TP={m_all.tp}, FN={m_all.fn}, recall={_fmt_pct(m_all.recall)}")

    inj = blockworthy[blockworthy["intent_label_norm"] == "prompt_injection"]
    m_inj = _recall(inj, "prompt_injection")
    print(f"{m_inj.name}: n={m_inj.n}, TP={m_inj.tp}, FN={m_inj.fn}, recall={_fmt_pct(m_inj.recall)}")

    abuse = blockworthy[blockworthy["intent_label_norm"] == "abuse_policy"]
    m_abuse = _recall(abuse, "abuse_policy")
    print(f"{m_abuse.name}: n={m_abuse.n}, TP={m_abuse.tp}, FN={m_abuse.fn}, recall={_fmt_pct(m_abuse.recall)}")

    noise = blockworthy[blockworthy["intent_label_norm"] == "noise_spam"]
    m_noise = _recall(noise, "noise_spam")
    print(f"{m_noise.name}: n={m_noise.n}, TP={m_noise.tp}, FN={m_noise.fn}, recall={_fmt_pct(m_noise.recall)}")

    # --- Underblock breakdown by reason for FN ---
    fn_df = blockworthy[blockworthy["pred_action"] == "allow"]
    print("\n=== Underblocks (FN) breakdown ===")
    if len(fn_df) == 0:
        print("No FN.")
    else:
        print(fn_df["intent_label_norm"].value_counts().to_string())
        print("\nTop reasons (should be 'none' for ok rows):")
        print(fn_df["reason_norm"].value_counts().head(10).to_string())

    # --- Overblock breakdown by intent for FP ---
    fp_df = df[(df["expected_action_norm"] == "allow") & (df["pred_action"] == "block")]
    print("\n=== Overblocks (FP) breakdown ===")
    if len(fp_df) == 0:
        print("No FP.")
    else:
        print(fp_df["intent_label_norm"].value_counts().to_string())
        print("\nTop block reasons:")
        print(fp_df["reason_norm"].value_counts().head(10).to_string())

    # --- Per-layer Catch Rate (для expected_action=block) ---
    # В реальном продовом порядке (как сейчас в bot.py): input_validator -> rate_limiter -> llm_censor
    per_layer_prod = _per_layer_catch_rate(
        blockworthy,
        layer_order=["input_validator", "rate_limiter", "llm_censor"],
        name="prod_order",
    )
    # Вариант без rate limiter (если исключаете его из анализа): input_validator -> llm_censor
    per_layer_no_rl = _per_layer_catch_rate(
        blockworthy,
        layer_order=["input_validator", "llm_censor"],
        name="no_rate_limiter",
    )

    print("\n=== Per-Layer Attack Catch Rate (expected_action=block) ===")
    print("Order: input_validator -> rate_limiter -> llm_censor")
    print("| Layer | Attacks Reached | Attacks Blocked | Catch Rate |")
    print("|---|---:|---:|---:|")
    for r in per_layer_prod:
        print(f"| {r['layer']} | {r['attacks_reached']} | {r['attacks_blocked']} | {_fmt_pct(r['catch_rate'])} |")

    # --- Optional: write JSON report ---
    if args.json_out:
        out = {
            "source": {
                "xlsx": args.xlsx,
                "sheet": args.sheet,
            },
            "rows": {
                "total_after_drop": int(len(df)),
                "dropped_bad_expected_action": int(bad_expected),
                "dropped_bad_pred_action": int(bad_pred),
            },
            "overall": {
                "tp": int(tp),
                "fp": int(fp),
                "tn": int(tn),
                "fn": int(fn),
                "fpr": float(fpr),
                "balanced_accuracy": float(balanced_accuracy),
            },
            "recall": {
                m_all.name: asdict(m_all),
                m_inj.name: asdict(m_inj),
                m_abuse.name: asdict(m_abuse),
                m_noise.name: asdict(m_noise),
            },
            "per_layer_catch_rate": {
                "prod_order": per_layer_prod,
                "no_rate_limiter": per_layer_no_rl,
            },
            "breakdowns": {
                "fn_intent_counts": fn_df["intent_label_norm"].value_counts().to_dict() if len(fn_df) else {},
                "fn_reason_counts": fn_df["reason_norm"].value_counts().head(50).to_dict() if len(fn_df) else {},
                "fp_intent_counts": fp_df["intent_label_norm"].value_counts().to_dict() if len(fp_df) else {},
                "fp_reason_counts": fp_df["reason_norm"].value_counts().head(50).to_dict() if len(fp_df) else {},
            },
        }

        Path(args.json_out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nSaved JSON report to: {args.json_out}")


if __name__ == "__main__":
    main()


