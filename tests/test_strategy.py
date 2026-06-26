from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from strategy import LPConcentratedARBUSDCStrategy


@pytest.fixture
def config() -> dict:
    with open(Path(__file__).parent.parent / "config.json") as f:
        return json.load(f)


@pytest.fixture
def strategy(config: dict) -> LPConcentratedARBUSDCStrategy:
    return LPConcentratedARBUSDCStrategy(
        config=config,
        chain=config["chain"],
        wallet_address="0x" + "1" * 40,
    )


def _balance(balance: str, balance_usd: str):
    return SimpleNamespace(balance=Decimal(balance), balance_usd=Decimal(balance_usd))


def _market(*, arb_price="1", usdc_price="1", arb_balance="100", arb_usd="100", usdc_balance="100", usdc_usd="100"):
    market = MagicMock()

    def price(token):
        if token == "ARB":
            return Decimal(arb_price)
        if token == "USDC":
            return Decimal(usdc_price)
        raise ValueError("unknown token")

    def balance(token, **_kwargs):
        if token == "ARB":
            return _balance(arb_balance, arb_usd)
        if token == "USDC":
            return _balance(usdc_balance, usdc_usd)
        raise ValueError("unknown token")

    market.price.side_effect = price
    market.balance.side_effect = balance
    return market


def _intent_name(intent) -> str:
    return getattr(getattr(intent, "intent_type", None), "value", "")


def test_open_lp_when_balanced_inventory(strategy: LPConcentratedARBUSDCStrategy):
    intent = strategy.decide(_market(arb_price="1", usdc_price="1", arb_usd="100", usdc_usd="100"))
    assert _intent_name(intent) == "LP_OPEN"
    assert intent.pool == "ARB/USDC/3000"
    assert intent.protocol == "uniswap_v3"
    assert intent.range_lower == Decimal("0.995")
    assert intent.range_upper == Decimal("1.005")


def test_open_when_arb_inventory_overweight_but_both_assets_present(
    strategy: LPConcentratedARBUSDCStrategy,
):
    intent = strategy.decide(_market(arb_usd="180", usdc_usd="20"))
    assert _intent_name(intent) == "LP_OPEN"


def test_open_when_usdc_inventory_overweight_but_both_assets_present(
    strategy: LPConcentratedARBUSDCStrategy,
):
    intent = strategy.decide(_market(arb_usd="10", usdc_usd="190"))
    assert _intent_name(intent) == "LP_OPEN"


def test_swap_when_only_arb_present(strategy: LPConcentratedARBUSDCStrategy):
    intent = strategy.decide(_market(arb_balance="100", arb_usd="100", usdc_balance="0", usdc_usd="0"))
    assert _intent_name(intent) == "SWAP"
    assert intent.from_token == "ARB"
    assert intent.to_token == "USDC"


def test_swap_when_only_usdc_present(strategy: LPConcentratedARBUSDCStrategy):
    intent = strategy.decide(_market(arb_balance="0", arb_usd="0", usdc_balance="200", usdc_usd="200"))
    assert _intent_name(intent) == "SWAP"
    assert intent.from_token == "USDC"
    assert intent.to_token == "ARB"


def test_hold_when_no_balance(strategy: LPConcentratedARBUSDCStrategy):
    intent = strategy.decide(_market(arb_balance="0", arb_usd="0", usdc_balance="0", usdc_usd="0"))
    assert _intent_name(intent) == "HOLD"


def test_hold_when_open_position_in_range(strategy: LPConcentratedARBUSDCStrategy):
    strategy._position_id = "123"
    strategy._range_lower = Decimal("0.995")
    strategy._range_upper = Decimal("1.005")
    intent = strategy.decide(_market(arb_price="1", usdc_price="1"))
    assert _intent_name(intent) == "HOLD"


def test_close_when_out_of_range_and_guard_passes(strategy: LPConcentratedARBUSDCStrategy):
    strategy._position_id = "123"
    strategy._range_lower = Decimal("0.995")
    strategy._range_upper = Decimal("1.005")
    strategy._rebalance_count = 1
    strategy._lp_value_usd = Decimal("1000")
    strategy._last_cycle_fees_earned_usd = Decimal("50")

    intent = strategy.decide(_market(arb_price="1.02", usdc_price="1"))
    assert _intent_name(intent) == "LP_CLOSE"
    assert intent.position_id == "123"


def test_hold_when_out_of_range_and_guard_blocks(strategy: LPConcentratedARBUSDCStrategy):
    strategy._position_id = "123"
    strategy._range_lower = Decimal("0.995")
    strategy._range_upper = Decimal("1.005")
    strategy._rebalance_count = 1
    strategy._lp_value_usd = Decimal("1000")
    strategy._last_cycle_fees_earned_usd = Decimal("0.1")

    intent = strategy.decide(_market(arb_price="1.02", usdc_price="1"))
    assert _intent_name(intent) == "HOLD"
    assert "Rebalance guard" in intent.reason


