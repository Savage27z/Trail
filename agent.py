"""RuntimeClient (all BTL runtime interaction) + the autonomous investigation loop.

Fully decoupled from Telegram: progress is reported through an `on_step` callback
(sync or async) with signature on_step(step_description, result_summary) — either
argument may be None (description-only when a step starts, summary-only when done).
"""

import asyncio
import inspect
import json
import logging
import re
import time
from typing import Any, Callable, Optional

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
)

import prompts
from config import Config
from tools import STEP_PHRASES, TOOL_SCHEMAS, ToolBox, short, summarize_result

log = logging.getLogger("trail.agent")

RISK_LEVELS = {"low", "medium", "high", "critical"}
ENTITY_PROFILES = {
    "serial_deployer", "insider", "bot", "normal_trader", "cex_linked", "fund", "unknown",
}


class RuntimeClientError(Exception):
    """Raised when the BTL runtime cannot serve a request after retries."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


def _trunc(s: Any, n: int = 600) -> str:
    s = str(s)
    return s if len(s) <= n else s[:n] + f"...[+{len(s) - n} chars]"


class RuntimeClient:
    """The ONLY place that talks to the BTL runtime. Isolated so compat quirks
    are fixed here and nowhere else."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.model = cfg.btl_model
        self.calls_made = 0
        # BTL billing-transparency headers (x-btl-customer-charge / x-btl-saved),
        # accumulated across the investigation so the case file can report cost.
        self.total_charge_usd = 0.0
        self.total_saved_usd = 0.0
        self.saw_cost_headers = False
        self._client = AsyncOpenAI(
            base_url=cfg.btl_base_url,
            api_key=cfg.btl_api_key,
            timeout=60.0,
            max_retries=0,  # we do our own retries so we can log them
        )

    def _track_cost(self, headers) -> None:
        def _f(name: str) -> Optional[float]:
            try:
                raw = headers.get(name)
                return float(raw) if raw not in (None, "") else None
            except (TypeError, ValueError):
                return None

        charge, saved = _f("x-btl-customer-charge"), _f("x-btl-saved")
        if charge is not None:
            self.total_charge_usd += charge
            self.saw_cost_headers = True
        if saved is not None:
            self.total_saved_usd += saved
            self.saw_cost_headers = True

    async def chat(self, messages: list[dict], tools: Optional[list[dict]] = None):
        """One chat-completions call with 2 retries on 5xx/timeout. Returns the message object."""
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        last_msg = messages[-1] if messages else {}
        log.info(
            "BTL >> call #%d model=%s msgs=%d tools=%s last_role=%s last_content=%s",
            self.calls_made + 1,
            self.model,
            len(messages),
            bool(tools),
            last_msg.get("role"),
            _trunc(last_msg.get("content"), 300),
        )

        delay = 1.5
        last_err: Optional[Exception] = None
        for attempt in range(3):  # 1 try + 2 retries
            try:
                t0 = time.monotonic()
                raw = await self._client.chat.completions.with_raw_response.create(**kwargs)
                self._track_cost(raw.headers)
                resp = raw.parse()
                self.calls_made += 1
                msg = resp.choices[0].message
                n_tools = len(msg.tool_calls or []) if getattr(msg, "tool_calls", None) else 0
                log.info(
                    "BTL << %.1fs tool_calls=%d content=%s",
                    time.monotonic() - t0,
                    n_tools,
                    _trunc(msg.content, 400),
                )
                return msg
            except (APIConnectionError, APITimeoutError) as e:
                last_err = e
                log.warning("BTL runtime unreachable (attempt %d/3): %s", attempt + 1, e)
            except APIStatusError as e:
                if e.status_code >= 500 or e.status_code == 429:
                    last_err = e
                    log.warning("BTL runtime %s (attempt %d/3)", e.status_code, attempt + 1)
                else:
                    # 4xx = our request is wrong (possibly a tools-param compat issue) — don't retry
                    body = _trunc(getattr(e, "message", "") or str(e), 300)
                    log.error("BTL runtime rejected request (%s): %s", e.status_code, body)
                    raise RuntimeClientError(
                        f"runtime rejected request ({e.status_code}): {body}",
                        status_code=e.status_code,
                    ) from e
            if attempt < 2:
                await asyncio.sleep(delay)
                delay *= 2
        raise RuntimeClientError(f"runtime unavailable after 3 attempts: {last_err}")


