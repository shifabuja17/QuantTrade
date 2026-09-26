import logging
from dataclasses import dataclass
from typing import Tuple, Optional

# Asumsi import dari file config yang telah kita buat
from config import RiskConfig

logger = logging.getLogger(__name__)

@dataclass
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
    partial_tp_ratio: float = 0.5
class RiskManager:
    """
    Modul Manajemen Risiko untuk memvalidasi dan membangun Trade Plan.
    Menerapkan position sizing dinamis berdasarkan Stop Loss distance dan toleransi risiko.
    """
    def __init__(self, config: RiskConfig):
        self.config = config

    def calculate_levels(self, entry_price: float, stop_loss_ref: float, 
                         atr_value: float, symbol: str = "BTC/USDT") -> Tuple[float, float, float]:
        """
        Menghitung level SL, TP, dan BEP.
        Menggunakan sistem Fixed Risk/Reward Ratio agar rasio selalu logis.
        Mendukung SL ATR Dinamis untuk Altcoin (1.5x - 2.0x ATR) agar tidak tersapu jarum (wick).
        """
        # 1. Tentukan pengali ATR: Altcoin menggunakan pengali lebih lebar agar tidak tersapu jarum
        btc_sym = getattr(self.config, 'btc_symbol', 'BTC/USDT')
        is_alt = (symbol != btc_sym)
        sl_mult = getattr(self.config, 'alt_sl_atr_multiplier', 1.8) if is_alt else self.config.sl_atr_multiplier

        # 2. Hitung Stop Loss (Dynamic Structural)
        stop_loss = stop_loss_ref
        
        # Pengaman: Jika OB terlalu sempit, gunakan minimal sl_mult * ATR agar tidak tersapu noise
        min_sl = entry_price - (atr_value * sl_mult)
        if stop_loss > min_sl:
            stop_loss = min_sl

        # LANTAI PENGAMAN SL ALTCOIN:
        # Jika Altcoin, pastikan jarak SL tidak pernah lebih sempit dari alt_min_sl_distance_pct (1.5%)
        # agar tidak tersapu gocekan jarum (wicks) normal 15m.
        if is_alt:
            alt_min_pct = getattr(self.config, 'alt_min_sl_distance_pct', 1.5)
            alt_floor_sl = entry_price * (1.0 - (alt_min_pct / 100.0))
            if stop_loss > alt_floor_sl:
                stop_loss = alt_floor_sl

        # Hitung jarak absolut dari Entry ke Stop Loss (Nilai 1R / 1 Risk)
        stop_distance = entry_price - stop_loss

        # 3. Hitung Take Profit (Berbasis Multiplier RRR)
        take_profit = entry_price + (stop_distance * self.config.tp_atr_multiplier)

        # 4. Hitung Break Even Trigger (Kapan SL dipindah ke Entry)
        bep_trigger_price = entry_price + (stop_distance * self.config.bep_trigger_atr_multiplier) 

        return stop_loss, take_profit, bep_trigger_price

    def build_trade_plan(self, symbol: str, entry_price: float, stop_loss_ref: float, 
                         atr_value: float, total_capital: float) -> Optional[TradePlan]:
        """
        Membangun rencana trading yang komprehensif. Menghitung jumlah koin (quantity)
        berdasarkan risiko maksimal per trade dan batasan alokasi modal (notional).
        Ukuran modal (notional) otomatis mengecil secara proporsional jika jarak SL melebar,
        sehingga toleransi risiko dolar tetap terkontrol (misal tepat $10).
        """
        # 1. Kalkulasi Level Harga (Dinamis Altcoin vs BTC)
        stop_loss, take_profit, bep_trigger_price = self.calculate_levels(
            entry_price=entry_price, 
            stop_loss_ref=stop_loss_ref, 
            atr_value=atr_value,
            symbol=symbol
        )

        # 2. Hitung Stop Distance (Jarak SL)
        stop_distance = entry_price - stop_loss

        stop_distance_pct = (stop_distance / entry_price) * 100.0
        
        if stop_distance <= 0:
            logger.error(f"[{symbol}] Invalid stop distance: {stop_distance}. Entry: {entry_price}, SL: {stop_loss}")
            return None

        # --- FILTER 1: JARAK SL MINIMAL (Dinamis: BTC 0.5%, Altcoin 1.5%) ---
        btc_sym = getattr(self.config, 'btc_symbol', 'BTC/USDT')
        is_alt = (symbol != btc_sym)
        min_sl_pct = getattr(self.config, 'alt_min_sl_distance_pct', 1.5) if is_alt else self.config.min_sl_distance_pct

        if stop_distance_pct < (min_sl_pct - 1e-4):
            logger.debug(f"[{symbol}] Trade ditolak: Jarak SL terlalu sempit ({stop_distance_pct:.2f}% < {min_sl_pct}%). Risiko Notional membengkak.")
            return None

        # --- FILTER 1B: JARAK SL MAKSIMAL (Dinamis: BTC 1.5%, Altcoin 3.5%) ---
        max_sl_pct = getattr(self.config, 'alt_max_sl_distance_pct', 3.5) if is_alt else self.config.max_sl_distance_pct

        if stop_distance_pct > (max_sl_pct + 1e-4):
            logger.debug(f"[{symbol}] Trade ditolak: Jarak SL terlalu lebar ({stop_distance_pct:.2f}% > {max_sl_pct}%).")
            return None

        # 3. Risk-Based Position Sizing
        max_risk_amount = total_capital * (self.config.max_risk_per_trade / 100.0)
        quantity = max_risk_amount / stop_distance
        notional = quantity * entry_price 

        reward_distance = take_profit - entry_price
        rrr = reward_distance / stop_distance
        
        if rrr < 1.3:
            logger.debug(f"[{symbol}] Trade dibatalkan: RRR terlalu rendah ({rrr:.2f}).")
            return None

        # --- FILTER 2: RASIO FEE TERHADAP RISIKO ---
        # Estimasi biaya fee bolak-balik (buka & tutup posisi)
        estimated_fee_amount = notional * (self.config.estimated_exchange_fee_pct / 100.0)
        fee_to_risk_ratio = estimated_fee_amount / max_risk_amount

        if fee_to_risk_ratio > self.config.max_fee_to_risk_ratio:
            logger.warning(
                f"[{symbol}] Trade dibatalkan: Estimasi Fee (${estimated_fee_amount:.2f}) "
                f"memakan {fee_to_risk_ratio*100:.1f}% dari Risiko (${max_risk_amount:.2f})."
            )
            return None

        # 4. Respect Max Quote Allocation
        max_allocation = total_capital * (self.config.max_quote_allocation_pct / 100.0)
        
        if notional > max_allocation:
            notional = max_allocation
            quantity = notional / entry_price
            actual_risk_amount = quantity * stop_distance
        else:
            actual_risk_amount = max_risk_amount

        # 5. Respect Min Notional Exchange
        if notional < self.config.min_notional:
            return None

        # Log final trade plan
        logger.info(
            f"[{symbol}] Trade Plan Dibangun - QTY: {quantity:.4f}, Notional: ${notional:.2f}, "
            f"Risk: ${actual_risk_amount:.2f}, Est.Fee: ${estimated_fee_amount:.2f}, SL_Pct: {stop_distance_pct:.2f}%"
        )

        tp1_mult = getattr(self.config, 'partial_tp_atr_multiplier', 1.5)
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
        )