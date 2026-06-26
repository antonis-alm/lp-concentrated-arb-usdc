"""LP-Concentrated-ARB-USDC strategy."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Optional

from almanak.framework.intents import Intent
from almanak.framework.market import MarketSnapshot
from almanak.framework.market.errors import (
    BalanceUnavailableError,
    MarketSnapshotError,
    PriceUnavailableError,
)
from almanak.framework.strategies import IntentStrategy, almanak_strategy

logger = logging.getLogger(__name__)


@almanak_strategy(
    name="l_p_concentrated_a_r_b_u_s_d_c",
    description="Concentrated ARB/USDC LP strategy on Uniswap V3 Arbitrum",
    version="1.0.0",
    author="Generated",
    tags=["generated", "dynamic_lp", "uniswap_v3"],
    supported_chains=["arbitrum"],
    supported_protocols=["uniswap_v3"],
    intent_types=["LP_OPEN", "LP_CLOSE", "SWAP", "HOLD"],
    default_chain="arbitrum",
    quote_asset="USD",
)
class LPConcentratedARBUSDCStrategy(IntentStrategy):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        def get_config(key: str, default: Any) -> Any:
            if isinstance(self.config, dict):
                return self.config.get(key, default)
            return getattr(self.config, key, default)

        self.protocol = str(get_config("protocol", "uniswap_v3"))
        self.pool = str(get_config("pool", "ARB/USDC/3000"))
        self.base_token = str(get_config("base_token", "ARB"))
        self.quote_token = str(get_config("quote_token", "USDC"))

        self.range_lower_multiplier = Decimal(str(get_config("range_lower_multiplier", "0.995")))
        self.range_upper_multiplier = Decimal(str(get_config("range_upper_multiplier", "1.005")))
        self.range_half_width_pct = Decimal(str(get_config("range_half_width_pct", "0.5")))

        self.deploy_ratio = Decimal(str(get_config("deploy_ratio_of_available", "0.999")))
        self.inventory_target_base_weight = Decimal(
            str(get_config("inventory_target_base_usd_weight", "0.5"))
        )
        self.inventory_imbalance_tolerance_pct = Decimal(
            str(get_config("inventory_imbalance_tolerance_pct", "0.25"))
        )
        self.min_swap_fraction = Decimal(str(get_config("min_swap_fraction_of_portfolio", "0.002")))

        self.max_swap_slippage = Decimal(str(get_config("max_swap_slippage_pct", "0.5"))) / Decimal("100")

        self.rebalance_guard_enabled = bool(get_config("rebalance_guard_enabled", True))
        self.rebalance_cost_estimate_bps = Decimal(
            str(get_config("rebalance_cost_estimate_bps_of_notional", "8"))
        )
        self.rebalance_guard_fee_cover_ratio = Decimal(
            str(get_config("rebalance_guard_fee_cover_ratio", "1.0"))
        )

        self.force_action = str(get_config("force_action", "") or "").strip().lower()
        self.force_position_id = get_config("force_position_id", None)
        self.force_swap_from_token = str(get_config("force_swap_from_token", self.base_token))
        self.force_swap_to_token = str(get_config("force_swap_to_token", self.quote_token))
        self.force_swap_amount_usd = Decimal(str(get_config("force_swap_amount_usd", "25")))
        self.force_swap_fraction = Decimal(str(get_config("force_swap_fraction_of_source", "0.10")))
        self.force_swap_min_usd = Decimal(str(get_config("force_swap_min_usd", "1")))
        self.force_swap_max_usd = Decimal(str(get_config("force_swap_max_usd", "10")))

        self.single_position_only = bool(get_config("single_position_only", True))
        self.close_before_open = bool(get_config("close_before_open", True))
        self.partial_rebalance = bool(get_config("partial_rebalance", False))

        self.base_token_decimals = int(get_config("base_token_decimals", 18))
        self.quote_token_decimals = int(get_config("quote_token_decimals", 6))

        self._position_id: str | None = None
        self._range_lower: Decimal | None = None
        self._range_upper: Decimal | None = None

        self._lp_entry_price: Decimal | None = None
        self._rebalance_count = 0
        self._fees_earned_usd = Decimal("0")
        self._swap_costs_usd = Decimal("0")
        self._lp_value_usd = Decimal("0")
        self._realized_pnl_per_rebalance: list[str] = []
        self._last_cycle_fees_earned_usd = Decimal("0")
        self._pre_close_lp_value_usd: Decimal | None = None

        self._last_base_price_usd = Decimal("0")
        self._last_quote_price_usd = Decimal("1")

    def decide(self, market: MarketSnapshot) -> Optional[Intent]:
        if self.force_action:
            return self._forced_intent(market)

        try:
            base_price_usd = Decimal(str(market.price(self.base_token)))
            quote_price_usd = Decimal(str(market.price(self.quote_token)))
            if quote_price_usd <= 0:
                return Intent.hold(reason="Quote price unavailable")
            spot = base_price_usd / quote_price_usd
            self._last_base_price_usd = base_price_usd
            self._last_quote_price_usd = quote_price_usd
        except (PriceUnavailableError, MarketSnapshotError, ValueError, KeyError, ZeroDivisionError) as err:
            return Intent.hold(reason=f"Price data unavailable: {err}")

        if self._position_id:
            if not self.single_position_only:
                return Intent.hold(reason="Strategy is configured for single position operation")

            if self.partial_rebalance:
                return Intent.hold(reason="Partial rebalance is disabled for this strategy")

            if self._range_lower is None or self._range_upper is None:
                return Intent.hold(reason=f"Position {self._position_id} exists but range unknown")

            if self._range_lower <= spot <= self._range_upper:
                return Intent.hold(reason=f"Position {self._position_id} remains in range")

            if self.rebalance_guard_enabled and self._rebalance_count > 0:
                estimated_cost = self._estimate_rebalance_cost_usd()
                min_required_fees = estimated_cost * self.rebalance_guard_fee_cover_ratio
                if self._last_cycle_fees_earned_usd < min_required_fees:
                    return Intent.hold(
                        reason=(
                            "Rebalance guard active: estimated tx cost exceeds fees "
                            "earned since previous rebalance"
                        )
                    )

            if self.close_before_open:
                self._pre_close_lp_value_usd = self._lp_value_usd
                return Intent.lp_close(
                    position_id=self._position_id,
                    pool=self.pool,
                    collect_fees=True,
                    protocol=self.protocol,
                )

        try:
            base_balance = market.balance(self.base_token, price=base_price_usd)
            quote_balance = market.balance(self.quote_token, price=quote_price_usd)
        except (BalanceUnavailableError, MarketSnapshotError, ValueError, KeyError) as err:
            return Intent.hold(reason=f"Balance data unavailable: {err}")

        base_amount = Decimal(str(base_balance.balance))
        quote_amount = Decimal(str(quote_balance.balance))
        base_usd = Decimal(str(base_balance.balance_usd))
        quote_usd = Decimal(str(quote_balance.balance_usd))
        total_usd = base_usd + quote_usd

        if total_usd <= 0:
            return Intent.hold(reason="No deployable balance")

        has_base = base_amount > 0
        has_quote = quote_amount > 0

        if has_base != has_quote:
            target_base_usd = total_usd * self.inventory_target_base_weight
            imbalance_usd = base_usd - target_base_usd
            imbalance_abs = abs(imbalance_usd)
            tolerance_usd = total_usd * (self.inventory_imbalance_tolerance_pct / Decimal("100"))
            min_swap_usd = total_usd * self.min_swap_fraction

            if imbalance_abs > max(tolerance_usd, min_swap_usd):
                from_token = self.base_token if imbalance_usd > 0 else self.quote_token
                to_token = self.quote_token if imbalance_usd > 0 else self.base_token
                return Intent.swap(
                    from_token=from_token,
                    to_token=to_token,
                    amount_usd=imbalance_abs,
                    max_slippage=self.max_swap_slippage,
                    protocol=self.protocol,
                )

        range_lower, range_upper = self._compute_range(spot)
        self._lp_value_usd = total_usd
        return Intent.lp_open(
            pool=self.pool,
            amount0=base_amount * self.deploy_ratio,
            amount1=quote_amount * self.deploy_ratio,
            range_lower=range_lower,
            range_upper=range_upper,
            protocol=self.protocol,
        )

    def _forced_intent(self, market: MarketSnapshot) -> Intent:
        try:
            base_price_usd = Decimal(str(market.price(self.base_token)))
            quote_price_usd = Decimal(str(market.price(self.quote_token)))
            spot = base_price_usd / quote_price_usd
            self._last_base_price_usd = base_price_usd
            self._last_quote_price_usd = quote_price_usd
        except (PriceUnavailableError, MarketSnapshotError, ValueError, KeyError, ZeroDivisionError):
            spot = Decimal("1")

        if self.force_action == "open":
            try:
                base_balance = market.balance(self.base_token, price=self._last_base_price_usd)
                quote_balance = market.balance(self.quote_token, price=self._last_quote_price_usd)
                amount0 = Decimal(str(base_balance.balance)) * self.deploy_ratio
                amount1 = Decimal(str(quote_balance.balance)) * self.deploy_ratio
            except (BalanceUnavailableError, MarketSnapshotError, ValueError, KeyError):
                amount0 = Decimal("1")
                amount1 = Decimal("1")
            range_lower, range_upper = self._compute_range(spot)
            return Intent.lp_open(
                pool=self.pool,
                amount0=amount0,
                amount1=amount1,
                range_lower=range_lower,
                range_upper=range_upper,
                protocol=self.protocol,
            )

        if self.force_action == "swap":
            try:
                base_balance = market.balance(self.base_token, price=self._last_base_price_usd)
                quote_balance = market.balance(self.quote_token, price=self._last_quote_price_usd)
                balances = {
                    self.base_token: Decimal(str(base_balance.balance_usd)),
                    self.quote_token: Decimal(str(quote_balance.balance_usd)),
                }
            except (BalanceUnavailableError, MarketSnapshotError, ValueError, KeyError):
                balances = {
                    self.base_token: Decimal("0"),
                    self.quote_token: Decimal("0"),
                }

            source_token = self.force_swap_from_token
            target_token = self.force_swap_to_token

            if balances.get(source_token, Decimal("0")) <= 0 or source_token == target_token:
                if balances.get(self.base_token, Decimal("0")) >= balances.get(self.quote_token, Decimal("0")):
                    source_token = self.base_token
                    target_token = self.quote_token
                else:
                    source_token = self.quote_token
                    target_token = self.base_token

            available_usd = balances.get(source_token, Decimal("0"))
            if available_usd <= 0:
                return Intent.hold(reason="Force swap skipped: no source balance")

            max_executable_usd = available_usd * Decimal("0.80")
            hard_cap_usd = min(self.force_swap_max_usd, self.force_swap_amount_usd)
            proposed_usd = available_usd * self.force_swap_fraction
            amount_usd = min(proposed_usd, hard_cap_usd, max_executable_usd)
            min_guard_usd = min(self.force_swap_min_usd, max_executable_usd)
            if amount_usd < min_guard_usd:
                amount_usd = min_guard_usd

            if amount_usd <= 0:
                return Intent.hold(reason="Force swap skipped: computed amount is zero")

            return Intent.swap(
                from_token=source_token,
                to_token=target_token,
                amount_usd=amount_usd,
                max_slippage=self.max_swap_slippage,
                protocol=self.protocol,
            )

        if self.force_action == "close":
            position_id = str(self.force_position_id or self._position_id or "0")
            return Intent.lp_close(
                position_id=position_id,
                pool=self.pool,
                collect_fees=True,
                protocol=self.protocol,
            )

        return Intent.hold(reason=f"Unsupported force_action: {self.force_action}")

    def _compute_range(self, spot: Decimal) -> tuple[Decimal, Decimal]:
        if self.range_lower_multiplier > 0 and self.range_upper_multiplier > 0:
            return spot * self.range_lower_multiplier, spot * self.range_upper_multiplier

        half_width = self.range_half_width_pct / Decimal("100")
        return spot * (Decimal("1") - half_width), spot * (Decimal("1") + half_width)

    def _estimate_rebalance_cost_usd(self) -> Decimal:
        notional = self._lp_value_usd if self._lp_value_usd > 0 else Decimal("1000")
        return notional * self.rebalance_cost_estimate_bps / Decimal("10000")

    def on_intent_executed(self, intent, success: bool, result):
        if not success:
            return

        intent_type = getattr(getattr(intent, "intent_type", None), "value", "")

        if intent_type == "LP_OPEN":
            pid = getattr(result, "position_id", None)
            self._position_id = str(pid) if pid is not None else self._position_id
            self._range_lower = Decimal(str(getattr(intent, "range_lower", "0")))
            self._range_upper = Decimal(str(getattr(intent, "range_upper", "0")))
            self._lp_entry_price = (self._range_lower + self._range_upper) / Decimal("2")

        elif intent_type == "SWAP":
            self._swap_costs_usd += self._extract_swap_cost_usd(result)

        elif intent_type == "LP_CLOSE":
            close_data = getattr(result, "lp_close_data", None)
            fees_usd = self._fees_from_close_data_usd(close_data)
            self._last_cycle_fees_earned_usd = fees_usd
            self._fees_earned_usd += fees_usd
            if self._pre_close_lp_value_usd is not None:
                pnl = fees_usd - self._swap_costs_usd
                self._realized_pnl_per_rebalance.append(str(pnl))
            self._rebalance_count += 1
            self._position_id = None
            self._range_lower = None
            self._range_upper = None
            self._lp_entry_price = None
            self._pre_close_lp_value_usd = None

    def _extract_swap_cost_usd(self, result: Any) -> Decimal:
        swap_amounts = getattr(result, "swap_amounts", None)
        if swap_amounts is None:
            return Decimal("0")

        expected_out = getattr(swap_amounts, "expected_out_decimal", None)
        actual_out = getattr(swap_amounts, "amount_out_decimal", None)
        if expected_out is not None and actual_out is not None:
            out_price = self._last_quote_price_usd
            return max(Decimal("0"), Decimal(str(expected_out)) - Decimal(str(actual_out))) * out_price

        slippage_bps = getattr(swap_amounts, "slippage_bps", None)
        amount_in = getattr(swap_amounts, "amount_in_decimal", None)
        if slippage_bps is not None and amount_in is not None:
            return Decimal(str(amount_in)) * Decimal(str(slippage_bps)) / Decimal("10000") * self._last_base_price_usd

        return Decimal("0")

    def _fees_from_close_data_usd(self, close_data: Any) -> Decimal:
        if close_data is None:
            return Decimal("0")

        fees0 = getattr(close_data, "fees0", None)
        fees1 = getattr(close_data, "fees1", None)
        fee0_tokens = Decimal(str(fees0)) / (Decimal("10") ** self.base_token_decimals) if fees0 else Decimal("0")
        fee1_tokens = Decimal(str(fees1)) / (Decimal("10") ** self.quote_token_decimals) if fees1 else Decimal("0")
        return fee0_tokens * self._last_base_price_usd + fee1_tokens * self._last_quote_price_usd

    def get_status(self) -> dict[str, Any]:
        return {
            "strategy": "l_p_concentrated_a_r_b_u_s_d_c",
            "chain": self.chain,
            "pool": self.pool,
            "position_id": self._position_id,
            "lp_entry_price": str(self._lp_entry_price) if self._lp_entry_price is not None else None,
            "lower_range_bound": str(self._range_lower) if self._range_lower is not None else None,
            "upper_range_bound": str(self._range_upper) if self._range_upper is not None else None,
            "rebalance_count": self._rebalance_count,
            "fees_earned": str(self._fees_earned_usd),
            "swap_costs": str(self._swap_costs_usd),
            "lp_value": str(self._lp_value_usd),
            "realized_pnl_per_rebalance": list(self._realized_pnl_per_rebalance),
        }

    def get_persistent_state(self):
        return {
            "position_id": self._position_id,
            "range_lower": str(self._range_lower) if self._range_lower is not None else None,
            "range_upper": str(self._range_upper) if self._range_upper is not None else None,
            "lp_entry_price": str(self._lp_entry_price) if self._lp_entry_price is not None else None,
            "rebalance_count": self._rebalance_count,
            "fees_earned_usd": str(self._fees_earned_usd),
            "swap_costs_usd": str(self._swap_costs_usd),
            "lp_value_usd": str(self._lp_value_usd),
            "last_cycle_fees_earned_usd": str(self._last_cycle_fees_earned_usd),
            "realized_pnl_per_rebalance": list(self._realized_pnl_per_rebalance),
        }

    def load_persistent_state(self, state):
        if not state:
            return

        self._position_id = state.get("position_id")
        self._range_lower = Decimal(state["range_lower"]) if state.get("range_lower") else None
        self._range_upper = Decimal(state["range_upper"]) if state.get("range_upper") else None
        self._lp_entry_price = Decimal(state["lp_entry_price"]) if state.get("lp_entry_price") else None
        self._rebalance_count = int(state.get("rebalance_count", 0))
        self._fees_earned_usd = Decimal(str(state.get("fees_earned_usd", "0")))
        self._swap_costs_usd = Decimal(str(state.get("swap_costs_usd", "0")))
        self._lp_value_usd = Decimal(str(state.get("lp_value_usd", "0")))
        self._last_cycle_fees_earned_usd = Decimal(str(state.get("last_cycle_fees_earned_usd", "0")))
        self._realized_pnl_per_rebalance = [str(v) for v in state.get("realized_pnl_per_rebalance", [])]

    def get_open_positions(self):
        from almanak.framework.teardown import PositionInfo, PositionType, TeardownPositionSummary

        positions = []
        if self._position_id is not None:
            positions.append(
                PositionInfo(
                    position_type=PositionType.LP,
                    position_id=str(self._position_id),
                    chain=self.chain,
                    protocol=self.protocol,
                    value_usd=self._lp_value_usd,
                    details={
                        "pool": self.pool,
                        "lower_range_bound": str(self._range_lower) if self._range_lower is not None else None,
                        "upper_range_bound": str(self._range_upper) if self._range_upper is not None else None,
                    },
                )
            )

        return TeardownPositionSummary(
            deployment_id=getattr(self, "deployment_id", "l_p_concentrated_a_r_b_u_s_d_c"),
            timestamp=datetime.now(UTC),
            positions=positions,
        )

    def generate_teardown_intents(self, mode=None, market=None) -> list[Intent]:
        if self._position_id is None:
            return []

        return [
            Intent.lp_close(
                position_id=str(self._position_id),
                pool=self.pool,
                collect_fees=True,
                protocol=self.protocol,
            )
        ]
