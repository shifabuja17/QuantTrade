"""
Script Sinkronisasi Utama & Menyeluruh:
Menerapkan:
1. Hybrid Adaptive Retest Entry (BTC Direct Market, Altcoins 50% Body Retest Limit Order, timeout 5 bar)
2. Partial Take-Profit (TP1 1.5R 50% + TP2 2.5R Runner 50%)
3. Early Invalidation Exit (HTF EMA50 Breakdown & BTC Bearish Emergency Exit)
ke seluruh file codebase:
- config.py
- config.json
- strategy.py
- risk_management.py
- execution.py
- bot.py
- backtest.py
- apply_adaptive_update.py
"""

import os
import json
import re

def update_config_py():
    path = "config.py"
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    # 1. Update StrategyConfig
    pattern_strategy = r'@dataclass\s+class StrategyConfig:.*?(?=@dataclass\s+class RiskConfig:)'
    new_strategy_block = '''@dataclass
class StrategyConfig:
    higher_timeframe: str = "1h"
    lower_timeframe: str = "15m"
    btc_lower_timeframe: str = "5m"
    alt_lower_timeframe: str = "15m"
    alt_require_golden_cross: bool = True
    btc_require_tema_filter: bool = True
    htf_limit: int = 1000
    ltf_limit: int = 1000
    ema_period: int = 50
    tema_period: int = 200
    adx_threshold: int = 20
    stochastic_k_period: int = 14
    stochastic_k_smoothing: int = 3
    stochastic_d_period: int = 3
    stochastic_oversold: int = 20
    atr_period: int = 14
    order_block_lookback: int = 50
    order_block_impulse_multiplier: float = 2.0
    max_zones_per_symbol: int = 5
    max_zone_distance_pct: float = 2.0
    zone_expiry_bars: int = 48
    enable_btc_filter: bool = True
    btc_filter_symbol: str = "BTC/USDT"
    ltf_min_body_ratio: float = 0.40       # Minimal body candle 40% (dilonggarkan dari 50%)
    ltf_volume_multiplier: float = 1.1     # Partisipasi volume 1.1x rata-rata 20 bar (dilonggarkan dari 1.3x)
    stochastic_overbought: float = 75.0    # Batas atas Stochastic %K untuk filter Anti-Overbought (mencegah beli di pucuk)
    btc_min_adx: float = 18.0              # Batas minimal ADX BTC untuk filter kekuatan tren (mencegah beli saat BTC sideway mati)
    enable_early_invalidation: bool = True # Cut loss dini saat candle 1H jebol di bawah EMA 50 atau BTC Bearish
    invalidation_check_htf_ema: bool = True
    invalidation_check_btc_filter: bool = True
    enable_adaptive_retest: bool = True    # Hybrid: Altcoin pakai Limit Retest 50% Body, BTC pakai Direct Market
    retest_body_ratio: float = 0.50        # Diskon 50% retracement dari body candle breakout
    retest_timeout_bars: int = 5           # Batas waktu tunggu limit order retest (5 bar LTF)
    retest_altcoins_only: bool = True      # Hanya altcoin yang pakai retest (BTC langsung tembak market)
    fast_symbols: List[str] = field(default_factory=lambda: ["BTC/USDT", "LINK/USDT"])

    def get_ltf_for_symbol(self, symbol: str) -> str:
        """Mengembalikan LTF adaptif: 5m untuk BTC dan LINK, 15m untuk Altcoin lainnya."""
        sym_clean = symbol.upper().replace('/', '')
        for fast_sym in self.fast_symbols:
            if fast_sym.upper().replace('/', '') in sym_clean:
                return self.btc_lower_timeframe
        return self.alt_lower_timeframe

'''
    content = re.sub(pattern_strategy, new_strategy_block, content, flags=re.DOTALL)

    # 2. Update RiskConfig
    pattern_risk = r'@dataclass\s+class RiskConfig:.*?(?=@dataclass\s+class ExecutionConfig:)'
    new_risk_block = '''@dataclass
class RiskConfig:
    max_risk_per_trade: float = 2.0  # Dalam persen (%) - 2.0% ($20 per trade)
    max_daily_loss_pct: float = 5.0
    max_daily_profit_pct: float = 6.0
    sl_atr_multiplier: float = 1.5
    tp_atr_multiplier: float = 2.5
    enable_partial_tp: bool = True           # Mengaktifkan Partial Take-Profit (Scaling Out)
    partial_tp_ratio: float = 0.5            # Porsi posisi yang dijual di TP1 (50%)
    partial_tp_atr_multiplier: float = 1.5   # TP1 di 1.5R (mengunci profit awal)
    bep_trigger_atr_multiplier: float = 1.1  # Terkunci di 1.1R
    bep_profit_pct: float = 0.15             # Buffer fee exchange 0.15% di atas entry
    enable_early_invalidation: bool = True   # Cut loss dini
    invalidation_check_htf_ema: bool = True
    invalidation_check_btc_filter: bool = True
    min_notional: float = 5.0  # Batas minimum order exchange (misal Binance = 5 USDT)
    max_quote_allocation_pct: float = 90.0
    use_static_sl_tp: bool = False

    min_sl_distance_pct: float = 0.5  # Jarak SL minimal 0.5% dari harga entri (BTC)
    max_sl_distance_pct: float = 1.5  # Jarak SL maksimal BTC 1.5% dari harga entri
    alt_min_sl_distance_pct: float = 1.5 # Jarak SL minimal Altcoin 1.5% agar kebal jarum volatilitas 15m
    alt_sl_atr_multiplier: float = 1.8   # Pengali ATR untuk SL Altcoin (1.5x - 2.0x ATR)
    alt_max_sl_distance_pct: float = 3.5 # Jarak SL maksimal untuk Altcoin agar tidak tersapu jarum (3.5%)
    btc_symbol: str = "BTC/USDT"
    max_fee_to_risk_ratio: float = 0.3 # Maksimal fee memakan 30% dari toleransi risiko
    estimated_exchange_fee_pct: float = 0.2

'''
    content = re.sub(pattern_risk, new_risk_block, content, flags=re.DOTALL)

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print("[+] config.py berhasil diperbarui.")