# --------------------------------------------------------------------------- #
# JSON parsing (defensive)                                                     #
# --------------------------------------------------------------------------- #


def parse_json_loose(text: Optional[str]) -> Optional[dict]:
    """Parse JSON out of model output: strips fences, trims to outermost braces."""
    if not text:
        return None
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        return None
    candidate = cleaned[start : end + 1]
    try:
        obj = json.loads(candidate)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        # common model slip: trailing commas
        try:
            obj = json.loads(re.sub(r",\s*([}\]])", r"\1", candidate))
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None


def _cap(v: Any, n: int) -> str:
    s = str(v)
    return s if len(s) <= n else s[: n - 1] + "…"


def _validate_verdict(v: dict, address: str) -> dict:
    out = dict(v)
    # length caps keep the Telegram case file renderable within the 4096 limit
    out["verdict"] = _cap(out.get("verdict") or "No conclusion produced.", 600)
    if out.get("risk_level") not in RISK_LEVELS:
        out["risk_level"] = "medium"
    if out.get("entity_profile") not in ENTITY_PROFILES:
        out["entity_profile"] = "unknown"
    try:
        out["confidence"] = max(0, min(100, int(out.get("confidence", 0))))
    except (TypeError, ValueError):
        out["confidence"] = 30
    evidence = out.get("evidence")
    out["evidence"] = [
        {
            "finding": _cap(e.get("finding", ""), 400),
            "reference": _cap(e.get("reference", address), 100),
            "why_it_matters": _cap(e.get("why_it_matters", ""), 300),
        }
        for e in (evidence if isinstance(evidence, list) else [])
        if isinstance(e, dict)
    ][:8]
    path = out.get("investigation_path")
    out["investigation_path"] = (
        [_cap(p, 200) for p in path][:12] if isinstance(path, list) else []
    )
    return out


def _fallback_verdict(address: str, reason: str, steps: list[str]) -> dict:
    return {
        "verdict": f"Investigation incomplete: {reason}. No reliable conclusion for {short(address)}.",
        "risk_level": "medium",
        "confidence": 10,
        "entity_profile": "unknown",
        "evidence": [],
        "investigation_path": steps,
        "error": reason,
    }


# --------------------------------------------------------------------------- #
# the investigation                                                            #
# --------------------------------------------------------------------------- #

OnStep = Optional[Callable[[Optional[str], Optional[str]], Any]]


async def _emit(on_step: OnStep, desc: Optional[str], summary: Optional[str]) -> None:
    if on_step is None:
        return
    try:
        r = on_step(desc, summary)
        if inspect.isawaitable(r):
            await r
    except Exception:
        log.exception("on_step callback failed (ignored)")


