import os
import sys
import asyncio
import argparse
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional

# Import modul internal
import indicators
import ccxt.async_support as ccxt
from config import load_config
from strategy import StrategyEngine, TradeSignal
from risk_management import RiskManager, TradePlan

def parse_timeframe_delta(tf: str) -> timedelta:
    """Mengonversi string timeframe (1m, 5m, 1h, 4h, 1d) menjadi objek timedelta."""
    tf = tf.lower()
    if tf.endswith('m'):
        return timedelta(minutes=int(tf[:-1]))
    elif tf.endswith('h'):
        return timedelta(hours=int(tf[:-1]))
    elif tf.endswith('d'):
        return timedelta(days=int(tf[:-1]))
    return timedelta(hours=1)

class Backtester:
    """
    Backtester presisi tinggi untuk strategi SMC Multi-Timeframe Spot.
    
    Fitur:
    1. Bebas Look-Ahead Bias: HTF hanya menggunakan candle yang telah resmi tertutup.
    2. Realistic Fill & Slippage Modeling: Memperhitungkan slippage eksekusi dan gap down.
    3. Intra-Bar Worst-Case Fix: Jika TP dan SL tersentuh di bar yang sama, diasumsikan terkena SL.
    4. Structural Trailing Stop Simulation: Mensimulasikan pergeseran SL ke swing low secara pasif.
    5. Circuit Breaker & Dynamic Cooldown.
    """
    def __init__(self, symbol: str, start_date: str, end_date: str, config_path: str = "config.json", timeframe: str = None):
        self.symbol = symbol
        self.start_date = start_date
        self.end_date = end_date
        self.config = load_config(config_path)
        
        self.strategy = StrategyEngine(self.config.strategy)
        self.risk_manager = RiskManager(self.config.risk)
        
        # Paksa seluruh LTF minimal ke 15m (mencegah fee erosion dan fakeouts di 5m)
        if timeframe:
            self.ltf = timeframe
        else:
            self.ltf = "15m"
        
        self.initial_capital = self.config.execution.paper_cash
        self.current_capital = self.initial_capital
        
        self.trades_history = []
        self.active_trade = None
        self.pending_limit_order = None
        self.cooldown_until = None
        # Optional walk-forward boundary. Data sebelum tanggal ini tetap dipakai
        # sebagai warm-up indikator, tetapi entry baru belum diizinkan.
        self.trade_start_date = None
        
        # State Circuit Breaker Harian
        self.current_day = None
        self.daily_pnl = 0.0
        self.circuit_breaker_active = False
        
        # Streak Breaker (Consecutive Losses Cooldown)
        self.consecutive_losses = 0

        # Folder cache data CSV
        self.data_dir = "backtest_data"
        os.makedirs(self.data_dir, exist_ok=True)

    def _check_daily_reset(self, current_time: datetime):
        """Mereset PNL Harian dan Circuit Breaker saat hari berganti (UTC)."""
        current_date_str = current_time.strftime("%Y-%m-%d")
        if self.current_day != current_date_str:
            self.current_day = current_date_str
            self.daily_pnl = 0.0
            self.circuit_breaker_active = False

    async def fetch_historical_data(self, timeframe: str, symbol: str = None) -> pd.DataFrame:
        """Mengunduh data historis secara terpaginasi jika belum ada di cache CSV."""
        target_symbol = symbol or self.symbol
        filename = f"{target_symbol.replace('/', '_')}_{timeframe}_{self.start_date[:10]}_{self.end_date[:10]}.csv"
        filepath = os.path.join(self.data_dir, filename)
        
        if os.path.exists(filepath):
            print(f"[*] Memuat data {timeframe} dari cache CSV: {filename}")
            df = pd.read_csv(filepath, parse_dates=['timestamp'])
            return df

        print(f"[*] Mengunduh data {timeframe} dari Binance ({self.start_date[:10]} s/d {self.end_date[:10]})...")
        exchange = ccxt.binance({'enableRateLimit': True})
        
        since = exchange.parse8601(self.start_date)
        until = exchange.parse8601(self.end_date)
        all_ohlcv = []
        
        while since < until:
            try:
                ohlcv = await exchange.fetch_ohlcv(target_symbol, timeframe, since, limit=1000)
                if not ohlcv:
                    break
                all_ohlcv.extend(ohlcv)
                since = ohlcv[-1][0] + 1
                await asyncio.sleep(0.5)
            except Exception as e:
                print(f"Error fetching data: {e}")
                await asyncio.sleep(2)

        await exchange.close()

        df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
        df = df[df['timestamp'] <= pd.to_datetime(self.end_date)]
        
        df.to_csv(filepath, index=False)
        print(f"[+] Data {timeframe} berhasil diunduh dan disimpan.")
        return df

    def simulate_execution(self, current_bar: pd.Series, df_window: pd.DataFrame):
        """Mengevaluasi posisi aktif terhadap pergerakan candle saat ini."""
        if not self.active_trade:
            return

        trade = self.active_trade
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

        enable_partial = getattr(self.config.risk, 'enable_partial_tp', True)
        tp1_mult = getattr(self.config.risk, 'partial_tp_atr_multiplier', 1.5)
        tp1_ratio = getattr(self.config.risk, 'partial_tp_ratio', 0.5)

        if not enable_partial:
            # Mode Single TP 2.5R standar (Fallback jika partial TP dimatikan di config)
            hit_tp = high_price >= trade['take_profit']
            hit_sl = low_price <= trade['stop_loss']

            if hit_tp and hit_sl:
                exit_price = open_price if open_price < trade['stop_loss'] else trade['stop_loss'] * (1.0 - slippage_rate)
                self.close_trade(exit_price, "STOP_LOSS", current_bar['timestamp'])
                return
            if hit_sl:
                exit_price = open_price if open_price < trade['stop_loss'] else trade['stop_loss'] * (1.0 - slippage_rate)
                self.close_trade(exit_price, "STOP_LOSS", current_bar['timestamp'])
                return
            if hit_tp:
                self.close_trade(trade['take_profit'], "TAKE_PROFIT", current_bar['timestamp'])
                return

            bep_mult = getattr(self.config.risk, 'bep_trigger_atr_multiplier', 1.1)
            if not trade.get('bep_activated', False) and (high_price >= trade['entry_price'] + (stop_distance * bep_mult)):
                # Tidak lagi memindahkan SL ke BEP murni, tetapi gunakan trailing 1.5 ATR (Dilonggarkan)
                atr_val = trade.get('atr_value', stop_distance / 1.2)
                atr_trail_sl = high_price - (1.5 * atr_val)
                if atr_trail_sl > trade['stop_loss']:
                    trade['stop_loss'] = atr_trail_sl
                    trade['bep_activated'] = True

            current_profit = high_price - trade['entry_price']
            if current_profit >= (stop_distance * 1.5):
                atr_val = trade.get('atr_value', stop_distance / 1.2)
                atr_trail_sl = high_price - (1.5 * atr_val)
                if atr_trail_sl > trade['stop_loss'] and atr_trail_sl > trade['entry_price']:
                    trade['stop_loss'] = atr_trail_sl
            return

        # =================================================================
        # LOGIKA TERVERIFIKASI: PARTIAL TAKE-PROFIT (TP1 1.5R 50% + TP2 2.5R 50%)
        # =================================================================
        tp1_price = trade['entry_price'] + (stop_distance * tp1_mult)
        tp2_price = trade['take_profit']

        hit_sl = low_price <= trade['stop_loss']
        hit_tp1 = high_price >= tp1_price
        hit_tp2 = high_price >= tp2_price

        if not trade['tp1_executed']:
            # Fase Sebelum TP1
            if hit_sl and hit_tp1:
                exit_price = open_price if open_price < trade['stop_loss'] else trade['stop_loss'] * (1.0 - slippage_rate)
                self.close_trade(exit_price, "STOP_LOSS", current_bar['timestamp'])
                return

            if hit_sl:
                exit_price = open_price if open_price < trade['stop_loss'] else trade['stop_loss'] * (1.0 - slippage_rate)
                self.close_trade(exit_price, "STOP_LOSS", current_bar['timestamp'])
                return

            if hit_tp1:
                # Eksekusi TP1 (50% posisi)
                close_qty = trade['initial_quantity'] * tp1_ratio
                trade['remaining_quantity'] -= close_qty
                trade['tp1_executed'] = True

                gross_pnl_tp1 = (tp1_price - trade['entry_price']) * close_qty
                entry_fee = (trade['entry_price'] * close_qty * fee_rate)
                exit_fee = (tp1_price * close_qty * fee_rate)
                net_pnl_tp1 = gross_pnl_tp1 - (entry_fee + exit_fee)

                self.current_capital += net_pnl_tp1
                self.daily_pnl += net_pnl_tp1
                trade['realized_pnl'] = net_pnl_tp1

                # Mulai Terapkan Trailing Stop pasca TP1 (Jarak 1.5 ATR - Dilonggarkan)
                atr_val = trade.get('atr_value', stop_distance / 1.2)
                atr_trail_sl = tp1_price - (1.5 * atr_val)
                if atr_trail_sl > trade['stop_loss']:
                    trade['stop_loss'] = atr_trail_sl
                    trade['bep_activated'] = True

                # Cek jika bar yang sama langsung tembus TP2 (2.5R)
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

            # Jika belum sentuh TP1, cek aktivasi Trailing biasa di 1.5R
            bep_mult = getattr(self.config.risk, 'bep_trigger_atr_multiplier', 1.5)
            if not trade.get('bep_activated', False) and (high_price >= trade['entry_price'] + (stop_distance * bep_mult)):
                atr_val = trade.get('atr_value', stop_distance / 1.2)
                atr_trail_sl = high_price - (1.5 * atr_val)
                if atr_trail_sl > trade['stop_loss']:
                    trade['stop_loss'] = atr_trail_sl
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

            # Trailing Stop ATR untuk sisa posisi pasca TP1 (1.5 ATR distance)
            current_profit = high_price - trade['entry_price']
            if current_profit >= (stop_distance * 1.5):
                atr_val = trade.get('atr_value', stop_distance / 1.2)
                atr_trail_sl = high_price - (1.5 * atr_val)
                if atr_trail_sl > trade['stop_loss']:
                    trade['stop_loss'] = atr_trail_sl

    def close_trade(self, exit_price: float, reason: str, exit_time: datetime):
        """Menutup posisi, menghitung fee dua arah, dan memperbarui state."""
        trade = self.active_trade
        rem_qty = trade.get('remaining_quantity', trade['quantity'])
        gross_pnl = (exit_price - trade['entry_price']) * rem_qty

        # Fee Binance Spot standar bolak-balik
        fee_rate = (self.config.risk.estimated_exchange_fee_pct / 100.0) / 2.0
        entry_fee = (trade['entry_price'] * rem_qty) * fee_rate
        exit_fee = (exit_price * rem_qty) * fee_rate
        total_fee = entry_fee + exit_fee

        net_pnl = gross_pnl - total_fee
        total_net_pnl = trade.get('realized_pnl', 0.0) + net_pnl
        init_notional = trade['entry_price'] * trade.get('initial_quantity', trade['quantity'])
        pnl_pct = (total_net_pnl / init_notional) * 100.0 if init_notional > 0 else 0.0

        # Update Modal & PnL Harian
        self.current_capital += net_pnl
        self.daily_pnl += net_pnl

        # Evaluasi Circuit Breaker Dinamis
        max_daily_loss_amount = self.current_capital * (self.config.risk.max_daily_loss_pct / 100.0)
        target_daily_profit = self.current_capital * (self.config.risk.max_daily_profit_pct / 100.0)

        if self.daily_pnl <= -max_daily_loss_amount:
            self.circuit_breaker_active = True
        elif self.daily_pnl >= target_daily_profit:
            self.circuit_breaker_active = True

        final_reason = f"PARTIAL_TP1_THEN_{reason}" if trade.get('tp1_executed', False) else reason
        self.trades_history.append({
            'entry_time': trade['entry_time'],
            'exit_time': exit_time,
            'entry_price': trade['entry_price'],
            'exit_price': exit_price,
            'reason': final_reason,
            'pnl': total_net_pnl,
            'pnl_pct': pnl_pct,
            'capital_after': self.current_capital
        })

        self.active_trade = None

        # Update Streak Breaker
        if total_net_pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

        # Cooldown Dinamis & Streak Breaker
        max_losses = getattr(self.config.execution, 'max_consecutive_losses', 2)
        streak_hours = getattr(self.config.execution, 'streak_cooldown_hours', 24)

        if self.consecutive_losses >= max_losses:
            cd_minutes = streak_hours * 60
            print(f"[{exit_time}] Streak Breaker: {max_losses} losses beruntun. Cooldown selama {streak_hours} jam.")
            self.consecutive_losses = 0  # Reset agar setelah cooldown bisa trade normal lagi
        elif reason == "STOP_LOSS":
            cd_minutes = self.config.execution.sl_cooldown_minutes
        else:
            cd_minutes = self.config.execution.cooldown_minutes
            
        self.cooldown_until = exit_time + timedelta(minutes=cd_minutes)

    async def run(self):
        """Loop Utama Simulasi Backtest."""
        print(f"\n{'='*55}\nMEMULAI BACKTEST: {self.symbol} [HTF: {self.config.strategy.higher_timeframe}, LTF: {self.ltf}]\n{'='*55}")
        df_htf_raw = await self.fetch_historical_data(self.config.strategy.higher_timeframe)
        df_ltf_raw = await self.fetch_historical_data(self.ltf)
        
        print("\n[*] Melakukan pre-calculation indikator...")
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

        htf_delta = parse_timeframe_delta(self.config.strategy.higher_timeframe)
        htf_close_times = df_htf.index + htf_delta
        
        htf_limit = self.config.strategy.htf_limit
        ltf_lookback = 200 
        
        last_htf_pos = -1
        context = None

        # BTC Market Filter Initialization untuk Altcoins
        use_btc_filter = (
            self.config.strategy.enable_btc_filter and 
            self.symbol != self.config.strategy.btc_filter_symbol
        )
        df_btc = None
        btc_close_times = None
        last_btc_pos = -1

        if use_btc_filter:
            btc_sym = self.config.strategy.btc_filter_symbol
            print(f"[*] BTC Market Filter aktif. Memuat data 1h {btc_sym}...")
            df_btc_raw = await self.fetch_historical_data(
                self.config.strategy.higher_timeframe, symbol=btc_sym
            )
            df_btc = indicators.prepare_htf_frame(
                df_btc_raw, 
                ema_period=self.config.strategy.ema_period, 
                tema_period=self.config.strategy.tema_period
            )
            df_btc['timestamp'] = pd.to_datetime(df_btc['timestamp'], utc=True)
            df_btc.set_index('timestamp', inplace=True, drop=False)
            btc_close_times = df_btc.index + htf_delta

        for i in range(ltf_lookback, len(df_ltf)):
            current_ltf_time = df_ltf.index[i]
            
            # 1. Reset Circuit Breaker saat hari baru
            self._check_daily_reset(current_ltf_time)
            
            df_ltf_window = df_ltf.iloc[i - ltf_lookback : i + 1]
            current_bar = df_ltf_window.iloc[-1]
            
            # 2. Simulasi posisi aktif
            self.simulate_execution(current_bar, df_ltf_window)

            # SINKRONISASI BEBAS LOOK-AHEAD BIAS:
            htf_pos = htf_close_times.searchsorted(current_ltf_time, side='right') - 1
            if htf_pos < 50:
                continue

            # Update status HTF saat candle 1H baru selesai
            new_htf_bar = False
            if htf_pos != last_htf_pos:
                new_htf_bar = True
                start_pos = max(0, htf_pos - htf_limit + 1)
                df_htf_window = df_htf.iloc[start_pos : htf_pos + 1]
                context = self.strategy.analyze_higher_timeframe(self.symbol, df_htf_window)
                last_htf_pos = htf_pos

            # Sinkronisasi status BTC Market Filter
            df_btc_window_for_rs = None  # Akan diisi jika use_btc_filter aktif
            if use_btc_filter and btc_close_times is not None:
                btc_pos = btc_close_times.searchsorted(current_ltf_time, side='right') - 1
                if btc_pos >= 1 and btc_pos != last_btc_pos:
                    df_btc_window = df_btc.iloc[max(0, btc_pos - 10) : btc_pos + 1]
                    self.strategy.update_btc_market_status(df_btc_window)
                    last_btc_pos = btc_pos
                # Ambil window BTC HTF untuk RS filter (bebas look-ahead bias: pakai btc_pos saat ini)
                if btc_pos >= 1:
                    rs_start = max(0, btc_pos - self.config.strategy.rs_ema_period - 10)
                    df_btc_window_for_rs = df_btc.iloc[rs_start : btc_pos + 1]

            # 3. EARLY INVALIDATION CHECK (Proteksi Dinamis)
            # Jika BTC crash atau koin turun di bawah EMA HTF, kita TIDAK menutup posisi langsung.
            # Sebaliknya, jika posisi sedang profit, kita geser Stop Loss ke BEP.
            enable_inval = getattr(self.config.strategy, 'enable_early_invalidation', True)
            if self.active_trade and enable_inval and new_htf_bar:
                last_htf_candle = df_htf.iloc[htf_pos]
                check_htf_ema = getattr(self.config.strategy, 'invalidation_check_htf_ema', True)
                coin_htf_broken = check_htf_ema and (last_htf_candle['close'] < last_htf_candle['ema'])

                check_btc = getattr(self.config.strategy, 'invalidation_check_btc_filter', True)
                is_altcoin = "BTC" not in self.symbol.upper()
                btc_broken = check_btc and is_altcoin and use_btc_filter and (not self.strategy.btc_market_bullish)

                if coin_htf_broken or btc_broken:
                    trade = self.active_trade
                    current_price = current_bar['close']
                    bep_profit_pct = getattr(self.config.risk, 'bep_profit_pct', 0.15)
                    bep_level = trade['entry_price'] * (1.0 + (bep_profit_pct / 100.0))

                    # Hanya pindahkan SL ke BEP jika harga SAAT INI sudah di atas BEP level
                    if current_price > bep_level and not trade.get('bep_activated', False):
                        if bep_level > trade['stop_loss']:
                            trade['stop_loss'] = bep_level
                            trade['bep_activated'] = True

            # 4. Evaluasi Pending Limit Retest Order (jika ada)
            if self.pending_limit_order is not None:
                pending = self.pending_limit_order
                low_p = current_bar['low']
                open_p = current_bar['open']

                # Timeout / Expired
                if i > pending['expires_bar']:
                    self.strategy.unmark_zone(self.symbol, pending['zone_id'])
                    self.pending_limit_order = None
                # Invalidation SL sebelum terjemput
                elif low_p <= pending['stop_loss']:
                    self.strategy.unmark_zone(self.symbol, pending['zone_id'])
                    self.pending_limit_order = None
                # Terjemput (FILLED)!
                elif low_p <= pending['limit_price']:
                    fill_price = pending['limit_price']
                    if open_p < fill_price:
                        fill_price = open_p
                    
                    plan = self.risk_manager.build_trade_plan(
                        symbol=self.symbol,
                        entry_price=fill_price,
                        stop_loss_ref=pending['stop_loss_ref'],
                        atr_value=pending['atr_value'],
                        total_capital=self.current_capital
                    )
                    if plan:
                        effective_quantity = plan.notional / fill_price
                        self.active_trade = {
                            'entry_time': current_ltf_time,
                            'entry_price': fill_price,
                            'stop_loss': plan.stop_loss,
                            'initial_stop_loss': plan.stop_loss,
                            'take_profit': plan.take_profit,
                            'take_profit_1': plan.take_profit_1,
                            'take_profit_2': plan.take_profit_2,
                            'bep_trigger_price': plan.bep_trigger_price,
                            'quantity': effective_quantity,
                            'initial_quantity': effective_quantity,
                            'remaining_quantity': effective_quantity,
                            'bep_activated': False,
                            'tp1_executed': False,
                            'realized_pnl': 0.0,
                            'zone_id': pending['zone_id']
                        }
                    self.pending_limit_order = None

            if self.active_trade or self.pending_limit_order:
                continue

            if self.circuit_breaker_active:
                continue

            if self.cooldown_until and current_ltf_time < self.cooldown_until:
                continue

            if self.trade_start_date is not None:
                trade_start = pd.Timestamp(self.trade_start_date)
                if trade_start.tzinfo is None:
                    trade_start = trade_start.tz_localize('UTC')
                else:
                    trade_start = trade_start.tz_convert('UTC')
                if current_ltf_time < trade_start:
                    continue
            
            # 5. Evaluasi LTF (Trigger Micro BOS)
            # Teruskan df_htf_window (HTF Alt) + df_btc_window_for_rs (HTF BTC) untuk RS Filter
            df_htf_for_rs = df_htf.iloc[max(0, htf_pos - self.config.strategy.rs_ema_period - 10) : htf_pos + 1] if htf_pos >= 0 else None
            signal = self.strategy.evaluate_lower_timeframe(
                self.symbol, df_ltf_window, context,
                df_htf=df_htf_for_rs,
                df_btc_htf=df_btc_window_for_rs
            )
            
            # 6. Eksekusi Sinyal (Hybrid: Limit Retest untuk Altcoin vs Market Direct untuk BTC)
            if signal:
                if signal.entry_type == "LIMIT_RETEST":
                    est_sl = signal.stop_loss_ref - (0.5 * signal.atr_value)
                    self.pending_limit_order = {
                        'limit_price': signal.entry_price,
                        'stop_loss_ref': signal.stop_loss_ref,
                        'stop_loss': est_sl,
                        'atr_value': signal.atr_value,
                        'zone_id': signal.zone_id,
                        'created_bar': i,
                        'expires_bar': i + signal.timeout_bars
                    }
                    self.strategy.mark_zone_traded(self.symbol, signal.zone_id)
                else:
                    # Direct Market Entry (BTC)
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
                            'take_profit_1': plan.take_profit_1,
                            'take_profit_2': plan.take_profit_2,
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

        self.print_results()

    def print_results(self):
        """Menghitung dan mencetak statistik performa lengkap."""
        print(f"\n{'='*55}\nHASIL BACKTEST TERVERIFIKASI: {self.symbol} (LTF: {self.ltf})\n{'='*55}")
        
        total_trades = len(self.trades_history)
        if total_trades == 0:
            print("Tidak ada trade yang tereksekusi selama periode ini.")
            return

        df_res = pd.DataFrame(self.trades_history)
        
        wins = df_res[df_res['pnl'] > 0]
        losses = df_res[df_res['pnl'] <= 0]
        win_count = len(wins)
        loss_count = len(losses)
        win_rate = (win_count / total_trades) * 100
        
        total_pnl = df_res['pnl'].sum()
        roi_pct = (total_pnl / self.initial_capital) * 100
        
        gross_profit = wins['pnl'].sum() if not wins.empty else 0.0
        gross_loss = abs(losses['pnl'].sum()) if not losses.empty else 0.0
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float('inf')
        
        avg_win = wins['pnl'].mean() if not wins.empty else 0.0
        avg_loss = losses['pnl'].mean() if not losses.empty else 0.0
        
        max_capital = df_res['capital_after'].cummax()
        drawdowns = (df_res['capital_after'] - max_capital) / max_capital
        max_drawdown = drawdowns.min() * 100

        print(f"Periode         : {self.start_date[:10]} s/d {self.end_date[:10]}")
        print(f"Modal Awal      : ${self.initial_capital:.2f}")
        print(f"Modal Akhir     : ${self.current_capital:.2f}")
        print(f"Total Net PnL   : ${total_pnl:.2f} ({roi_pct:.2f}%)")
        print(f"Profit Factor   : {profit_factor:.2f}")
        print(f"Total Trades    : {total_trades}")
        print(f"Win Rate        : {win_rate:.2f}% ({win_count} Menang / {loss_count} Kalah)")
        print(f"Rata-rata Win   : ${avg_win:.2f}")
        print(f"Rata-rata Loss  : ${avg_loss:.2f}")
        print(f"Max Drawdown    : {max_drawdown:.2f}%")
        print(f"\nRincian 10 Trade Terakhir:")
        print(df_res[['entry_time', 'reason', 'pnl', 'pnl_pct']].tail(10).to_string(index=False))

