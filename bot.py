"""Trail Telegram bot: /start, /scan <address> — live-updating investigation view.

Run:  python bot.py
"""

import asyncio
import html
import logging
import re
import time

from telegram import BotCommand, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from agent import investigate
from config import load_config, setup_logging

log = logging.getLogger("trail.bot")

BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
SIG_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{64,90}$")  # tx signatures are longer than addresses

RISK_EMOJI = {"low": "🟢", "medium": "🟡", "high": "🟠", "critical": "🔴"}
TG_LIMIT = 4096

# one investigation per user at a time (in-memory is fine for the hackathon)
ACTIVE_USERS: set[int] = set()
# protect Helius/Birdeye rate limits under judge-traffic spikes
MAX_GLOBAL_INVESTIGATIONS = 3
# last finished case file per user, for /last
LAST_CASES: dict[int, str] = {}

CFG = None  # set in main()


def _short(addr: str) -> str:
    return addr if len(addr) <= 12 else f"{addr[:4]}..{addr[-4:]}"


def _solscan_link(ref: str) -> str:
    """Turn a base58 reference into an HTML solscan link; otherwise escape as plain text."""
    ref = (ref or "").strip()
    if SIG_RE.match(ref):
        return f'<a href="https://solscan.io/tx/{ref}">{_short(ref)}</a>'
    if BASE58_RE.match(ref):
        return f'<a href="https://solscan.io/account/{ref}">{_short(ref)}</a>'
    return html.escape(ref) if ref else ""


class LiveMessage:
    """Edits one Telegram message as the investigation progresses.

    Throttled to ~1 edit/sec (Telegram limit); rapid steps get batched naturally
    because each edit re-renders all lines collected so far.
    """

    def __init__(self, message, header: str):
        self.message = message
        self.header = header
        self.lines: list[str] = []
        self._last_edit = 0.0
        self._last_text = ""
        self._lock = asyncio.Lock()

    async def add_line(self, line: str) -> None:
        self.lines.append(html.escape(line))
        await self._flush()

    async def _flush(self) -> None:
        async with self._lock:
            wait = 1.05 - (time.monotonic() - self._last_edit)
            if wait > 0:
                await asyncio.sleep(wait)
            body = "\n".join(self.lines)
            text = f"{self.header}\n\n{body}"
            if len(text) > TG_LIMIT - 100:  # keep the newest steps visible
                text = f"{self.header}\n\n…\n" + "\n".join(self.lines[-25:])
            if text == self._last_text:
                return
            await self._edit(text)

    async def finalize(self, final_html: str) -> None:
        async with self._lock:
            wait = 1.05 - (time.monotonic() - self._last_edit)
            if wait > 0:
                await asyncio.sleep(wait)
            await self._edit(final_html)

    async def _edit(self, text: str) -> None:
        for attempt in (1, 2):
            try:
                await self.message.edit_text(
                    text, parse_mode=ParseMode.HTML, disable_web_page_preview=True
                )
                self._last_text = text
                self._last_edit = time.monotonic()
                return
            except RetryAfter as e:
                log.warning("telegram flood control: waiting %ss", e.retry_after)
                await asyncio.sleep(float(e.retry_after) + 0.5)
            except BadRequest as e:
                if "not modified" in str(e).lower():
                    return
                log.warning("telegram edit failed: %s", e)
                return
            except TelegramError as e:
                log.warning("telegram error on edit: %s", e)
                return


