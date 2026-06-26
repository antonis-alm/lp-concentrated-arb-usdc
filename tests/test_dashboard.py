from __future__ import annotations

import importlib
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch


def _load_ui_module():
    if "streamlit" not in sys.modules:
        streamlit = ModuleType("streamlit")
        streamlit.title = lambda *_args, **_kwargs: None
        sys.modules["streamlit"] = streamlit
    return importlib.import_module("dashboard.ui")


def test_dashboard_module_imports():
    ui = _load_ui_module()
    assert callable(ui.render_custom_dashboard)


def test_render_custom_dashboard_builds_uniswap_lp_config():
    ui = _load_ui_module()
    strategy_config = {
        "chain": "arbitrum",
        "protocol": "uniswap_v3",
        "pool": "ARB/USDC/3000",
    }
    api_client = object()
    session_state = {"foo": "bar"}
    config = SimpleNamespace(protocol="uniswap_v3")

    mock_get_config = MagicMock(return_value=config)
    mock_prepare = MagicMock(return_value={"prepared": True})
    mock_render = MagicMock()

    with (
        patch("dashboard.ui.st.title") as mock_title,
        patch("dashboard.ui._template_tools", return_value=(mock_get_config, mock_prepare, mock_render)),
    ):
        ui.render_custom_dashboard("dep-1", strategy_config, api_client, session_state)

    mock_title.assert_called_once_with("LP-Concentrated-ARB-USDC")
    mock_get_config.assert_called_once_with(
        token0="ARB",
        token1="USDC",
        fee_tier="0.30%",
        chain="arbitrum",
    )
    mock_prepare.assert_called_once_with(
        api_client,
        session_state=session_state,
        config=config,
        deployment_id="dep-1",
    )
    mock_render.assert_called_once_with(
        "dep-1",
        strategy_config,
        {"prepared": True},
        config,
        api_client=api_client,
    )