def main():
    parser = argparse.ArgumentParser(description="Precision SMC Spot Backtester")
    parser.add_argument("--symbol", type=str, default="BTC/USDT", help="Trading pair symbol (default: BTC/USDT)")
    parser.add_argument("--start", type=str, default="2024-11-01T00:00:00Z", help="Start datetime ISO format")
    parser.add_argument("--end", type=str, default="2025-11-01T00:00:00Z", help="End datetime ISO format")
    parser.add_argument("--config", type=str, default="config.json", help="Path to config.json")
    parser.add_argument("--timeframe", type=str, default=None, help="Override LTF timeframe (misal 5m atau 15m).")
    parser.add_argument("--htf", type=str, default=None, help="Override HTF timeframe (misal 1h atau 4h).")
    parser.add_argument("--ltf", type=str, default=None, help="Alias for --timeframe.")
    args = parser.parse_args()

    # Apply aliases
    ltf_val = args.ltf if args.ltf else args.timeframe

    backtester = Backtester(
        symbol=args.symbol,
        start_date=args.start,
        end_date=args.end,
        config_path=args.config,
        timeframe=ltf_val
    )
    
    if args.htf:
        backtester.config.strategy.higher_timeframe = args.htf

    try:
        asyncio.run(backtester.run())
    except KeyboardInterrupt:
        print("\nBacktest dihentikan oleh pengguna.")

if __name__ == "__main__":
    main()