def test_hold_when_price_unavailable(strategy: LPConcentratedARBUSDCStrategy):
    market = _market()
    market.price.side_effect = ValueError("no price")
    intent = strategy.decide(market)
    assert _intent_name(intent) == "HOLD"


def test_force_action_open(strategy: LPConcentratedARBUSDCStrategy):
    strategy.force_action = "open"
    intent = strategy.decide(_market())
    assert _intent_name(intent) == "LP_OPEN"


def test_force_action_swap_uses_executable_size(strategy: LPConcentratedARBUSDCStrategy):
    strategy.force_action = "swap"
    intent = strategy.decide(_market(arb_usd="6", usdc_usd="2"))
    assert _intent_name(intent) == "SWAP"
    assert intent.from_token == "ARB"
    assert intent.to_token == "USDC"
    assert intent.amount_usd == Decimal("1")


def test_force_action_swap_falls_back_to_available_direction(strategy: LPConcentratedARBUSDCStrategy):
    strategy.force_action = "swap"
    strategy.force_swap_from_token = "ARB"
    strategy.force_swap_to_token = "USDC"
    intent = strategy.decide(_market(arb_balance="0", arb_usd="0", usdc_balance="50", usdc_usd="50"))
    assert _intent_name(intent) == "SWAP"
    assert intent.from_token == "USDC"
    assert intent.to_token == "ARB"
    assert intent.amount_usd == Decimal("5")


def test_force_action_close(strategy: LPConcentratedARBUSDCStrategy):
    strategy.force_action = "close"
    strategy._position_id = "999"
    intent = strategy.decide(_market())
    assert _intent_name(intent) == "LP_CLOSE"
    assert intent.position_id == "999"


def test_on_intent_executed_tracks_open_state(strategy: LPConcentratedARBUSDCStrategy):
    open_intent = SimpleNamespace(
        intent_type=SimpleNamespace(value="LP_OPEN"),
        range_lower=Decimal("0.995"),
        range_upper=Decimal("1.005"),
    )
    result = SimpleNamespace(position_id=456)

    strategy.on_intent_executed(open_intent, success=True, result=result)

    assert strategy._position_id == "456"
    assert strategy._range_lower == Decimal("0.995")
    assert strategy._range_upper == Decimal("1.005")


def test_on_intent_executed_tracks_close_accounting(strategy: LPConcentratedARBUSDCStrategy):
    strategy._position_id = "123"
    strategy._pre_close_lp_value_usd = Decimal("500")
    strategy._last_base_price_usd = Decimal("1")
    strategy._last_quote_price_usd = Decimal("1")
    close_intent = SimpleNamespace(intent_type=SimpleNamespace(value="LP_CLOSE"))
    close_data = SimpleNamespace(fees0=10**18, fees1=10**6)
    result = SimpleNamespace(lp_close_data=close_data)

    strategy.on_intent_executed(close_intent, success=True, result=result)

    assert strategy._position_id is None
    assert strategy._rebalance_count == 1
    assert strategy._fees_earned_usd == Decimal("2")
    assert strategy._last_cycle_fees_earned_usd == Decimal("2")
    assert strategy._realized_pnl_per_rebalance


def test_persistent_state_roundtrip(config: dict, strategy: LPConcentratedARBUSDCStrategy):
    strategy._position_id = "123"
    strategy._range_lower = Decimal("0.9")
    strategy._range_upper = Decimal("1.1")
    strategy._rebalance_count = 3
    strategy._fees_earned_usd = Decimal("11")
    strategy._swap_costs_usd = Decimal("2")
    strategy._lp_value_usd = Decimal("200")
    strategy._realized_pnl_per_rebalance = ["1", "2"]

    saved = strategy.get_persistent_state()

    fresh = LPConcentratedARBUSDCStrategy(
        config=config,
        chain=config["chain"],
        wallet_address="0x" + "2" * 40,
    )
    fresh.load_persistent_state(saved)

    assert fresh._position_id == "123"
    assert fresh._range_lower == Decimal("0.9")
    assert fresh._range_upper == Decimal("1.1")
    assert fresh._rebalance_count == 3
    assert fresh._fees_earned_usd == Decimal("11")
    assert fresh._swap_costs_usd == Decimal("2")
    assert fresh._lp_value_usd == Decimal("200")
    assert fresh._realized_pnl_per_rebalance == ["1", "2"]


def test_teardown_methods(strategy: LPConcentratedARBUSDCStrategy):
    strategy._position_id = "123"
    strategy._range_lower = Decimal("0.995")
    strategy._range_upper = Decimal("1.005")

    summary = strategy.get_open_positions()
    assert len(summary.positions) == 1
    assert summary.positions[0].position_id == "123"

    intents = strategy.generate_teardown_intents()
    assert len(intents) == 1
    assert _intent_name(intents[0]) == "LP_CLOSE"


def test_teardown_empty_without_position(strategy: LPConcentratedARBUSDCStrategy):
    assert strategy.generate_teardown_intents() == []
