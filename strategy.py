import logging
import pandas as pd
from typing import List, Optional, Set
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Asumsi import dari file yang telah dibuat sebelumnya
from config import StrategyConfig
import indicators
from zone_detection import Zone, detect_bullish_order_blocks_with_bos, detect_bullish_fvgs, merge_waiting_zones, detect_swing_points, detect_bullish_market_structure

logger = logging.getLogger(__name__)

@dataclass
class SymbolContext:
    """
    Menyimpan status analisa Higher Timeframe (HTF) untuk sebuah symbol.
    """
    symbol: str
    is_bullish: bool = False
    active_zones: List[Zone] = field(default_factory=list)
    traded_zone_ids: Set[str] = field(default_factory=set)
    last_updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

@dataclass
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
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
class StrategyEngine:
    """
    Engine utama untuk mengevaluasi strategi SMC + Indikator multi-timeframe.
    """
    
    def __init__(self, config: StrategyConfig):
        self.config = config
        self.contexts: dict[str, SymbolContext] = {}
        self.btc_market_bullish: bool = True

    def check_relative_strength(self, symbol: str, df_htf: pd.DataFrame, df_btc_htf: pd.DataFrame) -> bool:
        """
        Filter Adaptive Relative Strength (Alt/BTC).

        Menghitung rasio: Ratio = Close_Alt / Close_BTC pada candle HTF 1H.
        Hanya berlaku untuk simbol yang terdaftar di rs_target_symbols.

        Mode EMA : Rasio harus berada DI ATAS EMA(Ratio, rs_ema_period).
        Mode ROC  : ROC(Ratio, rs_roc_period) harus > 0 (Altcoin sedang outperform BTC).

        Keduanya mensyaratkan setidaknya (rs_period + 5) baris data valid.
        Return: True = boleh masuk, False = sinyal diblokir.
        """
        if not getattr(self.config, 'enable_relative_strength_filter', True):
            return True  # Filter dimatikan, izinkan semua sinyal

        rs_targets = getattr(self.config, 'rs_target_symbols', [])
        if symbol not in rs_targets:
            return True  # Simbol tidak dalam target RS, izinkan langsung

        if df_htf is None or df_htf.empty or df_btc_htf is None or df_btc_htf.empty:
            fail_closed = getattr(self.config, 'rs_fail_closed_on_missing_data', True)
            logger.warning(
                f"[{symbol} RS] Data HTF Alt atau BTC kosong, RS Filter "
                f"{'menolak sinyal' if fail_closed else 'dilewati'}."
            )
            return not fail_closed

        # Sinkronisasi panjang dataframe agar perbandingan index-by-index valid
        n = min(len(df_htf), len(df_btc_htf))
        if n < 10:
            fail_closed = getattr(self.config, 'rs_fail_closed_on_missing_data', True)
            logger.warning(
                f"[{symbol} RS] Data terlalu sedikit ({n} baris), RS Filter "
                f"{'menolak sinyal' if fail_closed else 'dilewati'}."
            )
            return not fail_closed

        # Paksa float agar penggantian nilai nol dengan NaN aman untuk data
        # historis yang terbaca sebagai integer maupun float.
        alt_close = df_htf['close'].iloc[-n:].to_numpy(dtype=float)
        btc_close = df_btc_htf['close'].iloc[-n:].to_numpy(dtype=float)

        # Hindari division by zero
        btc_close_safe = btc_close.copy()
        btc_close_safe[btc_close_safe == 0] = float('nan')
        ratio = alt_close / btc_close_safe

        ratio_series = pd.Series(ratio)

        # Mode per-simbol: simbol yang terdaftar di rs_roc_symbols menggunakan ROC
        # (override rs_filter_mode global). ETH pakai EMA, SOL/AVAX pakai ROC.
        rs_roc_symbols = getattr(self.config, 'rs_roc_symbols', [])
        if symbol in rs_roc_symbols:
            rs_mode = 'ROC'
        else:
            rs_mode = getattr(self.config, 'rs_filter_mode', 'EMA').upper()

        if rs_mode == 'ROC':
            roc_period = getattr(self.config, 'rs_roc_period', 24)
            min_bars = roc_period + 5
            if len(ratio_series.dropna()) < min_bars:
                return True
            roc = ratio_series.pct_change(periods=roc_period).iloc[-1]
            is_outperforming = (roc > 0)
            if not is_outperforming:
                logger.debug(
                    f"[{symbol} RS] Diblokir: ROC({roc_period}) = {roc:.4f} <= 0 "
                    f"(Altcoin underperform BTC)"
                )
            return is_outperforming
        else:
            # Default: Mode EMA
            ema_period = getattr(self.config, 'rs_ema_period', 20)
            min_bars = ema_period + 5
            if len(ratio_series.dropna()) < min_bars:
                return True
            ratio_ema = ratio_series.ewm(span=ema_period, adjust=False).mean()
            current_ratio = ratio_series.iloc[-1]
            current_ema = ratio_ema.iloc[-1]
            is_outperforming = (current_ratio > current_ema)
            if not is_outperforming:
                logger.debug(
                    f"[{symbol} RS] Diblokir: Ratio={current_ratio:.6f} <= EMA({ema_period})={current_ema:.6f} "
                    f"(Altcoin sedang underperform BTC)"
                )
            return is_outperforming


    def get_symbol_ltf(self, symbol: str) -> str:
        """Mengembalikan LTF adaptif: 5m untuk BTC dan LINK, 15m untuk Altcoin lainnya."""
        if hasattr(self.config, 'get_ltf_for_symbol'):
            return self.config.get_ltf_for_symbol(symbol)
        sym_clean = symbol.upper().replace('/', '')
        for fast_sym in ["BTC", "LINK"]:
            if fast_sym in sym_clean:
                return getattr(self.config, 'btc_lower_timeframe', '5m')
        return getattr(self.config, 'alt_lower_timeframe', '15m')

    def update_btc_market_status(self, df_btc_htf: pd.DataFrame) -> bool:
        """
        Mengevaluasi status tren Bitcoin 1H sebagai Induk Pasar (BTC Market Filter).
        Crash Protection Only: HANYA akan memblokir altcoin jika BTC
        mengalami drop ekstrem (misal > 2.0%) dalam 1 bar.
        Trend/sideways diabaikan agar Altcoin bebas terbang.
        """
        if df_btc_htf.empty or len(df_btc_htf) < 2:
            self.btc_market_bullish = True
            return self.btc_market_bullish

        last_row = df_btc_htf.iloc[-1]

        # Hitung perubahan persentase (ROC / Change %) BTC dalam 1 bar terakhir
        pct_change = ((last_row['close'] - last_row['open']) / last_row['open']) * 100.0

        crash_threshold = getattr(self.config, 'btc_crash_threshold_pct', -2.0)

        if pct_change <= crash_threshold:
            self.btc_market_bullish = False
            logger.debug(f"[BTC Market] Crash terdeteksi: {pct_change:.2f}% <= {crash_threshold}%. Memblokir Altcoin.")
        else:
            self.btc_market_bullish = True

        return self.btc_market_bullish

    def get_context(self, symbol: str) -> SymbolContext:
        """Mengambil atau membuat context baru untuk sebuah symbol."""
        if symbol not in self.contexts:
            self.contexts[symbol] = SymbolContext(symbol=symbol)
        return self.contexts[symbol]

    def analyze_higher_timeframe(self, symbol: str, df_htf: pd.DataFrame) -> SymbolContext:
        """
        Mengevaluasi tren makro (HTF).
        """
        context = self.get_context(symbol)
        
        # HAPUS pemanggilan indicators.prepare_htf_frame di sini
        # df_htf sudah mengandung kolom indikator hasil pre-calculation
        
        if df_htf.empty or len(df_htf) < 2:
            logger.warning(f"[{symbol} HTF] Data tidak cukup untuk analisa.")
            return context

        last_row = df_htf.iloc[-1]
        prev_row = df_htf.iloc[-2]
        current_price = last_row['close']

        # 2. Logika Tren: Bias Bullish (Indikator Non-Lagging)
        # Menghapus syarat lambat EMA 50 > TEMA 200 agar bot dapat menangkap awal tren lebih dini
        ema_slope_up = last_row['ema'] > prev_row['ema']
        indicator_bullish = (
            (last_row['close'] > last_row['ema']) and 
            ema_slope_up and 
            (last_row['adx'] >= self.config.adx_threshold)
        )

        # Altcoin Macro Golden Cross & Baseline Filter (EMA 50 > TEMA 200 & Close > TEMA 200)
        # Menolak setup jika Altcoin sedang berada di rezim Death Cross / Bearish Trend Makro
        is_altcoin = "BTC" not in symbol.upper()
        require_gc = getattr(self.config, 'alt_require_golden_cross', True)
        if is_altcoin and require_gc:
            tema_val = last_row.get('tema')
            ema_val = last_row.get('ema')
            if tema_val is not None and not pd.isna(tema_val) and ema_val is not None and not pd.isna(ema_val):
                if ema_val <= tema_val or last_row['close'] <= tema_val:
                    logger.debug(f"[{symbol} HTF] Altcoin ditolak: Belum Golden Cross (EMA50: {ema_val:.4f} <= TEMA200: {tema_val:.4f}) atau Close <= TEMA200.")
                    indicator_bullish = False

        # 3. Logika Struktur SMC: Higher High + Higher Low (Non-Lagging)
        # Memastikan market sedang membentuk pola HH-HL yang valid
        # sebelum mencari Order Block / FVG
        structure_bullish = detect_bullish_market_structure(df_htf, left_bars=5, right_bars=2)

        # Gabungkan kedua syarat: indikator DAN struktur harus bullish
        is_bullish = indicator_bullish and structure_bullish
        context.is_bullish = is_bullish

        if not is_bullish:
            if not indicator_bullish:
                logger.debug(f"[{symbol} HTF] Indikator tidak bullish (ADX: {last_row['adx']:.2f}).")
            if not structure_bullish:
                logger.debug(f"[{symbol} HTF] Struktur pasar tidak bullish (Tidak ada HH+HL).")
            context.active_zones = []
            return context

        # 3. Cari Zona (Gunakan df_htf langsung)
        obs = detect_bullish_order_blocks_with_bos(
            df_htf, symbol, self.config.higher_timeframe, 
            self.config.order_block_lookback, self.config.order_block_impulse_multiplier
        )
        fvgs = detect_bullish_fvgs(df_htf, symbol, self.config.higher_timeframe)
        
        all_zones = obs + fvgs

        # 4. Filter & Merge Zones (dengan kedaluwarsa & mitigasi historis)
        current_time = last_row['timestamp'] if 'timestamp' in last_row else df_htf.index[-1]
        valid_zones = merge_waiting_zones(
            all_zones, 
            current_price=current_price, 
            max_distance_pct=self.config.max_zone_distance_pct,
            current_time=current_time,
            max_expiry_bars=self.config.zone_expiry_bars,
            df_htf=df_htf
        )
        context.active_zones = valid_zones[-self.config.max_zones_per_symbol:]
        context.last_updated = datetime.now(timezone.utc)
        
        return context

    def evaluate_lower_timeframe(self, symbol: str, df_ltf: pd.DataFrame, context: SymbolContext,
                                  df_htf: pd.DataFrame = None, df_btc_htf: pd.DataFrame = None) -> Optional[TradeSignal]:
        """
        Mencari trigger eksekusi (LTF).

        df_htf      : DataFrame HTF 1H koin itu sendiri (untuk RS filter Alt/BTC).
        df_btc_htf  : DataFrame HTF 1H BTC/USDT (untuk RS filter Alt/BTC).
        """
        if not context.is_bullish or not context.active_zones:
            return None

        # BTC Market Filter: Blokir entry Altcoin jika Bitcoin sedang bearish/di bawah EMA 50
        if self.config.enable_btc_filter and symbol != self.config.btc_filter_symbol:
            if not self.btc_market_bullish:
                logger.debug(f"[{symbol} LTF] Sinyal dibatalkan: Induk Pasar ({self.config.btc_filter_symbol}) sedang Bearish / di bawah EMA 50!")
                return None

        # === ADAPTIVE RELATIVE STRENGTH FILTER ===
        # Hanya memeriksa simbol yang terdaftar di rs_target_symbols.
        # Bypass mekanisme ini jika enable_relative_strength_filter False.
        if getattr(self.config, 'enable_relative_strength_filter', True):
            rs_targets = getattr(self.config, 'rs_target_symbols', [])
            if symbol in rs_targets:
                rs_pass = self.check_relative_strength(symbol, df_htf, df_btc_htf)
                if not rs_pass:
                    logger.info(f"[{symbol} RS] Sinyal diblokir oleh Adaptive RS Filter (Altcoin sedang underperform BTC).")
                    return None

        if df_ltf.empty or len(df_ltf) < 2:
            return None

        # FILTER TREN MANDIRI ASET PADA HTF
        # Mencegah entry pada saat kondisi tren HTF koin itu sendiri sudah hancur (di bawah EMA 50)
        # Sesuai request, filter ini diterapkan spesifik dan diwajibkan untuk koin LINK
        if "LINK" in symbol.upper():
            if df_htf is not None and not df_htf.empty:
                htf_last_row = df_htf.iloc[-1]
                if 'ema' in htf_last_row and not pd.isna(htf_last_row['ema']):
                    if htf_last_row['close'] <= htf_last_row['ema']:
                        logger.debug(f"[{symbol} LTF] Sinyal LINK dibatalkan: Harga Close HTF ({htf_last_row['close']:.4f}) <= EMA50 HTF ({htf_last_row['ema']:.4f}).")
                        return None

        last_row = df_ltf.iloc[-1]
        current_price = last_row['close']


        # 2. Cek apakah harga masuk ke zona aktif yang masih valid
        active_zone = None
        for z in context.active_zones:
            if z.zone_id in context.traded_zone_ids:
                continue
            
            # Pengaman Mitigasi: Jangan beli jika harga candle LTF sudah jebol ke bawah zona
            if current_price < z.lower:
                continue
            
            if z.contains(current_price) or z.contains(last_row['low']):
                active_zone = z
                break

        if not active_zone:
            return None

        # =====================================================================
        # 3. LOGIKA BARU: Micro Break of Structure berbasis True Swing High
        # =====================================================================
        # Ambil irisan 60 candle terakhir agar bot tetap ringan (tidak boros CPU)
        df_recent = df_ltf.tail(60).copy()
        
        # Deteksi struktur swing (Butuh 3 baris kiri dan 2 baris kanan untuk konfirmasi puncak)
        df_recent = detect_swing_points(df_recent, left_bars=3, right_bars=2)
        
        # Ambil nilai Swing High terakhir yang terkonfirmasi (dari baris sebelum current candle)
        true_swing_high = df_recent['last_swing_high'].iloc[-2]
        
        # Fallback pengaman: Jika pasar sangat volatil/choppy dan tidak ada struktur 
        # swing yang utuh terbentuk dalam 60 bar terakhir, gunakan highest high 20 candle.
        if pd.isna(true_swing_high):
            true_swing_high = df_recent['high'].iloc[-20:-1].max()

        is_bullish_candle = last_row['close'] > last_row['open']
        
        # Micro BOS tervalidasi jika Close menembus True Swing High
        micro_bos = last_row['close'] > true_swing_high

        # --- FILTER BODY DOMINAN ---
        # Candle penembus harus memiliki body minimal (default 40% dari total range).
        # Menyaring candle doji / berekor panjang yang menandakan keragu-raguan pasar.
        min_body_ratio = getattr(self.config, 'ltf_min_body_ratio', 0.40)
        candle_range = last_row['high'] - last_row['low']
        candle_body = abs(last_row['close'] - last_row['open'])
        body_ratio = (candle_body / candle_range) if candle_range > 0 else 0
        body_dominant = body_ratio >= min_body_ratio

        # --- FILTER VOLUME SPIKE ---
        # Volume harus >= (default 1.1x rata-rata 20 candle terakhir)
        # untuk memastikan ada partisipasi volume yang cukup signifikan.
        vol_multiplier = getattr(self.config, 'ltf_volume_multiplier', 1.1)
        avg_volume = df_ltf['volume'].iloc[-21:-1].mean()
        volume_spike = last_row['volume'] > (avg_volume * vol_multiplier)

        # 4. Validasi Volatilitas (Kolom ATR sudah ada dari pre-calculation)
        atr_value = last_row['atr']
        atr_valid = atr_value > 0

        # --- FILTER ANTI-OVERBOUGHT (STOCHASTIC) ---
        # Cegah pembelian di pucuk swing / saat euforia pasar jenuh beli (%K > stochastic_overbought)
        max_stoch = getattr(self.config, 'stochastic_overbought', 75.0)
        stoch_k = last_row.get('stoch_k', None)
        not_overbought = True
        if stoch_k is not None and not pd.isna(stoch_k):
            if stoch_k > max_stoch:
                logger.debug(f"[{symbol} LTF] Sinyal dibatalkan: Stochastic %K ({stoch_k:.1f}) melebihi batas overbought ({max_stoch}).")
                not_overbought = False

        if is_bullish_candle and micro_bos and body_dominant and volume_spike and atr_valid and not_overbought:
            stoch_info = f", Stoch: {stoch_k:.1f}" if (stoch_k is not None and not pd.isna(stoch_k)) else ""
            logger.info(f"[{symbol} LTF] Micro BOS (True Swing High @ {true_swing_high:.4f}) Terdeteksi di zona {active_zone.zone_type}! Body: {body_ratio:.0%}, Vol: {last_row['volume']:.0f}/{avg_volume:.0f}{stoch_info}")
            
            # Relaksasi Retest Entry (15m Optimal):
            # Pada 15m, tidak perlu menanti pullback body candle. Langsung Limit di batas struktural:
            # Jika tipe zona FVG -> Limit di Equilibrium (50%) FVG
            # Jika tipe zona OB -> Limit di Upper Border (tepi atas OB)
            is_altcoin = "BTC" not in symbol.upper()
            enable_retest = getattr(self.config, 'enable_adaptive_retest', True)
            retest_altcoins_only = getattr(self.config, 'retest_altcoins_only', False)
            
            use_retest = enable_retest and (is_altcoin if retest_altcoins_only else True)
            if use_retest:
                if active_zone.zone_type == "FVG":
                    retest_p = (active_zone.upper + active_zone.lower) / 2.0
                    reason_type = "FVG 50% Eq Limit"
                else:
                    # OB_BOS atau OB
                    retest_p = active_zone.upper
                    reason_type = "OB Upper Edge Limit"

                # Pengaman: pastikan retest tidak melampaui harga close saat ini (jika close < upper OB, gunakan close)
                retest_p = min(retest_p, current_price * 0.9995)
                
                entry_type = "LIMIT_RETEST"
                entry_price = retest_p
                timeout_bars = getattr(self.config, 'retest_timeout_bars', 8)
                reason_str = f"{active_zone.zone_type} Rejection + True BOS + {reason_type} + Vol({last_row['volume']:.0f}/{avg_volume:.0f}){stoch_info}"
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

        return None


    def mark_zone_traded(self, symbol: str, zone_id: str) -> None:
        """
        Menandai zona sudah dieksekusi agar tidak terjadi double entry 
        jika harga masih berkutat di dalam area zona tersebut.
        """
        context = self.get_context(symbol)
        context.traded_zone_ids.add(zone_id)
        logger.debug(f"[{symbol}] Zone {zone_id} ditandai sebagai 'Traded'.")

    def unmark_zone(self, symbol: str, zone_id: str) -> None:
        """
        Menghapus tanda 'Traded' jika order batal/gagal dieksekusi di market,
        sehingga zona bisa dipantau kembali.
        """
        context = self.get_context(symbol)
        if zone_id in context.traded_zone_ids:
            context.traded_zone_ids.remove(zone_id)
            logger.debug(f"[{symbol}] Zone {zone_id} dikembalikan statusnya (Unmarked).")