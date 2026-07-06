"""On-chain data tools: Helius + Birdeye wrappers.

Design rules (important for the agent loop):
- Every tool returns a compact, JSON-serializable dict. NEVER raises into the
  agent loop — failures come back as {"error": "..."} so the LLM can route around them.
- Results are truncated aggressively (top-10 arrays, shortened addresses in prose)
  to keep runtime token usage low.
- Per-investigation cache (keyed by tool+args) so the agent can't burn API credits
  by re-asking the same question.
- Global semaphore (3 concurrent) + single retry on 429/5xx.

Standalone testing:  python tools.py get_token_overview <mint>
"""

import asyncio
import json
import logging
import sys
import time
from typing import Any, Optional

import httpx

from config import Config

log = logging.getLogger("trail.tools")

HELIUS_RPC = "https://mainnet.helius-rpc.com/"
HELIUS_API = "https://api.helius.xyz"
BIRDEYE_API = "https://public-api.birdeye.so"

SOL_LAMPORTS = 1_000_000_000

# Global rate-limit guard across all in-flight investigations.
_SEMAPHORE = asyncio.Semaphore(3)

# Best-effort labels for well-known accounts so pools/CEX wallets aren't
# misread as suspicious individual holders.
KNOWN_ACCOUNTS = {
    # DEX / launchpad infrastructure
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1": "Raydium AMM authority (liquidity pool)",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM V4 program",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "pump.fun bonding curve program",
    "TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM": "pump.fun mint authority (platform infra)",
    "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg": "pump.fun fee account (platform infra)",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "pump.fun AMM program (platform infra)",
    "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN": "Meteora dynamic bonding curve (platform infra)",
    # CEX hot wallets (best-effort labels from public explorers — treat as
    # "very likely CEX", used to mark funders/holders as benign custody)
    "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9": "Binance hot wallet (CEX)",
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM": "Binance hot wallet (CEX)",
    "2ojv9BAiHUrvsm9gxDe7fJSzbNZSJcxZvf8dqmWGHG8S": "Binance hot wallet (CEX)",
    "H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS": "Coinbase hot wallet (CEX)",
    "GJRs4FwHtemZ5ZE9x3FNvJ8TgwitkBjVGhrKKrWV6mNx": "Coinbase hot wallet (CEX)",
    "AC5RDfQFmDS1deWZos921JfqscXdByf8BKHs5ACWjtW2": "Bybit hot wallet (CEX)",
    "5VCwKtCXgCJ6kit5FybXjvriW3xELsFDhYrPSqtJNmcD": "OKX hot wallet (CEX)",
    "ASTyfSima4LLAdDgoFGkgqoKowG1LZFDr9fAQrg7iaJZ": "MEXC hot wallet (CEX)",
    "FWznbcNXWQuHTawe9RxvQ2LdCENssh12dsznf4RiouN5": "Kraken hot wallet (CEX)",
    "u6PJ8DtQuPFnfmwHbGFULQ4u4EgjDiyYKjVEsynXq2w": "Gate.io hot wallet (CEX)",
}

# Platform accounts that show up as the DAS "creator" of tokens they didn't
# really create (e.g. pump.fun mints every token through its own authority).
# Never report these as the human deployer — dig for the real one instead.
INFRA_CREATORS = {
    "TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
    "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg",
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
    "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN",
}


def short(addr: Optional[str]) -> str:
    """Shorten an address for prose fields: 7xKpq2 -> 7xKp..q2Fh"""
    if not addr:
        return "?"
    return addr if len(addr) <= 12 else f"{addr[:4]}..{addr[-4:]}"


def ts_to_date(ts: Optional[int]) -> Optional[str]:
    if not ts:
        return None
    from datetime import datetime, timezone

    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _days_ago(ts: Optional[int]) -> Optional[float]:
    if not ts:
        return None
    return round((time.time() - int(ts)) / 86400, 1)


