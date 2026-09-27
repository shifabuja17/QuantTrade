import json
import os
import logging
from dataclasses import dataclass, field, fields
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

def _safe_init(cls, data: Dict[str, Any]) -> Any:
    """
    Helper untuk menginisialisasi dataclass secara aman.
    Mengabaikan key dari JSON yang tidak ada di dalam definisi dataclass.
    """
    valid_keys = {f.name for f in fields(cls)}
    filtered_data = {k: v for k, v in data.items() if k in valid_keys}
    return cls(**filtered_data)

@dataclass
class ExchangeConfig:
    exchange_id: str = "binance"
    api_key: str = ""
    api_secret: str = ""
    enable_rate_limit: bool = True
    recv_window: int = 5000
    testnet: bool = False

@dataclass
class StrategyConfig:
    higher_timeframe: str = "1h"
    lower_timeframe: str = "15m"
    btc_lower_timeframe: str = "15m"       # Standarisasi minimal 15m untuk memangkas fee impact
    alt_lower_timeframe: str = "15m"
    enable_dynamic_timeframe: bool = True
    dynamic_candidate_timeframes: List[str] = field(default_factory=lambda: ["5m", "15m"])
    dynamic_timeframe_evaluation_bars: int = 1000
    dynamic_timeframe_min_trades: int = 5
    dynamic_timeframe_min_profit_factor: float = 1.05
    dynamic_timeframe_min_expectancy_pct: float = 0.0
    dynamic_timeframe_switch_margin: float = 0.10
    dynamic_timeframe_cache_minutes: int = 1440
    alt_require_golden_cross: bool = True
    btc_require_tema_filter: bool = True
    htf_limit: int = 1000
    ltf_limit: int = 1000
    ema_period: int = 50
    tema_period: int = 200
    adx_threshold: int = 20                # Dilonggarkan kembali ke 20 (berkaca pada profit ETH baseline)
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
    ltf_min_body_ratio: float = 0.50       # Breakout wajib 50% nyata
    ltf_volume_multiplier: float = 0.5     # Sangat dilonggarkan agar tidak memotong rally kompresi
    stochastic_overbought: float = 70.0    # Batas atas Stochastic %K (diketatkan ke 70 agar tidak beli di pucuk)
    ltf_min_rsi: float = 50.0              # Batas minimal RSI LTF untuk memastikan ada momentum bullish
    btc_min_adx: float = 18.0              # Batas minimal ADX BTC untuk filter kekuatan tren (mencegah beli saat BTC sideway mati)
    btc_crash_threshold_pct: float = -2.0  # Deviasi ekstrem (misal BTC turun > 2.0% dalam 1 jam)
    enable_early_invalidation: bool = True # Cut loss dini saat candle 1H jebol di bawah EMA 50 atau BTC Bearish
    invalidation_check_htf_ema: bool = True
    invalidation_check_btc_filter: bool = True
    enable_adaptive_retest: bool = False   # Eksekusi instan di Market (karena retest jarang terjadi di 15m)
    retest_body_ratio: float = 0.35        # Retest sedalam 35%
    retest_timeout_bars: int = 8           # Batas waktu tunggu limit order retest (8 bar LTF)
    retest_altcoins_only: bool = False     # BTC juga dipaksa retest (mencegah market order di pucuk candle)
    fast_symbols: List[str] = field(default_factory=lambda: ["BTC/USDT", "LINK/USDT"])

    # === ADAPTIVE RELATIVE STRENGTH FILTER ===
    # Filter selektif untuk altcoin fakeout-prone (ETH, AVAX, SOL) agar hanya
    # long saat rasio Alt/BTC sedang outperform (di atas EMA rasio).
    # Koin high-alpha (BTC, LINK, ADA, DOGE) tidak difilter agar tidak memotong
    # early breakout yang profitable.
    enable_relative_strength_filter: bool = True   # Master switch RS filter
    rs_ema_period: int = 20                         # Periode EMA dari rasio Alt/BTC (EMA 20 terbukti optimal di simulasi)
    rs_roc_period: int = 24                         # Periode ROC dari rasio Alt/BTC (alternatif mode ROC)
    rs_filter_mode: str = "EMA"                     # "EMA" atau "ROC" — mode pengecekan rasio
    rs_fail_closed_on_missing_data: bool = True      # Jangan izinkan LONG target RS jika data pembanding belum tersedia
    rs_target_symbols: List[str] = field(          # Simbol yang WAJIB lolos RS sebelum LONG diizinkan
        default_factory=lambda: ["ETH/USDT", "AVAX/USDT", "SOL/USDT"]
    )
    # Simbol yang menggunakan mode ROC (override rs_filter_mode untuk simbol tertentu).
    # ETH pakai EMA (lebih stabil, terbukti +$18.10 PF 40.37), SOL/AVAX pakai ROC (lebih responsif).
    rs_roc_symbols: List[str] = field(
        default_factory=lambda: ["SOL/USDT", "AVAX/USDT"]
    )

    def get_ltf_for_symbol(self, symbol: str) -> str:
        """Mengembalikan LTF adaptif: 5m untuk BTC dan LINK, 15m untuk Altcoin lainnya."""
        sym_clean = symbol.upper().replace('/', '')
        for fast_sym in self.fast_symbols:
            if fast_sym.upper().replace('/', '') in sym_clean:
                return self.btc_lower_timeframe
        return self.alt_lower_timeframe

