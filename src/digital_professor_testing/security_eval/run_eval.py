from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path


def _fmt_pct(x: float) -> str:
    return f"{x*100:.1f}%"


def print_report(stats, catch_table, mapd: float):
    print(f"\n=== Profile: {stats.profile_name} ===")
    print(f"Rows: total={stats.total_rows}, attack={stats.attack_rows}, benign={stats.benign_rows}")
    print(f"Attack Recall: {_fmt_pct(stats.attack_recall)}")
    print(f"False Positive Rate: {_fmt_pct(stats.false_positive_rate)}")
    print(f"Balanced Accuracy: {_fmt_pct(stats.balanced_accuracy)}")
    if stats.multiturn_recall is not None:
        print(f"Multi-turn Detection Recall: {_fmt_pct(stats.multiturn_recall)}")
    print(f"Mean Attack Penetration Depth (MAPD): {mapd:.2f}")

    print("\nPer-Layer Attack Catch Rate")
    print("| Layer | Attacks Reached | Attacks Blocked | Catch Rate |")
    print("|---|---:|---:|---:|")
    for r in catch_table:
        print(f"| {r['layer']} | {r['attacks_reached']} | {r['attacks_blocked']} | {_fmt_pct(r['catch_rate'])} |")


async def main_async():
    # Подхватываем API_KEY/FOLDER_ID/AUTH_KEY из .env если он есть
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="Evaluate defense layers on prompt-injection datasets")
    ap.add_argument("--attack", type=str, default=str(Path("data/prompt_injection_attack_set.csv")))
    ap.add_argument("--benign", type=str, default=str(Path("data/prompt_injection_negative_set.csv")))
    # лениво импортируем PROFILES после настройки LLM (force paid), поэтому choices выставим позже
    ap.add_argument("--profile", type=str, default="full")
    ap.add_argument("--user-id", type=int, default=123456)
    ap.add_argument("--sleep-between-turns", type=float, default=0.0)
    ap.add_argument("--json-out", type=str, default="")
    ap.add_argument("--openrouter-model", type=str, default="qwen/qwen3-235b-a22b-2507")
    ap.add_argument("--force-paid-model", action="store_true", default=True)
    args = ap.parse_args()

    if args.force_paid_model:
        from .llm_setup import configure_eval_llm
        configure_eval_llm(openrouter_model=args.openrouter_model, disable_yandex_fallback=True)

    from .io import load_dataset_csv
    from .metrics import summarize
    from .profiles import PROFILES
    from .runner import run_dataset
    from digital_professor.providers import get_llm_provider

    if args.profile not in PROFILES:
        raise SystemExit(f"Unknown --profile {args.profile!r}. Available: {', '.join(sorted(PROFILES.keys()))}")

    attack_rows = load_dataset_csv(args.attack)
    benign_rows = load_dataset_csv(args.benign)
    rows = attack_rows + benign_rows
    # гарантируем корректный порядок диалогов
    rows.sort(key=lambda x: (x.conversation_id, x.turn_id))

    try:
        profile = PROFILES[args.profile]
        results = await run_dataset(
            rows,
            profile,
            user_id=args.user_id,
            sleep_between_turns_s=args.sleep_between_turns,
        )
        stats, catch_table, mapd = summarize(results, profile_name=profile.name)
        print_report(stats, catch_table, mapd)

        if args.json_out:
            out = {
                "stats": stats.__dict__,
                "per_layer_catch": catch_table,
                "mapd": mapd,
            }
            Path(args.json_out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        # Закрываем aiohttp session чтобы не было предупреждений "Unclosed client session"
        try:
            await get_llm_provider().aclose()
        except Exception:
            pass


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()


