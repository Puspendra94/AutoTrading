"""The decision loop: features -> gate -> LLM -> validate -> size -> execute.

Every path through here produces exactly one recorded decision — including the free ones. A bar
where the gate said "no trigger" is logged as a SKIP with its reason, because a gate that is too
tight is otherwise invisible: the system would simply never trade and nothing would say why.

Safety properties this module is responsible for:

  * An LLM outage is a SKIP, never a trade. The shared LlmService falls back to a SYNTHETIC
    canned response when every model fails; that is survivable for strategy generation and
    unacceptable here, so a fallback response is detected and refused.
  * The validator can only reject. Nothing here repairs a bad decision.
  * Sizing is computed from policy constants, then handed to the risk gate as a requested
    quantity — so the gate's kill switch, daily-loss halt and concurrency checks all still apply.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from ...llm.service import LlmPurpose, LlmUnavailableError
from ..features.state import FeatureState
from . import gate as gate_mod
from .prompts import MIN_RISK_REWARD, build_entry_prompt, build_exit_prompt
from .schemas import EntryDecision, ExitDecision
from .sizing import DAILY_RISK_PCT, size_position, take_profit_for, validate_risk_reward
from .validator import validate_entry, validate_exit

log = logging.getLogger("worker.pattern.decision")

# Decision outcomes as stored. Every bar produces one.
SKIPPED_NO_TRIGGER = "skipped_no_trigger"   # the gate declined — free, no LLM call
SKIPPED_BY_MODEL = "skipped_by_model"       # the model was asked and said SKIP
REJECTED = "rejected"                       # the model proposed a trade the guardrails refused
EXECUTED = "executed"
EXIT_EXECUTED = "exit_executed"
EXIT_HELD = "exit_held"
ERROR = "error"

# Token budgets. These must cover a reasoning model's hidden thinking AND the emitted JSON —
# see the note at the entry call site.
ENTRY_MAX_TOKENS = 3072
EXIT_MAX_TOKENS = 2048


def _is_synthetic(response: dict) -> bool:
    """LlmService returns a canned fallback when every model in the chain fails, always suffixed
    '-fallback'. Trading on it would be trading on a fabrication."""
    return str(response.get("model", "")).endswith("-fallback")


class DecisionEngine:
    def __init__(self, llm: Any, execution: Any, store: Any, pool: Any) -> None:
        self.llm = llm
        self.execution = execution
        self.store = store
        self.pool = pool

    # ------------------------------------------------------------------ main entry point
    async def decide(
        self,
        state: FeatureState,
        *,
        open_position: Optional[dict],
        account: dict,
        bars_since_last_close: Optional[int] = None,
        failures: Optional[list[dict]] = None,
    ) -> dict:
        """Run one bar through the loop and return the recorded decision."""
        result = gate_mod.evaluate_gate(
            state,
            open_position,
            bars_since_last_close=bars_since_last_close,
            daily_budget_exhausted=account.get("remainingRiskUsd", 0) <= 0,
        )

        if not result.should_call:
            return self._record(state, SKIPPED_NO_TRIGGER, gate=result, reason=result.reason)

        try:
            if result.kind == gate_mod.EXIT:
                return await self._decide_exit(state, open_position, result)
            return await self._decide_entry(state, account, result, failures or [])
        except LlmUnavailableError as err:
            # Every model failed AND no fallback fits the schema. Refusing is the point.
            log.warning("LLM unavailable for %s — skipping the bar: %s", state.ticker_id, err)
            return self._record(state, ERROR, gate=result, reason=f"LLM unavailable: {err}")
        except Exception as err:  # noqa: BLE001 — a decision failure must not kill the loop
            log.exception("Decision failed for %s", state.ticker_id)
            return self._record(state, ERROR, gate=result, reason=f"Decision error: {err}")

    # ------------------------------------------------------------------ entry
    async def _decide_entry(
        self, state: FeatureState, account: dict, result: gate_mod.GateResult, failures: list[dict]
    ) -> dict:
        pack = state.to_state_pack()
        prompt = build_entry_prompt(pack, account, list(result.triggers), failures)

        # DeepSeek v4-flash is a REASONING model: measured against this prompt it spent 1024
        # completion tokens entirely on reasoning and had none left to emit the JSON, so the first
        # attempt failed the length limit and only the retry succeeded — three calls billed for one
        # decision. The budget has to cover thinking AND the answer.
        response = await self.llm.generate_structured_completion(prompt, EntryDecision, max_tokens=ENTRY_MAX_TOKENS)
        if _is_synthetic(response):
            return self._record(state, ERROR, gate=result,
                                reason="LLM chain exhausted — refusing to trade on the synthetic fallback.")

        decision: EntryDecision = response["data"]
        await self._log_cost(state, response)

        if decision.action == "SKIP":
            return self._record(state, SKIPPED_BY_MODEL, gate=result,
                                reason=decision.reasoning, llm=decision.model_dump(), response=response)

        # --- Guardrails. Reject-only: the model's numbers are stored exactly as proposed.
        verdict = validate_entry(
            decision, current_price=state.close, regime_label=state.regime["label"],
            min_rr=MIN_RISK_REWARD,
        )
        if not verdict.ok:
            log.info("Entry REJECTED for %s: %s", state.ticker_id, verdict.reason)
            return self._record(state, REJECTED, gate=result, reason=verdict.reason,
                                llm=decision.model_dump(), response=response,
                                validation=verdict.as_dict())

        # --- Sizing owns the money; the model never sees a quantity.
        atr_pct = (pack["indicators"].get("atrPct") or 0.0) / 100.0
        sizing = size_position(
            side=decision.side,
            entry_price=state.close,
            requested_stop_price=float(decision.stop_loss),
            atr_pct=atr_pct,
            day_start_balance=account["dayStartBalance"],
            realized_loss_today=account["realizedLossToday"],
            free_balance=account["freeBalance"],
            filters=account["filters"],
        )
        if not sizing.approved:
            log.info("Entry sizing REJECTED for %s: %s", state.ticker_id, sizing.reason)
            return self._record(state, REJECTED, gate=result, reason=sizing.reason,
                                llm=decision.model_dump(), response=response,
                                validation=verdict.as_dict(), sizing=sizing.as_dict())

        # Clamping the stop moves the target too, or the trade silently loses the reward:risk it
        # was approved on.
        target = float(decision.take_profit)
        if sizing.clamped:
            ok, _, _ = validate_risk_reward(decision.side, state.close, sizing.stop_price, target, MIN_RISK_REWARD)
            if not ok:
                target = take_profit_for(decision.side, state.close, sizing.stop_price, MIN_RISK_REWARD)

        exec_result = await self.execution.execute_trade_signal(
            state.ticker_id, decision.side, state.close, None,
            requested_quantity=sizing.quantity,
        )
        if exec_result.get("status") == "REJECTED":
            return self._record(state, REJECTED, gate=result,
                                reason=f"Risk gate: {exec_result.get('reason')}",
                                llm=decision.model_dump(), response=response,
                                validation=verdict.as_dict(), sizing=sizing.as_dict())

        # Stamp the protective levels onto the position so the exit ladder has something to
        # enforce. Without this the trade would be running with no stop at all — the ladder reads
        # them from the row, not from this decision.
        position_id = exec_result.get("positionId")
        if position_id and self.store is not None:
            try:
                await self.store.set_protective_levels(
                    position_id, stop_loss=sizing.stop_price, take_profit=target,
                    entry_price=state.close,
                )
            except Exception:  # noqa: BLE001
                # An unprotected position is the one failure here that must be loud. Flatten it
                # rather than leave it open with no stop.
                log.exception("Could not set protective levels on %s — closing it immediately.", position_id)
                try:
                    await self.execution.close_position(position_id, state.close)
                except Exception:  # noqa: BLE001
                    log.exception("Emergency close of unprotected position %s FAILED.", position_id)
                return self._record(state, ERROR, gate=result,
                                    reason="Could not stamp protective levels; position closed.",
                                    llm=decision.model_dump(), response=response,
                                    validation=verdict.as_dict(), sizing=sizing.as_dict())

        log.warning(
            "ENTRY %s %s qty=%s @ %.2f | stop %.2f (%.2f%%) target %.2f | risking %.2f of %.2f USD",
            decision.side, state.ticker_id, sizing.quantity, state.close,
            sizing.stop_price, sizing.stop_pct * 100, target, sizing.risk_usd, sizing.risk_budget_usd,
        )
        return self._record(
            state, EXECUTED, gate=result, reason=decision.reasoning,
            llm=decision.model_dump(), response=response, validation=verdict.as_dict(),
            sizing=sizing.as_dict(), position_id=exec_result.get("positionId"),
            stop_price=sizing.stop_price, take_profit=target,
        )

    # ------------------------------------------------------------------ exit
    async def _decide_exit(
        self, state: FeatureState, position: dict, result: gate_mod.GateResult
    ) -> dict:
        pack = state.to_state_pack()
        prompt = build_exit_prompt(pack, position, list(result.triggers))

        response = await self.llm.generate_structured_completion(prompt, ExitDecision, max_tokens=EXIT_MAX_TOKENS)
        if _is_synthetic(response):
            # An exit we cannot get an answer on is HELD, not closed: the deterministic stop is
            # still in place, so holding is the conservative outcome rather than the risky one.
            return self._record(state, EXIT_HELD, gate=result,
                                reason="LLM chain exhausted — holding; the protective stop still applies.")

        decision: ExitDecision = response["data"]
        await self._log_cost(state, response)

        verdict = validate_exit(decision, position.get("side"))
        if not verdict.ok or decision.action == "HOLD":
            return self._record(state, EXIT_HELD, gate=result,
                                reason=verdict.reason or decision.reasoning,
                                llm=decision.model_dump(), response=response)

        if self.store is not None:
            try:
                await self.store.record_exit_reason(position["id"], f"AI: {decision.reasoning}")
            except Exception:  # noqa: BLE001 — annotation is not worth blocking the exit
                log.warning("Could not record the exit reason", exc_info=True)
        await self.execution.close_position(position["id"], state.close)
        log.warning("EXIT %s position %s @ %.2f: %s",
                    position.get("side"), position["id"], state.close, decision.reasoning)
        return self._record(state, EXIT_EXECUTED, gate=result, reason=decision.reasoning,
                            llm=decision.model_dump(), response=response,
                            position_id=position["id"])

    # ------------------------------------------------------------------ helpers
    async def _log_cost(self, state: FeatureState, response: dict) -> None:
        try:
            await self.llm.log_cost(self.pool, state.ticker_id, None, LlmPurpose.LIVE_DECISION, response)
        except Exception:  # noqa: BLE001 — cost accounting must never block a trade
            log.warning("Failed to log LLM cost", exc_info=True)

    def _record(
        self, state: FeatureState, outcome: str, *, gate: gate_mod.GateResult, reason: str,
        llm: Optional[dict] = None, response: Optional[dict] = None,
        validation: Optional[dict] = None, sizing: Optional[dict] = None,
        position_id: Optional[str] = None, stop_price: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> dict:
        return {
            "tickerId": state.ticker_id,
            "interval": state.interval,
            "barTimeMs": state.bar_time_ms,
            "outcome": outcome,
            "reason": reason,
            "gate": gate.as_dict(),
            "statePack": state.to_state_pack(),
            "llmDecision": llm,
            "validation": validation,
            "sizing": sizing,
            "positionId": position_id,
            "stopPrice": stop_price,
            "takeProfit": take_profit,
            "model": (response or {}).get("model"),
            # costUsd is 0 for any model missing from LlmService.PRICING (DeepSeek is), so the
            # token counts are the usage signal that always holds.
            "costUsd": (response or {}).get("costUsd", 0.0),
            "inputTokens": (response or {}).get("inputTokens", 0),
            "outputTokens": (response or {}).get("outputTokens", 0),
        }


def build_account_snapshot(
    *, day_start_balance: float, realized_loss_today: float, free_balance: float, filters
) -> dict:
    """The account facts a decision needs. Kept tiny on purpose — the model is told the day's
    starting balance and what is left of the risk budget, and nothing about trade counts. The
    operator was explicit that overtrading is not a concern to encode here."""
    r_day = DAILY_RISK_PCT * max(day_start_balance, 0.0)
    return {
        "dayStartBalance": day_start_balance,
        "realizedLossToday": realized_loss_today,
        "freeBalance": free_balance,
        "remainingRiskUsd": max(r_day - max(realized_loss_today, 0.0), 0.0),
        "filters": filters,
    }
