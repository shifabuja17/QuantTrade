"""
Skrip sinkronisasi permanen untuk:
1. Adaptive Hybrid Timeframe (BTC & LINK 5m, Altcoin 15m)
2. Proteksi Break-Even Terkunci (BEP 1.1R - 1.2R)
3. Macro Golden Cross & Filter BTC TEMA 200
4. Partial Take-Profit (TP1 1.5R 50% + TP2 2.5R Runner 50%)
5. Early Invalidation Exit (HTF EMA50 Breakdown & BTC Bearish Emergency Exit)
6. Hybrid Adaptive Retest Entry (BTC Direct Market, Altcoins 50% Body Retest Limit Order)

Dapat dijalankan kapan saja untuk memastikan seluruh file di disk tersinkronisasi 100%.
"""

import os
import json
import re

from update_all_codebase import (
    update_config_py,
    update_config_json,
    update_strategy_py,
    update_risk_management_py,
    update_bot_py
)

if __name__ == "__main__":
    update_config_py()
    update_config_json()
    update_strategy_py()
    update_risk_management_py()
    update_bot_py()
    print("[+] Sinkronisasi apply_adaptive_update.py selesai dengan sukses!")