def render_case_file_html(case: dict, address: str) -> str:
    emoji = RISK_EMOJI.get(case.get("risk_level", ""), "⚪")
    risk = html.escape(str(case.get("risk_level", "unknown")).upper())
    verdict = html.escape(str(case.get("verdict", "")))
    profile = html.escape(str(case.get("entity_profile", "unknown")).replace("_", " "))
    confidence = case.get("confidence", "?")

    head = (
        f"{emoji} <b>TRAIL CASE FILE — {risk} RISK</b>\n"
        f"Target: {_solscan_link(address)}\n\n"
        f"<b>Verdict:</b> {verdict}\n"
        f"<b>Confidence:</b> {confidence}%   <b>Profile:</b> {profile}\n"
    )

    meta = case.get("_meta") or {}
    footer = ""
    if meta:
        footer = (
            f"\n<i>{meta.get('tool_calls', '?')} on-chain lookups · "
            f"{meta.get('runtime_calls', '?')} agent reasoning steps · "
            f"{meta.get('seconds', '?')}s</i>"
        )
        if "btl_charge_usd" in meta:
            charge = meta["btl_charge_usd"]
            saved = meta.get("btl_saved_usd") or 0
            cost_bits = f"free route" if charge == 0 else f"${charge:.4f}"
            if saved > 0:
                cost_bits += f", saved ${saved:.4f} via BTL routing"
            footer += f"\n<i>runtime cost: {cost_bits}</i>"

    # Build evidence + path, trimming to fit the 4096-char message limit
    evidence = case.get("evidence") or []
    path = case.get("investigation_path") or []

    def build(n_ev: int, n_path: int) -> str:
        parts = [head]
        if evidence[:n_ev]:
            parts.append("<b>Evidence:</b>")
            for i, e in enumerate(evidence[:n_ev], 1):
                finding = html.escape(str(e.get("finding", "")))
                why = html.escape(str(e.get("why_it_matters", "")))
                ref = _solscan_link(str(e.get("reference", "")))
                item = f"{i}. {finding}"
                if ref:
                    item += f" [{ref}]"
                if why:
                    item += f"\n   <i>{why}</i>"
                parts.append(item)
            if n_ev < len(evidence):
                parts.append(f"<i>…{len(evidence) - n_ev} more finding(s) truncated</i>")
            parts.append("")
        if path[:n_path]:
            parts.append("<b>Investigation path:</b>")
            for i, p in enumerate(path[:n_path], 1):
                parts.append(f"{i}. {html.escape(str(p))}")
        parts.append(footer)
        return "\n".join(parts)

    n_ev, n_path = len(evidence), len(path)
    text = build(n_ev, n_path)
    while len(text) > TG_LIMIT - 50 and (n_ev > 1 or n_path > 0):
        if n_path > 0:
            n_path -= 1
        else:
            n_ev -= 1
        text = build(n_ev, n_path)
    return text[: TG_LIMIT - 1]


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:  # edited messages / channel posts
        return
    await update.message.reply_text(
        "🕵️ <b>I'm Trail. I investigate Solana addresses.</b>\n\n"
        "Drop me any address — a token mint or a wallet — and I'll put an AI "
        "detective on the case. It decides what to check, follows the money "
        "across wallets, and comes back with a verdict backed by on-chain "
        "evidence. You watch every step happen live, including its reasoning.\n\n"
        "🔴 rug setups &amp; insider launches\n"
        "🤖 sniper bots &amp; serial deployers\n"
        "🟢 …or a clean bill of health\n\n"
        "<b>Just paste an address.</b> Or:\n"
        "<code>/scan &lt;address&gt;</code> — standard investigation (~90s)\n"
        "<code>/scan &lt;address&gt; deep</code> — longer trail, more wallet hops\n"
        "<code>/last</code> — resend your latest case file\n"
        "<code>/help</code> — full command reference &amp; how to read a case file\n\n"
        "Try it on BONK:\n"
        "<code>DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_text(
        "🕵️ <b>Trail — command reference</b>\n\n"
        "<b>Commands</b>\n"
        "<code>/scan &lt;address&gt;</code> — investigate a token mint or wallet "
        "(~90s, up to 8 AI reasoning rounds)\n"
        "<code>/scan &lt;address&gt; deep</code> — deep scan: longer trail, more "
        "wallet hops (~3 min, up to 12 rounds)\n"
        "<code>/last</code> — resend your most recent case file\n"
        "<code>/start</code> — intro &amp; quick example\n"
        "<code>/help</code> — this page\n\n"
        "<b>Shortcut:</b> you don't need <code>/scan</code> at all — just paste "
        "a Solana address on its own and I'll investigate it.\n\n"
        "<b>How to read a case file</b>\n"
        "🟢 low &nbsp; 🟡 medium &nbsp; 🟠 high &nbsp; 🔴 critical — overall risk\n"
        "<b>Confidence</b> — how much evidence backs the verdict (low if data "
        "was missing or the scan was cut short)\n"
        "<b>Entity profile</b> — my best label for what this address is: "
        "<i>serial_deployer, insider, bot, normal_trader, cex_linked, fund, "
        "unknown</i>\n"
        "<b>Evidence</b> — each finding links to the real address/tx on Solscan "
        "so you can verify it yourself\n\n"
        "<b>While a scan runs</b> you'll see it live: 🔍 = pulling data, 💭 = "
        "the agent reasoning about what it found, → = a result. That's an LLM "
        "deciding the investigation path in real time, not a canned script.\n\n"
        "<b>Limits:</b> one scan per person at a time, up to 3 running across "
        "all users at once (so I don't get rate-limited mid-investigation). "
        "Just paste an address — a token mint or a wallet — to get started.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_last(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    case_html = LAST_CASES.get(update.effective_user.id)
    if case_html:
        await update.message.reply_text(
            case_html, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )
    else:
        await update.message.reply_text(
            "No case files yet — paste a Solana address and I'll open one."
        )


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    user_id = update.effective_user.id
    args = context.args or []

    # accept "/scan <addr>", "/scan <addr> deep", "/scan deep <addr>"
    deep = any(a.lower() == "deep" for a in args)
    address = next((a.strip() for a in args if a.lower() != "deep"), "")
    if not address:
        await update.message.reply_text(
            "Send me an address: /scan <token mint or wallet> [deep]"
        )
        return
    if not BASE58_RE.match(address):
        await update.message.reply_text(
            "Hmm, that doesn't look like a valid Solana address "
            "(base58, 32–44 characters). Double-check and try again."
        )
        return
    if user_id in ACTIVE_USERS:
        await update.message.reply_text(
            "⏳ I'm already working a case for you — one investigation at a time."
        )
        return
    if len(ACTIVE_USERS) >= MAX_GLOBAL_INVESTIGATIONS:
        await update.message.reply_text(
            "🚦 All my investigators are on cases right now — try again in a minute."
        )
        return

    ACTIVE_USERS.add(user_id)
    try:
        mode_tag = " (deep scan)" if deep else ""
        header = (
            f"🔎 <b>Trail is on the case…{mode_tag}</b>\n"
            f"Target: <code>{html.escape(address)}</code>"
        )
        msg = await update.message.reply_text(
            header, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )
        live = LiveMessage(msg, header)

        async def on_step(desc, summary):
            if desc:
                await live.add_line(desc)
            if summary:
                await live.add_line(f"→ {summary}")

        case = await investigate(
            CFG,
            address,
            addr_type=None,
            on_step=on_step,
            max_rounds=12 if deep else None,
            max_seconds=180 if deep else None,
        )
        final_html = render_case_file_html(case, address)
        LAST_CASES[user_id] = final_html
        await live.finalize(final_html)
    except Exception:
        log.exception("investigation crashed for %s", address)
        try:
            await update.message.reply_text(
                "😵 Something went wrong during the investigation. Try again in a minute."
            )
        except TelegramError:
            pass
    finally:
        ACTIVE_USERS.discard(user_id)


async def bare_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Treat a bare pasted address as /scan <address>."""
    if update.message is None:
        return
    text = (update.message.text or "").strip()
    if BASE58_RE.match(text):
        context.args = [text]
        await cmd_scan(update, context)
    elif update.effective_chat and update.effective_chat.type == "private":
        # only nag about invalid input in DMs — in groups, stay silent
        await update.message.reply_text(
            "Paste a Solana address or use /scan <address>. /help for the full guide."
        )


async def _post_init(app: Application) -> None:
    """Populate Telegram's "/" command menu so commands are discoverable with descriptions."""
    await app.bot.set_my_commands(
        [
            BotCommand("scan", "Investigate a token mint or wallet address"),
            BotCommand("last", "Resend your most recent case file"),
            BotCommand("help", "Full command reference & how to read a case file"),
            BotCommand("start", "Intro & quick example"),
        ]
    )


def main() -> None:
    global CFG
    setup_logging()
    CFG = load_config(require_telegram=True)

    app = (
        Application.builder()
        .token(CFG.telegram_bot_token)
        .concurrent_updates(True)  # don't let one user's 90s scan block everyone
        .post_init(_post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("last", cmd_last))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bare_address))

    log.info("Trail bot starting (model=%s, native_tools=%s)", CFG.btl_model, CFG.btl_use_native_tools)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
