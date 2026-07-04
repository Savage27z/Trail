# 🕵️ Trail — Autonomous On-Chain Investigator

**Live bot: [@trail_inv_bot](https://t.me/trail_inv_bot)** — paste any Solana address and watch the investigation happen.

Paste any Solana address into a Telegram bot and watch an AI agent investigate it **live**. Trail is not a static rug-checker: an LLM agent *decides the investigation path itself* — it pulls on-chain data, forms a hypothesis, and chases suspicious leads across wallets (funding sources, linked deployments, holder clusters) the way a human analyst would. The output is a case file: a verdict, a risk level, an entity profile, and cited on-chain evidence.

The differentiator: hard-coded heuristics can't follow a trail like *"deployer is 5 days old → funded by a fresh wallet → that funder financed 5 other deployers → 4 of their tokens rugged."* An agent that picks its own next move can.

## Architecture

```
 Telegram user                     BTL Runtime (/v1/chat/completions)
      │                                        ▲
      │ /scan <address>                        │ chat + tool schemas
      ▼                                        │ (or ReAct JSON fallback)
 ┌──────────┐   on_step events   ┌─────────────┴────────────┐
 │  bot.py   │◄──────────────────│         agent.py          │
 │ live-edit │                   │  RuntimeClient + agent    │
 │  message  │                   │  loop (max 8 rounds/90s)  │
 └──────────┘                    └─────────────┬────────────┘
                                               │ execute tool_calls
                                               ▼
                                 ┌──────────────────────────┐
                                 │         tools.py          │
                                 │ 7 investigation tools     │
                                 │ cache · semaphore · retry │
                                 └─────────┬───────┬────────┘
                                           ▼       ▼
                                        Helius   Birdeye
                                     (RPC/DAS/   (market/
                                      parsed tx)  security/price)
```

**The loop:** the model receives the address + 7 tool schemas → decides which tool(s) to call → results are appended to the conversation → it decides the *next* move based on what it found → repeat until it concludes (or hits the 8-round / 90-second budget) → a final structured-output call produces the JSON case file. Every step is streamed to the Telegram message as it happens.

## BTL Runtime Usage (rubric checklist)

- **Endpoint:** `POST {BTL_BASE_URL}/v1/chat/completions` via the OpenAI SDK with a custom `base_url`.
- **Agentic tool-calling loop:** the runtime is the *decision-maker*, not a summarizer. Each investigation is a multi-turn conversation where the model chooses among 7 on-chain tools (OpenAI `tools`/function-calling format), reacts to results, and follows leads across wallets. Parallel tool calls in a round are executed concurrently.
- **Structured output:** the investigation ends with a dedicated runtime call that must emit a strict JSON case file (schema-validated, markdown-fence stripping + one automatic JSON-repair retry on malformed output).
- **Runtime calls per investigation:** typically **4–10** (1 per reasoning round + 1 verdict call + optional repair call). The exact count is printed in every case file footer (`_meta.runtime_calls`).
- **Compatibility hardening:** all runtime interaction is isolated in `RuntimeClient` (`agent.py`) — 2 retries with backoff on 5xx/timeouts, request/response logging, and an automatic **ReAct fallback** (model emits `{"thought", "action", "args"}` JSON, parsed manually) if the runtime rejects the native `tools` parameter. Also switchable explicitly via `BTL_USE_NATIVE_TOOLS=false`.
- **Billing transparency:** Trail reads BTL's `x-btl-customer-charge` / `x-btl-saved` response headers on every call and reports the total runtime cost of each investigation in the case-file footer.

## The 7 investigation tools

| Tool | Source | What it answers |
|---|---|---|
| `get_token_overview` | Birdeye | Vital signs: age, liquidity, holders, top-10 concentration, authorities |
| `get_deployer` | Helius DAS (+ Birdeye fallback) | Who created this token? |
| `get_wallet_funding` | Helius parsed history | Where did this wallet's money come from? (the lead-generator) |
| `get_wallet_tokens_deployed` | Helius DAS | What else has this wallet launched? |
| `get_wallet_activity` | Helius RPC | Wallet age, tx rate, failed-tx ratio (bot signal), counterparties |
| `get_holder_overlap` | Helius RPC | Top-20 holders, concentration, fresh-wallet insiders, known pools/CEX |
| `check_token_outcome` | Birdeye price history | Did a past launch rug? (ATH drawdown classification) |

All tools: compact JSON output, per-investigation cache, 3-concurrent semaphore, single retry on 429/5xx, and errors returned as data (`{"error": ...}`) so the agent routes around failures instead of crashing.

## Setup

```bash
git clone <this repo> && cd trail
python -m venv .venv && .venv\Scripts\activate   # (Windows; use source .venv/bin/activate elsewhere)
pip install -r requirements.txt
copy .env.example .env                            # then fill in keys
```

Required env vars (see `.env.example` for comments): `BTL_API_KEY`, `BTL_MODEL`, `HELIUS_API_KEY`, `BIRDEYE_API_KEY`, and `TELEGRAM_BOT_TOKEN` for the bot. Startup fails loudly with the exact list of anything missing.

### Run the CLI (fastest way to see it work)

```bash
python cli.py <any-solana-address>
python cli.py <address> --json          # include raw case-file JSON
python tools.py get_token_overview <mint>   # test any single tool standalone
```

### Run the Telegram bot

```bash
python bot.py
```

Then in Telegram: `/start`, then `/scan <address>` (or just paste an address).

## Example investigation (real, unedited)

This is an actual Trail run against a pump.fun token that was **minutes old** at scan
time. Between our first and second test runs, its liquidity went from $2,311 to $5.44 —
Trail documented the rug *while it was happening*.

```
🔎 Trail is on the case: Fj3r3Dv44T8HRA1LHTkiohxfJWx3rTYWDaAaWcRjpump

  🔍 Pulling token overview (Fj3r..pump)...
    → $Venom, liq $5
  🔍 Identifying the deployer (Fj3r..pump)...
  🔍 Analyzing top holders (Fj3r..pump)...
    → deployer J7mf..yanX
    → top10 own 100%, ⚠️ 2 fresh-wallet holder(s)
  🔍 Profiling wallet activity (J7mf..yanX)...
  🔍 Tracing wallet funding (49CA..UimX)...
    → first funded by J7mf..yanX with 0.0017 SOL
  🔍 Profiling wallet activity (BwWK..de6s)...
  🔍 Profiling wallet activity (49CA..UimX)...
    → wallet age 0.0d, 19 txs
  📋 Compiling the case file...

🔴 TRAIL CASE FILE — risk: CRITICAL | confidence: 85%
Verdict: This is an insider-coordinated token launch where the deployer
and a sniper bot jointly funded a fresh wallet that now holds 50% of
supply, with the sniper bot holding the other 50%, near-zero liquidity
($5.44), and an 85% price crash — highly likely a rug pull or
pump-and-dump that has already been executed.
Entity profile: insider

Evidence (excerpt):
1. 100% of supply is controlled by exactly 2 wallets: a fresh 0-day-old
   wallet (49CAdz...) holding 50.22% and a bot wallet (BwWK17...) holding
   49.78%.  [49CAdzPXLnsH648AnhRJRE7uDiaKxrvFEnL3fqyRUimX]
2. The fresh 50% holder was funded by both the deployer (J7mfert...) and
   the other holder in the same minute the token was created.
   [KsXDSSCnybp7LoJQWjy7Vhmc3HCcvvhutrr55TKmRbtv4XdMEuU8NkZs...]
3. Token has only $5.44 in liquidity, 3 holders, and -85% price change
   in 24h — holders cannot exit.

[7 runtime calls, 11 tool calls, mode=native_tools]
```

For contrast, the same agent scanned BONK and concluded 🟢 low risk / "mature blue-chip
meme coin" in 7 runtime calls — including routing around two Helius outages mid-run and
noting the data gaps in its case file.

## Project structure

```
trail/
  bot.py          # telegram handlers, throttled live message editing
  agent.py        # RuntimeClient (all BTL calls) + agent loop + limits + progress events
  tools.py        # helius/birdeye wrappers, tool schemas, cache, rate limiting
  prompts.py      # investigator system prompt w/ worked examples, verdict schema
  config.py       # env loading + loud validation
  cli.py          # terminal runner for the full loop
```

## Design decisions

- **Agent decoupled from Telegram** — `investigate()` emits progress via an `on_step` callback; CLI and bot are thin views over the same loop.
- **Defensive everywhere** — external APIs flake: every tool call returns errors as data, the verdict JSON gets a repair retry, and hitting the round/time budget still forces a (lower-confidence) verdict instead of failing.
- **Token-frugal tools** — results truncated to top-10 arrays with shortened addresses, so 8 rounds of context stay cheap.
