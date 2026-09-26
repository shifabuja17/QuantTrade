import pandas as pd
from test_retest_entry import run_simulation

pairs = ['BTC/USDT', 'LINK/USDT', 'ETH/USDT']

print("--- HASIL KOMPARASI PORTOFOLIO LENGKAP ---")
# 1. Baseline Current (Market at Close)
pnl_curr = 0
for p in pairs:
    r = run_simulation(p, variant='BASELINE', timeout_bars=0)
    pnl_curr += r['net_pnl']
    print(f"[CURRENT BASELINE] {p:9} | PnL: ${r['net_pnl']:+6.2f} | PF: {r['profit_factor']:4.2f} | WR: {r['win_rate']:4.1f}% | Trades: {r['trades']}")
print(f"TOTAL CURRENT PORTFOLIO PNL: ${pnl_curr:+6.2f}\n")

# 2. Universal Retest (Midpoint 5 bars)
pnl_univ = 0
for p in pairs:
    r = run_simulation(p, variant='RETEST_MIDPOINT', timeout_bars=5)
    pnl_univ += r['net_pnl']
    print(f"[UNIV RETEST MID]  {p:9} | PnL: ${r['net_pnl']:+6.2f} | PF: {r['profit_factor']:4.2f} | WR: {r['win_rate']:4.1f}% | Trades: {r['trades']}")
print(f"TOTAL UNIVERSAL RETEST PNL: ${pnl_univ:+6.2f}\n")

# 3. Hybrid: BTC Market, Altcoins Retest Body 50
r_btc = run_simulation('BTC/USDT', variant='BASELINE', timeout_bars=0)
r_link = run_simulation('LINK/USDT', variant='RETEST_BODY_50', timeout_bars=5)
r_eth = run_simulation('ETH/USDT', variant='RETEST_BODY_50', timeout_bars=5)
pnl_hyb = r_btc['net_pnl'] + r_link['net_pnl'] + r_eth['net_pnl']
print(f"[HYBRID ADAPTIVE]  BTC/USDT  | PnL: ${r_btc['net_pnl']:+6.2f} | PF: {r_btc['profit_factor']:4.2f} | WR: {r_btc['win_rate']:4.1f}% | Trades: {r_btc['trades']}")
print(f"[HYBRID ADAPTIVE]  LINK/USDT | PnL: ${r_link['net_pnl']:+6.2f} | PF: {r_link['profit_factor']:4.2f} | WR: {r_link['win_rate']:4.1f}% | Trades: {r_link['trades']}")
print(f"[HYBRID ADAPTIVE]  ETH/USDT  | PnL: ${r_eth['net_pnl']:+6.2f} | PF: {r_eth['profit_factor']:4.2f} | WR: {r_eth['win_rate']:4.1f}% | Trades: {r_eth['trades']}")
print(f"TOTAL HYBRID ADAPTIVE PNL: ${pnl_hyb:+6.2f}\n")

# 4. Hybrid: BTC Market, LINK Retest Body 50, ETH Baseline
pnl_hyb2 = r_btc['net_pnl'] + r_link['net_pnl'] + run_simulation('ETH/USDT', variant='BASELINE', timeout_bars=0)['net_pnl']
print(f"[HYBRID 2] Total PnL (BTC Base + LINK Retest + ETH Base): ${pnl_hyb2:+6.2f}")
