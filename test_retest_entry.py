"""
Simulasi Pengujian Empiris: Limit Order Retest Entry
Membandingkan strategi saat ini (Market Entry saat Close) vs Limit Order Retest Entry:
Variasi Level Retest:
1. Retest A: true_swing_high (Level Breakout S/R Flip)
2. Retest B: Midpoint antara Close dan true_swing_high: (close + true_swing_high) / 2
3. Retest C: Diskon ATR: close - (0.25 * atr)
4. Retest D: 50% Body Retracement dari candle breakout: close - 0.5 * (close - open)

Parameter Tambahan:
- Expiration timeout: 3, 5, 8 candle LTF.
- Pembatalan jika harga menembus SL sebelum terjemput.
"""

import os
import asyncio
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from config import BotConfig, load_config
import indicators
from backtest import parse_timeframe_delta
from zone_detection import detect_swing_points

def run_simulation(symbol: str, variant: str = "BASELINE", timeout_bars: int = 5):
    ltf = "5m" if symbol in ["BTC/USDT", "LINK/USDT"] else "15m"
    htf = "1h"
    start_date = "2024-05-01"
    end_date = "2024-11-01"
    
    data_dir = "backtest_data"
    htf_file = os.path.join(data_dir, f"{symbol.replace('/', '_')}_{htf}_{start_date}_{end_date}.csv")
    ltf_file = os.path.join(data_dir, f"{symbol.replace('/', '_')}_{ltf}_{start_date}_{end_date}.csv")
    
    if not os.path.exists(htf_file) or not os.path.exists(ltf_file):
        print(f"File cache not found for {symbol}")
        return None

    df_htf_raw = pd.read_csv(htf_file, parse_dates=['timestamp'])
    df_ltf_raw = pd.read_csv(ltf_file, parse_dates=['timestamp'])
    
    cfg = load_config()
    
    df_htf = indicators.prepare_htf_frame(
        df_htf_raw,
        ema_period=cfg.strategy.ema_period,
        tema_period=cfg.strategy.tema_period
    )
    df_ltf = indicators.prepare_ltf_frame(
        df_ltf_raw,
        atr_period=cfg.strategy.atr_period,
        stoch_k=cfg.strategy.stochastic_k_period,
        stoch_k_smooth=cfg.strategy.stochastic_k_smoothing,
        stoch_d=cfg.strategy.stochastic_d_period
    )
    
    df_htf.set_index('timestamp', inplace=True, drop=False)
    df_ltf.set_index('timestamp', inplace=True, drop=False)
    
    htf_delta = parse_timeframe_delta(htf)
    htf_close_times = df_htf.index + htf_delta
    
    use_btc_filter = cfg.strategy.enable_btc_filter and symbol != cfg.strategy.btc_filter_symbol
    df_btc = None
    btc_close_times = None
    if use_btc_filter:
        btc_file = os.path.join(data_dir, f"BTC_USDT_{htf}_{start_date}_{end_date}.csv")
        df_btc_raw = pd.read_csv(btc_file, parse_dates=['timestamp'])
        df_btc = indicators.prepare_htf_frame(
            df_btc_raw,
            ema_period=cfg.strategy.ema_period,
            tema_period=cfg.strategy.tema_period
        )
        df_btc.set_index('timestamp', inplace=True, drop=False)
        btc_close_times = df_btc.index + htf_delta

    from strategy import StrategyEngine
    from risk_management import RiskManager
    
    strat = StrategyEngine(cfg.strategy)
    risk_mgr = RiskManager(cfg.risk)
    
    ltf_lookback = 200
    last_htf_pos = -1
    last_btc_pos = -1
    context = None
    
    capital = 1000.0
    initial_capital = 1000.0
    daily_pnl = 0.0
    current_day = ""
    circuit_breaker_active = False
    cooldown_until = None
    
    active_trade = None
    pending_limit_order = None
    trades_history = []
    signals_count = 0
    filled_count = 0
    expired_count = 0
    cancelled_count = 0
    
    fee_rate = 0.00075
    slippage_market = 0.00025
    
    for i in range(ltf_lookback, len(df_ltf)):
        current_ltf_time = df_ltf.index[i]
        
        cur_day_str = current_ltf_time.strftime("%Y-%m-%d")
        if current_day != cur_day_str:
            current_day = cur_day_str
            daily_pnl = 0.0
            circuit_breaker_active = False
            
        df_ltf_window = df_ltf.iloc[i - ltf_lookback : i + 1]
        current_bar = df_ltf_window.iloc[-1]
        open_p = current_bar['open']
        high_p = current_bar['high']
        low_p = current_bar['low']
        close_p = current_bar['close']
        
        # --- 1. EVALUATE PENDING LIMIT ORDER (IF ANY) ---
        if pending_limit_order is not None:
            if i > pending_limit_order['expires_bar']:
                expired_count += 1
                strat.unmark_zone(symbol, pending_limit_order['zone_id'])
                pending_limit_order = None
            elif low_p <= pending_limit_order['stop_loss']:
                cancelled_count += 1
                strat.unmark_zone(symbol, pending_limit_order['zone_id'])
                pending_limit_order = None
            elif low_p <= pending_limit_order['limit_price']:
                filled_count += 1
                fill_price = pending_limit_order['limit_price']
                if open_p < fill_price:
                    fill_price = open_p
                
                plan = risk_mgr.build_trade_plan(
                    symbol=symbol,
                    entry_price=fill_price,
                    stop_loss_ref=pending_limit_order['stop_loss_ref'],
                    atr_value=pending_limit_order['atr_value'],
                    total_capital=capital
                )
                
                if plan:
                    qty = plan.notional / fill_price
                    stop_dist = plan.entry_price - plan.stop_loss
                    tp1_mult = getattr(cfg.risk, 'partial_tp_atr_multiplier', 1.5)
                    tp1_p = plan.entry_price + (stop_dist * tp1_mult)
                    active_trade = {
                        'entry_time': current_ltf_time,
                        'entry_price': fill_price,
                        'stop_loss': plan.stop_loss,
                        'initial_stop_loss': plan.stop_loss,
                        'take_profit': plan.take_profit,
                        'take_profit_1': tp1_p,
                        'take_profit_2': plan.take_profit,
                        'quantity': qty,
                        'initial_quantity': qty,
                        'remaining_quantity': qty,
                        'bep_activated': False,
                        'tp1_executed': False,
                        'realized_pnl': 0.0,
                        'zone_id': pending_limit_order['zone_id']
                    }
                pending_limit_order = None

        # --- 2. EVALUATE ACTIVE TRADE (IF ANY) ---
        if active_trade is not None:
            trade = active_trade
            stop_dist = trade['entry_price'] - trade['initial_stop_loss']
            tp1_price = trade['take_profit_1']
            tp2_price = trade['take_profit_2']
            bep_profit_pct = cfg.risk.bep_profit_pct
            
            htf_pos = htf_close_times.searchsorted(current_ltf_time, side='right') - 1
            if htf_pos >= 50 and htf_pos != last_htf_pos:
                df_htf_window = df_htf.iloc[max(0, htf_pos - cfg.strategy.htf_limit + 1) : htf_pos + 1]
                last_htf_row = df_htf_window.iloc[-1]
                
                if last_htf_row['close'] < last_htf_row['ema']:
                    exit_price = open_p
                    rem_qty = trade['remaining_quantity']
                    gross = (exit_price - trade['entry_price']) * rem_qty
                    fee = (trade['entry_price'] * rem_qty * fee_rate) + (exit_price * rem_qty * fee_rate)
                    net = gross - fee
                    capital += net
                    daily_pnl += net
                    tot_pnl = trade['realized_pnl'] + net
                    tot_notional = trade['entry_price'] * trade['initial_quantity']
                    trades_history.append({
                        'entry_time': trade['entry_time'],
                        'exit_time': current_ltf_time,
                        'pnl': tot_pnl,
                        'pnl_pct': (tot_pnl / tot_notional) * 100,
                        'reason': 'EARLY_INVALID_HTF_EMA'
                    })
                    active_trade = None
                    cooldown_until = current_ltf_time + timedelta(minutes=cfg.execution.cooldown_minutes)
                    continue

                if use_btc_filter and btc_close_times is not None:
                    btc_pos = btc_close_times.searchsorted(current_ltf_time, side='right') - 1
                    if btc_pos >= 1:
                        df_btc_win = df_btc.iloc[max(0, btc_pos - 10) : btc_pos + 1]
                        strat.update_btc_market_status(df_btc_win)
                        if not strat.btc_market_bullish:
                            exit_price = open_p
                            rem_qty = trade['remaining_quantity']
                            gross = (exit_price - trade['entry_price']) * rem_qty
                            fee = (trade['entry_price'] * rem_qty * fee_rate) + (exit_price * rem_qty * fee_rate)
                            net = gross - fee
                            capital += net
                            daily_pnl += net
                            tot_pnl = trade['realized_pnl'] + net
                            tot_notional = trade['entry_price'] * trade['initial_quantity']
                            trades_history.append({
                                'entry_time': trade['entry_time'],
                                'exit_time': current_ltf_time,
                                'pnl': tot_pnl,
                                'pnl_pct': (tot_pnl / tot_notional) * 100,
                                'reason': 'EARLY_INVALID_BTC_BEARISH'
                            })
                            active_trade = None
                            cooldown_until = current_ltf_time + timedelta(minutes=cfg.execution.cooldown_minutes)
                            continue

            if not trade['tp1_executed']:
                hit_tp1 = high_p >= tp1_price
                hit_sl = low_p <= trade['stop_loss']
                
                if hit_tp1 and hit_sl:
                    hit_sl = True
                    hit_tp1 = False
                
                if hit_sl:
                    exit_price = open_p if open_p < trade['stop_loss'] else trade['stop_loss'] * (1.0 - slippage_market)
                    gross = (exit_price - trade['entry_price']) * trade['quantity']
                    fee = (trade['entry_price'] * trade['quantity'] * fee_rate) + (exit_price * trade['quantity'] * fee_rate)
                    net = gross - fee
                    capital += net
                    daily_pnl += net
                    tot_notional = trade['entry_price'] * trade['quantity']
                    trades_history.append({
                        'entry_time': trade['entry_time'],
                        'exit_time': current_ltf_time,
                        'pnl': net,
                        'pnl_pct': (net / tot_notional) * 100,
                        'reason': 'STOP_LOSS'
                    })
                    active_trade = None
                    cooldown_until = current_ltf_time + timedelta(minutes=cfg.execution.sl_cooldown_minutes)
                    continue
                
                if hit_tp1:
                    close_qty = trade['quantity'] * 0.5
                    trade['remaining_quantity'] = trade['quantity'] - close_qty
                    trade['tp1_executed'] = True
                    gross1 = (tp1_price - trade['entry_price']) * close_qty
                    fee1 = (trade['entry_price'] * close_qty * fee_rate) + (tp1_price * close_qty * fee_rate)
                    net1 = gross1 - fee1
                    capital += net1
                    daily_pnl += net1
                    trade['realized_pnl'] = net1
                    
                    bep_level = trade['entry_price'] * (1.0 + (bep_profit_pct / 100.0))
                    trade['stop_loss'] = max(trade['stop_loss'], bep_level)
                    trade['bep_activated'] = True
                
                if not trade['bep_activated'] and (high_p >= trade['entry_price'] + (stop_dist * 1.1)):
                    bep_level = trade['entry_price'] * (1.0 + (bep_profit_pct / 100.0))
                    if bep_level > trade['stop_loss']:
                        trade['stop_loss'] = bep_level
                        trade['bep_activated'] = True
            else:
                hit_sl = low_p <= trade['stop_loss']
                hit_tp2 = high_p >= tp2_price
                
                if hit_sl:
                    exit_price = open_p if open_p < trade['stop_loss'] else trade['stop_loss'] * (1.0 - slippage_market)
                    rem_qty = trade['remaining_quantity']
                    gross = (exit_price - trade['entry_price']) * rem_qty
                    fee = (trade['entry_price'] * rem_qty * fee_rate) + (exit_price * rem_qty * fee_rate)
                    net = gross - fee
                    capital += net
                    daily_pnl += net
                    tot_pnl = trade['realized_pnl'] + net
                    tot_notional = trade['entry_price'] * trade['initial_quantity']
                    trades_history.append({
                        'entry_time': trade['entry_time'],
                        'exit_time': current_ltf_time,
                        'pnl': tot_pnl,
                        'pnl_pct': (tot_pnl / tot_notional) * 100,
                        'reason': 'PARTIAL_TP1_THEN_SL/BEP'
                    })
                    active_trade = None
                    cooldown_until = current_ltf_time + timedelta(minutes=cfg.execution.cooldown_minutes)
                    continue

                if hit_tp2:
                    rem_qty = trade['remaining_quantity']
                    gross = (tp2_price - trade['entry_price']) * rem_qty
                    fee = (trade['entry_price'] * rem_qty * fee_rate) + (tp2_price * rem_qty * fee_rate)
                    net = gross - fee
                    capital += net
                    daily_pnl += net
                    tot_pnl = trade['realized_pnl'] + net
                    tot_notional = trade['entry_price'] * trade['initial_quantity']
                    trades_history.append({
                        'entry_time': trade['entry_time'],
                        'exit_time': current_ltf_time,
                        'pnl': tot_pnl,
                        'pnl_pct': (tot_pnl / tot_notional) * 100,
                        'reason': 'TAKE_PROFIT_ALL (TP1+TP2)'
                    })
                    active_trade = None
                    cooldown_until = current_ltf_time + timedelta(minutes=cfg.execution.cooldown_minutes)
                    continue

                if (high_p - trade['entry_price']) >= (stop_dist * 1.7):
                    recent_swing_low = df_ltf_window['low'].iloc[-15:].min()
                    st_sl = recent_swing_low * 0.998
                    if st_sl > trade['stop_loss'] and st_sl > trade['entry_price']:
                        trade['stop_loss'] = st_sl

        if active_trade is not None or pending_limit_order is not None:
            continue
        if circuit_breaker_active:
            continue
        if cooldown_until and current_ltf_time < cooldown_until:
            continue

        htf_pos = htf_close_times.searchsorted(current_ltf_time, side='right') - 1
        if htf_pos < 50:
            continue

        if htf_pos != last_htf_pos:
            df_htf_win = df_htf.iloc[max(0, htf_pos - cfg.strategy.htf_limit + 1) : htf_pos + 1]
            context = strat.analyze_higher_timeframe(symbol, df_htf_win)
            last_htf_pos = htf_pos

        if use_btc_filter and btc_close_times is not None:
            btc_pos = btc_close_times.searchsorted(current_ltf_time, side='right') - 1
            if btc_pos >= 1 and btc_pos != last_btc_pos:
                df_btc_win = df_btc.iloc[max(0, btc_pos - 10) : btc_pos + 1]
                strat.update_btc_market_status(df_btc_win)
                last_btc_pos = btc_pos

        signal = strat.evaluate_lower_timeframe(symbol, df_ltf_window, context)
        if signal:
            signals_count += 1
            
            df_recent = detect_swing_points(df_ltf_window.tail(60).copy(), left_bars=3, right_bars=2)
            t_swing_high = df_recent['last_swing_high'].iloc[-2]
            if pd.isna(t_swing_high):
                t_swing_high = df_recent['high'].iloc[-20:-1].max()

            if variant == "BASELINE":
                limit_price = signal.entry_price * (1.0 + slippage_market)
                plan = risk_mgr.build_trade_plan(
                    symbol=symbol,
                    entry_price=limit_price,
                    stop_loss_ref=signal.stop_loss_ref,
                    atr_value=signal.atr_value,
                    total_capital=capital
                )
                if plan:
                    qty = plan.notional / limit_price
                    stop_dist = plan.entry_price - plan.stop_loss
                    tp1_mult = getattr(cfg.risk, 'partial_tp_atr_multiplier', 1.5)
                    tp1_p = plan.entry_price + (stop_dist * tp1_mult)
                    active_trade = {
                        'entry_time': current_ltf_time,
                        'entry_price': limit_price,
                        'stop_loss': plan.stop_loss,
                        'initial_stop_loss': plan.stop_loss,
                        'take_profit': plan.take_profit,
                        'take_profit_1': tp1_p,
                        'take_profit_2': plan.take_profit,
                        'quantity': qty,
                        'initial_quantity': qty,
                        'remaining_quantity': qty,
                        'bep_activated': False,
                        'tp1_executed': False,
                        'realized_pnl': 0.0,
                        'zone_id': signal.zone_id
                    }
                    strat.mark_zone_traded(symbol, signal.zone_id)
            else:
                breakout_close = signal.entry_price
                breakout_open = current_bar['open']
                atr_val = signal.atr_value
                
                if variant == "RETEST_SWING_HIGH":
                    limit_price = t_swing_high
                elif variant == "RETEST_MIDPOINT":
                    limit_price = (breakout_close + t_swing_high) / 2.0
                elif variant == "RETEST_ATR_OFFSET":
                    limit_price = breakout_close - (0.25 * atr_val)
                elif variant == "RETEST_BODY_50":
                    limit_price = breakout_close - 0.5 * (breakout_close - breakout_open)
                else:
                    limit_price = breakout_close
                
                limit_price = min(limit_price, breakout_close * 0.9995)
                if limit_price <= signal.stop_loss_ref:
                    limit_price = (breakout_close + signal.stop_loss_ref) / 2.0
                
                est_sl = signal.stop_loss_ref - (0.5 * atr_val)
                
                pending_limit_order = {
                    'limit_price': limit_price,
                    'stop_loss_ref': signal.stop_loss_ref,
                    'stop_loss': est_sl,
                    'atr_value': signal.atr_value,
                    'zone_id': signal.zone_id,
                    'created_bar': i,
                    'expires_bar': i + timeout_bars
                }
                strat.mark_zone_traded(symbol, signal.zone_id)

    tot_trades = len(trades_history)
    if tot_trades == 0:
        return {
            'symbol': symbol,
            'variant': variant,
            'timeout': timeout_bars,
            'signals': signals_count,
            'trades': 0,
            'win_rate': 0.0,
            'net_pnl': 0.0,
            'pnl_pct': 0.0,
            'profit_factor': 0.0,
            'max_dd': 0.0,
            'filled': filled_count,
            'expired': expired_count,
            'cancelled': cancelled_count
        }

    df_res = pd.DataFrame(trades_history)
    wins = df_res[df_res['pnl'] > 0]
    losses = df_res[df_res['pnl'] <= 0]
    win_rate = (len(wins) / tot_trades) * 100.0
    tot_pnl = df_res['pnl'].sum()
    gross_win = wins['pnl'].sum() if len(wins) > 0 else 0.0
    gross_loss = abs(losses['pnl'].sum()) if len(losses) > 0 else 0.0
    pf = (gross_win / gross_loss) if gross_loss > 0 else 999.0
    
    equity = [initial_capital]
    for p in df_res['pnl']:
        equity.append(equity[-1] + p)
    peak = equity[0]
    max_dd = 0.0
    for e in equity:
        if e > peak:
            peak = e
        dd = (e - peak) / peak * 100.0
        if dd < max_dd:
            max_dd = dd

    return {
        'symbol': symbol,
        'variant': variant,
        'timeout': timeout_bars,
        'signals': signals_count,
        'trades': tot_trades,
        'win_rate': win_rate,
        'net_pnl': tot_pnl,
        'pnl_pct': (tot_pnl / initial_capital) * 100.0,
        'profit_factor': pf,
        'max_dd': max_dd,
        'filled': filled_count,
        'expired': expired_count,
        'cancelled': cancelled_count
    }