@dataclass
class RiskConfig:
    max_risk_per_trade: float = 2.0  # Dalam persen (%) - 2.0% ($20 per trade)
    max_daily_loss_pct: float = 5.0
    max_daily_profit_pct: float = 6.0
    sl_atr_multiplier: float = 1.5
    tp_atr_multiplier: float = 3.5           # TP2 diperlebar ke 3.5R untuk membiarkan winner berlari
    enable_partial_tp: bool = True           # Aktifkan kembali Partial TP
    partial_tp_ratio: float = 0.5            # Porsi posisi yang dijual di TP1 (50%)
    partial_tp_atr_multiplier: float = 1.5   # TP1 di 1.5R
    bep_trigger_atr_multiplier: float = 1.5  # Mulai aktifkan trailing saat TP1 (1.5R) hit
    bep_profit_pct: float = 0.15             # Buffer fee exchange 0.15% di atas entry
    enable_early_invalidation: bool = True   # Cut loss dini
    invalidation_check_htf_ema: bool = True
    invalidation_check_btc_filter: bool = True
    min_notional: float = 5.0  # Batas minimum order exchange (misal Binance = 5 USDT)
    max_quote_allocation_pct: float = 90.0
    use_static_sl_tp: bool = False
    min_rrr: float = 1.8                     # Syarat Mutlak Minimal Net Reward-to-Risk = 1.8

    min_sl_distance_pct: float = 0.8  # Jarak SL minimal absolut 0.8% dari harga entri (mencegah fee inflation)
    max_sl_distance_pct: float = 1.5  # Jarak SL maksimal BTC 1.5% dari harga entri
    alt_min_sl_distance_pct: float = 1.5 # Jarak SL minimal Altcoin 1.5% agar kebal jarum volatilitas 15m
    alt_sl_atr_multiplier: float = 1.8   # Pengali ATR untuk SL Altcoin (1.5x - 2.0x ATR)
    alt_max_sl_distance_pct: float = 3.5 # Jarak SL maksimal untuk Altcoin agar tidak tersapu jarum (3.5%)
    btc_symbol: str = "BTC/USDT"
    max_fee_to_risk_ratio: float = 0.45 # Maksimal fee memakan 45% dari toleransi risiko (dinaikkan dari 30%)
    estimated_exchange_fee_pct: float = 0.2

@dataclass
class ExecutionConfig:
    dry_run: bool = True
    enable_exchange_protection: bool = True
    poll_interval_seconds: int = 5
    max_concurrent_symbols: int = 3
    cooldown_minutes: int = 15
    sl_cooldown_minutes: int = 240
    quote_currency: str = "USDT"
    paper_cash: float = 1000.0
    max_slippage_pct: float = 0.1

@dataclass
class LoggingConfig:
    level: str = "INFO"
    log_file: str = "bot.log"
    trade_journal_file: str = "trades.csv"

@dataclass
class NotificationConfig:
    enabled: bool = True
    telegram_bot_token: str = "8608602887:AAHn6i1QWErp4iejksZ1uffXMc8LjWBBqfA"
    telegram_chat_id: str = "5465938032"

@dataclass
class BotConfig:
    symbols: List[str] = field(default_factory=lambda: ["BTC/USDT", "LINK/USDT", "ADA/USDT"])
    exchange: ExchangeConfig = field(default_factory=ExchangeConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    notification: NotificationConfig = field(default_factory=NotificationConfig)

def load_config(path: str = "config.json") -> BotConfig:
    """
    Memuat konfigurasi dari file JSON. Jika file tidak ada atau ada data yang kosong,
    akan menggunakan nilai default yang ada pada dataclasses.
    """
    data = {}
    
    # 1. Baca dari file JSON jika ada
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                data = json.load(f)
            logger.info(f"Berhasil memuat konfigurasi dari {path}")
        except json.JSONDecodeError as e:
            logger.error(f"Gagal parsing {path}: {e}. Menggunakan default config.")
        except Exception as e:
            logger.error(f"Error membaca {path}: {e}. Menggunakan default config.")
    else:
        logger.warning(f"File {path} tidak ditemukan. Membuat config instance dengan default value.")

    # 2. Ambil data nested dictionary
    exchange_data = data.get("exchange", {})
    strategy_data = data.get("strategy", {})
    risk_data = data.get("risk", {})
    execution_data = data.get("execution", {})
    logging_data = data.get("logging", {})
    notification_data = data.get("notification", {})

    # 3. Fallback ke Environment Variables untuk kredensial API (Aman)
    # Variabel env akan menimpa nilai dari file JSON jika tersedia.
    exchange_data["api_key"] = os.getenv("EXCHANGE_API_KEY", exchange_data.get("api_key", ""))
    exchange_data["api_secret"] = os.getenv("EXCHANGE_API_SECRET", exchange_data.get("api_secret", ""))

    # 4. Bangun instance BotConfig
    config = BotConfig(
        symbols=data.get("symbols", ["BTC/USDT", "ETH/USDT"]),
        exchange=_safe_init(ExchangeConfig, exchange_data),
        strategy=_safe_init(StrategyConfig, strategy_data),
        risk=_safe_init(RiskConfig, risk_data),
        execution=_safe_init(ExecutionConfig, execution_data),
        logging=_safe_init(LoggingConfig, logging_data),
        notification=_safe_init(NotificationConfig, notification_data)
    )

    return config

# --- Contoh Penggunaan Sementara (Bisa dihapus nantinya) ---
if __name__ == "__main__":
    # Test loading config
    cfg = load_config()
    print("Exchange ID:", cfg.exchange.exchange_id)
    print("Is Dry Run:", cfg.execution.dry_run)
    print("API Key (from default/env):", "Set" if cfg.exchange.api_key else "Not Set")
