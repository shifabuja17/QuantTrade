"""
Simulasi Pengujian Empiris: Dynamic Risk Sizing Berdasarkan Tingkat Keyakinan (Trend Conviction)
Membandingkan:
1. Baseline: Fixed 2.0% Risk per trade
2. Dynamic Risk Model 1 (Binary A+ vs B):
   - Grade A+ (Vol >= 1.5x dan BTC ADX >= 22.0): Risk 2.5% ($25)
   - Grade B (Lainnya): Risk 1.0% ($10)
3. Dynamic Risk Model 2 (Tiered 3-Level):
   - Grade A+ (Vol >= 1.6x dan BTC ADX >= 25.0): Risk 2.5%
   - Grade A (Vol >= 1.2x dan BTC ADX >= 20.0): Risk 2.0%
   - Grade B (Lainnya): Risk 1.0%
4. Dynamic Risk Model 3 (Conservative):
   - Grade A (Standard): Risk 2.0%
   - Grade B (Borderline / Vol < 1.3x atau BTC ADX < 22): Risk 1.0%
"""

import os
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from config import BotConfig, load_config
import indicators
from backtest import parse_timeframe_delta

def run_simulation(symbol: str, risk_model: str = "FIXED_2PCT"):
    ltf = "5m" if symbol in ["BTC/USDT", "LINK/USDT"] else "15m"
    htf = "1h"
    start_date = "2024-05-01"
    end_date = "2024-11-01"
    
    data_dir = "backtest_data"
    htf_file = os.path.join(data_dir, f"{symbol.replace('/', '_')}_{htf}_{start_date}_{end_date}.csv")
    ltf_file = os.path.join(data_dir, f"{symbol.replace('/', '_')}_{ltf}_{start_date}_{end_date}.csv")
    
    if not os.path.exists(htf_file) or not os.path.exists(ltf_file):
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
    btc_file = os.path.join(data_dir, f"BTC_USDT_{htf}_{start_date}_{end_date}.csv")
    if os.path.exists(btc_file):
        df_btc_raw = pd.read_csv(btc_file, parse_dates=['timestamp'])
        df_btc = indicators.prepare_htf_frame(
            df_btc_raw,
            ema_period=cfg.strategy.ema_period,
            tema_period=cfg.strategy.tema_period
        )
        df_btc['timestamp'] = pd.to_datetime(df_btc['timestamp'], utc=True)
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
        
        htf_pos = htf_close_times.searchsorted(current_ltf_time, side='right') - 1
        new_htf_bar = False
        if htf_pos >= 50 and htf_pos != last_htf_pos:
            new_htf_bar = True
            start_pos = max(0, htf_pos - cfg.strategy.htf_limit + 1)
            df_htf_window = df_htf.iloc[start_pos : htf_pos + 1]
            context = strat.analyze_higher_timeframe(symbol, df_htf_window)
            last_htf_pos = htf_pos

        if use_btc_filter and btc_close_times is not None:
            btc_pos = btc_close_times.searchsorted(current_ltf_time, side='right') - 1
            if btc_pos >= 1 and btc_pos != last_btc_pos:
                df_btc_win = df_btc.iloc[max(0, btc_pos - 10) : btc_pos + 1]
                strat.update_btc_market_status(df_btc_win)
                last_btc_pos = btc_pos

        # --- 1. EARLY INVALIDATION CHECK (HTF EMA & BTC BEARISH) ---
        if active_trade is not None and new_htf_bar:
            last_htf_candle = df_htf.iloc[htf_pos]
            coin_htf_broken = (last_htf_candle['close'] < last_htf_candle['ema'])
            is_alt = "BTC" not in symbol.upper()
            btc_broken = is_alt and use_btc_filter and (not strat.btc_market_bullish)

            if coin_htf_broken or btc_broken:
                reason = "EARLY_INVALID_HTF_EMA" if coin_htf_broken else "EARLY_INVALID_BTC_BEARISH"
                exit_price = open_p
                trade = active_trade
                rem_qty = trade['remaining_quantity']
                gross = (exit_price - trade['entry_price']) * rem_qty
                fee = (trade['entry_price'] * rem_qty * fee_rate) + (exit_price * rem_qty * fee_rate)
                net = gross - fee
                capital += net
                daily_pnl += net
                tot_pnl = trade['realized_pnl'] + net
                tot_notional = trade['entry_price'] * trade['initial_quantity']
                final_reason = f"PARTIAL_TP1_THEN_{reason}" if trade['tp1_executed'] else reason
                trades_history.append({
                    'entry_time': trade['entry_time'],
                    'exit_time': current_ltf_time,
                    'pnl': tot_pnl,
                    'pnl_pct': (tot_pnl / tot_notional) * 100,
                    'reason': final_reason,
                    'risk_pct': trade.get('risk_pct', 2.0),
                    'grade': trade.get('grade', 'B')
                })
                active_trade = None
                cooldown_until = current_ltf_time + timedelta(minutes=cfg.execution.cooldown_minutes)

        # --- 2. EVALUATE PENDING LIMIT ORDER (RETEST) ---
        if pending_limit_order is not None:
            pending = pending_limit_order
            if i > pending['expires_bar']:
                strat.unmark_zone(symbol, pending['zone_id'])
                pending_limit_order = None
            elif low_p <= pending['stop_loss']:
                strat.unmark_zone(symbol, pending['zone_id'])
                pending_limit_order = None
            elif low_p <= pending['limit_price']:
                fill_price = pending['limit_price']
                if open_p < fill_price:
                    fill_price = open_p
                
                # Gunakan risk_pct dinamis yang tersimpan di pending order
                custom_risk = pending.get('risk_pct', 2.0)
                cfg.risk.max_risk_per_trade = custom_risk
                risk_mgr = RiskManager(cfg.risk)

                plan = risk_mgr.build_trade_plan(
                    symbol=symbol,
                    entry_price=fill_price,
                    stop_loss_ref=pending['stop_loss_ref'],
                    atr_value=pending['atr_value'],
                    total_capital=capital
                )
                if plan:
                    qty = plan.notional / fill_price
                    active_trade = {
                        'entry_time': current_ltf_time,
                        'entry_price': fill_price,
                        'stop_loss': plan.stop_loss,
                        'initial_stop_loss': plan.stop_loss,
                        'take_profit': plan.take_profit,
                        'take_profit_1': plan.take_profit_1,
                        'take_profit_2': plan.take_profit_2,
                        'quantity': qty,
                        'initial_quantity': qty,
                        'remaining_quantity': qty,
                        'bep_activated': False,
                        'tp1_executed': False,
                        'realized_pnl': 0.0,
                        'zone_id': pending['zone_id'],
                        'risk_pct': custom_risk,
                        'grade': pending.get('grade', 'B')
                    }
                pending_limit_order = None

        # --- 3. EVALUATE ACTIVE TRADE (TP1, TP2, SL, BEP) ---
        if active_trade is not None:
            trade = active_trade
            stop_dist = trade['entry_price'] - trade['initial_stop_loss']
            tp1_price = trade['take_profit_1']
            tp2_price = trade['take_profit_2']
            bep_profit_pct = cfg.risk.bep_profit_pct

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
                        'reason': 'STOP_LOSS',
                        'risk_pct': trade.get('risk_pct', 2.0),
                        'grade': trade.get('grade', 'B')
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
                        'reason': 'PARTIAL_TP1_THEN_SL/BEP',
                        'risk_pct': trade.get('risk_pct', 2.0),
                        'grade': trade.get('grade', 'B')
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
                        'reason': 'TAKE_PROFIT_ALL (TP1+TP2)',
                        'risk_pct': trade.get('risk_pct', 2.0),
                        'grade': trade.get('grade', 'B')
                    })
                    active_trade = None
                    cooldown_until = current_ltf_time + timedelta(minutes=cfg.execution.cooldown_minutes)
                    continue

        if active_trade is not None or pending_limit_order is not None:
            continue
        if circuit_breaker_active:
            continue
        if cooldown_until and current_ltf_time < cooldown_until:
            continue
        if htf_pos < 50:
            continue

        # --- 4. EVALUATE SIGNAL ---
        signal = strat.evaluate_lower_timeframe(symbol, df_ltf_window, context)
        if signal:
            # Kalkulasi Trend Conviction Grade:
            # 1. Volume participation ratio
            avg_vol = df_ltf_window['volume'].iloc[-21:-1].mean()
            vol_ratio = current_bar['volume'] / avg_vol if avg_vol > 0 else 1.0
            
            # 2. BTC ADX
            btc_adx = 20.0
            if df_btc is not None:
                b_pos = btc_close_times.searchsorted(current_ltf_time, side='right') - 1
                if b_pos >= 0 and 'adx' in df_btc.columns:
                    btc_adx = df_btc['adx'].iloc[b_pos]

            # 3. HTF EMA Slope
            htf_slope_pct = 0.0
            if htf_pos >= 1 and 'ema' in df_htf.columns:
                prev_ema = df_htf['ema'].iloc[htf_pos - 1]
                cur_ema = df_htf['ema'].iloc[htf_pos]
                if prev_ema > 0:
                    htf_slope_pct = ((cur_ema - prev_ema) / prev_ema) * 100.0

            # Determine Risk based on Model
            if risk_model == "FIXED_2PCT":
                assigned_risk = 2.0
                grade = "STANDARD"
            elif risk_model == "DYNAMIC_MODEL_1":
                # Model 1: Grade A+ (Vol >= 1.5x & BTC ADX >= 22) -> 2.5%, Grade B -> 1.0%
                if vol_ratio >= 1.5 and btc_adx >= 22.0:
                    assigned_risk = 2.5
                    grade = "A+"
                else:
                    assigned_risk = 1.0
                    grade = "B"
            elif risk_model == "DYNAMIC_MODEL_2":
                # Model 2 (Tiered): A+ (2.5%), A (2.0%), B (1.0%)
                if vol_ratio >= 1.6 and btc_adx >= 24.0:
                    assigned_risk = 2.5
                    grade = "A+"
                elif vol_ratio >= 1.2 and btc_adx >= 20.0:
                    assigned_risk = 2.0
                    grade = "A"
                else:
                    assigned_risk = 1.0
                    grade = "B"
            elif risk_model == "DYNAMIC_MODEL_3":
                # Model 3: A+ (2.5%) only if super strong, else normal 2.0%, weak 1.2%
                if vol_ratio >= 1.8 and btc_adx >= 25.0 and htf_slope_pct > 0.05:
                    assigned_risk = 2.5
                    grade = "A+"
                elif vol_ratio >= 1.2:
                    assigned_risk = 2.0
                    grade = "A"
                else:
                    assigned_risk = 1.2
                    grade = "B"
            else:
                assigned_risk = 2.0
                grade = "STANDARD"

            cfg.risk.max_risk_per_trade = assigned_risk
            risk_mgr = RiskManager(cfg.risk)

            if signal.entry_type == "LIMIT_RETEST":
                est_sl = signal.stop_loss_ref - (0.5 * signal.atr_value)
                pending_limit_order = {
                    'limit_price': signal.entry_price,
                    'stop_loss_ref': signal.stop_loss_ref,
                    'stop_loss': est_sl,
                    'atr_value': signal.atr_value,
                    'zone_id': signal.zone_id,
                    'created_bar': i,
                    'expires_bar': i + signal.timeout_bars,
                    'risk_pct': assigned_risk,
                    'grade': grade
                }
                strat.mark_zone_traded(symbol, signal.zone_id)
            else:
                plan = risk_mgr.build_trade_plan(
                    symbol=symbol,
                    entry_price=signal.entry_price,
                    stop_loss_ref=signal.stop_loss_ref,
                    atr_value=signal.atr_value,
                    total_capital=capital
                )
                if plan:
                    eff_p = plan.entry_price * (1.0 + slippage_market)
                    qty = plan.notional / eff_p
                    active_trade = {
                        'entry_time': current_ltf_time,
                        'entry_price': eff_p,
                        'stop_loss': plan.stop_loss,
                        'initial_stop_loss': plan.stop_loss,
                        'take_profit': plan.take_profit,
                        'take_profit_1': plan.take_profit_1,
                        'take_profit_2': plan.take_profit_2,
                        'quantity': qty,
                        'initial_quantity': qty,
                        'remaining_quantity': qty,
                        'bep_activated': False,
                        'tp1_executed': False,
                        'realized_pnl': 0.0,
                        'zone_id': signal.zone_id,
                        'risk_pct': assigned_risk,
                        'grade': grade
                    }
                    strat.mark_zone_traded(symbol, signal.zone_id)

    tot_trades = len(trades_history)
    if tot_trades == 0:
        return {'symbol': symbol, 'model': risk_model, 'trades': 0, 'net_pnl': 0.0, 'pf': 0.0, 'wr': 0.0, 'max_dd': 0.0}

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

    grade_counts = df_res['grade'].value_counts().to_dict()

    return {
        'symbol': symbol,
        'model': risk_model,
        'trades': tot_trades,
        'net_pnl': tot_pnl,
        'pnl_pct': (tot_pnl / 1000) * 100.0 if tot_trades > 0 else 0,
        'pnl_pct': (tot_pnl / initial_capital) * 100.0,
        'pf': pf,
        'wr': win_rate,
        'max_dd': max_dd,
        'grades': grade_counts
    }

if __name__ == "__main__":
    symbols = ["BTC/USDT", "LINK/USDT", "ETH/USDT"]
    models = ["FIXED_2PCT", "DYNAMIC_MODEL_1", "DYNAMIC_MODEL_2", "DYNAMIC_MODEL_3"]
    
    all_res = []
    print("="*80)
    print("PENGUJIAN EMPIRIS: DYNAMIC RISK SIZING (TREND CONVICTION)")
    print("="*80)
    
    for m in models:
        print(f"\nEvaluating Model: {m}...")
        tot_pnl_m = 0
        for s in symbols:
            r = run_simulation(s, risk_model=m)
            if r:
                all_res.append(r)
                tot_pnl_m += r['net_pnl']
                print(f"[{s:9}] Trades: {r['trades']:2d} | WR: {r['wr']:4.1f}% | PnL: ${r['net_pnl']:+6.2f} ({r['pnl_pct']:+5.2f}%) | PF: {r['pf']:4.2f} | DD: {r['max_dd']:5.2f}% | Grades: {r['grades']}")
        print(f"--> TOTAL PORTFOLIO PnL ({m}): ${tot_pnl_m:+6.2f}")