async def investigate(
    cfg: Config,
    address: str,
    addr_type: Optional[str] = None,
    on_step: OnStep = None,
) -> dict:
    """Run a full autonomous investigation. Returns the validated case-file dict.

    Never raises for runtime/tool failures — returns a degraded case file instead.
    """
    t_start = time.monotonic()
    box = ToolBox(cfg)
    runtime = RuntimeClient(cfg)
    steps_taken: list[str] = []

    try:
        # 1. detect address type if the caller didn't
        type_note = ""
        if addr_type is None:
            await _emit(on_step, "🔎 Identifying address type...", None)
            addr_type, type_note = await box.detect_address_type(address)
            await _emit(on_step, None, f"{addr_type}" + (f" — {type_note}" if type_note else ""))

        native = cfg.btl_use_native_tools
        try:
            if native:
                truncated = await _run_native_loop(cfg, runtime, box, address, addr_type, type_note, on_step, steps_taken)
                messages = truncated["messages"]
                hit_limit = truncated["hit_limit"]
            else:
                raise _UseReact()
        except RuntimeClientError as e:
            # If the runtime choked on the `tools` param (4xx), auto-fall back to ReAct.
            if native and e.status_code is not None and 400 <= e.status_code < 500 and runtime.calls_made == 0:
                log.warning("native tool-calling rejected by runtime — falling back to ReAct loop")
                await _emit(on_step, "↩️ Runtime lacks native tools — switching to ReAct mode...", None)
                result = await _run_react_loop(cfg, runtime, box, address, addr_type, type_note, on_step, steps_taken)
                messages, hit_limit = result["messages"], result["hit_limit"]
            else:
                return _fallback_verdict(address, str(e), steps_taken)
        except _UseReact:
            result = await _run_react_loop(cfg, runtime, box, address, addr_type, type_note, on_step, steps_taken)
            messages, hit_limit = result["messages"], result["hit_limit"]

        # 2. final structured verdict
        await _emit(on_step, "📋 Compiling the case file...", None)
        verdict_prompt = prompts.VERDICT_PROMPT_LIMITED if hit_limit else prompts.VERDICT_PROMPT
        messages.append({"role": "user", "content": verdict_prompt})

        try:
            msg = await runtime.chat(messages)  # no tools: force prose/JSON
        except RuntimeClientError as e:
            return _fallback_verdict(address, f"verdict call failed: {e}", steps_taken)

        verdict = parse_json_loose(msg.content)
        if verdict is None:
            # one repair retry
            log.warning("verdict was not valid JSON — asking runtime to repair")
            messages.append({"role": "assistant", "content": msg.content or ""})
            messages.append({"role": "user", "content": prompts.JSON_REPAIR_PROMPT})
            try:
                msg2 = await runtime.chat(messages)
                verdict = parse_json_loose(msg2.content)
            except RuntimeClientError:
                verdict = None

        if verdict is None:
            return _fallback_verdict(address, "runtime never produced valid verdict JSON", steps_taken)

        case = _validate_verdict(verdict, address)
        if not case["investigation_path"]:
            case["investigation_path"] = steps_taken
        case["_meta"] = {
            "address": address,
            "address_type": addr_type,
            "runtime_calls": runtime.calls_made,
            "tool_calls": len(steps_taken),
            "seconds": round(time.monotonic() - t_start, 1),
            "hit_limit": hit_limit,
            "mode": "native_tools" if cfg.btl_use_native_tools else "react",
        }
        if getattr(runtime, "saw_cost_headers", False):
            case["_meta"]["btl_charge_usd"] = round(runtime.total_charge_usd, 6)
            case["_meta"]["btl_saved_usd"] = round(runtime.total_saved_usd, 6)
        return case
    finally:
        await box.close()


class _UseReact(Exception):
    pass


async def _execute_tool_call(
    box: ToolBox, name: str, args: dict, on_step: OnStep, steps_taken: list[str]
) -> dict:
    phrase = STEP_PHRASES.get(name, name.replace("_", " "))
    target = args.get("mint") or args.get("address") or ""
    await _emit(on_step, f"🔍 {phrase} ({short(target)})...", None)
    result = await box.execute(name, args)
    summary = summarize_result(name, args, result)
    steps_taken.append(f"{phrase} ({short(target)}) → {summary}")
    await _emit(on_step, None, summary)
    return result


