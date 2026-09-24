import importlib
from zoneinfo import ZoneInfo

import pytest


@pytest.mark.parametrize("module", ["config", "data", "indicators", "strategy", "bot",
                                  "bot_manager", "backtester", "optimizer", "main",
                                  "nobitex_client", "server"])
def test_core_imports_without_network(module):
    importlib.import_module(module)


def test_windows_timezone_database():
    assert ZoneInfo("Asia/Tehran").key == "Asia/Tehran"
