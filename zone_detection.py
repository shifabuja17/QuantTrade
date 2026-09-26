import uuid
import pandas as pd
import numpy as np
from typing import List, Union
from dataclasses import dataclass, field
from datetime import datetime

@dataclass
class Zone:
    """
    Representasi dari zona Order Block (OB) atau Fair Value Gap (FVG).
    """
    symbol: str
    zone_type: str  # "OB" atau "FVG"
    timeframe: str
    lower: float
    upper: float
    created_at: Union[datetime, pd.Timestamp]
    source_index: Union[datetime, pd.Timestamp]
    zone_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    is_fresh: bool = True

    def contains(self, price: float) -> bool:
        """Mengecek apakah harga saat ini berada di dalam zona ini."""
        return self.lower <= price <= self.upper
    
def detect_swing_points(df: pd.DataFrame, left_bars: int = 5, right_bars: int = 2) -> pd.DataFrame:
    """
    Mendeteksi Swing High dan Swing Low yang sudah terkonfirmasi (Numpy Accelerated).
    """
    data = df.copy()
    n = len(data)
    is_swing_high = np.zeros(n, dtype=bool)
    swing_high_val = np.full(n, np.nan)
    is_swing_low = np.zeros(n, dtype=bool)
    swing_low_val = np.full(n, np.nan)
    
    highs = data['high'].to_numpy()
    lows = data['low'].to_numpy()
    
    for i in range(left_bars, n - right_bars):
        h = highs[i]
        if h == np.max(highs[i - left_bars : i + right_bars + 1]):
            is_swing_high[i + right_bars] = True
            swing_high_val[i + right_bars] = h

        l = lows[i]
        if l == np.min(lows[i - left_bars : i + right_bars + 1]):
            is_swing_low[i + right_bars] = True
            swing_low_val[i + right_bars] = l

    data['is_swing_high'] = is_swing_high
    data['swing_high_val'] = swing_high_val
    data['is_swing_low'] = is_swing_low
    data['swing_low_val'] = swing_low_val
    data['last_swing_high'] = data['swing_high_val'].ffill()
    data['last_swing_low'] = data['swing_low_val'].ffill()
    
    return data


def detect_bullish_market_structure(df: pd.DataFrame, left_bars: int = 5, right_bars: int = 2) -> bool:
    """
    Mengevaluasi apakah struktur pasar pada timeframe ini menunjukkan kecenderungan Bullish.
    
    Logika (Dilonggarkan):
    - Ambil 2 Swing High terkonfirmasi terakhir → cek Higher High (SH2 > SH1)
    - Ambil 2 Swing Low terkonfirmasi terakhir  → cek Higher Low  (SL2 > SL1)
    - Minimal SALAH SATU syarat terpenuhi (HH ATAU HL).
    
    Returns:
        True jika minimal ada satu tanda bullish (HH atau HL), False jika keduanya bearish.
    """
    data = detect_swing_points(df, left_bars=left_bars, right_bars=right_bars)
    
    # Kumpulkan semua Swing High yang terkonfirmasi (bukan NaN)
    swing_highs = data.loc[data['swing_high_val'].notna(), 'swing_high_val']
    swing_lows = data.loc[data['swing_low_val'].notna(), 'swing_low_val']
    
    # Butuh minimal 2 swing point untuk membandingkan struktur
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return False
    
    # Ambil 2 terakhir
    last_two_highs = swing_highs.iloc[-2:]
    last_two_lows = swing_lows.iloc[-2:]
    
    # Higher High: Swing High terbaru > Swing High sebelumnya
    higher_high = last_two_highs.iloc[-1] > last_two_highs.iloc[-2]
    
    # Higher Low: Swing Low terbaru > Swing Low sebelumnya
    higher_low = last_two_lows.iloc[-1] > last_two_lows.iloc[-2]
    
    # Minimal salah satu tanda bullish harus ada
    return higher_high or higher_low

def detect_bullish_order_blocks_with_bos(df: pd.DataFrame, symbol: str, timeframe: str, 
                                lookback: int = 50, impulse_multiplier: float = 2.0,
                                max_history_bars: int = 120) -> List[Zone]:
    """
    Mendeteksi Bullish Order Blocks dengan Validasi Break of Structure (BOS).
    Dibatasi hingga max_history_bars terbaru agar efisien dan bebas zona purba.
    """
    zones: List[Zone] = []
    if df.empty or len(df) < 15:
        return zones
        
    # Ambil irisan candle secukupnya untuk lookback dan swing detection
    data_slice = df.tail(max_history_bars).copy()
    data = detect_swing_points(data_slice, left_bars=5, right_bars=2)
    
    # 2. Kalkulasi ukuran body (seperti sebelumnya)
    data['body'] = abs(data['close'] - data['open'])
    data['avg_body'] = data['body'].rolling(window=lookback).mean()
    
    # 3. Logika Arah
    data['is_bearish'] = data['close'] < data['open']
    data['is_bullish'] = data['close'] > data['open']
    
    cond_prev_bear = data['is_bearish'].shift(1)
    cond_curr_bull_impulse = data['is_bullish'] & (data['body'] > (data['avg_body'].shift(1) * impulse_multiplier))
    
    # 4. Syarat Baru: Break of Structure (BOS)
    # Harga close dari candle impulse HARUS lebih tinggi dari Swing High terakhir
    cond_bos = data['close'] > data['last_swing_high'].shift(1)
    
    # Terapkan semua kondisi
    valid_ob_signals = data[cond_prev_bear & cond_curr_bull_impulse & cond_bos]
    
    for idx, row in valid_ob_signals.iterrows():
        prev_idx = data.index.get_loc(idx) - 1
        prev_row = data.iloc[prev_idx]
        
        source_ts = str(prev_row['timestamp'])
        zid = f"{symbol}_{timeframe}_OB_{source_ts}"
        zone = Zone(
            symbol=symbol,
            zone_type="OB_BOS", # Menandakan OB ini tervalidasi BOS
            timeframe=timeframe,
            lower=prev_row['low'],
            upper=prev_row['high'],
            created_at=row['timestamp'],
            source_index=prev_row['timestamp'],
            zone_id=zid
        )
        zones.append(zone)
        
    return zones