async def _run_native_loop(
    cfg: Config,
    runtime: RuntimeClient,
    box: ToolBox,
    address: str,
    addr_type: str,
    type_note: str,
    on_step: OnStep,
    steps_taken: list[str],
) -> dict:
    """Native OpenAI tool-calling loop. Returns {'messages': [...], 'hit_limit': bool}."""
    messages: list[dict] = [
        {"role": "system", "content": prompts.SYSTEM_PROMPT},
        {"role": "user", "content": prompts.initial_user_message(address, addr_type, type_note)},
    ]
    t0 = time.monotonic()
    hit_limit = False

    for round_no in range(cfg.max_tool_rounds):
        if time.monotonic() - t0 > cfg.max_wall_seconds:
            log.info("wall-time limit reached after %d rounds", round_no)
            hit_limit = True
            break

        msg = await runtime.chat(messages, tools=TOOL_SCHEMAS)
        tool_calls = getattr(msg, "tool_calls", None) or []

        if not tool_calls:
            # model is done reasoning — keep its conclusion in context
            messages.append({"role": "assistant", "content": msg.content or ""})
            log.info("model concluded after %d tool round(s)", round_no)
            break

        messages.append(
            {
                "role": "assistant",
                "content": msg.content or None,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments or "{}",
                        },
                    }
                    for tc in tool_calls
                ],
            }
        )

        # execute all tool calls from this round in parallel
        async def _one(tc):
            try:
                args = json.loads(tc.function.arguments or "{}")
                if not isinstance(args, dict):
                    args = {}
            except json.JSONDecodeError:
                return {"error": f"could not parse arguments: {_trunc(tc.function.arguments, 120)}"}
            return await _execute_tool_call(box, tc.function.name, args, on_step, steps_taken)

        results = await asyncio.gather(*(_one(tc) for tc in tool_calls))
        for tc, result in zip(tool_calls, results):
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, separators=(",", ":")),
                }
            )
    else:
        hit_limit = True
        log.info("round limit (%d) reached", cfg.max_tool_rounds)

    return {"messages": messages, "hit_limit": hit_limit}


async def _run_react_loop(
    cfg: Config,
    runtime: RuntimeClient,
    box: ToolBox,
    address: str,
    addr_type: str,
    type_note: str,
    on_step: OnStep,
    steps_taken: list[str],
) -> dict:
    """ReAct fallback: model outputs strict JSON actions, we parse manually."""
    messages: list[dict] = [
        {"role": "system", "content": prompts.build_react_system_prompt(TOOL_SCHEMAS)},
        {"role": "user", "content": prompts.initial_user_message(address, addr_type, type_note)},
    ]
    t0 = time.monotonic()
    hit_limit = False
    bad_replies = 0

    for round_no in range(cfg.max_tool_rounds):
        if time.monotonic() - t0 > cfg.max_wall_seconds:
            hit_limit = True
            break

        msg = await runtime.chat(messages)
        content = msg.content or ""
        messages.append({"role": "assistant", "content": content})

        action = parse_json_loose(content)
        if action is None or "action" not in action:
            bad_replies += 1
            if bad_replies > 2:
                log.warning("ReAct: too many unparseable replies, forcing verdict")
                hit_limit = True
                break
            messages.append(
                {
                    "role": "user",
                    "content": 'Invalid reply. Respond with EXACTLY one JSON object: '
                    '{"thought": "...", "action": "tool_name", "args": {...}} or '
                    '{"thought": "...", "action": "finish"}. Nothing else.',
                }
            )
            continue

        if action.get("action") == "finish":
            log.info("ReAct: model finished after %d round(s)", round_no)
            break

        name = str(action.get("action"))
        args = action.get("args") if isinstance(action.get("args"), dict) else {}
        result = await _execute_tool_call(box, name, args, on_step, steps_taken)
        messages.append(
            {
                "role": "user",
                "content": f"TOOL RESULT ({name}): {json.dumps(result, separators=(',', ':'))}",
            }
        )
    else:
        hit_limit = True

    return {"messages": messages, "hit_limit": hit_limit}
