import pandas as pd
import numpy as np
import ta
from typing import Tuple

def ema(series: pd.Series, period: int) -> pd.Series:
    """
    Menghitung Exponential Moving Average (EMA).
    """
    indicator = ta.trend.EMAIndicator(close=series, window=period)
    return indicator.ema_indicator()

def tema(series: pd.Series, period: int) -> pd.Series:
    """
    Menghitung Triple Exponential Moving Average (TEMA) secara manual
    karena library 'ta' standar tidak memiliki TEMAIndicator.
    Rumus: (3 * EMA1) - (3 * EMA2) + EMA3
    """
    # Menggunakan fungsi ewm (Exponential Weighted Math) bawaan pandas
    ema1 = series.ewm(span=period, adjust=False).mean()
    ema2 = ema1.ewm(span=period, adjust=False).mean()
    ema3 = ema2.ewm(span=period, adjust=False).mean()
    
    return (3 * ema1) - (3 * ema2) + ema3

def stochastic(high: pd.Series, low: pd.Series, close: pd.Series, 
               k_period: int, k_smoothing: int, d_period: int) -> Tuple[pd.Series, pd.Series]:
    """
    Menghitung Smoothed Stochastic Oscillator.
    Menghasilkan %K (smoothed) dan %D (signal line).
    """
    # 1. Hitung Fast %K standar menggunakan library ta
    stoch_indicator = ta.momentum.StochasticOscillator(
        high=high, low=low, close=close, window=k_period, smooth_window=d_period
    )
    fast_k = stoch_indicator.stoch()
    
    # 2. Terapkan smoothing pada Fast %K untuk mendapatkan Slow/Smoothed %K
    smooth_k = fast_k.rolling(window=k_smoothing).mean()
    
    # 3. Hitung %D dengan mengambil Simple Moving Average dari Smoothed %K
    smooth_d = smooth_k.rolling(window=d_period).mean()
    
    return smooth_k, smooth_d

def prepare_htf_frame(df: pd.DataFrame, ema_period: int = 50, 
                      tema_period: int = 200, adx_period: int = 10) -> pd.DataFrame:
    """
    Menyiapkan DataFrame untuk Higher Timeframe (HTF) dengan menambahkan indikator tren.
    Output membuang nilai NaN/Null akibat lookback calculation (dropna).
    """
    # Menggunakan copy untuk mencegah pandas SettingWithCopyWarning
    df_htf = df.copy()
    
    df_htf['ema'] = ema(df_htf['close'], period=ema_period)
    df_htf['tema'] = tema(df_htf['close'], period=tema_period)
    
    adx_indicator = ta.trend.ADXIndicator(
        high=df_htf['high'], low=df_htf['low'], close=df_htf['close'], window=adx_period
    )
    df_htf['adx'] = adx_indicator.adx()
    
    return df_htf.dropna()

def prepare_ltf_frame(df: pd.DataFrame, atr_period: int = 14,
                      stoch_k: int = 14, stoch_k_smooth: int = 3, stoch_d: int = 3) -> pd.DataFrame:
    """
    Menyiapkan DataFrame LTF.
    Menghitung ATR untuk manajemen risiko dinamis dan Smoothed Stochastic Oscillator
    sebagai filter anti-overbought / anti-FOMO.
    """
    df_ltf = df.copy()
    
    # Hitung ATR
    atr_indicator = ta.volatility.AverageTrueRange(
        high=df_ltf['high'], low=df_ltf['low'], close=df_ltf['close'], window=atr_period
    )
    df_ltf['atr'] = atr_indicator.average_true_range()
    
    # Hitung Smoothed Stochastic Oscillator (%K dan %D)
    k, d = stochastic(
        high=df_ltf['high'], low=df_ltf['low'], close=df_ltf['close'],
        k_period=stoch_k, k_smoothing=stoch_k_smooth, d_period=stoch_d
    )
    df_ltf['stoch_k'] = k
    df_ltf['stoch_d'] = d
    
    return df_ltf.dropna()

