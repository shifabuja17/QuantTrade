import asyncio
import logging
import pandas as pd
import ccxt.pro as ccxt
from ccxt.base.errors import NetworkError, ExchangeError, RateLimitExceeded
from typing import List, Dict, Any, Optional, AsyncGenerator
from functools import wraps

# Asumsi ExchangeConfig sudah di-import dari config.py
from config import ExchangeConfig

logger = logging.getLogger(__name__)

def async_retry(max_retries: int = 3, base_delay: float = 1.0):
    """
    Decorator untuk mekanisme retry dengan exponential backoff
    untuk mem-wrap fungsi asynchronous.
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            retries = 0
            while retries <= max_retries:
                try:
                    return await func(*args, **kwargs)
                except (NetworkError, ExchangeError, RateLimitExceeded) as e:
                    logger.warning(f"Error pada {func.__name__}: {e}. Percobaan {retries + 1}/{max_retries}")
                    if retries == max_retries:
                        logger.error(f"Gagal mengeksekusi {func.__name__} setelah {max_retries} percobaan.")
                        raise e
                    
                    # Exponential backoff: 1s, 2s, 4s, dst.
                    delay = base_delay * (2 ** retries)
                    logger.info(f"Menunggu {delay} detik sebelum mencoba lagi...")
                    await asyncio.sleep(delay)
                    retries += 1
                except Exception as e:
                    # Jangan retry jika error bukan terkait jaringan/exchange (misal: TypeError)
                    logger.error(f"Critical error pada {func.__name__}: {e}")
                    raise e
        return wrapper
    return decorator

class DataFetcher:
    """
    Modul untuk mengambil data dari exchange menggunakan ccxt.pro.
    Mendukung REST API asinkron dan WebSocket streaming.
    """

    def __init__(self, config: ExchangeConfig, max_retries: int = 3):
        self.config = config
        self.max_retries = max_retries
        
        # Konfigurasi instance ccxt.pro Binance
        exchange_class = getattr(ccxt, self.config.exchange_id)
        exchange_args = {
            'apiKey': self.config.api_key,
            'secret': self.config.api_secret,
            'enableRateLimit': self.config.enable_rate_limit,
            'options': {
                'defaultType': 'spot',
                'recvWindow': self.config.recv_window
            }
        }
        
        self.exchange = exchange_class(exchange_args)
        
        if self.config.testnet:
            self.exchange.set_sandbox_mode(True)

    @async_retry(max_retries=3)
    async def connect(self) -> None:
        """Memuat data market dari exchange."""
        logger.info(f"Menghubungkan ke {self.config.exchange_id} dan memuat markets...")
        await self.exchange.load_markets()
        logger.info("Markets berhasil dimuat.")

    async def close(self) -> None:
        """Menutup koneksi ke exchange."""
        logger.info("Menutup koneksi exchange...")
        await self.exchange.close()

    def _drop_incomplete_candle(self, df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
        """
        Membuang candle terakhir jika candle tersebut belum selesai.

        CCXT mengembalikan timestamp candle sebagai waktu pembukaan candle.
        Karena itu candle terakhir belum boleh dipakai sebelum:

            timestamp_open + durasi_timeframe <= waktu_sekarang_UTC

        Ini penting untuk mencegah indikator HTF/LTF berubah-ubah di tengah
        candle dan menghasilkan sinyal yang tidak dapat direproduksi.
        """
        if df.empty or 'timestamp' not in df.columns:
            return df

        try:
            timeframe_seconds = self.exchange.parse_timeframe(timeframe)
            last_open = pd.Timestamp(df.iloc[-1]['timestamp'])
            if last_open.tzinfo is None:
                last_open = last_open.tz_localize('UTC')
            else:
                last_open = last_open.tz_convert('UTC')

            candle_close = last_open + pd.Timedelta(seconds=timeframe_seconds)
            now_utc = pd.Timestamp.now(tz='UTC')

            if candle_close > now_utc:
                logger.debug(
                    f"[{timeframe}] Membuang candle berjalan: "
                    f"open={last_open.isoformat()}, close={candle_close.isoformat()}"
                )
                return df.iloc[:-1].reset_index(drop=True)
        except Exception as e:
            # Jangan membuat bot berhenti hanya karena validasi candle gagal.
            # Error dicatat agar bisa ditindaklanjuti; caller tetap menerima data.
            logger.warning(f"Gagal memvalidasi candle terakhir {timeframe}: {e}")

        return df

    @async_retry(max_retries=3)
    async def fetch_ohlcv_df(self, symbol: str, timeframe: str, limit: int = 1000) -> pd.DataFrame:
        """
        Mengambil data historis OHLCV dan mengembalikannya sebagai Pandas DataFrame.
        """
        ohlcv = await self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        # Konversi timestamp (milliseconds) menjadi format datetime UTC
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)

        # Jangan gunakan candle yang masih berjalan untuk indikator maupun sinyal.
        return self._drop_incomplete_candle(df, timeframe)

    @async_retry(max_retries=3)
    async def fetch_last_price(self, symbol: str) -> float:
        """Mengambil harga terakhir (last price) dari sebuah pair."""
        ticker = await self.exchange.fetch_ticker(symbol)
        return float(ticker['last'])

    @async_retry(max_retries=3)
    async def fetch_quote_balance(self, quote_currency: str = "USDT") -> float:
        """Mengambil balance (free/available) dari mata uang kuotasi (contoh: USDT)."""
        balance = await self.exchange.fetch_balance()
        if quote_currency in balance:
            return float(balance[quote_currency]['free'])
        return 0.0

    def get_market(self, symbol: str) -> Dict[str, Any]:
        """Mengembalikan informasi presisi dan rule market untuk sebuah pair."""
        return self.exchange.market(symbol)

    def format_amount(self, symbol: str, amount: float) -> float:
        """
        Memformat quantity (amount) aset agar sesuai dengan aturan presisi/step size exchange.
        """
        formatted_amount = self.exchange.amount_to_precision(symbol, amount)
        return float(formatted_amount)

    def format_price(self, symbol: str, price: float) -> float:
        """
        Memformat harga agar sesuai dengan aturan tick size exchange.
        """
        formatted_price = self.exchange.price_to_precision(symbol, price)
        return float(formatted_price)

    async def start_websocket_stream(self, symbols: List[str]) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Memulai WebSocket stream untuk memantau pergerakan harga (tickers) secara real-time.
        Menggunakan async generator (yield) agar data bisa diolah di module lain (bot.py).
        """
        logger.info(f"Memulai WebSocket stream untuk {len(symbols)} symbols...")
        
        while True:
            try:
                # watch_tickers akan menunggu dan mengembalikan data saat ada update harga
                tickers = await self.exchange.watch_tickers(symbols)
                yield tickers
            except Exception as e:
                logger.error(f"WebSocket error terdeteksi: {e}. Mencoba reconnect dalam 5 detik...")
                await asyncio.sleep(5)
