"""
Simulasi Komparasi Presisi:
Subclass langsung dari Backtester untuk memastikan 100% sinkronisasi data, filter, dan timing.
Membandingkan Baseline (Single TP 2.5R) vs Partial TP (TP1 1.5R 50% + TP2 2.5R 50%).
"""

import asyncio
import pandas as pd
from datetime import datetime, timedelta
from backtest import Backtester

class ComparativeBacktester(Backtester):
    def __init__(self, symbol: str, start_date: str, end_date: str, use_partial_tp: bool = False):
        super().__init__(symbol, start_date, end_date)
        self.use_partial_tp = use_partial_tp

    def simulate_execution(self, current_bar: pd.Series, df_window: pd.DataFrame):
        if not self.use_partial_tp:
            # Jalankan logika persis bawaan backtest.py
            return super().simulate_execution(current_bar, df_window)

        if not self.active_trade:
            return

        trade = self.active_trade
        # Inisialisasi atribut Partial TP jika belum ada
        if 'tp1_executed' not in trade:
            trade['tp1_executed'] = False
            trade['initial_quantity'] = trade['quantity']
            trade['remaining_quantity'] = trade['quantity']
            trade['realized_pnl'] = 0.0

        high_price = current_bar['high']
        low_price = current_bar['low']
        open_price = current_bar['open']

        slippage_rate = (self.config.execution.max_slippage_pct / 100.0) * 0.5
        stop_distance = trade['entry_price'] - trade['initial_stop_loss']
        bep_profit_pct = getattr(self.config.risk, 'bep_profit_pct', 0.15)
        fee_rate = (self.config.risk.estimated_exchange_fee_pct / 100.0) / 2.0

        tp1_price = trade['entry_price'] + (stop_distance * 1.5)
        tp2_price = trade['take_profit']  # 2.5R

        hit_sl = low_price <= trade['stop_loss']
        hit_tp1 = high_price >= tp1_price
        hit_tp2 = high_price >= tp2_price

        if not trade['tp1_executed']:
            # Fase Sebelum TP1
            if hit_sl and hit_tp1:
                # Intra-bar: anggap pesimis kena SL
                exit_price = open_price if open_price < trade['stop_loss'] else trade['stop_loss'] * (1.0 - slippage_rate)
                self.close_trade(exit_price, "STOP_LOSS", current_bar['timestamp'])
                return

            if hit_sl:
                exit_price = open_price if open_price < trade['stop_loss'] else trade['stop_loss'] * (1.0 - slippage_rate)
                self.close_trade(exit_price, "STOP_LOSS", current_bar['timestamp'])
                return

            if hit_tp1:
                # Eksekusi TP1 (50% dari total koin)
                close_qty = trade['initial_quantity'] * 0.5
                trade['remaining_quantity'] -= close_qty
                trade['tp1_executed'] = True

                gross_pnl_tp1 = (tp1_price - trade['entry_price']) * close_qty
                fee_tp1 = (trade['entry_price'] * close_qty * fee_rate) + (tp1_price * close_qty * fee_rate)
                net_pnl_tp1 = gross_pnl_tp1 - fee_tp1

                self.current_capital += net_pnl_tp1
                self.daily_pnl += net_pnl_tp1
                trade['realized_pnl'] = net_pnl_tp1

                # Otomatis kunci SL sisa posisi ke BEP (Entry + 0.15%)
                bep_level = trade['entry_price'] * (1.0 + (bep_profit_pct / 100.0))
                if bep_level > trade['stop_loss']:
                    trade['stop_loss'] = bep_level
                    trade['bep_activated'] = True

                # Cek jika bar yang sama juga tembus TP2 (2.5R)
                if hit_tp2:
                    rem_qty = trade['remaining_quantity']
                    gross_pnl_tp2 = (tp2_price - trade['entry_price']) * rem_qty
                    fee_tp2 = (trade['entry_price'] * rem_qty * fee_rate) + (tp2_price * rem_qty * fee_rate)
                    net_pnl_tp2 = gross_pnl_tp2 - fee_tp2

                    self.current_capital += net_pnl_tp2
                    self.daily_pnl += net_pnl_tp2

                    total_pnl = trade['realized_pnl'] + net_pnl_tp2
                    total_initial_notional = trade['entry_price'] * trade['initial_quantity']
                    pnl_pct = (total_pnl / total_initial_notional) * 100.0

                    self.trades_history.append({
                        'entry_time': trade['entry_time'],
                        'exit_time': current_bar['timestamp'],
                        'entry_price': trade['entry_price'],
                        'exit_price': tp2_price,
                        'reason': "TAKE_PROFIT_ALL (TP1+TP2)",
                        'pnl': total_pnl,
                        'pnl_pct': pnl_pct,
                        'capital_after': self.current_capital
                    })
                    self.active_trade = None
                    self.cooldown_until = current_bar['timestamp'] + timedelta(minutes=self.config.execution.cooldown_minutes)
                    return
                return

            # Jika belum sentuh TP1, cek aktivasi BEP biasa di 1.1R
            bep_mult = getattr(self.config.risk, 'bep_trigger_atr_multiplier', 1.1)
            if bep_mult > 1.2:
                bep_mult = 1.1
            if not trade.get('bep_activated', False) and (high_price >= trade['entry_price'] + (stop_distance * bep_mult)):
                bep_level = trade['entry_price'] * (1.0 + (bep_profit_pct / 100.0))
                if bep_level > trade['stop_loss']:
                    trade['stop_loss'] = bep_level
                    trade['bep_activated'] = True

        else:
            # Fase Setelah TP1 Berhasil (Sisa 50% Posisi)
            if hit_sl:
                exit_price = open_price if open_price < trade['stop_loss'] else trade['stop_loss'] * (1.0 - slippage_rate)
                rem_qty = trade['remaining_quantity']
                gross_pnl_rem = (exit_price - trade['entry_price']) * rem_qty
                fee_rem = (trade['entry_price'] * rem_qty * fee_rate) + (exit_price * rem_qty * fee_rate)
                net_pnl_rem = gross_pnl_rem - fee_rem

                self.current_capital += net_pnl_rem
                self.daily_pnl += net_pnl_rem

                total_pnl = trade['realized_pnl'] + net_pnl_rem
                total_initial_notional = trade['entry_price'] * trade['initial_quantity']
                pnl_pct = (total_pnl / total_initial_notional) * 100.0

                self.trades_history.append({
                    'entry_time': trade['entry_time'],
                    'exit_time': current_bar['timestamp'],
                    'entry_price': trade['entry_price'],
                    'exit_price': exit_price,
                    'reason': "PARTIAL_TP1_THEN_SL/BEP",
                    'pnl': total_pnl,
                    'pnl_pct': pnl_pct,
                    'capital_after': self.current_capital
                })
                self.active_trade = None
                self.cooldown_until = current_bar['timestamp'] + timedelta(minutes=self.config.execution.cooldown_minutes)
                return

            if hit_tp2:
                rem_qty = trade['remaining_quantity']
                gross_pnl_rem = (tp2_price - trade['entry_price']) * rem_qty
                fee_rem = (trade['entry_price'] * rem_qty * fee_rate) + (tp2_price * rem_qty * fee_rate)
                net_pnl_rem = gross_pnl_rem - fee_rem

                self.current_capital += net_pnl_rem
                self.daily_pnl += net_pnl_rem

                total_pnl = trade['realized_pnl'] + net_pnl_rem
                total_initial_notional = trade['entry_price'] * trade['initial_quantity']
                pnl_pct = (total_pnl / total_initial_notional) * 100.0

                self.trades_history.append({
                    'entry_time': trade['entry_time'],
                    'exit_time': current_bar['timestamp'],
                    'entry_price': trade['entry_price'],
                    'exit_price': tp2_price,
                    'reason': "TAKE_PROFIT_ALL (TP1+TP2)",
                    'pnl': total_pnl,
                    'pnl_pct': pnl_pct,
                    'capital_after': self.current_capital
                })
                self.active_trade = None
                self.cooldown_until = current_bar['timestamp'] + timedelta(minutes=self.config.execution.cooldown_minutes)
                return

            # Trailing Stop Struktural untuk sisa 50% (>= 1.7R)
            current_profit = high_price - trade['entry_price']
            if current_profit >= (stop_distance * 1.7):
                recent_swing_low = df_window['low'].iloc[-15:].min()
                structural_trail_sl = recent_swing_low * 0.998
                if structural_trail_sl > trade['stop_loss'] and structural_trail_sl > trade['entry_price']:
                    trade['stop_loss'] = structural_trail_sl

    def get_metrics(self):
        total_trades = len(self.trades_history)
        if total_trades == 0:
            return {
                'trades': 0, 'win_rate': 0.0, 'profit_factor': 0.0,
                'pnl': 0.0, 'pnl_pct': 0.0, 'max_drawdown': 0.0
            }

        df_res = pd.DataFrame(self.trades_history)
        wins = df_res[df_res['pnl'] > 0]
        losses = df_res[df_res['pnl'] < 0]

        win_rate = (len(wins) / total_trades) * 100.0
        net_pnl = df_res['pnl'].sum()
        net_pnl_pct = (net_pnl / self.initial_capital) * 100.0

        gross_profit = wins['pnl'].sum() if not wins.empty else 0.0
        gross_loss = abs(losses['pnl'].sum()) if not losses.empty else 0.0
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else 999.0

        df_res['cum_capital'] = self.initial_capital + df_res['pnl'].cumsum()
        df_res['peak'] = df_res['cum_capital'].cummax()
        df_res['drawdown'] = (df_res['cum_capital'] - df_res['peak']) / df_res['peak']
        max_drawdown = df_res['drawdown'].min() * 100.0

        return {
            'trades': total_trades,
            'win_rate': win_rate,
            'profit_factor': profit_factor,
            'pnl': net_pnl,
            'pnl_pct': net_pnl_pct,
            'max_drawdown': max_drawdown,
            'df_res': df_res
        }

