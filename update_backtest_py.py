import re

def update_backtest():
    path = "backtest.py"
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    # 1. Add self.pending_limit_order in __init__
    old_init = """        self.trades_history = []
        self.active_trade = None
        self.cooldown_until = None"""

    new_init = """        self.trades_history = []
        self.active_trade = None
        self.pending_limit_order = None
        self.cooldown_until = None"""
    if old_init in content:
        content = content.replace(old_init, new_init)

    # 2. Update close_trade to support partial TP accounting
    old_close = """    def close_trade(self, exit_price: float, reason: str, exit_time: datetime):
        \"\"\"Menutup posisi, menghitung fee dua arah, dan memperbarui state.\"\"\"
        trade = self.active_trade
        gross_pnl = (exit_price - trade['entry_price']) * trade['quantity']

        # Fee Binance Spot standar bolak-balik
        fee_rate = (self.config.risk.estimated_exchange_fee_pct / 100.0) / 2.0
        entry_fee = (trade['entry_price'] * trade['quantity']) * fee_rate
        exit_fee = (exit_price * trade['quantity']) * fee_rate
        total_fee = entry_fee + exit_fee

        net_pnl = gross_pnl - total_fee
        pnl_pct = (net_pnl / (trade['entry_price'] * trade['quantity'])) * 100.0

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

        self.trades_history.append({
            'entry_time': trade['entry_time'],
            'exit_time': exit_time,
            'entry_price': trade['entry_price'],
            'exit_price': exit_price,
            'reason': reason,
            'pnl': net_pnl,
            'pnl_pct': pnl_pct,
            'capital_after': self.current_capital
        })

        self.active_trade = None

        # Cooldown Dinamis: SL dikenai hukuman istirahat lebih panjang
        if reason == "STOP_LOSS":
            cd_minutes = self.config.execution.sl_cooldown_minutes
        else:
            cd_minutes = self.config.execution.cooldown_minutes
            
        self.cooldown_until = exit_time + timedelta(minutes=cd_minutes)"""

    new_close = """    def close_trade(self, exit_price: float, reason: str, exit_time: datetime):
        \"\"\"Menutup posisi, menghitung fee dua arah, dan memperbarui state.\"\"\"
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

        # Cooldown Dinamis: SL dikenai hukuman istirahat lebih panjang
        if reason == "STOP_LOSS":
            cd_minutes = self.config.execution.sl_cooldown_minutes
        else:
            cd_minutes = self.config.execution.cooldown_minutes
            
        self.cooldown_until = exit_time + timedelta(minutes=cd_minutes)"""
    if old_close in content:
        content = content.replace(old_close, new_close)

    # 3. Update main loop
    pattern_loop = r'(\s+for i in range\(ltf_lookback, len\(df_ltf\)\):.*?\n\s+self\.print_results\(\))'
    new_loop = '''\n        for i in range(ltf_lookback, len(df_ltf)):
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
            if use_btc_filter and btc_close_times is not None:
                btc_pos = btc_close_times.searchsorted(current_ltf_time, side='right') - 1
                if btc_pos >= 1 and btc_pos != last_btc_pos:
                    df_btc_window = df_btc.iloc[max(0, btc_pos - 10) : btc_pos + 1]
                    self.strategy.update_btc_market_status(df_btc_window)
                    last_btc_pos = btc_pos

            # 3. EARLY INVALIDATION CHECK (Cut loss dini saat candle 1H patah tren atau BTC Bearish)
            enable_inval = getattr(self.config.strategy, 'enable_early_invalidation', True)
            if self.active_trade and enable_inval and new_htf_bar:
                last_htf_candle = df_htf.iloc[htf_pos]
                check_htf_ema = getattr(self.config.strategy, 'invalidation_check_htf_ema', True)
                coin_htf_broken = check_htf_ema and (last_htf_candle['close'] < last_htf_candle['ema'])

                check_btc = getattr(self.config.strategy, 'invalidation_check_btc_filter', True)
                is_altcoin = "BTC" not in self.symbol.upper()
                btc_broken = check_btc and is_altcoin and use_btc_filter and (not self.strategy.btc_market_bullish)

                if coin_htf_broken or btc_broken:
                    reason = "EARLY_INVALID_HTF_EMA" if coin_htf_broken else "EARLY_INVALID_BTC_BEARISH"
                    self.close_trade(current_bar['open'], reason, current_ltf_time)
                    self.cooldown_until = current_ltf_time + timedelta(minutes=self.config.execution.cooldown_minutes)

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
            
            # 5. Evaluasi LTF (Trigger Micro BOS)
            signal = self.strategy.evaluate_lower_timeframe(self.symbol, df_ltf_window, context)
            
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

        self.print_results()'''
    content = re.sub(pattern_loop, new_loop, content, flags=re.DOTALL)

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print("[+] backtest.py berhasil diperbarui.")

if __name__ == "__main__":
    update_backtest()