class ToolBox:
    """One instance per investigation: holds the HTTP client and the result cache."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.cache: dict[str, dict] = {}
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=8.0))

    async def close(self) -> None:
        await self.client.aclose()

    # ------------------------------------------------------------------ #
    # low-level HTTP helpers                                             #
    # ------------------------------------------------------------------ #

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict] = None,
        json_body: Optional[Any] = None,
        headers: Optional[dict] = None,
        provider: str = "api",
    ) -> tuple[Optional[Any], Optional[str]]:
        """Returns (data, error). Retries once on 429/5xx/timeouts. Never raises."""
        last_err = "unknown error"
        for attempt in (1, 2):
            try:
                async with _SEMAPHORE:
                    resp = await self.client.request(
                        method, url, params=params, json=json_body, headers=headers
                    )
                if resp.status_code == 429:
                    last_err = f"{provider} rate limited"
                    log.warning("429 from %s, backing off (attempt %d)", provider, attempt)
                    await asyncio.sleep(2.0 * attempt)
                    continue
                if resp.status_code >= 500:
                    last_err = f"{provider} server error {resp.status_code}"
                    log.warning("%s from %s (attempt %d)", resp.status_code, provider, attempt)
                    await asyncio.sleep(1.5 * attempt)
                    continue
                if resp.status_code >= 400:
                    body = resp.text[:200]
                    log.warning("%s %s from %s: %s", method, resp.status_code, provider, body)
                    return None, f"{provider} error {resp.status_code}"
                return resp.json(), None
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_err = f"{provider} network error: {type(e).__name__}"
                log.warning("network error calling %s: %s (attempt %d)", provider, e, attempt)
                await asyncio.sleep(1.0 * attempt)
            except json.JSONDecodeError:
                return None, f"{provider} returned non-JSON response"
        return None, last_err

    async def _rpc(self, method: str, params: Any) -> tuple[Optional[Any], Optional[str]]:
        """Helius Solana RPC call. Returns (result, error).

        Note: classic RPC methods take params as a LIST; Helius DAS methods
        (getAsset, getAssetsByCreator, ...) take params as an OBJECT.
        """
        data, err = await self._request(
            "POST",
            HELIUS_RPC,
            params={"api-key": self.cfg.helius_api_key},
            json_body={"jsonrpc": "2.0", "id": "trail", "method": method, "params": params},
            provider="helius",
        )
        if err:
            return None, err
        if isinstance(data, dict) and data.get("error"):
            msg = data["error"].get("message", "rpc error")
            return None, f"helius rpc: {str(msg)[:120]}"
        return (data or {}).get("result"), None

    async def _birdeye(self, path: str, params: dict) -> tuple[Optional[dict], Optional[str]]:
        headers = {
            "X-API-KEY": self.cfg.birdeye_api_key,
            "x-chain": "solana",
            "accept": "application/json",
        }
        data, err = await self._request(
            "GET", f"{BIRDEYE_API}{path}", params=params, headers=headers, provider="birdeye"
        )
        if err:
            return None, err
        if not isinstance(data, dict) or not data.get("success", True):
            return None, f"birdeye: {str((data or {}).get('message', 'request failed'))[:120]}"
        return data.get("data"), None

    async def _parsed_txs(self, signatures: list[str]) -> tuple[Optional[list], Optional[str]]:
        """Helius enhanced (parsed) transactions endpoint. Max 100 sigs per call."""
        if not signatures:
            return [], None
        data, err = await self._request(
            "POST",
            f"{HELIUS_API}/v0/transactions",
            params={"api-key": self.cfg.helius_api_key},
            json_body={"transactions": signatures[:100]},
            provider="helius",
        )
        if err:
            return None, err
        return data if isinstance(data, list) else [], None

    async def _signatures(
        self, address: str, max_pages: int = 5
    ) -> tuple[list[dict], bool, Optional[str]]:
        """Page getSignaturesForAddress newest-first.

        Returns (signatures, exhausted, error). exhausted=True means we reached the
        wallet's very first transaction (so sigs[-1] is the genesis tx).
        """
        sigs: list[dict] = []
        before = None
        for _ in range(max_pages):
            params: list = [address, {"limit": 1000}]
            if before:
                params[1]["before"] = before
            result, err = await self._rpc("getSignaturesForAddress", params)
            if err:
                return sigs, False, err
            batch = result or []
            sigs.extend(batch)
            if len(batch) < 1000:
                return sigs, True, None
            before = batch[-1]["signature"]
        return sigs, False, None

    # ------------------------------------------------------------------ #
    # address type detection (used by bot/cli before the agent starts)   #
    # ------------------------------------------------------------------ #

    async def detect_address_type(self, address: str) -> tuple[str, str]:
        """Returns (type, note) where type is 'token' or 'wallet'."""
        result, err = await self._rpc("getAccountInfo", [address, {"encoding": "jsonParsed"}])
        if err:
            return "wallet", f"could not verify on-chain ({err}); assuming wallet"
        value = (result or {}).get("value")
        if value is None:
            return "wallet", "account not found on-chain (never funded, or wrong network)"
        data = value.get("data")
        if isinstance(data, dict):
            parsed = data.get("parsed", {})
            if parsed.get("type") == "mint":
                return "token", "SPL token mint"
            if parsed.get("type") == "account":
                owner = parsed.get("info", {}).get("owner", "")
                return "wallet", f"this is a token ACCOUNT; its owner wallet is {owner}"
        if value.get("owner") == "11111111111111111111111111111111":
            return "wallet", "system-owned wallet"
        return "wallet", f"program-owned account (owner {short(value.get('owner'))})"

    # ------------------------------------------------------------------ #
    # cached dispatch — the single entry point used by the agent loop    #
    # ------------------------------------------------------------------ #

    async def execute(self, name: str, args: dict) -> dict:
        key = f"{name}:{json.dumps(args, sort_keys=True)}"
        if key in self.cache:
            log.info("cache hit: %s", key)
            return self.cache[key]

        fn = getattr(self, name, None)
        if fn is None or name.startswith("_") or name not in TOOL_NAMES:
            return {"error": f"unknown tool '{name}'. available: {', '.join(TOOL_NAMES)}"}
        try:
            result = await fn(**args)
        except TypeError as e:
            return {"error": f"bad arguments for {name}: {e}"}
        except Exception as e:  # absolute last resort — never leak into the loop
            log.exception("tool %s crashed", name)
            result = {"error": f"{name} failed internally: {type(e).__name__}"}

        # Don't cache errors — the agent may retry a tool after a transient
        # failure, and a poisoned cache would make the failure permanent.
        if "error" not in result:
            self.cache[key] = result
        log.info("tool %s(%s) -> %s", name, args, json.dumps(result)[:300])
        return result

    # ------------------------------------------------------------------ #
    # the 7 tools                                                        #
    # ------------------------------------------------------------------ #

    async def get_token_overview(self, mint: str) -> dict:
        overview_task = self._birdeye("/defi/token_overview", {"address": mint})
        security_task = self._birdeye("/defi/token_security", {"address": mint})
        (ov, ov_err), (sec, sec_err) = await asyncio.gather(overview_task, security_task)

        if ov_err and sec_err:
            return {"error": f"token overview unavailable ({ov_err}; {sec_err})"}

        out: dict[str, Any] = {"mint": mint}
        if ov:
            out.update(
                {
                    "name": ov.get("name"),
                    "symbol": ov.get("symbol"),
                    "price_usd": ov.get("price"),
                    "liquidity_usd": _round(ov.get("liquidity")),
                    "market_cap_usd": _round(ov.get("marketCap") or ov.get("mc") or ov.get("realMc")),
                    "holder_count": ov.get("holder"),
                    "volume_24h_usd": _round(ov.get("v24hUSD")),
                    "price_change_24h_pct": _round(
                        ov.get("priceChange24hPercent") or ov.get("priceChange24h")
                    ),
                }
            )
            # social presence: legit projects link a site/socials in metadata;
            # anonymous zero-presence NEW tokens are a meaningful risk signal
            ext = ov.get("extensions") or {}
            socials = [
                k for k in ("website", "twitter", "telegram", "discord", "medium", "github")
                if ext.get(k)
            ]
            out["social_presence"] = (
                socials
                if socials
                else "NONE — no website/socials in token metadata (risk signal for new tokens)"
            )
        else:
            out["overview_note"] = f"birdeye overview failed: {ov_err}"

        if sec:
            top10 = sec.get("top10HolderPercent")
            if isinstance(top10, (int, float)) and top10 <= 1.0:
                top10 = top10 * 100  # birdeye returns a fraction
            creation_ts = sec.get("creationTime")
            out.update(
                {
                    "creator": sec.get("creatorAddress"),
                    "created_at": ts_to_date(creation_ts),
                    "token_age_days": _days_ago(creation_ts),
                    "top10_holder_pct": _round(top10),
                    "freeze_authority": sec.get("freezeAuthority"),
                    "mutable_metadata": sec.get("mutableMetadata"),
                }
            )
        else:
            out["security_note"] = f"birdeye security data failed: {sec_err}"

        return _clean(out)

    async def get_deployer(self, mint: str) -> dict:
        # Primary: Helius DAS getAsset (creators / update authority)
        das_deployer = None
        das_meta = {}
        result, err = await self._rpc("getAsset", {"id": mint})
        if result and not err:
            creators = result.get("creators") or []
            authorities = result.get("authorities") or []
            das_meta = (result.get("content") or {}).get("metadata") or {}
            if creators:
                verified = [c for c in creators if c.get("verified")]
                das_deployer = (verified or creators)[0].get("address")
            elif authorities:
                das_deployer = authorities[0].get("address")

        # Launchpads (pump.fun etc.) mint through their own authority, so the
        # DAS creator is platform infra, not the human. Birdeye's creation info
        # exposes both: "owner" (often the platform) and "creator" (the wallet
        # that actually initiated the creation tx) — prefer the creator.
        creation, c_err = await self._birdeye("/defi/token_creation_info", {"address": mint})
        creation_owner = None
        for candidate in ((creation or {}).get("creator"), (creation or {}).get("owner")):
            if candidate and candidate not in INFRA_CREATORS:
                creation_owner = candidate
                break

        if das_deployer and das_deployer not in INFRA_CREATORS:
            return _clean(
                {
                    "mint": mint,
                    "deployer": das_deployer,
                    "source": "helius_das_creator",
                    "token_name": das_meta.get("name"),
                    "token_symbol": das_meta.get("symbol"),
                    "note": "creator/update-authority from on-chain metadata",
                }
            )

        if creation_owner and creation_owner not in INFRA_CREATORS:
            out = {
                "mint": mint,
                "deployer": creation_owner,
                "source": "birdeye_creation_tx",
                "creation_tx": (creation or {}).get("txHash"),
                "created_at": (creation or {}).get("blockHumanTime")
                or ts_to_date((creation or {}).get("blockUnixTime")),
            }
            if das_deployer:
                out["note"] = (
                    f"metadata creator is {KNOWN_ACCOUNTS.get(das_deployer, 'platform infra')} — "
                    "the real human deployer is the creation-tx fee payer reported here"
                )
            return _clean(out)

        if das_deployer:  # infra creator and no better answer
            return _clean(
                {
                    "mint": mint,
                    "deployer": das_deployer,
                    "source": "helius_das_creator",
                    "deployer_label": KNOWN_ACCOUNTS.get(das_deployer),
                    "note": "WARNING: this is launchpad platform infrastructure, not the human "
                    "deployer — the real creator could not be resolved",
                }
            )

        # Fallback 2: Birdeye token security creatorAddress
        sec, s_err = await self._birdeye("/defi/token_security", {"address": mint})
        if sec and sec.get("creatorAddress"):
            return _clean(
                {
                    "mint": mint,
                    "deployer": sec.get("creatorAddress"),
                    "source": "birdeye_token_security",
                    "created_at": ts_to_date(sec.get("creationTime")),
                }
            )

        return {"error": f"could not resolve deployer ({err or c_err or s_err or 'no data'})"}

    async def get_wallet_funding(self, address: str) -> dict:
        if address in KNOWN_ACCOUNTS:
            return {
                "address": address,
                "label": KNOWN_ACCOUNTS[address],
                "note": "this is a well-known platform/CEX account — tracing its funding is "
                "not informative; investigate the other wallets in your trail instead",
            }
        sigs, exhausted, err = await self._signatures(address, max_pages=5)
        if err and not sigs:
            return {"error": f"could not fetch tx history ({err})"}
        if not sigs:
            return {"address": address, "funding": [], "note": "no transactions found for this address"}

        note = None
        if not exhausted:
            note = (
                "wallet has 5000+ transactions; true earliest history is beyond our page cap — "
                "the 'earliest' transfers below are the oldest we could reach"
            )

        # Oldest ~30 signatures (successful only), oldest first
        oldest = [s for s in reversed(sigs) if s.get("err") is None][:30]
        parsed, p_err = await self._parsed_txs([s["signature"] for s in oldest])
        if p_err:
            return {"error": f"could not parse earliest transactions ({p_err})"}

        parsed.sort(key=lambda t: t.get("timestamp") or 0)
        transfers = []
        for tx in parsed:
            for nt in tx.get("nativeTransfers") or []:
                if nt.get("toUserAccount") == address and (nt.get("amount") or 0) >= 1_000_000:
                    src = nt.get("fromUserAccount")
                    if not src or src == address:
                        continue
                    transfers.append(
                        {
                            "from": src,
                            "from_label": KNOWN_ACCOUNTS.get(src),
                            "sol": round(nt["amount"] / SOL_LAMPORTS, 4),
                            "time": ts_to_date(tx.get("timestamp")),
                            "tx": tx.get("signature"),
                        }
                    )
            if len(transfers) >= 10:
                break

        return _clean(
            {
                "address": address,
                "wallet_first_seen": ts_to_date(oldest[0].get("blockTime")) if oldest else None,
                "earliest_incoming_sol": [_clean(t) for t in transfers[:10]],
                "note": note
                or (
                    "these are the wallet's EARLIEST incoming SOL transfers — "
                    "the first sender is very likely who funded/created this wallet"
                ),
            }
        )

    async def get_wallet_tokens_deployed(self, address: str) -> dict:
        if address in INFRA_CREATORS:
            return {
                "creator": address,
                "label": KNOWN_ACCOUNTS.get(address),
                "note": "this is launchpad platform infrastructure that mints thousands of "
                "tokens for users — its creation count says nothing about any one deployer; "
                "use get_deployer on the token to find the real human creator",
            }
        result, err = await self._rpc(
            "getAssetsByCreator",
            {"creatorAddress": address, "onlyVerified": False, "page": 1, "limit": 25},
        )
        if err:
            return {"error": f"could not fetch created tokens ({err})"}

        items = (result or {}).get("items") or []
        tokens = []
        for it in items[:10]:
            meta = (it.get("content") or {}).get("metadata") or {}
            tokens.append(
                {
                    "mint": it.get("id"),
                    "name": meta.get("name"),
                    "symbol": meta.get("symbol"),
                }
            )
        total = (result or {}).get("total", len(items))
        return _clean(
            {
                "creator": address,
                "total_tokens_created": total,
                "tokens": tokens,
                "note": "use check_token_outcome on these mints to see how the launches ended"
                if tokens
                else "no tokens created by this wallet (via metadata creator field)",
            }
        )

    async def get_wallet_activity(self, address: str) -> dict:
        if address in KNOWN_ACCOUNTS:
            return {
                "address": address,
                "label": KNOWN_ACCOUNTS[address],
                "note": "well-known platform/CEX account — its raw activity stats are not "
                "meaningful for this investigation",
            }
        sigs, exhausted, err = await self._signatures(address, max_pages=5)
        if err and not sigs:
            return {"error": f"could not fetch activity ({err})"}
        if not sigs:
            return {"address": address, "note": "no on-chain activity found"}

        first_ts = sigs[-1].get("blockTime")
        last_ts = sigs[0].get("blockTime")
        failed = sum(1 for s in sigs if s.get("err") is not None)
        age_days = _days_ago(first_ts)
        tx_count = len(sigs)

        out: dict[str, Any] = {
            "address": address,
            "label": KNOWN_ACCOUNTS.get(address),
            "last_tx": ts_to_date(last_ts),
            "failed_tx_pct": round(100 * failed / max(tx_count, 1), 1),
        }
        if exhausted:
            out["tx_count"] = tx_count
            out["first_tx"] = ts_to_date(first_ts)
            out["wallet_age_days"] = age_days
        else:
            out["tx_count"] = f"{tx_count}+ (only most recent {tx_count} visible)"
            out["oldest_reachable_tx"] = ts_to_date(first_ts)
            out["wallet_age_days"] = f"UNKNOWN — at least {age_days}, but history is capped; do NOT treat this wallet as fresh"
        if exhausted and age_days and age_days > 0:
            out["tx_per_day"] = round(tx_count / max(age_days, 0.05), 1)
        if failed / max(tx_count, 1) > 0.4:
            out["pattern_flag"] = "very high failed-tx ratio — typical of sniper/spam bots"

        # Top counterparties from the 20 most recent successful txs (cheap sample)
        recent = [s["signature"] for s in sigs if s.get("err") is None][:20]
        parsed, p_err = await self._parsed_txs(recent)
        if parsed and not p_err:
            counts: dict[str, int] = {}
            for tx in parsed:
                for nt in (tx.get("nativeTransfers") or []) + (tx.get("tokenTransfers") or []):
                    for side in ("fromUserAccount", "toUserAccount"):
                        who = nt.get(side)
                        if who and who != address:
                            counts[who] = counts.get(who, 0) + 1
            top = sorted(counts.items(), key=lambda kv: -kv[1])[:5]
            out["top_recent_counterparties"] = [
                {"address": a, "label": KNOWN_ACCOUNTS.get(a), "interactions": n} for a, n in top
            ]
        return _clean(out)

    async def get_holder_overlap(self, mint: str) -> dict:
        largest, err = await self._rpc("getTokenLargestAccounts", [mint])
        if err:
            return {"error": f"could not fetch holders ({err})"}
        accounts = (largest or {}).get("value") or []
        if not accounts:
            return {"error": "no holder accounts found for this mint"}

        supply_res, s_err = await self._rpc("getTokenSupply", [mint])
        total_supply = ((supply_res or {}).get("value") or {}).get("uiAmount") if not s_err else None

        # Resolve token-account -> owner wallet
        token_accounts = [a["address"] for a in accounts[:20]]
        multi, m_err = await self._rpc(
            "getMultipleAccounts", [token_accounts, {"encoding": "jsonParsed"}]
        )
        owners: list[Optional[str]] = []
        if multi and not m_err:
            for v in (multi.get("value") or []):
                try:
                    owners.append(v["data"]["parsed"]["info"]["owner"])
                except (TypeError, KeyError):
                    owners.append(None)
        owners += [None] * (len(token_accounts) - len(owners))

        holders = []
        for acc, owner in zip(accounts[:20], owners):
            amount = acc.get("uiAmount") or 0
            pct = round(100 * amount / total_supply, 2) if total_supply else None
            holders.append({"owner": owner, "pct_of_supply": pct, "amount": amount})

        top1 = holders[0]["pct_of_supply"] if holders else None
        top5 = round(sum(h["pct_of_supply"] or 0 for h in holders[:5]), 2) if total_supply else None
        top10 = round(sum(h["pct_of_supply"] or 0 for h in holders[:10]), 2) if total_supply else None

        # Freshness check on top 5 distinct owner wallets (skip known pools/programs)
        fresh_flags = []
        seen = set()
        check = []
        for h in holders:
            o = h.get("owner")
            if o and o not in seen and o not in KNOWN_ACCOUNTS:
                seen.add(o)
                check.append(o)
            if len(check) >= 5:
                break

        async def _age(owner: str) -> Optional[dict]:
            osigs, oexh, oerr = await self._signatures(owner, max_pages=1)
            if oerr or not osigs:
                return None
            oldest_reachable = _days_ago(osigs[-1].get("blockTime"))
            if not oexh:
                out = {"owner": owner, "wallet_age_days": "unknown (1000+ txs)"}
                if oldest_reachable is not None and oldest_reachable < 7:
                    # 1000+ txs and even the 1000th-most-recent is <7d old:
                    # either a brand-new hyperactive bot or an extremely busy service
                    out["hyperactive"] = True
                    out["note"] = (
                        f"1000+ txs but oldest reachable tx is only {oldest_reachable}d old — "
                        "bot-grade activity burst; treat as suspicious as a fresh wallet"
                    )
                return out
            days = oldest_reachable
            return {"owner": owner, "wallet_age_days": days, "fresh": bool(days is not None and days < 7)}

        ages = await asyncio.gather(*(_age(o) for o in check))
        for a in ages:
            if a:
                fresh_flags.append(_clean(a))

        labeled = [
            {"owner": h["owner"], "label": KNOWN_ACCOUNTS[h["owner"]]}
            for h in holders
            if h.get("owner") in KNOWN_ACCOUNTS
        ]
        n_fresh = sum(1 for a in fresh_flags if a.get("fresh") or a.get("hyperactive"))

        return _clean(
            {
                "mint": mint,
                "top_holders": [_clean(h) for h in holders[:10]],
                "top1_pct": top1,
                "top5_pct": top5,
                "top10_pct": top10,
                "known_accounts_in_top20": labeled or None,
                "top_owner_wallet_ages": fresh_flags,
                "fresh_wallet_holders": n_fresh,
                "note": "known pools/CEX in the top holders are usually benign; "
                "FRESH wallets holding large % are a strong insider signal",
            }
        )

    async def check_token_outcome(self, mint: str) -> dict:
        # Need launch time first
        launch_ts = None
        creation, _ = await self._birdeye("/defi/token_creation_info", {"address": mint})
        if creation:
            launch_ts = creation.get("blockUnixTime")
        if not launch_ts:
            sec, _ = await self._birdeye("/defi/token_security", {"address": mint})
            if sec:
                launch_ts = sec.get("creationTime")
        now = int(time.time())
        if not launch_ts:
            launch_ts = now - 30 * 86400  # unknown launch: look at last 30 days

        launch_ts = int(launch_ts)
        age_days = max((now - launch_ts) / 86400, 0.01)
        if age_days <= 2:
            bucket = "15m"
        elif age_days <= 14:
            bucket = "2H"
        elif age_days <= 120:
            bucket = "12H"
        else:
            bucket = "1D"

        hist, h_err = await self._birdeye(
            "/defi/history_price",
            {
                "address": mint,
                "address_type": "token",
                "type": bucket,
                "time_from": launch_ts,
                "time_to": now,
            },
        )
        items = (hist or {}).get("items") or []
        if h_err or not items:
            return {"error": f"no price history available ({h_err or 'empty series'})"}

        prices = [(it.get("unixTime"), it.get("value")) for it in items if it.get("value")]
        if not prices:
            return {"error": "price series was empty"}

        ath_ts, ath = max(prices, key=lambda p: p[1])
        current = prices[-1][1]
        first = prices[0][1]
        drawdown = round(100 * (1 - current / ath), 1) if ath else None
        ath_days_after_launch = round((ath_ts - launch_ts) / 86400, 1) if ath_ts else None

        if drawdown is not None and drawdown >= 90:
            if ath_days_after_launch is not None and ath_days_after_launch <= 7:
                classification = "rugged"
                explain = "pumped and lost >90% shortly after launch — classic rug/abandon pattern"
            else:
                classification = "collapsed"
                explain = ">90% below ATH — dead or rugged"
        elif drawdown is not None and drawdown >= 60:
            classification = "heavy_drawdown"
            explain = "60-90% below ATH"
        else:
            classification = "alive"
            explain = "price has held up relative to ATH"

        return _clean(
            {
                "mint": mint,
                "launched": ts_to_date(launch_ts),
                "token_age_days": round(age_days, 1),
                "first_price": first,
                "ath_price": ath,
                "ath_reached_days_after_launch": ath_days_after_launch,
                "current_price": current,
                "drawdown_from_ath_pct": drawdown,
                "classification": classification,
                "explain": explain,
            }
        )


# ---------------------------------------------------------------------- #
# helpers                                                                #
# ---------------------------------------------------------------------- #


def _round(v: Any) -> Any:
    if isinstance(v, float):
        return round(v, 2) if abs(v) >= 1 else round(v, 8)
    return v


def _clean(d: dict) -> dict:
    """Drop None values to keep payloads compact."""
    return {k: v for k, v in d.items() if v is not None}


# ---------------------------------------------------------------------- #
# OpenAI tool schemas — these descriptions are what the agent reasons    #
# over. Written like docs for a junior analyst.                          #
# ---------------------------------------------------------------------- #

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_token_overview",
            "description": (
                "Get a token's vital signs from market data: name/symbol, price, liquidity, "
                "market cap, holder count, 24h volume, creation date/age, creator address, "
                "top-10 holder concentration %, social presence (website/twitter/telegram — "
                "a NEW token with zero socials is a risk signal), and whether metadata is "
                "mutable or a freeze authority exists. START HERE for any token — it tells "
                "you if the token is fresh, illiquid, anonymous, or concentrated, which "
                "decides where to dig next."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mint": {"type": "string", "description": "The token mint address (base58)."}
                },
                "required": ["mint"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_deployer",
            "description": (
                "Resolve who CREATED a token: returns the deployer/creator wallet address "
                "(plus creation tx and time when available). The deployer is the single most "
                "important lead on a token — once you have it, investigate the deployer wallet "
                "itself (funding, other tokens created, age)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mint": {"type": "string", "description": "The token mint address (base58)."}
                },
                "required": ["mint"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_wallet_funding",
            "description": (
                "Trace where a wallet's money CAME FROM: returns the wallet's earliest incoming "
                "SOL transfers (source address, amount, time, tx signature). This is the "
                "lead-generator: the first funder usually created/controls the wallet. If the "
                "funder is itself a fresh wallet, trace ITS funding too — serial operators hide "
                "behind chains of throwaway wallets. A CEX-labeled funder is usually benign."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "address": {"type": "string", "description": "The wallet address (base58)."}
                },
                "required": ["address"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_wallet_tokens_deployed",
            "description": (
                "List tokens a wallet has CREATED (as on-chain metadata creator). A wallet that "
                "deployed many tokens is a serial deployer — check the outcomes of its previous "
                "launches with check_token_outcome: if most rugged, you've found a serial rug "
                "operator. Returns up to 10 tokens plus a total count."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "address": {"type": "string", "description": "The wallet address (base58)."}
                },
                "required": ["address"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_wallet_activity",
            "description": (
                "Profile a wallet's behavior: age (first tx date), total tx count, tx/day rate, "
                "failed-tx ratio (high = bot), and top recent counterparties. Use this to test "
                "hypotheses: a 3-day-old wallet deploying tokens is suspicious; a 2-year-old "
                "wallet with steady activity looks like a real user; 5000+ tx/day with high "
                "failure rate is a bot."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "address": {"type": "string", "description": "The wallet address (base58)."}
                },
                "required": ["address"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_holder_overlap",
            "description": (
                "Analyze a token's top 20 holders: each holder's owner wallet and % of supply, "
                "top-1/5/10 concentration, which holders are known pools/CEX (benign), and the "
                "AGE of the top holder wallets — multiple FRESH wallets (<7 days) holding large "
                "chunks means insiders pre-loaded the supply. Compare holder addresses against "
                "the deployer and its funding sources to find linked clusters."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mint": {"type": "string", "description": "The token mint address (base58)."}
                },
                "required": ["mint"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_token_outcome",
            "description": (
                "Check how a token launch ENDED: launch date, ATH, current drawdown from ATH, "
                "and a classification — 'rugged' (>90% drop shortly after launch), 'collapsed', "
                "'heavy_drawdown', or 'alive'. Use on tokens previously deployed by a suspect "
                "wallet to establish a track record: 4 of 5 past launches rugged = serial rug "
                "operation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mint": {"type": "string", "description": "The token mint address (base58)."}
                },
                "required": ["mint"],
            },
        },
    },
]

TOOL_NAMES = [t["function"]["name"] for t in TOOL_SCHEMAS]

# Human phrasing for the Telegram live view
STEP_PHRASES = {
    "get_token_overview": "Pulling token overview",
    "get_deployer": "Identifying the deployer",
    "get_wallet_funding": "Tracing wallet funding",
    "get_wallet_tokens_deployed": "Checking tokens deployed by wallet",
    "get_wallet_activity": "Profiling wallet activity",
    "get_holder_overlap": "Analyzing top holders",
    "check_token_outcome": "Checking token outcome",
}


def summarize_result(tool: str, args: dict, result: dict) -> str:
    """One compact human line describing what a tool call found (for the live view)."""
    if "error" in result:
        return f"⚠️ {result['error']}"
    try:
        if tool == "get_token_overview":
            bits = []
            if result.get("symbol"):
                bits.append(f"${result['symbol']}")
            age = result.get("token_age_days")
            if age is not None:
                bits.append(f"{age}d old")
            if result.get("liquidity_usd") is not None:
                bits.append(f"liq ${_fmt_num(result['liquidity_usd'])}")
            if result.get("top10_holder_pct") is not None:
                bits.append(f"top10 hold {round(result['top10_holder_pct'])}%")
            if isinstance(result.get("social_presence"), str):  # the NONE case
                bits.append("⚠️ no socials")
            return ", ".join(bits) or "overview retrieved"
        if tool == "get_deployer":
            return f"deployer {short(result.get('deployer'))}"
        if tool == "get_wallet_funding":
            transfers = result.get("earliest_incoming_sol") or []
            if not transfers:
                return "no incoming SOL transfers found"
            f0 = transfers[0]
            label = f" ({f0['from_label']})" if f0.get("from_label") else ""
            return f"first funded by {short(f0.get('from'))}{label} with {f0.get('sol')} SOL"
        if tool == "get_wallet_tokens_deployed":
            n = result.get("total_tokens_created", 0)
            return f"{n} token(s) created by this wallet"
        if tool == "get_wallet_activity":
            if result.get("label"):
                return f"known account: {result['label']}"
            age = result.get("wallet_age_days")
            age_str = f"{age}d" if isinstance(age, (int, float)) else "unknown (busy wallet)"
            txs = result.get("tx_count")
            txs_str = txs if isinstance(txs, int) else "5000+"
            flag = " ⚠️ bot-like failure rate" if result.get("pattern_flag") else ""
            return f"wallet age {age_str}, {txs_str} txs{flag}"
        if tool == "get_holder_overlap":
            bits = []
            if result.get("top10_pct") is not None:
                bits.append(f"top10 own {round(result['top10_pct'])}%")
            fresh = result.get("fresh_wallet_holders")
            if fresh:
                bits.append(f"⚠️ {fresh} fresh-wallet holder(s)")
            return ", ".join(bits) or "holder map retrieved"
        if tool == "check_token_outcome":
            cls = result.get("classification", "?")
            dd = result.get("drawdown_from_ath_pct")
            mark = "⚠️ " if cls in ("rugged", "collapsed") else ""
            return f"{mark}{cls}" + (f" ({dd}% below ATH)" if dd is not None else "")
    except Exception:
        pass
    return "done"


def _fmt_num(n: Any) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return str(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{n:.0f}"


# ---------------------------------------------------------------------- #
# standalone tool tester:  python tools.py get_token_overview <mint>     #
# ---------------------------------------------------------------------- #

if __name__ == "__main__":
    from config import load_config, setup_logging

    setup_logging()
    if len(sys.argv) < 3:
        print(f"usage: python tools.py <tool> <address>\ntools: {', '.join(TOOL_NAMES)}")
        sys.exit(1)

    tool_name, addr = sys.argv[1], sys.argv[2]
    arg_key = "mint" if "mint" in json.dumps(TOOL_SCHEMAS) and tool_name in (
        "get_token_overview",
        "get_deployer",
        "get_holder_overlap",
        "check_token_outcome",
    ) else "address"

    async def _main():
        cfg = load_config()
        box = ToolBox(cfg)
        try:
            if tool_name == "detect":
                print(await box.detect_address_type(addr))
                return
            result = await box.execute(tool_name, {arg_key: addr})
            print(json.dumps(result, indent=2))
            print("\nlive-view summary:", summarize_result(tool_name, {arg_key: addr}, result))
        finally:
            await box.close()

    asyncio.run(_main())