if __name__ == "__main__":
    symbols = ["BTC/USDT", "LINK/USDT", "ETH/USDT"]
    variants = [
        ("BASELINE", 0),
        ("RETEST_SWING_HIGH", 5),
        ("RETEST_MIDPOINT", 5),
        ("RETEST_ATR_OFFSET", 5),
        ("RETEST_BODY_50", 5),
        ("RETEST_MIDPOINT", 3),
        ("RETEST_MIDPOINT", 8),
    ]
    
    all_results = []
    for var, timeout in variants:
        print(f"\nEvaluating Variant: {var} (Timeout: {timeout} bars)...")
        for sym in symbols:
            res = run_simulation(sym, variant=var, timeout_bars=timeout)
            if res:
                all_results.append(res)
                print(f"[{sym:9}] Trades: {res['trades']:2d} (Sgnl: {res['signals']:2d}, Fill: {res['filled']:2d}, Exp: {res['expired']:2d}), "
                      f"WR: {res['win_rate']:5.1f}%, PnL: ${res['net_pnl']:+6.2f} ({res['pnl_pct']:+5.2f}%), "
                      f"PF: {res['profit_factor']:4.2f}, MaxDD: {res['max_dd']:5.2f}%")
                
    df_summary = pd.DataFrame(all_results)
    print("\n" + "="*80)
    print("RINGKASAN TOTAL PERFORMANSI PORTOFOLIO:")
    print("="*80)
    for (var, timeout), group in df_summary.groupby(['variant', 'timeout']):
        tot_pnl = group['net_pnl'].sum()
        avg_pf = group['profit_factor'].mean()
        tot_trades = group['trades'].sum()
        worst_dd = group['max_dd'].min()
        print(f"Variant: {var:20} (Timeout {timeout:2d} bars) -> Total PnL: ${tot_pnl:+6.2f}, Tot Trades: {tot_trades:2d}, Avg PF: {avg_pf:4.2f}, Worst DD: {worst_dd:5.2f}%")
