import asyncio
import argparse
import logging
import sys
from typing import Dict, Any
import indicators

# Import dari modul-modul yang telah kita bangun
from config import load_config
from data_fetcher import DataFetcher
from strategy import StrategyEngine
from risk_management import RiskManager
from execution import ExecutionEngine
from logger import setup_logger, TradeJournal
from notifier import TelegramNotifier
from timeframe_selector import DynamicTimeframeSelector

class SpotScalpingBot:
    """
    Main Orchestrator untuk Bot Trading Crypto Spot bersistem SMC.
    Menggabungkan seluruh modul dan menjalankan loop utama asinkron.
    """
    
    def __init__(self, config_path: str = "config.json"):
        # Load Config
        self.config = load_config(config_path)
        
        # Inisialisasi Logger & Journal
        self.logger = setup_logger(
            level=self.config.logging.level,
            log_file=self.config.logging.log_file
        )
        self.journal = TradeJournal(self.config.logging.trade_journal_file)
        
        # Inisialisasi Core Modules
        self.fetcher = DataFetcher(self.config.exchange)
        self.strategy = StrategyEngine(self.config.strategy)
        self.risk_manager = RiskManager(self.config.risk)
        self.notifier = TelegramNotifier(self.config.notification)
        self.execution = ExecutionEngine(
            exec_config=self.config.execution,
            risk_config=self.config.risk,
            fetcher=self.fetcher,
            journal=self.journal,
            notifier=self.notifier
        )
        self.timeframe_selector = DynamicTimeframeSelector(
            strategy_config=self.config.strategy,
            risk_config=self.config.risk,
            candidate_timeframes=getattr(
                self.config.strategy, 'dynamic_candidate_timeframes', ['5m', '15m']
            ),
            evaluation_bars=getattr(
                self.config.strategy, 'dynamic_timeframe_evaluation_bars', 1000
            ),
            min_trades=getattr(
                self.config.strategy, 'dynamic_timeframe_min_trades', 5
            ),
            min_profit_factor=getattr(
                self.config.strategy, 'dynamic_timeframe_min_profit_factor', 1.05
            ),
            min_expectancy_pct=getattr(
                self.config.strategy, 'dynamic_timeframe_min_expectancy_pct', 0.0
            ),
            switch_margin=getattr(
                self.config.strategy, 'dynamic_timeframe_switch_margin', 0.10
            ),
            cache_minutes=getattr(
                self.config.strategy, 'dynamic_timeframe_cache_minutes', 1440
            ),
        )
        
        # Semaphore untuk membatasi pemrosesan paralel agar tidak terkena Rate Limit API
        self.semaphore = asyncio.Semaphore(self.config.execution.max_concurrent_symbols)
        self.is_running = False
        # HTF BTC terakhir dipakai bersama oleh filter market dan Relative Strength.
        # Diisi sebelum pemindaian simbol dimulai pada setiap siklus utama.
        self.btc_htf_df = None

    async def initialize(self) -> None:
        """Koneksi awal ke exchange dan validasi mode eksekusi."""
        self.logger.info("Inisialisasi Bot dimulai...")
        await self.fetcher.connect()
        
        # Validasi LIVE MODE
        if not self.config.execution.dry_run:
            if not self.config.exchange.api_key or not self.config.exchange.api_secret:
                self.logger.critical("LIVE MODE: API Key dan Secret wajib diisi di config/env variables!")
                sys.exit(1)
                
            try:
                # Tes autentikasi dengan menarik saldo awal
                balance = await self.fetcher.fetch_quote_balance(self.config.execution.quote_currency)
                self.logger.info(f"LIVE MODE TERVERIFIKASI. Saldo {self.config.execution.quote_currency}: {balance:.2f}")
            except Exception as e:
                self.logger.critical(f"Gagal verifikasi API Key untuk LIVE MODE: {e}")
                sys.exit(1)
        else:
            self.logger.info(f"DRY RUN MODE AKTIF. Paper Cash: {self.config.execution.paper_cash}")

        self.logger.info("Bot berhasil diinisialisasi dan siap berjalan.")

    async def shutdown(self) -> None:
        """Shutdown sequence yang graceful untuk menutup semua koneksi."""
        self.logger.info("Memulai proses shutdown...")
        self.is_running = False
        
        # Simpan status posisi aktif & PNL terakhir
        self.execution._save_state()
        
        # Tutup koneksi ccxt
        await self.fetcher.close()
        self.logger.info("Bot telah dimatikan (Graceful Shutdown).")

    async def _process_symbol(self, symbol: str) -> None:
        """Alur inti untuk menganalisa dan mengeksekusi satu symbol pasar."""
        try:
            # Fallback lama tetap dipakai jika dynamic timeframe dimatikan atau
            # belum ada kandidat yang memenuhi syarat minimum.
            fallback_ltf = self.strategy.get_symbol_ltf(symbol)
            managing_existing_trade = (
                symbol in self.execution.active_trades or
                symbol in self.execution.pending_orders
            )

            # 1. Ambil data HTF terlebih dahulu
            df_htf_raw = await self.fetcher.fetch_ohlcv_df(
                symbol, self.config.strategy.higher_timeframe, self.config.strategy.htf_limit
            )

            # --- KALKULASI INDIKATOR ---
            df_htf = indicators.prepare_htf_frame(
                df_htf_raw, 
                ema_period=self.config.strategy.ema_period, 
                tema_period=self.config.strategy.tema_period
            )

            ltf = fallback_ltf
            if (
                getattr(self.config.strategy, 'enable_dynamic_timeframe', False) and
                not managing_existing_trade
            ):
                cached_ltf = self.timeframe_selector.get_cached_timeframe(symbol)
                if cached_ltf is not None:
                    ltf = cached_ltf
                    df_ltf_raw = await self.fetcher.fetch_ohlcv_df(
                        symbol, ltf, self.config.strategy.ltf_limit
                    )
                else:
                    candidate_frames = {}
                    for candidate_tf in self.timeframe_selector.candidate_timeframes:
                        candidate_frames[candidate_tf] = await self.fetcher.fetch_ohlcv_df(
                            symbol, candidate_tf, self.config.strategy.ltf_limit
                        )
                        await asyncio.sleep(0.25)

                    ltf, _ = self.timeframe_selector.choose(
                        symbol=symbol,
                        df_htf=df_htf,
                        candidate_frames=candidate_frames,
                        df_btc_htf=self.btc_htf_df,
                        fallback=fallback_ltf,
                    )
                    df_ltf_raw = candidate_frames.get(ltf)
                    if df_ltf_raw is None:
                        df_ltf_raw = await self.fetcher.fetch_ohlcv_df(
                            symbol, ltf, self.config.strategy.ltf_limit
                        )
            else:
                # Jangan mengganti timeframe saat posisi/pending order sedang
                # dikelola; gunakan cache aktif atau fallback lama.
                if managing_existing_trade:
                    ltf = self.timeframe_selector.get_cached_timeframe(symbol) or fallback_ltf
                # Jeda kecil untuk mengurangi burst request ketika mode legacy.
                await asyncio.sleep(0.5)
                df_ltf_raw = await self.fetcher.fetch_ohlcv_df(
                    symbol, ltf, self.config.strategy.ltf_limit
                )

            df_ltf = indicators.prepare_ltf_frame(
                df_ltf_raw,
                atr_period=self.config.strategy.atr_period,
                stoch_k=self.config.strategy.stochastic_k_period,
                stoch_k_smooth=self.config.strategy.stochastic_k_smoothing,
                stoch_d=self.config.strategy.stochastic_d_period
            )

            # =================================================================
            # 1. CEK STATUS POSISI AKTIF & PENDING LIMIT ORDER
            # =================================================================
            if symbol in self.execution.active_trades:
                # Pulihkan proteksi exchange untuk posisi yang dimuat dari state
                # lama atau posisi yang sempat kehilangan metadata OCO.
                active_trade = self.execution.active_trades[symbol]
                if (
                    self.execution._exchange_protection_enabled() and
                    not active_trade.protection_order_list_id and
                    not active_trade.protection_stop_order_id and
                    not active_trade.protection_take_profit_order_id
                ):
                    protection_ok = await self.execution._place_exchange_protection(active_trade)
                    if not protection_ok:
                        self.logger.critical(
                            f"[{symbol}] Posisi aktif belum memiliki OCO protection yang berhasil dipasang."
                        )
                    else:
                        self.execution._save_state()
                else:
                    self.execution._save_state()

                # Rekonsiliasi jika OCO sudah terisi saat WebSocket sempat
                # melewatkan tick harga.
                if await self.execution._check_exchange_protection_fill(active_trade):
                    return

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
            # =================================================================

            # 2. Analisa HTF (Tren & Deteksi Zona SMC) jika tidak ada posisi aktif
            context = self.strategy.analyze_higher_timeframe(symbol, df_htf)
            
            # 3. Evaluasi LTF (Trigger Eksekusi)
            # Kirim kedua frame HTF agar Adaptive Relative Strength Filter benar-benar
            # berjalan di live bot untuk ETH/AVAX/SOL.
            signal = self.strategy.evaluate_lower_timeframe(
                symbol,
                df_ltf,
                context,
                df_htf=df_htf,
                df_btc_htf=self.btc_htf_df
            )
            
            
            if not signal:
                return  # Tidak ada sinyal entry

            # 4. Log Sinyal yang terdeteksi
            mode_str = "DRY_RUN" if self.config.execution.dry_run else "LIVE"
            self.journal.log_signal(
                symbol=symbol, side=signal.direction, price=signal.entry_price, 
                reason=signal.reason, mode=mode_str
            )

            # 5. Check Execution Eligibility Dasar (Circuit Breaker & Active Trades)
            if self.execution.circuit_breaker_active or symbol in self.execution.active_trades or symbol in self.execution.pending_orders:
                return

            # 6. Fetch Balance
            if self.config.execution.dry_run:
                # Gunakan asumsi modal virtual ditambah profit harian
                available_balance = self.config.execution.paper_cash + self.execution.daily_pnl
            else:
                available_balance = await self.fetcher.fetch_quote_balance(self.config.execution.quote_currency)

            # 7. Build Trade Plan (Manajemen Risiko)
            plan = self.risk_manager.build_trade_plan(
                symbol=symbol,
                entry_price=signal.entry_price,
                stop_loss_ref=signal.stop_loss_ref,
                atr_value=signal.atr_value,
                total_capital=available_balance
            )

            if not plan:
                return # Dibatalkan oleh sistem risiko (misal modal kurang / SL terlalu lebar)

            # 8. Eksekusi Order (Hybrid: Limit Retest untuk Altcoin vs Market Direct untuk BTC)
            if signal.entry_type == "LIMIT_RETEST":
                success = await self.execution.place_retest_limit_order(signal, plan)
            else:
                success = await self.execution.open_long(signal, plan)
            
            # 9. Mark Zone (Tandai zona agar tidak di-trade berkali-kali)
            if success:
                self.strategy.mark_zone_traded(symbol, signal.zone_id)

        except Exception as e:
            self.logger.error(f"Error tidak terduga saat memproses {symbol}: {e}")

    async def _process_symbol_guarded(self, symbol: str) -> None:
        """Wrapper dengan Semaphore untuk membatasi konkurensi (mencegah Rate Limit)."""
        async with self.semaphore:
            await self._process_symbol(symbol)

    async def _ws_price_monitor(self) -> None:
        """Background task untuk menerima stream harga dan memonitor posisi aktif."""
        try:
            async for tickers in self.fetcher.start_websocket_stream(self.config.symbols):
                if not self.is_running:
                    break
                
                # Ekstrak harga terakhir ('last') untuk setiap symbol
                current_prices = {}
                for sym, data in tickers.items():
                    if 'last' in data and data['last'] is not None:
                        current_prices[sym] = float(data['last'])
                
                # Evaluasi posisi aktif (SL, TP, BEP) dengan harga terbaru
                if current_prices:
                    await self.execution.monitor_open_trades(current_prices)
                    
        except asyncio.CancelledError:
            self.logger.info("WebSocket monitor task cancelled.")
        except Exception as e:
            self.logger.error(f"WebSocket monitor terhenti karena error: {e}")

    async def run(self) -> None:
        """Main Loop: Engine detak jantung dari bot."""
        await self.initialize()
        self.is_running = True
        
        # Jalankan WebSocket pemantau harga sebagai task di background
        ws_task = asyncio.create_task(self._ws_price_monitor())
        
        try:
            while self.is_running:
                # 0. Update BTC Market Filter jika aktif
                need_btc_htf = (
                    self.config.strategy.enable_btc_filter or
                    (
                        getattr(self.config.strategy, 'enable_relative_strength_filter', False)
                        and bool(getattr(self.config.strategy, 'rs_target_symbols', []))
                    )
                )
                if need_btc_htf:
                    try:
                        btc_sym = self.config.strategy.btc_filter_symbol
                        df_btc_raw = await self.fetcher.fetch_ohlcv_df(
                            btc_sym, self.config.strategy.higher_timeframe, 100
                        )
                        df_btc = indicators.prepare_htf_frame(
                            df_btc_raw, 
                            ema_period=self.config.strategy.ema_period, 
                            tema_period=self.config.strategy.tema_period
                        )
                        self.btc_htf_df = df_btc
                        btc_bullish = self.strategy.update_btc_market_status(df_btc)
                        status_str = "BULLISH (Altcoins Diizinkan)" if btc_bullish else "BEARISH (Altcoins Diblokir)"
                        self.logger.debug(f"[BTC Market Filter] Status {btc_sym}: {status_str}")
                    except Exception as e:
                        self.btc_htf_df = None
                        self.logger.warning(f"[BTC Market Filter] Gagal update status BTC: {e}")

                # Pemindaian sinyal pada seluruh symbol secara bersamaan (parallel)
                scan_tasks = [self._process_symbol_guarded(sym) for sym in self.config.symbols]
                
                # return_exceptions=True agar jika 1 koin error jaringan, yang lain tetap jalan
                results = await asyncio.gather(*scan_tasks, return_exceptions=True)
                
                for res in results:
                    if isinstance(res, Exception):
                        self.logger.debug(f"Terjadi exception pada batch pemindaian: {res}")
                
                # Jeda sebelum pemindaian berikutnya sesuai konfigurasi (misal: 20 detik)
                await asyncio.sleep(self.config.execution.poll_interval_seconds)
                
        except asyncio.CancelledError:
            self.logger.info("Main loop dibatalkan oleh sistem.")
        except Exception as e:
            self.logger.critical(f"Critical error pada main loop: {e}")
        finally:
            ws_task.cancel()
            await self.shutdown()

def main():
    """Entry point untuk eksekusi CLI."""
    parser = argparse.ArgumentParser(description="Cryptocurrency SMC Spot Scalping Bot")
    parser.add_argument(
        "--config", 
        type=str, 
        default="config.json", 
        help="Path menuju file konfigurasi (default: config.json)"
    )
    args = parser.parse_args()

    bot = SpotScalpingBot(config_path=args.config)
    
    try:
        # Jalankan event loop utama asyncio
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        print("\n[INFO] Bot dihentikan secara manual oleh pengguna (Ctrl+C).")
    except Exception as e:
        print(f"\n[CRITICAL] Bot berhenti mendadak: {e}")

if __name__ == "__main__":
    main()