def update_config_json():
    path = "config.json"
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    data["symbols"] = ["BTC/USDT", "LINK/USDT", "ADA/USDT"]
    data["strategy"]["higher_timeframe"] = "1h"
    data["strategy"]["lower_timeframe"] = "15m"
    data["strategy"]["btc_lower_timeframe"] = "5m"
    data["strategy"]["alt_lower_timeframe"] = "15m"
    data["strategy"]["fast_symbols"] = ["BTC/USDT", "LINK/USDT"]
    data["strategy"]["alt_require_golden_cross"] = True
    data["strategy"]["btc_require_tema_filter"] = True
    data["strategy"]["enable_early_invalidation"] = True
    data["strategy"]["invalidation_check_htf_ema"] = True
    data["strategy"]["invalidation_check_btc_filter"] = True
    data["strategy"]["enable_adaptive_retest"] = True
    data["strategy"]["retest_body_ratio"] = 0.50
    data["strategy"]["retest_timeout_bars"] = 5
    data["strategy"]["retest_altcoins_only"] = True

    data["risk"]["max_risk_per_trade"] = 2.0
    data["risk"]["max_daily_loss_pct"] = 5.0
    data["risk"]["max_daily_profit_pct"] = 6.0
    data["risk"]["tp_atr_multiplier"] = 2.5
    data["risk"]["enable_partial_tp"] = True
    data["risk"]["partial_tp_ratio"] = 0.5
    data["risk"]["partial_tp_atr_multiplier"] = 1.5
    data["risk"]["bep_trigger_atr_multiplier"] = 1.1
    data["risk"]["bep_profit_pct"] = 0.15
    data["risk"]["enable_early_invalidation"] = True
    data["risk"]["invalidation_check_htf_ema"] = True
    data["risk"]["invalidation_check_btc_filter"] = True
    data["risk"]["max_quote_allocation_pct"] = 90.0

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print("[+] config.json berhasil diperbarui.")

