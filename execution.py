import json
import os
import logging
import asyncio
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field, asdict
from typing import Dict, Optional
import pandas as pd

# Asumsi import dari modul-modul yang telah dibuat sebelumnya
from config import ExecutionConfig, RiskConfig
from data_fetcher import DataFetcher
from logger import TradeJournal
from strategy import TradeSignal
from risk_management import TradePlan
from notifier import TelegramNotifier

logger = logging.getLogger(__name__)

@dataclass
class ActiveTrade:
    """Representasi dari posisi trading yang sedang terbuka (Open Position)."""
    symbol: str
    side: str
    zone_id: str
    entry_price: float
    quantity: float
    initial_quantity: float
    remaining_quantity: float
    stop_loss: float
    stop_loss_initial: float  # SL awal (tidak berubah) untuk kalkulasi jarak 1R
    take_profit: float
    take_profit_1: float
    take_profit_2: float
    bep_trigger_price: float
    order_id: str
    mode: str
    partial_tp_ratio: float = 0.5
    bep_activated: bool = False
    tp1_executed: bool = False
    realized_pnl: float = 0.0
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_price_update: float = 0.0
    # Exchange-side OCO protection (TP2 + SL). Optional agar state lama tetap kompatibel.
    protection_order_list_id: Optional[str] = None
    protection_stop_order_id: Optional[str] = None
    protection_take_profit_order_id: Optional[str] = None
    protection_quantity: float = 0.0

@dataclass
class PendingLimitOrder:
    """Representasi order limit retest yang sedang mengantre di orderbook / buffer."""
    symbol: str
    zone_id: str
    limit_price: float
    stop_loss_ref: float
    atr_value: float
    quantity: float
    notional: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    order_id: str
    mode: str
    timeout_bars: int = 5
    bars_elapsed: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

