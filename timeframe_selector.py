"""Dynamic Sweet-Spot Timeframe selector.

Modul ini memilih timeframe LTF per simbol berdasarkan evaluasi rolling yang
konservatif. Selector tidak mengoptimalkan timeframe pada setiap candle; hasil
disimpan di cache selama beberapa jam agar tidak berpindah karena noise.
"""

import logging
import math
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import pandas as pd

import indicators
from config import StrategyConfig, RiskConfig
from risk_management import RiskManager
from strategy import StrategyEngine

logger = logging.getLogger(__name__)


@dataclass
class TimeframeMetrics:
    symbol: str
    timeframe: str
    volatility_ratio: float = 0.0
    valid_signals: int = 0
    limit_orders: int = 0
    filled_limit_orders: int = 0
    fill_rate_pct: float = 0.0
    trades: int = 0
    expectancy_pct: float = 0.0
    profit_factor: float = 0.0
    max_drawdown_pct: float = 0.0
    score: float = float('-inf')
    eligible: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class DynamicTimeframeSelector:
    """Memilih LTF terbaik untuk setiap simbol berdasarkan metrik out-of-sample rolling."""

    def __init__(
        self,
        strategy_config: StrategyConfig,
        risk_config: RiskConfig,
        candidate_timeframes: Optional[List[str]] = None,
        evaluation_bars: int = 1000,
        min_trades: int = 20,
        min_profit_factor: float = 1.05,
        min_expectancy_pct: float = 0.0,
        switch_margin: float = 0.10,
        cache_minutes: int = 1440,
    ):
        self.strategy_config = strategy_config
        self.risk_config = risk_config
        self.candidate_timeframes = candidate_timeframes or ['5m', '15m']
        self.evaluation_bars = max(200, evaluation_bars)
        self.min_trades = max(1, min_trades)
        self.min_profit_factor = min_profit_factor
        self.min_expectancy_pct = min_expectancy_pct
        self.switch_margin = max(0.0, switch_margin)
        self.cache_minutes = max(1, cache_minutes)
        self.cache: Dict[str, Tuple[datetime, str, Dict[str, TimeframeMetrics]]] = {}

    def get_cached_timeframe(
        self,
        symbol: str,
        now: Optional[datetime] = None,
    ) -> Optional[str]:
        """Mengembalikan timeframe cache jika masih berlaku."""
        cached = self.cache.get(symbol)
        if cached is None:
            return None
        current_time = now or datetime.now(timezone.utc)
        created_at, selected, _ = cached
        age_minutes = (current_time - created_at).total_seconds() / 60.0
        return selected if age_minutes < self.cache_minutes else None

    @staticmethod
    def _true_range_pct(df: pd.DataFrame, window: int = 200) -> float:
        if df is None or df.empty:
            return 0.0
        prev_close = df['close'].shift(1)
        true_range = pd.concat(
            [
                df['high'] - df['low'],
                (df['high'] - prev_close).abs(),
                (df['low'] - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        pct = (true_range / df['close'].replace(0, pd.NA)).dropna()
        return float(pct.tail(window).mean()) if not pct.empty else 0.0

    @staticmethod
    def _timeframe_seconds(timeframe: str) -> int:
        value = int(timeframe[:-1])
        unit = timeframe[-1].lower()
        if unit == 'm':
            return value * 60
        if unit == 'h':
            return value * 3600
        if unit == 'd':
            return value * 86400
        raise ValueError(f'Timeframe tidak didukung: {timeframe}')

    def _volatility_ratio(self, df_htf: pd.DataFrame, df_ltf: pd.DataFrame) -> float:
        """Rasio volatilitas LTF terhadap HTF berdasarkan true-range percentage."""
        htf_vol = self._true_range_pct(df_htf)
        ltf_vol = self._true_range_pct(df_ltf)
        if htf_vol <= 0:
            return 0.0
        return ltf_vol / htf_vol

    def _build_plan(self, symbol: str, entry: float, stop_ref: float, atr: float, capital: float):
        return RiskManager(self.risk_config).build_trade_plan(
            symbol=symbol,
            entry_price=float(entry),
            stop_loss_ref=float(stop_ref),
            atr_value=float(atr),
            total_capital=float(capital),
        )

    def _close_simulated_trade(
        self,
        trade: dict,
        exit_price: float,
        reason: str,
        fee_rate: float,
        slippage_rate: float,
    ) -> float:
        """Menutup trade simulasi dan mengembalikan PnL percentage setelah fee."""
        if reason in ('STOP_LOSS', 'PARTIAL_TP1_THEN_SL'):
            exit_price *= (1.0 - slippage_rate)

        qty = trade['remaining_quantity']
        gross = (exit_price - trade['entry_price']) * qty
        fees = (trade['entry_price'] * qty + exit_price * qty) * fee_rate
        net = gross - fees
        denominator = trade['entry_price'] * trade['initial_quantity']
        return (net / denominator) * 100.0 if denominator > 0 else 0.0

    def _simulate_open_trade(
        self,
        trade: dict,
        bar: pd.Series,
        window: pd.DataFrame,
        fee_rate: float,
        slippage_rate: float,
    ) -> Optional[float]:
        """Simulasi exit konservatif, termasuk partial TP, BEP, dan trailing."""
        high = float(bar['high'])
        low = float(bar['low'])
        stop_distance = trade['entry_price'] - trade['initial_stop_loss']
        tp1 = trade['tp1']
        tp2 = trade['tp2']

        if not trade['tp1_executed']:
            hit_sl = low <= trade['stop_loss']
            hit_tp1 = high >= tp1
            if hit_sl and hit_tp1:
                return self._close_simulated_trade(
                    trade, trade['stop_loss'], 'STOP_LOSS', fee_rate, slippage_rate
                )
            if hit_sl:
                return self._close_simulated_trade(
                    trade, trade['stop_loss'], 'STOP_LOSS', fee_rate, slippage_rate
                )
            if hit_tp1:
                half_qty = trade['initial_quantity'] * trade['partial_ratio']
                tp1_gross = (tp1 - trade['entry_price']) * half_qty
                tp1_fees = (trade['entry_price'] * half_qty + tp1 * half_qty) * fee_rate
                trade['realized_pnl_pct'] += ((tp1_gross - tp1_fees) /
                                               (trade['entry_price'] * trade['initial_quantity'])) * 100.0
                trade['remaining_quantity'] -= half_qty
                trade['tp1_executed'] = True
                trade['stop_loss'] = trade['entry_price'] * (1.0 + trade['bep_profit_pct'])
                if high >= tp2:
                    return trade['realized_pnl_pct'] + self._close_simulated_trade(
                        trade, tp2, 'TAKE_PROFIT', fee_rate, slippage_rate
                    )
                return None

            if high >= trade['entry_price'] + stop_distance * trade['bep_trigger']:
                trade['stop_loss'] = max(
                    trade['stop_loss'],
                    trade['entry_price'] * (1.0 + trade['bep_profit_pct']),
                )
        else:
            if low <= trade['stop_loss']:
                return trade['realized_pnl_pct'] + self._close_simulated_trade(
                    trade, trade['stop_loss'], 'PARTIAL_TP1_THEN_SL', fee_rate, slippage_rate
                )
            if high >= tp2:
                return trade['realized_pnl_pct'] + self._close_simulated_trade(
                    trade, tp2, 'TAKE_PROFIT', fee_rate, slippage_rate
                )

        if high >= trade['entry_price'] + stop_distance * 1.7:
            recent_low = float(window['low'].tail(15).min())
            trail = recent_low * 0.998
            if trail > trade['stop_loss'] and trail > trade['entry_price']:
                trade['stop_loss'] = trail
        return None

    def evaluate(
        self,
        symbol: str,
        timeframe: str,
        df_htf: pd.DataFrame,
        df_ltf_raw: pd.DataFrame,
        df_btc_htf: Optional[pd.DataFrame] = None,
    ) -> TimeframeMetrics:
        """Mengukur satu kandidat timeframe memakai data rolling yang tersedia."""
        metrics = TimeframeMetrics(symbol=symbol, timeframe=timeframe)
        if df_ltf_raw is None or df_ltf_raw.empty or df_htf is None or df_htf.empty:
            return metrics

        ltf = indicators.prepare_ltf_frame(
            df_ltf_raw.tail(self.evaluation_bars).copy(),
            atr_period=self.strategy_config.atr_period,
            stoch_k=self.strategy_config.stochastic_k_period,
            stoch_k_smooth=self.strategy_config.stochastic_k_smoothing,
            stoch_d=self.strategy_config.stochastic_d_period,
        )
        htf = df_htf.copy()
        if ltf.empty or htf.empty:
            return metrics

        htf = htf.sort_values('timestamp').reset_index(drop=True)
        ltf = ltf.sort_values('timestamp').reset_index(drop=True)
        btc = None if df_btc_htf is None else df_btc_htf.sort_values('timestamp').reset_index(drop=True)
        htf_close_times = pd.to_datetime(htf['timestamp'], utc=True) + pd.to_timedelta(
            self._timeframe_seconds(self.strategy_config.higher_timeframe), unit='s'
        )
        btc_close_times = None
        if btc is not None and not btc.empty:
            btc_close_times = pd.to_datetime(btc['timestamp'], utc=True) + pd.to_timedelta(
                self._timeframe_seconds(self.strategy_config.higher_timeframe), unit='s'
            )

        strategy = StrategyEngine(self.strategy_config)
        capital = 1000.0
        active_trade = None
        pending = None
        last_htf_pos = -1
        last_btc_pos = -1
        context = None
        returns: List[float] = []
        fee_rate = self.risk_config.estimated_exchange_fee_pct / 100.0 / 2.0
        slippage_rate = 0.00025
        lookback = min(200, max(50, len(ltf) // 4))

        for i in range(lookback, len(ltf)):
            current_time = pd.Timestamp(ltf.iloc[i]['timestamp'])
            window = ltf.iloc[max(0, i - lookback + 1): i + 1]
            bar = window.iloc[-1]

            htf_pos = int(htf_close_times.searchsorted(current_time, side='right') - 1)
            if htf_pos < 50:
                continue

            if htf_pos != last_htf_pos:
                htf_window = htf.iloc[max(0, htf_pos - self.strategy_config.htf_limit + 1): htf_pos + 1]
                context = strategy.analyze_higher_timeframe(symbol, htf_window)
                last_htf_pos = htf_pos

            btc_window = None
            if btc is not None and btc_close_times is not None:
                btc_pos = int(btc_close_times.searchsorted(current_time, side='right') - 1)
                if btc_pos >= 1:
                    btc_window = btc.iloc[max(0, btc_pos - 100): btc_pos + 1]
                    if btc_pos != last_btc_pos:
                        strategy.update_btc_market_status(btc_window)
                        last_btc_pos = btc_pos

            if active_trade is not None:
                result = self._simulate_open_trade(
                    active_trade, bar, window, fee_rate, slippage_rate
                )
                if result is not None:
                    returns.append(result)
                    capital *= (1.0 + result / 100.0)
                    active_trade = None
                continue

            if pending is not None:
                pending['bars_elapsed'] += 1
                plan = self._build_plan(
                    symbol, pending['entry'], pending['stop_ref'], pending['atr'], capital
                )
                if pending['bars_elapsed'] > pending['timeout'] or plan is None:
                    pending = None
                    continue
                if float(bar['low']) <= plan.stop_loss:
                    pending = None
                    continue
                if float(bar['low']) <= pending['entry']:
                    metrics.filled_limit_orders += 1
                    active_trade = self._new_trade_state(plan)
                    pending = None
                else:
                    continue

            if active_trade is not None or context is None:
                continue

            metrics.valid_signals += 1
            signal = strategy.evaluate_lower_timeframe(
                symbol,
                window,
                context,
                df_htf=htf.iloc[max(0, htf_pos - self.strategy_config.rs_ema_period - 10): htf_pos + 1],
                df_btc_htf=btc_window,
            )
            if signal is None:
                metrics.valid_signals -= 1
                continue

            if signal.entry_type == 'LIMIT_RETEST':
                metrics.limit_orders += 1
                pending = {
                    'entry': signal.entry_price,
                    'stop_ref': signal.stop_loss_ref,
                    'atr': signal.atr_value,
                    'timeout': signal.timeout_bars,
                    'bars_elapsed': 0,
                }
            else:
                plan = self._build_plan(
                    symbol, signal.entry_price, signal.stop_loss_ref, signal.atr_value, capital
                )
                if plan is not None:
                    active_trade = self._new_trade_state(plan)

        metrics.trades = len(returns)
        metrics.expectancy_pct = float(sum(returns) / len(returns)) if returns else 0.0
        gross_profit = sum(x for x in returns if x > 0)
        gross_loss = abs(sum(x for x in returns if x < 0))
        metrics.profit_factor = gross_profit / gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
        metrics.fill_rate_pct = (
            metrics.filled_limit_orders / metrics.limit_orders * 100.0
            if metrics.limit_orders else 100.0
        )
        if returns:
            curve = pd.Series([100.0 * math.prod(1.0 + x / 100.0 for x in returns[:i + 1]) for i in range(len(returns))])
            drawdown = (curve - curve.cummax()) / curve.cummax() * 100.0
            metrics.max_drawdown_pct = float(drawdown.min())
        metrics.volatility_ratio = self._volatility_ratio(htf, df_ltf_raw)
        metrics.eligible = (
            metrics.trades >= self.min_trades and
            metrics.expectancy_pct > self.min_expectancy_pct and
            metrics.profit_factor >= self.min_profit_factor
        )
        return metrics

    @staticmethod
    def _new_trade_state(plan) -> dict:
        return {
            'entry_price': float(plan.entry_price),
            'initial_quantity': float(plan.quantity),
            'remaining_quantity': float(plan.quantity),
            'initial_stop_loss': float(plan.stop_loss),
            'stop_loss': float(plan.stop_loss),
            'tp1': float(plan.take_profit_1),
            'tp2': float(plan.take_profit_2),
            'tp1_executed': False,
            'realized_pnl_pct': 0.0,
            'partial_ratio': 0.5,
            'bep_trigger': 1.1,
            'bep_profit_pct': 0.0015,
        }

    def _score(self, metrics: TimeframeMetrics) -> float:
        if not metrics.eligible:
            return float('-inf')

        # Volatility ratio dipakai sebagai penalti bila LTF terlalu lambat/berisik
        # dibanding HTF. Target dapat dituning, tetapi tidak menggantikan metrik PnL.
        target_ratio = 0.35
        volatility_score = 1.0 / (1.0 + abs(math.log(max(metrics.volatility_ratio, 1e-9) / target_ratio)))
        expectancy_score = max(-1.0, min(1.0, metrics.expectancy_pct / 2.0))
        pf_score = max(0.0, min(1.0, (metrics.profit_factor - 1.0) / 2.0))
        fill_score = max(0.0, min(1.0, metrics.fill_rate_pct / 100.0))
        sample_score = min(1.0, metrics.trades / max(self.min_trades * 3, 1))
        dd_score = 1.0 / (1.0 + abs(metrics.max_drawdown_pct) / 10.0)

        return (
            0.30 * expectancy_score +
            0.20 * pf_score +
            0.15 * fill_score +
            0.15 * sample_score +
            0.10 * dd_score +
            0.10 * volatility_score
        )

    def choose(
        self,
        symbol: str,
        df_htf: pd.DataFrame,
        candidate_frames: Dict[str, pd.DataFrame],
        df_btc_htf: Optional[pd.DataFrame] = None,
        fallback: str = '15m',
        now: Optional[datetime] = None,
    ) -> Tuple[str, Dict[str, TimeframeMetrics]]:
        """Evaluasi kandidat jika cache expired dan mengembalikan timeframe terpilih."""
        current_time = now or datetime.now(timezone.utc)
        cached = self.cache.get(symbol)
        if cached is not None:
            created_at, selected, cached_metrics = cached
            age_minutes = (current_time - created_at).total_seconds() / 60.0
            if age_minutes < self.cache_minutes:
                return selected, cached_metrics

        metrics_by_tf: Dict[str, TimeframeMetrics] = {}
        for timeframe in self.candidate_timeframes:
            frame = candidate_frames.get(timeframe)
            if frame is None:
                continue
            metrics = self.evaluate(symbol, timeframe, df_htf, frame, df_btc_htf)
            metrics.score = self._score(metrics)
            metrics_by_tf[timeframe] = metrics

        selected = fallback
        ranked = sorted(
            (m for m in metrics_by_tf.values() if m.eligible),
            key=lambda item: item.score,
            reverse=True,
        )
        if ranked:
            best = ranked[0]
            previous = self.cache.get(symbol)
            previous_score = None
            if previous is not None:
                previous_score = metrics_by_tf.get(previous[1], TimeframeMetrics(symbol, previous[1])).score
            if previous_score is None or best.timeframe == previous[1] or best.score >= previous_score + self.switch_margin:
                selected = best.timeframe
            elif previous[1] in metrics_by_tf:
                selected = previous[1]

        self.cache[symbol] = (current_time, selected, metrics_by_tf)
        logger.info(
            f"[{symbol}] Dynamic timeframe memilih {selected}: "
            + ', '.join(
                f"{tf}=score:{m.score:.3f}, trades:{m.trades}, exp:{m.expectancy_pct:.3f}%, "
                f"PF:{m.profit_factor:.2f}, fill:{m.fill_rate_pct:.1f}%"
                for tf, m in metrics_by_tf.items()
            )
        )
        return selected, metrics_by_tf