def update_strategy_py():
    path = "strategy.py"
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    # Update TradeSignal dataclass
    pattern_tradesignal = r'@dataclass\s+class TradeSignal:.*?(?=\nclass StrategyEngine:)'
    new_tradesignal = '''@dataclass
class TradeSignal:
    """
    Representasi sinyal trading valid yang siap dieksekusi oleh Execution module.
    """
    symbol: str
    direction: str  # "LONG" / "SHORT"
    entry_price: float
    stop_loss_ref: float  # Referensi harga terendah dari zona (untuk pengaman)
    atr_value: float
    zone_id: str
    reason: str
    entry_type: str = "MARKET"  # "MARKET" (BTC) atau "LIMIT_RETEST" (Altcoins)
    retest_price: Optional[float] = None
    timeout_bars: int = 5
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))'''
    content = re.sub(pattern_tradesignal, new_tradesignal, content, flags=re.DOTALL)

    # Update evaluate_lower_timeframe return block
    pattern_ret = r'(\s+if is_bullish_candle and micro_bos and body_dominant and volume_spike and atr_valid and not_overbought:.*?\n\s+return None)'
    new_ret = '''\n        if is_bullish_candle and micro_bos and body_dominant and volume_spike and atr_valid and not_overbought:
            stoch_info = f", Stoch: {stoch_k:.1f}" if (stoch_k is not None and not pd.isna(stoch_k)) else ""
            logger.info(f"[{symbol} LTF] Micro BOS (True Swing High @ {true_swing_high:.4f}) Terdeteksi di zona {active_zone.zone_type}! Body: {body_ratio:.0%}, Vol: {last_row['volume']:.0f}/{avg_volume:.0f}{stoch_info}")
            
            # Hybrid Adaptive Retest Entry:
            # BTC menggunakan Direct Market Entry untuk menyambar ledakan momentum.
            # Altcoin (LINK, ETH, dll) memasang Limit Order pada retest 50% Body Retracement untuk memperketat SL & memangkas slippage.
            is_altcoin = "BTC" not in symbol.upper()
            enable_retest = getattr(self.config, 'enable_adaptive_retest', True)
            retest_altcoins_only = getattr(self.config, 'retest_altcoins_only', True)
            
            use_retest = enable_retest and (is_altcoin if retest_altcoins_only else True)
            if use_retest:
                retest_ratio = getattr(self.config, 'retest_body_ratio', 0.50)
                retest_p = current_price - (retest_ratio * (current_price - last_row['open']))
                retest_p = min(retest_p, current_price * 0.9995)
                if retest_p <= active_zone.lower:
                    retest_p = (current_price + active_zone.lower) / 2.0
                
                entry_type = "LIMIT_RETEST"
                entry_price = retest_p
                timeout_bars = getattr(self.config, 'retest_timeout_bars', 5)
                reason_str = f"{active_zone.zone_type} Rejection + True BOS + Retest 50% Body + Vol({last_row['volume']:.0f}/{avg_volume:.0f}){stoch_info}"
            else:
                entry_type = "MARKET"
                entry_price = current_price
                timeout_bars = 0
                reason_str = f"{active_zone.zone_type} Rejection + True BOS + Direct Market + Vol({last_row['volume']:.0f}/{avg_volume:.0f}){stoch_info}"

            return TradeSignal(
                symbol=symbol,
                direction="LONG",
                entry_price=entry_price,
                stop_loss_ref=active_zone.lower,
                atr_value=atr_value,
                zone_id=active_zone.zone_id,
                reason=reason_str,
                entry_type=entry_type,
                retest_price=entry_price,
                timeout_bars=timeout_bars
            )

        return None'''
    content = re.sub(pattern_ret, new_ret, content, flags=re.DOTALL)

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print("[+] strategy.py berhasil diperbarui.")

