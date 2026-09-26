import pandas as pd
from test_dynamic_risk import run_simulation

r_fixed = run_simulation("LINK/USDT", "FIXED_2PCT")
r_m3 = run_simulation("LINK/USDT", "DYNAMIC_MODEL_3")
print("LINK FIXED:", r_fixed['net_pnl'], r_fixed['pf'])
print("LINK M3:   ", r_m3['net_pnl'], r_m3['pf'])

r_eth_fix = run_simulation("ETH/USDT", "FIXED_2PCT")
r_eth_m3 = run_simulation("ETH/USDT", "DYNAMIC_MODEL_3")
print("ETH FIXED:", r_eth_fix['net_pnl'], r_eth_fix['pf'])
print("ETH M3:   ", r_eth_m3['net_pnl'], r_eth_m3['pf'])
