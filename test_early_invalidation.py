"""
Test Early Invalidation Exit on historical data (2024-05-01 to 2024-11-01)
Mengevaluasi efek Cut Loss Dini (Early Invalidation) terhadap:
1. Average Loss size ($)
2. Total Net PnL ($)
3. Profit Factor & Win Rate
4. Max Drawdown
"""

import asyncio
import pandas as pd
from datetime import datetime, timedelta
from backtest import Backtester

class InvalidationBacktester(Backtester):
    def __init__(self, symbol: str, start_date: str, end_date: str, 
                 enable_invalidation: bool = False,
                 check_htf_ema: bool = True,
                 check_btc_filter: bool = True):
        super().__init__(symbol, start_date, end_date)
        self.enable_invalidation = enable_invalidation
        self.check_htf_ema = check_htf_ema
        self.check_btc_filter = check_btc_filter

    async def run(self):
        # Override run to check early invalidation when a new HTF candle closes
        df_htf_raw = await self.fetch_historical_data(self.config.strategy.higher_timeframe)
        df_ltf_raw = await self.fetch_historical_data(self.ltf)
        
        import indicators
        df_htf = indicators.prepare_htf_frame(
            df_htf_raw, 
            ema_period=self.config.strategy.ema_period, 
            tema_period=self.config.strategy.tema_period
        )
        df_ltf = indicators.prepare_ltf_frame(
            df_ltf_raw,
            atr_period=self.config.strategy.atr_period,
            stoch_k=self.config.strategy.stochastic_k_period,
            stoch_k_smooth=self.config.strategy.stochastic_k_smoothing,
            stoch_d=self.config.strategy.stochastic_d_period
        )

        df_htf.set_index('timestamp', inplace=True, drop=False)
        df_ltf.set_index('timestamp', inplace=True, drop=False)

        from backtest import parse_timeframe_delta
        htf_delta = parse_timeframe_delta(self.config.strategy.higher_timeframe)
        htf_close_times = df_htf.index + htf_delta
        
        htf_limit = self.config.strategy.htf_limit
        ltf_lookback = 200 
        
        last_htf_pos = -1
        last_btc_pos = -1
        context = None

        use_btc_filter = self.config.strategy.enable_btc_filter and (self.symbol != self.config.strategy.btc_filter_symbol)
        df_btc = None
        btc_close_times = None

        if use_btc_filter:
            btc_sym = self.config.strategy.btc_filter_symbol
            df_btc_raw = await self.fetch_historical_data(
                self.config.strategy.higher_timeframe, symbol=btc_sym
            )
            df_btc = indicators.prepare_htf_frame(
                df_btc_raw, 
                ema_period=self.config.strategy.ema_period, 
                tema_period=self.config.strategy.tema_period
            )
            df_btc.set_index('timestamp', inplace=True, drop=False)
            btc_close_times = df_btc.index + htf_delta

        for i in range(ltf_lookback, len(df_ltf)):
            current_ltf_time = df_ltf.index[i]
            self._check_daily_reset(current_ltf_time)
            
            df_ltf_window = df_ltf.iloc[i - ltf_lookback : i + 1]
            current_bar = df_ltf_window.iloc[-1]
            
            # 1. Evaluasi eksekusi normal
            self.simulate_execution(current_bar, df_ltf_window)
            
            htf_pos = htf_close_times.searchsorted(current_ltf_time, side='right') - 1
            if htf_pos < 50:
                continue

            # Update HTF context saat candle 1H baru selesai
            new_htf_bar = False
            if htf_pos != last_htf_pos:
                new_htf_bar = True
                start_pos = max(0, htf_pos - htf_limit + 1)
                df_htf_window = df_htf.iloc[start_pos : htf_pos + 1]
                context = self.strategy.analyze_higher_timeframe(self.symbol, df_htf_window)
                last_htf_pos = htf_pos

            # Update BTC status saat candle 1H BTC baru selesai
            if use_btc_filter and btc_close_times is not None:
                btc_pos = btc_close_times.searchsorted(current_ltf_time, side='right') - 1
                if btc_pos >= 1 and btc_pos != last_btc_pos:
                    df_btc_window = df_btc.iloc[max(0, btc_pos - 10) : btc_pos + 1]
                    self.strategy.update_btc_market_status(df_btc_window)
                    last_btc_pos = btc_pos

            # =================================================================
            # CHECK EARLY INVALIDATION EXIT (Cut Loss Dini saat Rezim Rusak)
            # =================================================================
            if self.active_trade and self.enable_invalidation and new_htf_bar:
                last_htf_candle = df_htf.iloc[htf_pos]
                
                # Syarat 1: Koin itu sendiri ditutup di bawah EMA 50 (1H)
                coin_htf_broken = self.check_htf_ema and (last_htf_candle['close'] < last_htf_candle['ema'])
                
                # Syarat 2: Untuk Altcoin, Induk BTC berbalik Bearish
                btc_broken = self.check_btc_filter and use_btc_filter and (not self.strategy.btc_market_bullish)

                if coin_htf_broken or btc_broken:
                    reason = "EARLY_INVALID_HTF_EMA" if coin_htf_broken else "EARLY_INVALID_BTC_BEARISH"
                    # Exit di open candle LTF saat ini (harga terkini saat sinyal breakdown terdeteksi)
                    exit_price = current_bar['open']
                    self.close_trade(exit_price, reason, current_ltf_time)
                    continue

            if self.active_trade:
                continue

            if self.circuit_breaker_active:
                continue

            if self.cooldown_until and current_ltf_time < self.cooldown_until:
                continue

            # Evaluasi Signal Entry baru
            signal = self.strategy.evaluate_lower_timeframe(self.symbol, df_ltf_window, context)
            if signal:
                plan = self.risk_manager.build_trade_plan(
                    symbol=self.symbol,
                    entry_price=signal.entry_price,
                    stop_loss_ref=signal.stop_loss_ref,
                    atr_value=signal.atr_value,
                    total_capital=self.current_capital
                )
                
                if plan:
                    slippage_rate = (self.config.execution.max_slippage_pct / 100.0) * 0.5
                    effective_entry_price = plan.entry_price * (1.0 + slippage_rate)
                    effective_quantity = plan.notional / effective_entry_price
                    
                    self.active_trade = {
                        'entry_time': current_ltf_time,
                        'entry_price': effective_entry_price,
                        'stop_loss': plan.stop_loss,
                        'initial_stop_loss': plan.stop_loss,
                        'take_profit': plan.take_profit,
                        'bep_trigger_price': plan.bep_trigger_price,
                        'quantity': effective_quantity,
                        'initial_quantity': effective_quantity,
                        'remaining_quantity': effective_quantity,
                        'bep_activated': False,
                        'tp1_executed': False,
                        'realized_pnl': 0.0,
                        'zone_id': signal.zone_id
                    }
                    self.strategy.mark_zone_traded(self.symbol, signal.zone_id)

        return self.get_stats()

    def get_stats(self):
        if not self.trades_history:
            return {
                'trades': 0, 'win_rate': 0.0, 'profit_factor': 0.0,
                'pnl': 0.0, 'pnl_pct': 0.0, 'avg_loss': 0.0, 'avg_win': 0.0, 'max_drawdown': 0.0
            }

        df_res = pd.DataFrame(self.trades_history)
        wins = df_res[df_res['pnl'] > 0]
        losses = df_res[df_res['pnl'] < 0]

        win_rate = (len(wins) / len(df_res)) * 100.0
        net_pnl = df_res['pnl'].sum()
        net_pnl_pct = (net_pnl / self.initial_capital) * 100.0

        gross_profit = wins['pnl'].sum() if not wins.empty else 0.0
        gross_loss = abs(losses['pnl'].sum()) if not losses.empty else 0.0
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else 999.0

        avg_win = wins['pnl'].mean() if not wins.empty else 0.0
        avg_loss = losses['pnl'].mean() if not losses.empty else 0.0

        df_res['cum_capital'] = self.initial_capital + df_res['pnl'].cumsum()
        df_res['peak'] = df_res['cum_capital'].cummax()
        df_res['drawdown'] = (df_res['cum_capital'] - df_res['peak']) / df_res['peak']
        max_drawdown = df_res['drawdown'].min() * 100.0

        return {
            'trades': len(df_res),
            'win_rate': win_rate,
            'profit_factor': profit_factor,
            'pnl': net_pnl,
            'pnl_pct': net_pnl_pct,
            'avg_win': avg_win,
            'avg_loss': avg_loss,
            'max_drawdown': max_drawdown,
            'trades_list': self.trades_history
        }

