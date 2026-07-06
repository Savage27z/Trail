# 🕵️ Trail — Autonomous On-Chain Investigator

**Live bot: [@trail_inv_bot](https://t.me/trail_inv_bot)** — paste any Solana address and watch the investigation happen in real time.

Trail is a Telegram bot backed by an autonomous LLM agent running on the **BTL Runtime**. Give it any Solana address — a token mint or a wallet — and it investigates like a financial-crimes analyst: pulls on-chain data, forms a hypothesis, and *decides for itself* what to look at next, chasing leads across wallets (funding sources, linked deployments, holder clusters) until it can deliver a verdict with cited, verifiable on-chain evidence.

Built solo for the **BTL Runtime Hackathon**, July 2026.

---

## Why this is different

Static rug-checkers (RugCheck, token sniffers) run a fixed list of heuristics and print a score. Trail's investigation path is **chosen live by the LLM**, step by step, based on what each tool call reveals. That means it follows trails no fixed pipeline can:

> *deployer wallet is 5 days old → funded by another fresh wallet → that funder also financed the wallet holding 50% of supply → the funder deployed 25 other tokens → sampled outcomes: rugged.*

Each hop in that chain is a **decision**, made by the model, in reaction to evidence. The user watches every decision happen live — including the agent's own reasoning (💭 lines) between tool calls.

During development, Trail scanned a pump.fun token that was minutes old and produced a critical-risk case file proving (via the creation transaction) that the deployer and a sniper bot had jointly pre-funded the wallet holding 50% of supply. Between two test runs the token's liquidity fell from $2,311 to $5.44 — **Trail documented the rug while it was happening.**

---

## How it works

```
 Telegram user                          BTL Runtime  (/v1/chat/completions)
      │                                          ▲
      │ /scan <address>                          │  chat + 7 tool schemas
      ▼                                          │  (or ReAct JSON fallback)
 ┌───────────┐    on_step events    ┌────────────┴─────────────┐
 │  bot.py    │◄────────────────────│         agent.py          │
 │ live-edit  │  🔍 steps, 💭 agent │  RuntimeClient (all BTL   │
 │ Telegram   │  thoughts, → results│  I/O) + investigation     │
 │ message    │                     │  loop (8 rounds / 90s)    │
 └───────────┘                      └────────────┬─────────────┘
                                                 │ execute tool_calls
                                                 │ (parallel per round)
                                                 ▼
                                    ┌──────────────────────────┐
                                    │         tools.py          │
                                    │ 7 investigation tools     │
                                    │ cache · semaphore · retry │
                                    └────────┬────────┬────────┘
                                             ▼        ▼
                                          Helius    Birdeye
                                       (RPC · DAS ·  (market · security ·
                                        parsed txs)   creation · price history)
```

### The investigation loop, in detail

1. **Address triage** — one RPC call classifies the input: token mint, wallet, or token account (it explains the difference to the user if they pasted a token account).
2. **The agent takes over.** Each round, the full conversation (system prompt + everything learned so far) goes to the BTL runtime with 7 tool schemas. The model returns either tool calls (executed **in parallel**, results appended as `tool` messages) or a conclusion.
3. **Live progress** — every reasoning step and tool result is emitted through an `on_step` callback. The Telegram layer renders them as an editing message; the CLI prints them. The agent core has zero Telegram dependencies.
4. **Budget enforcement** — max 8 tool rounds and 90 seconds. Hitting a limit doesn't fail the run: the agent is told it's out of budget and must produce a verdict from the evidence it has, with reduced confidence.
5. **Structured verdict** — a final runtime call demands a strict JSON case file (schema below). Parsing is defensive: markdown fences stripped, trailing commas tolerated, one automatic model-repair retry, all fields validated and clamped.

### The case-file schema

```json
{
  "verdict": "one-sentence conclusion",
  "risk_level": "low | medium | high | critical",
  "confidence": 0-100,
  "entity_profile": "serial_deployer | insider | bot | normal_trader | cex_linked | fund | unknown",
  "evidence": [
    {"finding": "...", "reference": "real tx sig or address from tool results", "why_it_matters": "..."}
  ],
  "investigation_path": ["steps in order"]
}
```

The system prompt enforces **evidence discipline**: every citation must be a real value copied from tool results, and missing data must be disclosed in the case file rather than papered over. In live testing the agent does exactly that ("investigation truncated by budget but already conclusive").

---

## BTL Runtime usage

- **Endpoint:** `POST {BTL_BASE_URL}/v1/chat/completions` via the OpenAI SDK with a custom `base_url`. Model fully configurable (`BTL_MODEL`).
- **Agentic tool-calling loop:** the runtime is the *decision-maker*, not a one-shot summarizer. Each investigation is a multi-turn conversation (typically **4–10 runtime calls**; the exact count is printed in every case-file footer) where the model chooses among 7 on-chain tools in native OpenAI function-calling format, reacts to results, and follows leads across wallets. Parallel tool calls in one round execute concurrently.
- **Structured output:** the verdict call requires schema-exact JSON, with fence-stripping, trailing-comma repair, one automatic model-repair round trip, and full field validation.
- **Compatibility hardening:** every byte to/from the runtime goes through one class, `RuntimeClient` (`agent.py`): 2 retries with exponential backoff on 5xx/429/timeouts, timestamped truncated request/response logging, and clean typed errors. If the runtime rejects the native `tools` parameter (4xx), the agent **automatically restarts the investigation in ReAct mode** — the model emits `{"thought", "action", "args"}` JSON that Trail parses manually. Also forceable via `BTL_USE_NATIVE_TOOLS=false`.
- **Billing transparency:** Trail reads BTL's `x-btl-customer-charge` / `x-btl-saved` response headers on every call and reports the investigation's total runtime cost in the case-file footer (e.g. `runtime cost: free route` or `$0.0004, saved $0.0003 via BTL routing`).
- **Visible reasoning:** the model's inter-tool thinking is streamed into the live Telegram view as 💭 lines, so the runtime's decision-making is watchable, not hidden.

---

## The 7 investigation tools

| Tool | Source | Question it answers |
|---|---|---|
| `get_token_overview` | Birdeye overview + security | Vital signs: age, price, liquidity, holders, top-10 concentration %, freeze authority, mutable metadata, and **social presence** (a new token with zero website/socials is flagged as a risk signal) |
| `get_deployer` | Helius DAS → Birdeye creation tx | Who *really* created this token? Sees through launchpad infra (pump.fun mints via its own authority — Trail resolves the actual human creator from the creation tx) |
| `get_wallet_funding` | Helius parsed history | Where did this wallet's money come from? Earliest incoming SOL transfers with sources, amounts, timestamps, tx sigs — the lead-generator |
| `get_wallet_tokens_deployed` | Helius DAS | What else has this wallet launched? (serial-deployer detection) |
| `get_wallet_activity` | Helius RPC | Wallet age, tx count/rate, failed-tx ratio (a >40% failure rate is flagged as sniper/spam-bot behavior), top counterparties |
| `get_holder_overlap` | Helius RPC | Top-20 holders resolved to owner wallets with % of supply, concentration, known pools/CEX labeled as benign, and wallet-age freshness checks — including a *hyperactive* flag for wallets with 1000+ txs all within days |
| `check_token_outcome` | Birdeye price history | Did a past launch rug? ATH drawdown classified as `rugged` / `collapsed` / `heavy_drawdown` / `alive` |

**Engineering rules shared by all tools** (`tools.py`):
- Errors are returned as data (`{"error": "helius rate limited"}`), never raised — the agent routes around failures and says so in the case file.
- Compact JSON: top-10 array truncation, shortened addresses in prose, `None` fields dropped — 8 rounds of context stay token-cheap.
- Per-investigation cache keyed by tool+args (errors deliberately *not* cached, so retries after transient failures work).
- Global 3-slot semaphore + one retry with backoff on 429/5xx.
- A curated label map (Raydium/Meteora/pump.fun infra plus Binance, Coinbase, Bybit, OKX, MEXC, Kraken and Gate.io hot wallets) prevents the classic false positive of "OMG one wallet holds 60%!" when that wallet is a liquidity pool — and stops the agent wasting budget investigating platform infrastructure.

---

## Example investigation (real, unedited)

A pump.fun token scanned **minutes after launch**:

```
🔎 Trail is on the case: Fj3r3Dv44T8HRA1LHTkiohxfJWx3rTYWDaAaWcRjpump

  🔍 Pulling token overview (Fj3r..pump)...
    → $Venom, liq $5
  💭 3 holders, -85% daily price drop, tiny liquidity — this is screaming
     red flags. Let me get the deployer and holder breakdown simultaneously.
  🔍 Identifying the deployer (Fj3r..pump)...
  🔍 Analyzing top holders (Fj3r..pump)...
    → deployer J7mf..yanX
    → top10 own 100%, ⚠️ 2 fresh-wallet holder(s)
  🔍 Tracing wallet funding (49CA..UimX)...
    → first funded by J7mf..yanX with 0.0017 SOL
  💭 The fresh top holder (50.22%) was funded by the deployer AND by the
     other 49.78% holder. These wallets are linked.
  🔍 Profiling wallet activity (49CA..UimX)...
    → wallet age 0.0d, 19 txs
  📋 Compiling the case file...

🔴 TRAIL CASE FILE — risk: CRITICAL | confidence: 85%
Verdict: This is an insider-coordinated token launch where the deployer
and a sniper bot jointly funded a fresh wallet that now holds 50% of
supply, with near-zero liquidity ($5.44) and an 85% price crash —
highly likely a rug pull that has already been executed.
Entity profile: insider

Evidence (excerpt):
1. 100% of supply is controlled by exactly 2 wallets: a fresh 0-day-old
   wallet (49CAdz...) holding 50.22% and a bot wallet holding 49.78%.
2. The fresh 50% holder was funded by both the deployer (J7mfert...) and
   the other holder in the same minute the token was created.
   [tx KsXDSSCnybp7LoJQWjy7Vhmc3HCcvvhutrr55TKmRbtv4XdMEuU8NkZs...]
3. Token has only $5.44 in liquidity, 3 holders, -85% in 24h.

[7 runtime calls, 11 tool calls, mode=native_tools, runtime cost free route]
```

For contrast, the same agent scanned **BONK** and concluded 🟢 low risk — "mature blue-chip meme coin" — in 7 runtime calls, while routing around two Helius outages mid-run and disclosing the data gaps in its case file. It doesn't just cry rug.

---

## Setup

### 1. Get the keys (all free)

| Env var | Where to get it |
|---|---|
| `BTL_API_KEY` | BTL Runtime dashboard (hackathon signup) |
| `BTL_MODEL` | any tool-capable model from `GET /v1/models` — we use `deepseek-v4-flash` (free route, supports native tools) |
| `HELIUS_API_KEY` | [dashboard.helius.dev](https://dashboard.helius.dev) — free tier is plenty |
| `BIRDEYE_API_KEY` | [bds.birdeye.so](https://bds.birdeye.so) — Standard (free) tier |
| `TELEGRAM_BOT_TOKEN` | message [@BotFather](https://t.me/BotFather) → `/newbot` (only needed for the bot; the CLI runs without it) |

### 2. Install & configure

```bash
git clone https://github.com/Savage27z/Trail && cd Trail
python -m venv .venv
.venv\Scripts\activate            # Windows — use `source .venv/bin/activate` elsewhere
pip install -r requirements.txt
copy .env.example .env             # then fill in the keys (cp on macOS/Linux)
```

Startup validation is loud: if anything required is missing, Trail exits immediately with the exact list of missing variables.

### 3. Run it

```bash
# fastest way to see it work — full investigation in the terminal:
python cli.py DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263

# test any single tool standalone against live chain data:
python tools.py get_token_overview <mint>
python tools.py get_wallet_funding <wallet>

# the Telegram bot:
python bot.py
```

CLI flags: `--type token|wallet` skips auto-detection, `--json` prints the raw case file, `--deep` runs a 12-round / 180s deep scan.

**Bot commands:** paste a bare address (or `/scan <address>`) to investigate, `/scan <address> deep` for a longer trail with more wallet hops, `/last` to resend your latest case file. The bot runs at most one investigation per user and three globally (protects the data-provider rate limits under load).

### Configuration knobs (optional)

| Env var | Default | Effect |
|---|---|---|
| `BTL_BASE_URL` | `https://api.badtheorylabs.com/v1` | runtime endpoint |
| `BTL_USE_NATIVE_TOOLS` | `true` | `false` forces the ReAct fallback loop |
| `TRAIL_MAX_ROUNDS` | `8` | tool rounds per investigation (5 ≈ snappier demos, 8 ≈ deeper trails) |
| `TRAIL_MAX_SECONDS` | `90` | wall-clock budget before the verdict is forced |

---

## Deployment

Trail runs 24/7 on **Railway** as a worker (no HTTP port needed — the bot long-polls Telegram):

```bash
railway init
railway variables --set "BTL_API_KEY=..." --set "BTL_MODEL=deepseek-v4-flash" \
  --set "HELIUS_API_KEY=..." --set "BIRDEYE_API_KEY=..." --set "TELEGRAM_BOT_TOKEN=..."
railway up
```

`railway.json` sets the start command and an on-failure restart policy; `Procfile` and `.python-version` cover other Nixpacks-style hosts (Heroku, Render, Fly) too.

> ⚠️ Telegram allows **one** poller per bot token. Don't run `python bot.py` locally while a cloud instance is up — pause one of them first.

---

## Project structure

```
trail/
  bot.py          # telegram handlers, throttled live message editing (~1 edit/sec),
                  # HTML case-file rendering with solscan links, 4096-char budget
  agent.py        # RuntimeClient (ALL BTL runtime I/O) + native tool-calling loop +
                  # ReAct fallback + budgets + defensive verdict parsing + cost tracking
  tools.py        # 7 Helius/Birdeye tools, OpenAI schemas, cache, rate limiting,
                  # known-account labels, standalone test runner
  prompts.py      # investigator persona with worked reasoning examples, evidence
                  # discipline rules, verdict schema, ReAct protocol prompt
  config.py       # env loading with loud validation
  cli.py          # terminal runner for the full investigation loop
```

## Design decisions

- **Agent core is transport-agnostic.** `investigate()` reports progress through a single `on_step` callback; Telegram and the CLI are thin views over the same loop. Adding Discord/web would touch zero agent code.
- **Defensive by default.** External APIs flake constantly (we watched Helius return three different error types in one investigation). Every failure becomes data the agent can reason about, every budget overrun still produces a verdict, and a dead runtime yields a graceful low-confidence case file — never a stack trace at the user.
- **Honest verdicts over confident ones.** The prompt forbids invented citations, requires disclosure of missing data, and the tools themselves annotate ambiguity (e.g. a wallet whose history is capped is labeled `UNKNOWN — do NOT treat this wallet as fresh`).
- **Token frugality as a feature.** Compact tool outputs keep a full 8-round investigation cheap enough to run on free model routes.

## Known limitations

- Wall-clock can overshoot the 90s budget by the length of the final verdict call (~20s on free routes) — the budget gates *starting* rounds, not the in-flight call.
- Wallet history is paged to the 5,000 most recent transactions; older activity of very busy wallets is out of reach (and explicitly labeled as such to the agent).
- The known-accounts label map is small and curated; an unlabeled CEX wallet can look like a whale. Extending it is data entry, not engineering.
- In-memory state only: one investigation per user, forgotten on restart — the right trade-off for a hackathon, swappable for Redis later.

## License

MIT — see [LICENSE](LICENSE).