async def run_precise_comparison():
    symbols = ["BTC/USDT", "LINK/USDT", "ETH/USDT", "SOL/USDT"]
    start_date = "2024-05-01T00:00:00Z"
    end_date = "2024-11-01T00:00:00Z"

    results = []

    for sym in symbols:
        # 1. Baseline
        b_base = ComparativeBacktester(sym, start_date, end_date, use_partial_tp=False)
        await b_base.run()
        m_base = b_base.get_metrics()

        # 2. Partial TP
        b_part = ComparativeBacktester(sym, start_date, end_date, use_partial_tp=True)
        await b_part.run()
        m_part = b_part.get_metrics()

        results.append((sym, m_base, m_part))

    print("\n" + "="*85)
    print("HASIL KOMPARASI TERVERIFIKASI: BASELINE (SINGLE TP 2.5R) VS PARTIAL TP (1.5R + 2.5R)")
    print("="*85)

    for sym, base, part in results:
        print(f"\n--- PAIR: {sym} ---")
        print(f"{'Metrik':<20} | {'Baseline (Single TP 2.5R)':<26} | {'Partial TP (1.5R + 2.5R)':<26} | {'Perubahan'}")
        print("-" * 88)
        print(f"{'Total Trades':<20} | {base['trades']:<26} | {part['trades']:<26} | {part['trades'] - base['trades']}")
        print(f"{'Win Rate (%)':<20} | {base['win_rate']:<25.2f}% | {part['win_rate']:<25.2f}% | {part['win_rate'] - base['win_rate']:+.2f}%")
        print(f"{'Profit Factor':<20} | {base['profit_factor']:<26.2f} | {part['profit_factor']:<26.2f} | {part['profit_factor'] - base['profit_factor']:+.2f}")
        print(f"{'Net PnL ($)':<20} | ${base['pnl']:<25.2f} | ${part['pnl']:<25.2f} | ${part['pnl'] - base['pnl']:+.2f}")
        print(f"{'Net PnL (%)':<20} | {base['pnl_pct']:<25.2f}% | {part['pnl_pct']:<25.2f}% | {part['pnl_pct'] - base['pnl_pct']:+.2f}%")
        print(f"{'Max Drawdown (%)':<20} | {base['max_drawdown']:<25.2f}% | {part['max_drawdown']:<25.2f}% | {part['max_drawdown'] - base['max_drawdown']:+.2f}%")

    tot_base_pnl = sum(b['pnl'] for _, b, _ in results)
    tot_part_pnl = sum(p['pnl'] for _, _, p in results)
    print("\n" + "="*85)
    print(f"TOTAL PORTOFOLIO BASELINE   : ${tot_base_pnl:+.2f} (+{tot_base_pnl/10:.2f}%)")
    print(f"TOTAL PORTOFOLIO PARTIAL TP : ${tot_part_pnl:+.2f} (+{tot_part_pnl/10:.2f}%)")
    print(f"SELISIH PENINGKATAN PROFIT  : ${tot_part_pnl - tot_base_pnl:+.2f} ({(tot_part_pnl - tot_base_pnl)/10:+.2f}%)")
    print("="*85)

if __name__ == "__main__":
    asyncio.run(run_precise_comparison())
