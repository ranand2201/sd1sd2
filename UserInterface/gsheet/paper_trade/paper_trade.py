# -*- coding: utf-8 -*-
"""
paper_trade.py
"""
import traceback

from Utility.gsheet_utility import *
from ....DataTypes.trade_data import *


class UserInterfacePaperTrade:

    def __init__(self, key):
        self.googlesheet_utility = gsheet_utility(account_file=key,
                                                  spread_sheet_name="VWAPSD1SD2")
        self.gworksheet_paper_trade = self.googlesheet_utility.get_work_sheet("PaperTradeData")

    def write_trade(self, p_paper_trade_row: sd1sd2_trade_row, p_interval, p_best_case_exit):
        # Interval + Best Case Exit mirror the same two extra columns BackTestData has -- appended
        # at the end, same as UserInterfaceBackTest.write_trade.
        try:
            values = p_paper_trade_row.to_sheet_row() + [p_interval, p_best_case_exit]
            # append_table()'s "find the last table and append after it" heuristic drifts further
            # right on every call once any row's data doesn't start at column A -- find the next
            # empty row explicitly instead, so every row always starts at column A.
            rows = self.gworksheet_paper_trade.get_all_values()
            last_populated_row = max(
                (row_number for row_number, row in enumerate(rows, start=1)
                 if any(str(cell).strip() for cell in row)),
                default=0,
            )
            next_row = max(last_populated_row + 1, 3)
            self.gworksheet_paper_trade.update_values(crange=f"A{next_row}", values=[values], extend=True)
        except:
            print("Exception while writing paper trade row to PaperTradeData")
            traceback.print_exc()
