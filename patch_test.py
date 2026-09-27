import re

with open("test_dynamic_risk.py", "r") as f:
    content = f.read()

# replace pnl_pct in result to ensure it is returned
content = content.replace("'net_pnl': tot_pnl,", "'net_pnl': tot_pnl,\n        'pnl_pct': (tot_pnl / 1000) * 100.0 if tot_trades > 0 else 0,")

with open("test_dynamic_risk.py", "w") as f:
    f.write(content)
