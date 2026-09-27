# -*- coding: utf-8 -*-
"""
sd1sd2.py

Thin entry point for the live paper-trade/real-order engine. The actual state machine lives in
sd1sd2_engine.py's SD1SD2Engine, shared verbatim with the backtest engine (Logic/backtest_engine.py)
-- only this file's job is to run that shared engine in LIVE mode with the constructor shape
executor.py's LOGIC_REGISTRY / interfaces.py expect (args, broker_utility_manager, quotes_utility).
"""
from .sd1sd2_engine import SD1SD2Engine, Mode


class LogicSD1SD2(SD1SD2Engine):

    def __init__(self, args, broker_utility_manager, quotes_utility):
        super().__init__(mode=Mode.LIVE, args=args, broker_utility_manager=broker_utility_manager,
                         quotes_utility=quotes_utility)