def detect_bullish_fvgs(df: pd.DataFrame, symbol: str, timeframe: str, max_history_bars: int = 120) -> List[Zone]:
    """
    Mendeteksi Bullish Fair Value Gaps (FVG).
    Logika: High dari Candle 1 lebih rendah dari Low dari Candle 3, menciptakan gap.
    Dibatasi hingga max_history_bars terbaru agar efisien dan bebas zona purba.
    """
    zones: List[Zone] = []
    if df.empty or len(df) < 3:
        return zones
        
    data = df.tail(max_history_bars).copy()
    
    # Shift(2) merujuk ke Candle 1, baris saat ini (row) merujuk ke Candle 3
    # Shift(1) adalah Candle 2 (Candle impulse besar yang menyebabkan gap)
    cond_fvg = data['high'].shift(2) < data['low']
    
    # Filter opsional agar Candle 2 harus bullish
    cond_c2_bullish = data['close'].shift(1) > data['open'].shift(1)
    
    fvg_signals = data[cond_fvg & cond_c2_bullish]
    
    for idx, row in fvg_signals.iterrows():
        c1_idx = data.index.get_loc(idx) - 2
        c1_row = data.iloc[c1_idx]
        
        c3_row = row
        
        source_ts = str(c1_row['timestamp'])
        zid = f"{symbol}_{timeframe}_FVG_{source_ts}"
        # Gap tercipta antara High C1 dan Low C3
        zone = Zone(
            symbol=symbol,
            zone_type="FVG",
            timeframe=timeframe,
            lower=c1_row['high'],
            upper=c3_row['low'],
            created_at=c3_row['timestamp'],  # FVG valid setelah C3 tertutup
            source_index=c1_row['timestamp'], # Root of the gap start
            zone_id=zid
        )
        zones.append(zone)
        
    return zones

def merge_waiting_zones(
    zones: List[Zone], 
    current_price: float, 
    max_distance_pct: float = 2.0,
    current_time: Union[datetime, pd.Timestamp, None] = None,
    max_expiry_bars: int = 48,
    df_htf: Union[pd.DataFrame, None] = None
) -> List[Zone]:
    """
    Memfilter dan mengelola zona-zona yang sedang menunggu (pending zones).
    
    Filter yang diperbarui:
    1. Filter Kedaluwarsa (Zone Expiry): Membuang zona yang umurnya > max_expiry_bars jam/bar.
    2. Filter Mitigasi Historis (Unmitigated Check): Membuang zona yang pernah ditembus ke bawah
       oleh candle historis setelah zona terbentuk.
    3. Mempertahankan zona jika harga di atasnya (dalam batas jarak maksimal).
    4. MEMPERTAHANKAN zona jika harga sedang berada di dalamnya (Retest).
    5. Membuang zona jika harga saat ini sudah menembus batas bawahnya (Invalidasi).
    """
    valid_zones: List[Zone] = []
    curr_dt = pd.to_datetime(current_time) if current_time is not None else None
    
    for z in zones:
        z_created_dt = pd.to_datetime(z.created_at)
        
        # --- FILTER 1: KEDALUWARSA ZONA (ZONE EXPIRY) ---
        if curr_dt is not None:
            age_hours = (curr_dt - z_created_dt).total_seconds() / 3600.0
            if age_hours > max_expiry_bars:
                continue

        # --- FILTER 2: MITIGASI / BREAK HISTORIS ---
        # Jika ada candle antara waktu terbentuknya zona dan saat ini yang menembus ke bawah z.lower,
        # zona tersebut sudah rusak/ter-mitigasi secara tidak valid di masa lalu.
        if df_htf is not None and curr_dt is not None:
            try:
                if 'timestamp' in df_htf.columns:
                    ts_col = pd.to_datetime(df_htf['timestamp'])
                    mask = (ts_col > z_created_dt) & (ts_col < curr_dt)
                    sub = df_htf[mask]
                else:
                    sub = df_htf.loc[z_created_dt:curr_dt]
                    
                if not sub.empty and (sub['low'].min() < z.lower):
                    continue
            except Exception:
                pass

        # --- FILTER 3: POSISI HARGA SAAT INI TERHADAP ZONA ---
        # Skenario 1: Harga masih di atas zona (menunggu pullback ke bawah)
        if current_price > z.upper:
            distance_pct = ((current_price - z.upper) / current_price) * 100.0
            if distance_pct <= max_distance_pct:
                valid_zones.append(z)
                
        # Skenario 2: Harga SEDANG berada di dalam zona (Fase krusial untuk entry LTF)
        elif z.lower <= current_price <= z.upper:
            valid_zones.append(z)
            
        # Skenario 3: Harga < z.lower -> Terbuang otomatis karena tembus batas bawah
                
    return valid_zones