async def run_tests():
    symbols = ["BTC/USDT", "LINK/USDT", "ETH/USDT"]
    start_date = "2024-05-01T00:00:00Z"
    end_date = "2024-11-01T00:00:00Z"

    # Variations to test:
    # 1: Baseline (Partial TP, No Early Invalidation)
    # 2: Both (HTF Close < EMA50 OR BTC Bearish)
    # 3: Only BTC Bearish
    # 4: Only HTF Close < EMA50
    variants = [
        ("1. Tanpa Invalidation (Baseline)", False, False, False),
        ("2. Invalidation Penuh (HTF EMA + BTC)", True, True, True),
        ("3. Invalidation BTC Bearish Saja", True, False, True),
        ("4. Invalidation HTF EMA Saja", True, True, False),
    ]

    print("\n" + "="*95)
    print("ANALISIS EMPIRIS: PENGARUH EARLY INVALIDATION EXIT (MEI - NOV 2024)")
    print("="*95)

    for v_name, enable_inv, check_ema, check_btc in variants:
        print(f"\n>>> VARIANT: {v_name} <<<")
        tot_pnl = 0.0
        for sym in symbols:
            b = InvalidationBacktester(
                sym, start_date, end_date,
                enable_invalidation=enable_inv,
                check_htf_ema=check_ema,
                check_btc_filter=check_btc
            )
            await b.run()
            st = b.get_stats()
            tot_pnl += st['pnl']
            print(f"  [{sym:<9}] PnL: ${st['pnl']:>6.2f} ({st['pnl_pct']:>5.2f}%) | WR: {st['win_rate']:>5.1f}% | PF: {st['profit_factor']:>4.2f} | Avg Win: ${st['avg_win']:>5.2f} | Avg Loss: ${st['avg_loss']:>6.2f} | MaxDD: {st['max_drawdown']:>5.2f}%")
        print(f"  --> TOTAL PORTOFOLIO: ${tot_pnl:+.2f} (+{tot_pnl/10:.2f}%)")

if __name__ == "__main__":
    asyncio.run(run_tests())
