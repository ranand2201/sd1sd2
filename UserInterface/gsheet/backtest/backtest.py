# -*- coding: utf-8 -*-
"""
backtest.py
"""
import traceback

from Utility.gsheet_utility import *
from ....DataTypes.trade_data import *


class UserInterfaceBackTest:

    def __init__(self, key):
        self.googlesheet_utility = gsheet_utility(account_file=key,
                                                  spread_sheet_name="VWAPSD1SD2")
        self.gworksheet_backtest = self.googlesheet_utility.get_work_sheet("BackTestData")

    def write_trade(self, p_trade_row: sd1sd2_trade_row, p_interval, p_best_case_exit):
        # Interval + Best Case Exit are BackTestData-only columns (appended at the end).
        try:
            values = p_trade_row.to_sheet_row() + [p_interval, p_best_case_exit]
            next_row = len(self.gworksheet_backtest.get_col(1, include_tailing_empty=False)) + 1
            self.gworksheet_backtest.update_values(crange=f"A{next_row}", values=[values], extend=True)
        except:
            print("Exception while writing backtest row to BackTestData")
            traceback.print_exc()
