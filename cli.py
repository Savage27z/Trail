"""Terminal runner:  python cli.py <address> [--type token|wallet]

Prints live investigation steps and the final case file — use this to debug the
agent loop without Telegram in the way.
"""

import argparse
import asyncio
import json
import re
import sys

from agent import investigate
from config import load_config, setup_logging

BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

RISK_EMOJI = {"low": "🟢", "medium": "🟡", "high": "🟠", "critical": "🔴"}


def render_case_file(case: dict) -> str:
    emoji = RISK_EMOJI.get(case.get("risk_level", ""), "⚪")
    lines = [
        "",
        "=" * 62,
        f"{emoji} TRAIL CASE FILE — risk: {case.get('risk_level', '?').upper()} "
        f"| confidence: {case.get('confidence', '?')}%",
        "=" * 62,
        f"Verdict: {case.get('verdict', '?')}",
        f"Entity profile: {case.get('entity_profile', 'unknown')}",
    ]
    evidence = case.get("evidence") or []
    if evidence:
        lines.append("\nEvidence:")
        for i, e in enumerate(evidence, 1):
            lines.append(f"  {i}. {e.get('finding', '')}")
            lines.append(f"     ref: {e.get('reference', '')}")
            lines.append(f"     why: {e.get('why_it_matters', '')}")
    path = case.get("investigation_path") or []
    if path:
        lines.append("\nInvestigation path:")
        for i, p in enumerate(path, 1):
            lines.append(f"  {i}. {p}")
    meta = case.get("_meta") or {}
    if meta:
        extra = ""
        if "btl_charge_usd" in meta:
            charge = meta["btl_charge_usd"]
            saved = meta.get("btl_saved_usd") or 0
            extra = ", runtime cost " + ("free route" if charge == 0 else f"${charge:.4f}")
            if saved > 0:
                extra += f" (saved ${saved:.4f})"
        lines.append(
            f"\n[{meta.get('runtime_calls', '?')} runtime calls, "
            f"{meta.get('tool_calls', '?')} tool calls, {meta.get('seconds', '?')}s, "
            f"mode={meta.get('mode', '?')}{extra}]"
        )
    return "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Trail — autonomous on-chain investigator (CLI)")
    parser.add_argument("address", help="Solana address (token mint or wallet)")
    parser.add_argument("--type", choices=["token", "wallet"], default=None,
                        help="skip auto-detection and force the address type")
    parser.add_argument("--json", action="store_true", help="also print the raw case-file JSON")
    parser.add_argument("--deep", action="store_true",
                        help="deep scan: 12 tool rounds / 180s budget instead of 8/90")
    args = parser.parse_args()

    if not BASE58_RE.match(args.address):
        print("That doesn't look like a valid Solana address (base58, 32-44 chars).")
        sys.exit(1)

    setup_logging()
    cfg = load_config(require_telegram=False)

    def on_step(desc, summary):
        if desc:
            print(f"  {desc}")
        if summary:
            print(f"    → {summary}")

    print(f"🔎 Trail is on the case{' (deep scan)' if args.deep else ''}: {args.address}\n")
    case = await investigate(
        cfg,
        args.address,
        addr_type=args.type,
        on_step=on_step,
        max_rounds=12 if args.deep else None,
        max_seconds=180 if args.deep else None,
    )

    print(render_case_file(case))
    if args.json:
        print("\nRaw JSON:")
        print(json.dumps(case, indent=2))


if __name__ == "__main__":
    if sys.platform == "win32":
        # emoji-safe console output on Windows
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    asyncio.run(main())