def update_risk_management_py():
    path = "risk_management.py"
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    # Update TradePlan
    pattern_plan = r'@dataclass\s+class TradePlan:.*?(?=\nclass RiskManager:)'
    new_plan = '''@dataclass
class TradePlan:
    """
    Representasi dari rencana eksekusi trading yang sudah melewati 
    filter manajemen risiko dan kalkulasi position sizing.
    """
    symbol: str
    entry_price: float
    stop_loss: float
    take_profit: float
    bep_trigger_price: float
    quantity: float
    risk_amount: float
    notional: float
    take_profit_1: float = 0.0
    take_profit_2: float = 0.0
    partial_tp_ratio: float = 0.5'''
    content = re.sub(pattern_plan, new_plan, content, flags=re.DOTALL)

    # Update build_trade_plan return
    target_ret = """        return TradePlan(
            symbol=symbol,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            bep_trigger_price=bep_trigger_price,
            quantity=quantity,
            risk_amount=actual_risk_amount,
            notional=notional
        )"""
    new_ret = """        tp1_mult = getattr(self.config, 'partial_tp_atr_multiplier', 1.5)
        tp1_price = entry_price + (stop_distance * tp1_mult)
        tp2_price = take_profit
        partial_ratio = getattr(self.config, 'partial_tp_ratio', 0.5)

        return TradePlan(
            symbol=symbol,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            bep_trigger_price=bep_trigger_price,
            quantity=quantity,
            risk_amount=actual_risk_amount,
            notional=notional,
            take_profit_1=tp1_price,
            take_profit_2=tp2_price,
            partial_tp_ratio=partial_ratio
        )"""
    if target_ret in content:
        content = content.replace(target_ret, new_ret)

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print("[+] risk_management.py berhasil diperbarui.")

def update_bot_py():
    path = "bot.py"
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    # Update Active/Pending check
    old_active_check = """            # =================================================================
            # LOGIKA BARU INTEGRASI: Update Trailing Live Jika Posisi Terbuka
            # =================================================================
            if symbol in self.execution.active_trades:
                await self.execution.update_structural_trailing(symbol, df_ltf)
                return  # Selesai, lanjut ke koin berikutnya dalam antrean semapor
            # ================================================================="""

    new_active_check = """            # =================================================================
            # 1. CEK STATUS POSISI AKTIF & PENDING LIMIT ORDER
            # =================================================================
            if symbol in self.execution.active_trades:
                exited_early = await self.execution.check_early_invalidation(
                    symbol, df_htf, self.strategy.btc_market_bullish
                )
                if exited_early:
                    return

                await self.execution.update_structural_trailing(symbol, df_ltf)
                return

            if symbol in self.execution.pending_orders:
                current_bar = df_ltf.iloc[-1]
                await self.execution.update_pending_retest_orders(symbol, current_bar, self.strategy)
                return
            # ================================================================="""
    if old_active_check in content:
        content = content.replace(old_active_check, new_active_check)

    # Update execution check
    old_exec_check = "if self.execution.circuit_breaker_active or symbol in self.execution.active_trades:"
    new_exec_check = "if self.execution.circuit_breaker_active or symbol in self.execution.active_trades or symbol in self.execution.pending_orders:"
    if old_exec_check in content:
        content = content.replace(old_exec_check, new_exec_check)

    # Update order opening
    old_open = """            # 8. Open Trade
            success = await self.execution.open_long(signal, plan)"""

    new_open = """            # 8. Eksekusi Order (Hybrid: Limit Retest untuk Altcoin vs Market Direct untuk BTC)
            if signal.entry_type == "LIMIT_RETEST":
                success = await self.execution.place_retest_limit_order(signal, plan)
            else:
                success = await self.execution.open_long(signal, plan)"""
    if old_open in content:
        content = content.replace(old_open, new_open)

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print("[+] bot.py berhasil diperbarui.")

if __name__ == "__main__":
    update_config_py()
    update_config_json()
    update_strategy_py()
    update_risk_management_py()
    update_bot_py()
    print("\n[V] Seluruh codebase berhasil disinkronkan ke arsitektur Hybrid Adaptive Retest Entry!")