class ExecutionEngine:
    """
    Engine untuk mengeksekusi, memonitor, dan menutup posisi trading.
    Dilengkapi dengan manajemen state, circuit breaker, cooldown,
    serta dukungan Hybrid Adaptive Retest Entry & Partial Take-Profit.
    """
    def __init__(self, exec_config: ExecutionConfig, risk_config: RiskConfig, 
                 fetcher: DataFetcher, journal: TradeJournal, notifier: TelegramNotifier, state_file: str = "execution_state.json"):
        self.exec_config = exec_config
        self.risk_config = risk_config
        self.fetcher = fetcher
        self.journal = journal
        self.notifier = notifier
        self.state_file = state_file

        # State Variables
        self.active_trades: Dict[str, ActiveTrade] = {}
        self.pending_orders: Dict[str, PendingLimitOrder] = {}
        self.cooldowns: Dict[str, datetime] = {}
        
        # PNL & Circuit Breaker tracking
        self.current_day: str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.daily_pnl: float = 0.0
        self.circuit_breaker_active: bool = False
        
        # Muat state dari JSON (jika ada) saat bot restart
        self._load_state()

    async def update_structural_trailing(self, symbol: str, df_ltf: pd.DataFrame) -> None:
        """
        Memperbarui Stop Loss posisi aktif secara bertahap:
        - Fase 1 (Profit >= 1.1R - 1.2R): Pindah SL ke Break-Even Point (Entry + fee buffer)
        - Fase 2 (Profit >= 1.7R): Aktifkan trailing struktural berbasis swing low 15 candle
        """
        if symbol not in self.active_trades:
            return

        trade = self.active_trades[symbol]
        
        current_price = df_ltf['close'].iloc[-1]
        high_price = df_ltf['high'].iloc[-1]

        stop_distance = trade.entry_price - trade.stop_loss_initial
        current_profit = high_price - trade.entry_price
        protection_changed = False

        # --- FASE 1: BREAK-EVEN PROTECTION ---
        bep_mult = getattr(self.risk_config, 'bep_trigger_atr_multiplier', 1.1)
        bep_profit_pct = getattr(self.risk_config, 'bep_profit_pct', 0.15)
        if not trade.bep_activated and current_profit >= (stop_distance * bep_mult):
            bep_level = trade.entry_price * (1.0 + (bep_profit_pct / 100.0))
            if bep_level > trade.stop_loss:
                trade.stop_loss = self.fetcher.format_price(symbol, bep_level)
                trade.bep_activated = True
                protection_changed = True
                logger.info(f"[{symbol} LIVE] 🛡️ BEP Terpicu (+{bep_mult:.1f}R)! Stop Loss dipindahkan ke {trade.stop_loss} (Entry + Fee Buffer {bep_profit_pct}%)")

        # --- FASE 2: TRAILING STOP STRUKTURAL (Profit >= 1.7R) ---
        if current_profit >= (stop_distance * 1.7):
            recent_swing_low = df_ltf['low'].iloc[-15:].min()
            structural_trail_sl = recent_swing_low * 0.998  # Buffer 0.2%

            if structural_trail_sl > trade.stop_loss and structural_trail_sl > trade.entry_price:
                trade.stop_loss = self.fetcher.format_price(symbol, structural_trail_sl)
                protection_changed = True
                logger.info(f"[{symbol} LIVE] 📈 Trailing Stop Struktural dinaikkan ke {trade.stop_loss}")

        if protection_changed:
            refreshed = await self._refresh_exchange_protection(trade)
            if not refreshed:
                logger.critical(
                    f"[{symbol}] Proteksi OCO gagal diperbarui setelah SL berubah; "
                    "monitor Python tetap aktif sebagai fallback."
                )
            self._save_state()

    def _save_state(self) -> None:
        """Menyimpan state posisi aktif, pending order, dan PNL harian ke dalam JSON file."""
        state_data = {
            "current_day": self.current_day,
            "daily_pnl": self.daily_pnl,
            "circuit_breaker_active": self.circuit_breaker_active,
            "active_trades": {},
            "pending_orders": {}
        }
        
        for symbol, trade in self.active_trades.items():
            trade_dict = asdict(trade)
            trade_dict["opened_at"] = trade.opened_at.isoformat()
            state_data["active_trades"][symbol] = trade_dict

        for symbol, p_order in self.pending_orders.items():
            p_dict = asdict(p_order)
            p_dict["created_at"] = p_order.created_at.isoformat()
            state_data["pending_orders"][symbol] = p_dict

        try:
            with open(self.state_file, "w") as f:
                json.dump(state_data, f, indent=4)
        except Exception as e:
            logger.error(f"Gagal menyimpan execution state: {e}")

    def _load_state(self) -> None:
        """Memuat state dari JSON file saat inisialisasi bot."""
        if not os.path.exists(self.state_file):
            return

        try:
            with open(self.state_file, "r") as f:
                data = json.load(f)
                
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if data.get("current_day") == today_str:
                self.current_day = data["current_day"]
                self.daily_pnl = data.get("daily_pnl", 0.0)
                self.circuit_breaker_active = data.get("circuit_breaker_active", False)
            else:
                logger.info("Hari berganti. Mereset Daily PNL dan Circuit Breaker.")
                self.current_day = today_str
                self.daily_pnl = 0.0
                self.circuit_breaker_active = False

            # Load Active Trades
            trades_data = data.get("active_trades", {})
            for symbol, t_data in trades_data.items():
                t_data["opened_at"] = datetime.fromisoformat(t_data["opened_at"])
                if "initial_quantity" not in t_data:
                    t_data["initial_quantity"] = t_data.get("quantity", 0.0)
                if "remaining_quantity" not in t_data:
                    t_data["remaining_quantity"] = t_data.get("quantity", 0.0)
                if "take_profit_1" not in t_data:
                    t_data["take_profit_1"] = t_data.get("take_profit", 0.0)
                if "take_profit_2" not in t_data:
                    t_data["take_profit_2"] = t_data.get("take_profit", 0.0)
                self.active_trades[symbol] = ActiveTrade(**t_data)
                
            # Load Pending Orders
            pending_data = data.get("pending_orders", {})
            for symbol, p_data in pending_data.items():
                p_data["created_at"] = datetime.fromisoformat(p_data["created_at"])
                self.pending_orders[symbol] = PendingLimitOrder(**p_data)

            logger.info(f"Berhasil memuat state: {len(self.active_trades)} posisi aktif & {len(self.pending_orders)} pending limit order dipulihkan.")
            
        except Exception as e:
            logger.error(f"Gagal memuat execution state: {e}")

    def _exchange_protection_enabled(self) -> bool:
        """True jika proteksi OCO exchange-side aktif dan bot berjalan live."""
        return (
            not self.exec_config.dry_run and
            getattr(self.exec_config, 'enable_exchange_protection', True)
        )

    async def _place_exchange_protection(
        self,
        trade: ActiveTrade,
        quantity: Optional[float] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> bool:
        """
        Memasang OCO SELL di Binance Spot:

        - above: LIMIT_MAKER pada TP2
        - below: STOP_LOSS_LIMIT pada SL

        Binance akan membatalkan leg lainnya ketika salah satu leg OCO aktif.
        Ini mencegah TP dan SL terpisah menjual posisi yang sama dua kali.
        """
        if not self._exchange_protection_enabled():
            return True

        qty = quantity if quantity is not None else trade.remaining_quantity
        sl = stop_loss if stop_loss is not None else trade.stop_loss
        tp = take_profit if take_profit is not None else trade.take_profit_2

        if qty <= 0 or sl >= trade.entry_price or tp <= trade.entry_price:
            logger.error(
                f"[{trade.symbol}] Proteksi OCO invalid: qty={qty}, "
                f"entry={trade.entry_price}, SL={sl}, TP={tp}"
            )
            return False

        try:
            exchange = self.fetcher.exchange
            market_id = exchange.market(trade.symbol)['id']
            formatted_qty = self.fetcher.format_amount(trade.symbol, qty)
            formatted_sl = self.fetcher.format_price(trade.symbol, sl)
            formatted_tp = self.fetcher.format_price(trade.symbol, tp)

            # Stop-limit diberi ruang slippage kecil agar lebih mungkin terisi
            # saat harga menembus SL dengan cepat.
            slippage_pct = getattr(self.exec_config, 'max_slippage_pct', 0.1) / 100.0
            stop_limit = self.fetcher.format_price(
                trade.symbol, formatted_sl * (1.0 - slippage_pct)
            )

            # Endpoint orderList/oco adalah endpoint OCO Spot terbaru Binance.
            # LIMIT_MAKER menjadi TP di atas harga pasar, sedangkan
            # STOP_LOSS_LIMIT menjadi proteksi downside.
            params = {
                'symbol': market_id,
                'side': 'SELL',
                'quantity': formatted_qty,
                'aboveType': 'LIMIT_MAKER',
                'abovePrice': formatted_tp,
                'belowType': 'STOP_LOSS_LIMIT',
                'belowPrice': stop_limit,
                'belowStopPrice': formatted_sl,
                'belowTimeInForce': 'GTC',
                'newOrderRespType': 'RESULT',
            }

            create_oco = getattr(exchange, 'privatePostOrderListOco', None)
            if create_oco is None:
                raise RuntimeError(
                    'CCXT Binance tidak menyediakan endpoint privatePostOrderListOco'
                )

            response = await create_oco(params)
            orders = response.get('orders', []) if isinstance(response, dict) else []

            trade.protection_order_list_id = str(response['orderListId']) if response.get('orderListId') is not None else None
            trade.protection_quantity = formatted_qty
            trade.protection_stop_order_id = None
            trade.protection_take_profit_order_id = None

            # Response Binance biasanya menyimpan order di urutan above/below.
            if len(orders) >= 2:
                trade.protection_take_profit_order_id = str(orders[0].get('orderId'))
                trade.protection_stop_order_id = str(orders[1].get('orderId'))

            for report in response.get('orderReports', []) if isinstance(response, dict) else []:
                order_type = str(report.get('type', '')).upper()
                order_id = report.get('orderId')
                if order_id is None:
                    continue
                if 'STOP' in order_type:
                    trade.protection_stop_order_id = str(order_id)
                else:
                    trade.protection_take_profit_order_id = str(order_id)

            logger.info(
                f"[{trade.symbol}] OCO exchange protection aktif: "
                f"SL={formatted_sl}, TP={formatted_tp}, qty={formatted_qty}, "
                f"list={trade.protection_order_list_id}"
            )
            return True

        except Exception as e:
            logger.critical(
                f"[{trade.symbol}] Gagal memasang OCO exchange protection: {e}"
            )
            return False

    async def _cancel_exchange_protection(self, trade: ActiveTrade) -> bool:
        """Membatalkan pasangan OCO sebelum posisi ditutup atau proteksi diubah."""
        if not self._exchange_protection_enabled():
            return True

        if not (
            trade.protection_order_list_id or
            trade.protection_stop_order_id or
            trade.protection_take_profit_order_id
        ):
            return True

        try:
            exchange = self.fetcher.exchange
            market_id = exchange.market(trade.symbol)['id']

            if trade.protection_order_list_id:
                cancel_oco = getattr(exchange, 'privateDeleteOrderList', None)
                if cancel_oco is not None:
                    await cancel_oco({
                        'symbol': market_id,
                        'orderListId': trade.protection_order_list_id,
                    })
                else:
                    raise RuntimeError(
                        'CCXT Binance tidak menyediakan endpoint privateDeleteOrderList'
                    )
            else:
                # Fallback untuk state lama yang hanya memiliki order ID.
                for order_id in (
                    trade.protection_stop_order_id,
                    trade.protection_take_profit_order_id,
                ):
                    if order_id:
                        await exchange.cancel_order(order_id, trade.symbol)

            trade.protection_order_list_id = None
            trade.protection_stop_order_id = None
            trade.protection_take_profit_order_id = None
            trade.protection_quantity = 0.0
            return True

        except Exception as e:
            logger.error(f"[{trade.symbol}] Gagal membatalkan OCO exchange protection: {e}")
            return False

    async def _refresh_exchange_protection(self, trade: ActiveTrade) -> bool:
        """Mengganti OCO setelah SL, TP, atau quantity posisi berubah."""
        if not self._exchange_protection_enabled():
            return True

        if not await self._cancel_exchange_protection(trade):
            return False

        return await self._place_exchange_protection(
            trade,
            quantity=trade.remaining_quantity,
            stop_loss=trade.stop_loss,
            take_profit=trade.take_profit_2,
        )

    async def _emergency_close_unprotected(self, trade: ActiveTrade) -> bool:
        """Menutup posisi live jika proteksi exchange gagal dipasang."""
        try:
            balance_data = await self.fetcher.exchange.fetch_balance()
            base_currency = trade.symbol.split('/')[0]
            available = float(balance_data.get(base_currency, {}).get('free', 0.0))
            sell_amount = self.fetcher.format_amount(
                trade.symbol, min(trade.remaining_quantity, available)
            )
            if sell_amount <= 0:
                return False
            await self.fetcher.exchange.create_market_sell_order(
                symbol=trade.symbol,
                amount=sell_amount,
            )
            logger.critical(
                f"[{trade.symbol}] Posisi ditutup darurat karena OCO protection gagal."
            )
            return True
        except Exception as e:
            logger.critical(
                f"[{trade.symbol}] GAGAL menutup posisi tanpa proteksi exchange: {e}"
            )
            return False

    async def _check_exchange_protection_fill(self, trade: ActiveTrade) -> bool:
        """
        Mendeteksi jika OCO sudah terisi di exchange sebelum Python sempat
        memproses ticker. Jika sudah terisi, posisi hanya difinalisasi secara
        internal dan tidak dikirim sell kedua kali.
        """
        if not self._exchange_protection_enabled():
            return False

        checks = [
            (trade.protection_take_profit_order_id, 'EXCHANGE_OCO_TAKE_PROFIT'),
            (trade.protection_stop_order_id, 'EXCHANGE_OCO_STOP_LOSS'),
        ]
        for order_id, reason in checks:
            if not order_id:
                continue
            try:
                order = await self.fetcher.exchange.fetch_order(order_id, trade.symbol)
                filled = float(order.get('filled') or 0.0)
                status = str(order.get('status', '')).lower()
                if status in ('closed', 'filled') and filled > 0:
                    fill_price = float(
                        order.get('average') or
                        order.get('price') or
                        trade.last_price_update or
                        trade.entry_price
                    )
                    await self.close_trade(
                        trade.symbol,
                        fill_price,
                        reason,
                        position_already_closed=True,
                    )
                    return True
            except Exception as e:
                logger.debug(
                    f"[{trade.symbol}] Gagal sinkronisasi status OCO {order_id}: {e}"
                )
        return False

    def _check_daily_reset(self) -> None:
        """Helper untuk mengecek pergantian hari secara runtime."""
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.current_day != today_str:
            self.current_day = today_str
            self.daily_pnl = 0.0
            self.circuit_breaker_active = False
            logger.info("Hari berganti. Daily PNL dan Circuit Breaker direset.")
            self._save_state()

    async def open_long(self, signal: TradeSignal, plan: TradePlan) -> bool:
        """Mengeksekusi order buy (LONG) ke market secara instan (Direct Momentum Entry)."""
        symbol = plan.symbol
        self._check_daily_reset()

        # 1. Pre-execution Checks
        if self.circuit_breaker_active:
            logger.warning(f"[{symbol}] Entry ditolak: Circuit Breaker aktif untuk hari ini.")
            return False

        if symbol in self.active_trades or symbol in self.pending_orders:
            logger.debug(f"[{symbol}] Entry ditolak: Masih ada posisi atau pending order terbuka.")
            return False

        if (len(self.active_trades) + len(self.pending_orders)) >= self.exec_config.max_concurrent_symbols:
            logger.debug(f"[{symbol}] Entry ditolak: Batas maksimal posisi serentak tercapai.")
            return False

        if symbol in self.cooldowns and datetime.now(timezone.utc) < self.cooldowns[symbol]:
            logger.debug(f"[{symbol}] Entry ditolak: Masih dalam masa cooldown.")
            return False

        quantity = self.fetcher.format_amount(symbol, plan.quantity)
        entry_price = self.fetcher.format_price(symbol, plan.entry_price)
        
        max_price = entry_price * (1 + (self.exec_config.max_slippage_pct / 100.0))
        max_price = self.fetcher.format_price(symbol, max_price)
        
        mode = "DRY_RUN" if self.exec_config.dry_run else "LIVE"
        order_id = f"sim_{int(datetime.now().timestamp() * 1000)}"

        try:
            if self.exec_config.dry_run:
                logger.info(f"[{symbol}] Simulasi Buy dieksekusi di harga {entry_price}")
                fill_price = entry_price
            else:
                logger.info(f"[{symbol}] Mengirim LIVE IOC Buy Order...")
                order = await self.fetcher.exchange.create_order(
                    symbol=symbol,
                    type='limit',
                    side='buy',
                    amount=quantity,
                    price=max_price,
                    params={'timeInForce': 'IOC'}
                )
                
                if order['status'] in ['closed', 'filled']:
                    fill_price = float(order['average'] or entry_price)
                    order_id = str(order['id'])
                else:
                    logger.warning(f"[{symbol}] IOC Buy gagal tereksekusi (Slippage terlalu tinggi/Likuiditas kurang).")
                    return False
                    
            trade = ActiveTrade(
                symbol=symbol,
                side="LONG",
                zone_id=signal.zone_id,
                entry_price=fill_price,
                quantity=quantity,
                initial_quantity=quantity,
                remaining_quantity=quantity,
                stop_loss=plan.stop_loss,
                stop_loss_initial=plan.stop_loss,
                take_profit=plan.take_profit,
                take_profit_1=plan.take_profit_1,
                take_profit_2=plan.take_profit_2,
                partial_tp_ratio=getattr(plan, 'partial_tp_ratio', 0.5),
                bep_trigger_price=plan.bep_trigger_price,
                order_id=order_id,
                mode=mode,
                last_price_update=fill_price
            )
            
            self.active_trades[symbol] = trade

            # Pasang proteksi exchange segera setelah entry terisi. Jika gagal,
            # posisi live ditutup darurat agar tidak dibiarkan tanpa SL/TP.
            if self._exchange_protection_enabled():
                protection_ok = await self._place_exchange_protection(trade)
                if not protection_ok:
                    emergency_closed = await self._emergency_close_unprotected(trade)
                    if emergency_closed:
                        del self.active_trades[symbol]
                    self._save_state()
                    self.journal.log_error(
                        symbol, 'Exchange Protection Error',
                        'OCO protection gagal dipasang setelah entry; '
                        + ('posisi ditutup darurat.' if emergency_closed else 'posisi dipertahankan untuk fallback monitor.'),
                        mode
                    )
                    return False

            self._save_state()
            
            self.journal.log_entry(
                symbol=symbol, side="LONG", price=fill_price, quantity=quantity,
                notional=plan.notional, stop_loss=plan.stop_loss, take_profit=plan.take_profit,
                order_id=order_id, reason=signal.reason, mode=mode
            )
            msg = (
                f"🟢 <b>OPEN LONG (MARKET) | {mode}</b>\n"
                f"<b>Pair:</b> #{symbol.replace('/', '')}\n"
                f"<b>Entry:</b> ${fill_price:.4f}\n"
                f"<b>Target TP1 (50%):</b> ${plan.take_profit_1:.4f}\n"
                f"<b>Target TP2 (Runner):</b> ${plan.take_profit_2:.4f}\n"
                f"<b>Stop Loss:</b> ${plan.stop_loss:.4f}\n"
                f"<b>Notional:</b> ${plan.notional:.2f}\n"
                f"<b>Alasan:</b> <i>{signal.reason}</i>"
            )
            asyncio.create_task(self.notifier.send_message(msg))
            return True

        except Exception as e:
            logger.error(f"[{symbol}] Error saat eksekusi open_long: {e}")
            self.journal.log_error(symbol, "Open Long Error", str(e), mode)
            return False

    async def place_retest_limit_order(self, signal: TradeSignal, plan: TradePlan) -> bool:
        """Memasang Limit Order Retest (Diskon 50% Body) untuk Altcoin."""
        symbol = plan.symbol
        self._check_daily_reset()

        if self.circuit_breaker_active or symbol in self.active_trades or symbol in self.pending_orders:
            return False

        if (len(self.active_trades) + len(self.pending_orders)) >= self.exec_config.max_concurrent_symbols:
            return False

        if symbol in self.cooldowns and datetime.now(timezone.utc) < self.cooldowns[symbol]:
            return False

        limit_price = self.fetcher.format_price(symbol, signal.entry_price)
        quantity = self.fetcher.format_amount(symbol, plan.quantity)
        mode = "DRY_RUN" if self.exec_config.dry_run else "LIVE"
        order_id = f"retest_{int(datetime.now().timestamp() * 1000)}"

        try:
            if not self.exec_config.dry_run:
                logger.info(f"[{symbol}] Mengirim LIVE Limit Buy Retest Order di {limit_price}...")
                order = await self.fetcher.exchange.create_order(
                    symbol=symbol,
                    type='limit',
                    side='buy',
                    amount=quantity,
                    price=limit_price
                )
                order_id = str(order['id'])

            pending = PendingLimitOrder(
                symbol=symbol,
                zone_id=signal.zone_id,
                limit_price=limit_price,
                stop_loss_ref=signal.stop_loss_ref,
                atr_value=signal.atr_value,
                quantity=quantity,
                notional=plan.notional,
                stop_loss=plan.stop_loss,
                take_profit_1=plan.take_profit_1,
                take_profit_2=plan.take_profit_2,
                order_id=order_id,
                mode=mode,
                timeout_bars=signal.timeout_bars
            )

            self.pending_orders[symbol] = pending
            self._save_state()

            logger.info(f"[{symbol}] ⏳ Limit Order Retest aktif di ${limit_price:.4f} (Timeout: {signal.timeout_bars} bar LTF).")
            msg = (
                f"⏳ <b>LIMIT RETEST PLACED | {mode}</b>\n"
                f"<b>Pair:</b> #{symbol.replace('/', '')}\n"
                f"<b>Limit Price:</b> ${limit_price:.4f}\n"
                f"<b>Stop Loss:</b> ${plan.stop_loss:.4f}\n"
                f"<b>Timeout:</b> {signal.timeout_bars} bar LTF\n"
                f"<b>Alasan:</b> <i>{signal.reason}</i>"
            )
            asyncio.create_task(self.notifier.send_message(msg))
            return True

        except Exception as e:
            logger.error(f"[{symbol}] Gagal memasang retest limit order: {e}")
            return False

    async def update_pending_retest_orders(self, symbol: str, current_bar: pd.Series, strategy) -> bool:
        """Memantau apakah limit order retest terisi, expired, atau dibatalkan karena SL tertembus."""
        if symbol not in self.pending_orders:
            return False

        order = self.pending_orders[symbol]
        low_p = current_bar['low']
        open_p = current_bar['open']

        # 1. Cek Timeout / Expired
        order.bars_elapsed += 1
        if order.bars_elapsed > order.timeout_bars:
            logger.info(f"[{symbol}] ⌛ Limit Order Retest di ${order.limit_price} expired ({order.bars_elapsed} bars). Dibatalkan.")
            if not self.exec_config.dry_run and order.order_id:
                try:
                    await self.fetcher.exchange.cancel_order(order.order_id, symbol)
                except Exception as e:
                    logger.warning(f"[{symbol}] Error cancel expired order: {e}")
            strategy.unmark_zone(symbol, order.zone_id)
            del self.pending_orders[symbol]
            self._save_state()
            return False

        # 2. Cek Invalidation: harga menembus Stop Loss sebelum limit terjemput
        if low_p <= order.stop_loss:
            logger.info(f"[{symbol}] ⚠️ Harga menembus SL (${order.stop_loss}) sebelum retest terisi. Limit order dibatalkan.")
            if not self.exec_config.dry_run and order.order_id:
                try:
                    await self.fetcher.exchange.cancel_order(order.order_id, symbol)
                except Exception as e:
                    logger.warning(f"[{symbol}] Error cancel invalidated order: {e}")
            strategy.unmark_zone(symbol, order.zone_id)
            del self.pending_orders[symbol]
            self._save_state()
            return False

        # 3. Cek Keterisian Limit Order (low <= limit_price)
        if low_p <= order.limit_price:
            fill_price = order.limit_price
            if open_p < fill_price:
                fill_price = open_p  # Price improvement

            trade = ActiveTrade(
                symbol=symbol,
                side="LONG",
                zone_id=order.zone_id,
                entry_price=fill_price,
                quantity=order.quantity,
                initial_quantity=order.quantity,
                remaining_quantity=order.quantity,
                stop_loss=order.stop_loss,
                stop_loss_initial=order.stop_loss,
                take_profit=order.take_profit_2,
                take_profit_1=order.take_profit_1,
                take_profit_2=order.take_profit_2,
                bep_trigger_price=fill_price,
                order_id=order.order_id,
                mode=order.mode,
                last_price_update=fill_price
            )

            self.active_trades[symbol] = trade
            del self.pending_orders[symbol]

            if self._exchange_protection_enabled():
                protection_ok = await self._place_exchange_protection(trade)
                if not protection_ok:
                    emergency_closed = await self._emergency_close_unprotected(trade)
                    if emergency_closed:
                        del self.active_trades[symbol]
                        strategy.unmark_zone(symbol, order.zone_id)
                    self._save_state()
                    return False

            self._save_state()

            logger.info(f"[{symbol}] 🎯 LIMIT ORDER RETEST TERISI (FILLED) di ${fill_price:.4f}!")
            self.journal.log_entry(
                symbol=symbol, side="LONG", price=fill_price, quantity=order.quantity,
                notional=order.notional, stop_loss=order.stop_loss, take_profit=order.take_profit_2,
                order_id=order.order_id, reason="Limit Retest Filled", mode=order.mode
            )
            msg = (
                f"🎯 <b>LIMIT RETEST FILLED | {order.mode}</b>\n"
                f"<b>Pair:</b> #{symbol.replace('/', '')}\n"
                f"<b>Fill Price:</b> ${fill_price:.4f}\n"
                f"<b>Target TP1 (50%):</b> ${order.take_profit_1:.4f}\n"
                f"<b>Target TP2 (Runner):</b> ${order.take_profit_2:.4f}\n"
                f"<b>Stop Loss:</b> ${order.stop_loss:.4f}\n"
                f"<b>Notional:</b> ${order.notional:.2f}"
            )
            asyncio.create_task(self.notifier.send_message(msg))
            return True

        return False

    async def check_early_invalidation(self, symbol: str, df_htf: pd.DataFrame, btc_market_bullish: bool) -> bool:
        """
        Mengevaluasi apakah rezim pasar makro rusak saat posisi aktif berjalan:
        1. Candle 1H koin ditutup di bawah EMA 50.
        2. Filter BTC berubah menjadi Bearish (untuk Altcoin).
        Proteksi Dinamis: Jika posisi sedang profit, geser SL ke BEP. Jangan market close.
        """
        if symbol not in self.active_trades:
            return False

        if not getattr(self.risk_config, 'enable_early_invalidation', True):
            return False

        if df_htf.empty or len(df_htf) < 2:
            return False

        last_row = df_htf.iloc[-1]
        current_price = float(last_row['close'])
        trade = self.active_trades[symbol]
        triggered = False

        # Syarat A: Candle HTF Close di bawah EMA 50
        check_htf_ema = getattr(self.risk_config, 'invalidation_check_htf_ema', True)
        if check_htf_ema and 'ema' in last_row and not pd.isna(last_row['ema']):
            if last_row['close'] < last_row['ema']:
                triggered = True
                logger.debug(f"[{symbol}] 🛡️ Early Invalidation Triggered: HTF Candle Close < EMA 50.")

        # Syarat B: Induk BTC Market berubah menjadi Bearish (Khusus Altcoin)
        check_btc = getattr(self.risk_config, 'invalidation_check_btc_filter', True)
        is_altcoin = "BTC" not in symbol.upper()
        if check_btc and is_altcoin and not btc_market_bullish:
            triggered = True
            logger.debug(f"[{symbol}] 🛡️ Early Invalidation Triggered: BTC Market Bearish.")

        if triggered:
            bep_profit_pct = getattr(self.risk_config, 'bep_profit_pct', 0.15)
            bep_level = trade.entry_price * (1.0 + (bep_profit_pct / 100.0))

            # Jika harga saat ini sudah di atas BEP, geser Stop Loss ke BEP.
            if current_price > bep_level and not trade.bep_activated:
                if bep_level > trade.stop_loss:
                    trade.stop_loss = self.fetcher.format_price(symbol, bep_level)
                    trade.bep_activated = True
                    logger.warning(f"[{symbol}] 🛡️ Proteksi Dinamis Aktif: Menggeser SL ke BEP akibat Early Invalidation.")

                    if self._exchange_protection_enabled():
                        # Update OCO SL di bursa
                        await self._update_exchange_sl(trade)
                    else:
                        self._save_state()

        return False

    async def close_partial(self, symbol: str, market_price: float, ratio: float = 0.5, reason: str = "PARTIAL_TP1") -> None:
        """Mengeksekusi Partial Take-Profit: Menjual 50% kuantitas posisi dan memindahkan SL ke BEP."""
        if symbol not in self.active_trades:
            return

        trade = self.active_trades[symbol]
        if trade.tp1_executed:
            return

        close_price = self.fetcher.format_price(symbol, market_price)
        close_qty = self.fetcher.format_amount(symbol, trade.quantity * ratio)
        mode = trade.mode

        try:
            # OCO awal mencakup seluruh posisi. Batalkan dulu sebelum partial sell,
            # lalu pasang ulang untuk sisa quantity agar tidak terjadi oversell.
            if self._exchange_protection_enabled():
                if not await self._cancel_exchange_protection(trade):
                    logger.error(f"[{symbol}] Partial TP ditunda: OCO protection belum berhasil dibatalkan.")
                    return

            if self.exec_config.dry_run:
                fill_price = close_price
            else:
                base_currency = symbol.split('/')[0]
                balance_data = await self.fetcher.exchange.fetch_balance()
                actual_balance = float(balance_data.get(base_currency, {}).get('free', 0.0))
                sell_amount = min(close_qty, actual_balance)
                sell_amount = self.fetcher.format_amount(symbol, sell_amount)

                order = await self.fetcher.exchange.create_market_sell_order(symbol=symbol, amount=sell_amount)
                fill_price = float(order['average'] or close_price)

            gross_pnl = (fill_price - trade.entry_price) * close_qty
            entry_fee = (trade.entry_price * close_qty) * 0.00075
            exit_fee = (fill_price * close_qty) * 0.00075
            net_pnl = gross_pnl - (entry_fee + exit_fee)

            trade.remaining_quantity -= close_qty
            trade.realized_pnl += net_pnl
            trade.tp1_executed = True

            # Naikkan SL sisa posisi ke Break-Even Point (+0.15% fee buffer)
            bep_profit_pct = getattr(self.risk_config, 'bep_profit_pct', 0.15)
            bep_level = trade.entry_price * (1.0 + (bep_profit_pct / 100.0))
            if bep_level > trade.stop_loss:
                trade.stop_loss = self.fetcher.format_price(symbol, bep_level)
                trade.bep_activated = True

            if self._exchange_protection_enabled() and trade.remaining_quantity > 0:
                refreshed = await self._place_exchange_protection(
                    trade,
                    quantity=trade.remaining_quantity,
                    stop_loss=trade.stop_loss,
                    take_profit=trade.take_profit_2,
                )
                if not refreshed:
                    logger.critical(
                        f"[{symbol}] OCO sisa posisi gagal dipasang setelah TP1; "
                        "monitor Python menjadi fallback aktif."
                    )

            self.daily_pnl += net_pnl
            self._save_state()

            logger.info(f"[{symbol}] 🎯 PARTIAL TP1 TERCAPAI! Dijual {close_qty} @ ${fill_price:.4f}. Locked PnL: ${net_pnl:.2f}. SL sisa dipindah ke BEP ${trade.stop_loss:.4f}.")
            msg = (
                f"🎯 <b>PARTIAL TAKE-PROFIT (TP1 1.5R) | {mode}</b>\n"
                f"<b>Pair:</b> #{symbol.replace('/', '')}\n"
                f"<b>Terjual:</b> {ratio*100:.0f}% posisi ({close_qty})\n"
                f"<b>Harga Exit:</b> ${fill_price:.4f}\n"
                f"<b>Terkunci Net PnL:</b> ${net_pnl:.2f}\n"
                f"<b>Stop Loss Sisa:</b> Dipindahkan ke BEP ${trade.stop_loss:.4f}"
            )
            asyncio.create_task(self.notifier.send_message(msg))

        except Exception as e:
            logger.error(f"[{symbol}] Error saat eksekusi close_partial: {e}")
            if self._exchange_protection_enabled() and trade.remaining_quantity > 0:
                await self._place_exchange_protection(
                    trade,
                    quantity=trade.remaining_quantity,
                    stop_loss=trade.stop_loss,
                    take_profit=trade.take_profit_2,
                )

    async def close_trade(
        self,
        symbol: str,
        market_price: float,
        reason: str,
        position_already_closed: bool = False,
    ) -> None:
        """Menutup posisi sisa yang sedang berjalan dan mengkalkulasi total akumulasi PNL."""
        if symbol not in self.active_trades:
            return

        trade = self.active_trades[symbol]
        mode = trade.mode
        close_price = self.fetcher.format_price(symbol, market_price)
        qty_to_sell = trade.remaining_quantity
        
        try:
            if self._exchange_protection_enabled() and not position_already_closed:
                if not await self._cancel_exchange_protection(trade):
                    logger.error(f"[{symbol}] Close ditunda: OCO protection belum berhasil dibatalkan.")
                    return

            if position_already_closed:
                fill_price = close_price
            elif self.exec_config.dry_run:
                fill_price = close_price
            else:
                base_currency = symbol.split('/')[0]
                balance_data = await self.fetcher.exchange.fetch_balance()
                actual_balance = float(balance_data.get(base_currency, {}).get('free', 0.0))
                sell_amount = min(qty_to_sell, actual_balance)
                sell_amount = self.fetcher.format_amount(symbol, sell_amount)

                logger.info(f"[{symbol}] Mengirim LIVE Market Sell Order sejumlah {sell_amount} (Reason: {reason})...")
                order = await self.fetcher.exchange.create_market_sell_order(
                    symbol=symbol,
                    amount=sell_amount
                )
                fill_price = float(order['average'] or close_price)

            gross_pnl_rem = (fill_price - trade.entry_price) * qty_to_sell
            entry_fee = (trade.entry_price * qty_to_sell) * 0.00075
            exit_fee = (fill_price * qty_to_sell) * 0.00075
            net_pnl_rem = gross_pnl_rem - (entry_fee + exit_fee)
            
            total_net_pnl = trade.realized_pnl + net_pnl_rem
            initial_notional = trade.entry_price * trade.initial_quantity
            total_pnl_pct = (total_net_pnl / initial_notional) * 100.0 if initial_notional > 0 else 0.0

            self.daily_pnl += net_pnl_rem
            logger.info(f"[{symbol}] Trade Closed ({reason}). Total Net PNL: ${total_net_pnl:.2f} ({total_pnl_pct:.2f}%). Daily PNL: ${self.daily_pnl:.2f}")

            # Circuit Breaker Evaluation
            if self.exec_config.dry_run:
                current_balance = self.exec_config.paper_cash + self.daily_pnl
            else:
                current_balance = await self.fetcher.fetch_quote_balance(self.exec_config.quote_currency)

            max_daily_loss_amount = current_balance * (self.risk_config.max_daily_loss_pct / 100.0)
            if self.daily_pnl <= -max_daily_loss_amount:
                logger.critical(f"🔴 CIRCUIT BREAKER AKTIF! Daily Loss (${self.daily_pnl:.2f}) melebihi toleransi.")
                self.circuit_breaker_active = True

            target_daily_profit = current_balance * (self.risk_config.max_daily_profit_pct / 100.0)
            if self.daily_pnl >= target_daily_profit:
                logger.info(f"🟢 TARGET HARIAN TERCAPAI! Profit harian (${self.daily_pnl:.2f}) mencapai target.")
                self.circuit_breaker_active = True

            final_reason = f"PARTIAL_TP1_THEN_{reason}" if trade.tp1_executed else reason
            self.journal.log_exit(
                symbol=symbol, side=trade.side, price=fill_price, quantity=trade.initial_quantity,
                pnl=total_net_pnl, pnl_pct=total_pnl_pct, reason=final_reason, order_id=trade.order_id, mode=mode
            )

            icon = "🔴" if total_net_pnl < 0 else "🔵"
            msg = (
                f"{icon} <b>CLOSE TRADE | {mode}</b>\n"
                f"<b>Pair:</b> #{symbol.replace('/', '')}\n"
                f"<b>Alasan:</b> {final_reason}\n"
                f"<b>Exit Price:</b> ${fill_price:.4f}\n"
                f"<b>Total Net PnL:</b> ${total_net_pnl:.2f} ({total_pnl_pct:.2f}%)\n"
                f"<b>Daily PnL:</b> ${self.daily_pnl:.2f}"
            )
            asyncio.create_task(self.notifier.send_message(msg))

            del self.active_trades[symbol]
            
            # Dynamic Cooldown
            if "STOP_LOSS" in reason:
                cd_minutes = self.exec_config.sl_cooldown_minutes
            else:
                cd_minutes = self.exec_config.cooldown_minutes
                
            self.cooldowns[symbol] = datetime.now(timezone.utc) + timedelta(minutes=cd_minutes)
            self._save_state()

        except Exception as e:
            logger.error(f"[{symbol}] Error saat eksekusi close_trade: {e}")
            self.journal.log_error(symbol, "Close Trade Error", str(e), mode)
            if self._exchange_protection_enabled() and not position_already_closed and trade.remaining_quantity > 0:
                await self._place_exchange_protection(
                    trade,
                    quantity=trade.remaining_quantity,
                    stop_loss=trade.stop_loss,
                    take_profit=trade.take_profit_2,
                )
            self.cooldowns[symbol] = datetime.now(timezone.utc) + timedelta(minutes=self.exec_config.cooldown_minutes)
            self._save_state()

    async def monitor_open_trades(self, current_prices: Dict[str, float]) -> None:
        """
        Memonitor harga real-time untuk seluruh posisi aktif:
        - TP1 (1.5R): Mengeksekusi penutupan parsial 50% lot
        - TP2 (2.5R): Mengeksekusi penutupan penuh sisa posisi runner
        - Stop Loss / BEP: Menutup posisi jika menyentuh batas pengaman
        """
        self._check_daily_reset()
        
        for symbol in list(self.active_trades.keys()):
            if symbol not in current_prices:
                continue
                
            trade = self.active_trades[symbol]
            current_price = current_prices[symbol]
            trade.last_price_update = current_price

            # OCO dapat terisi lebih cepat daripada ticker yang diproses Python.
            # Sinkronisasi hanya saat harga menyentuh area exit agar tidak
            # menambah REST request pada setiap update harga.
            protection_area_hit = (
                current_price >= trade.take_profit_2 or
                current_price <= trade.stop_loss
            )
            if protection_area_hit and await self._check_exchange_protection_fill(trade):
                continue

            enable_partial = getattr(self.risk_config, 'enable_partial_tp', True)

            # Jika Partial TP belum tereksekusi
            if enable_partial and not trade.tp1_executed:
                # 1. Cek TP1 (1.5R)
                if current_price >= trade.take_profit_1:
                    await self.close_partial(symbol, current_price, ratio=0.5, reason="PARTIAL_TP1")
                    continue

                # 2. Cek Stop Loss
                if current_price <= trade.stop_loss:
                    await self.close_trade(symbol, current_price, "STOP_LOSS")
                    continue

                # 3. Real-time BEP check sebelum TP1
                stop_distance = trade.entry_price - trade.stop_loss_initial
                bep_mult = getattr(self.risk_config, 'bep_trigger_atr_multiplier', 1.1)
                bep_profit_pct = getattr(self.risk_config, 'bep_profit_pct', 0.15)
                if not trade.bep_activated and current_price >= (trade.entry_price + (stop_distance * bep_mult)):
                    bep_level = trade.entry_price * (1.0 + (bep_profit_pct / 100.0))
                    if bep_level > trade.stop_loss:
                        trade.stop_loss = self.fetcher.format_price(symbol, bep_level)
                        trade.bep_activated = True
                        logger.info(f"[{symbol} LIVE] 🛡️ Real-time BEP Terpicu (+{bep_mult:.1f}R)! Stop Loss dipindahkan ke {trade.stop_loss}")
                        if self._exchange_protection_enabled():
                            refreshed = await self._refresh_exchange_protection(trade)
                            if not refreshed:
                                logger.critical(
                                    f"[{symbol}] Gagal memindahkan OCO SL ke BEP; "
                                    "monitor Python tetap aktif sebagai fallback."
                                )
                        self._save_state()
            else:
                # Sisa posisi 50% setelah TP1 (atau mode single TP)
                target_tp = trade.take_profit_2 if enable_partial else trade.take_profit
                if current_price >= target_tp:
                    await self.close_trade(symbol, current_price, "TAKE_PROFIT_ALL (TP1+TP2)" if enable_partial else "TAKE_PROFIT")
                    continue

                if current_price <= trade.stop_loss:
                    await self.close_trade(symbol, current_price, "PARTIAL_TP1_THEN_SL/BEP" if trade.tp1_executed else "STOP_LOSS")
                    continue
                    self._save_state()
