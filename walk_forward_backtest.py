"""Walk-forward backtest untuk Dynamic Sweet-Spot Timeframe.

Contoh:
    python walk_forward_backtest.py --symbol ADA/USDT \
        --start 2024-05-01 --end 2025-11-01 \
        --train-days 90 --test-days 30 --step-days 30

Pada setiap fold:
1. Kandidat timeframe dipilih hanya dari window training.
2. Timeframe dikunci pada window test berikutnya.
3. Data training tetap diberikan ke backtester sebagai warm-up, tetapi entry
   sebelum test_start dilarang.
"""

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import pandas as pd

import indicators
from backtest import Backtester
from config import load_config
from timeframe_selector import DynamicTimeframeSelector, TimeframeMetrics

logger = logging.getLogger(__name__)


def parse_utc(value: str) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize('UTC')
    else:
        timestamp = timestamp.tz_convert('UTC')
    return timestamp


def slice_frame(df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    timestamps = pd.to_datetime(df['timestamp'], utc=True)
    return df.loc[(timestamps >= start) & (timestamps <= end)].copy().reset_index(drop=True)


def build_folds(
    start: pd.Timestamp,
    end: pd.Timestamp,
    train_days: int,
    test_days: int,
    step_days: int,
) -> List[Tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp]]:
    if train_days <= 0 or test_days <= 0 or step_days <= 0:
        raise ValueError('train-days, test-days, dan step-days harus lebih besar dari 0.')

    folds = []
    cursor = start
    while cursor + timedelta(days=train_days + test_days) <= end:
        train_end = cursor + timedelta(days=train_days)
        test_end = train_end + timedelta(days=test_days)
        folds.append((cursor, train_end, test_end))
        cursor += timedelta(days=step_days)
    return folds


def summarize_trades(trades: list, capital_before: float, capital_after: float) -> dict:
    if not trades:
        return {
            'trades': 0,
            'wins': 0,
            'losses': 0,
            'win_rate_pct': 0.0,
            'expectancy_pct': 0.0,
            'profit_factor': 0.0,
            'net_pnl': 0.0,
            'return_pct': 0.0,
            'max_drawdown_pct': 0.0,
            'capital_before': capital_before,
            'capital_after': capital_after,
        }

    pnls = [float(t.get('pnl', 0.0)) for t in trades]
    pnl_pcts = [float(t.get('pnl_pct', 0.0)) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))

    equity = [capital_before]
    equity.extend(float(t.get('capital_after', capital_before)) for t in trades)
    equity_series = pd.Series(equity, dtype=float)
    drawdown = (equity_series - equity_series.cummax()) / equity_series.cummax() * 100.0

    return {
        'trades': len(trades),
        'wins': len(wins),
        'losses': len(losses),
        'win_rate_pct': (len(wins) / len(trades)) * 100.0,
        'expectancy_pct': sum(pnl_pcts) / len(pnl_pcts),
        'profit_factor': gross_profit / gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0),
        'net_pnl': sum(pnls),
        'return_pct': ((capital_after - capital_before) / capital_before) * 100.0 if capital_before else 0.0,
        'max_drawdown_pct': float(drawdown.min()),
        'capital_before': capital_before,
        'capital_after': capital_after,
    }


class WalkForwardRunner:
    def __init__(
        self,
        symbol: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
        config_path: str,
        train_days: int,
        test_days: int,
        step_days: int,
        selector_bars: int,
        candidate_timeframes: Optional[List[str]] = None,
    ):
        self.symbol = symbol
        self.start = start
        self.end = end
        self.config_path = config_path
        self.config = load_config(config_path)
        self.train_days = train_days
        self.test_days = test_days
        self.step_days = step_days
        self.selector_bars = selector_bars
        self.candidate_timeframes = candidate_timeframes or getattr(
            self.config.strategy, 'dynamic_candidate_timeframes', ['5m', '15m']
        )
        self.htf = self.config.strategy.higher_timeframe
        self.data: Dict[str, pd.DataFrame] = {}
        self.capital = float(self.config.execution.paper_cash)
        self.fold_results: List[dict] = []
        self.all_trades: List[dict] = []

    async def load_master_data(self) -> None:
        """Unduh/muat satu master dataset agar setiap fold memakai sumber data yang sama."""
        start_iso = self.start.isoformat()
        end_iso = self.end.isoformat()
        loader = Backtester(
            symbol=self.symbol,
            start_date=start_iso,
            end_date=end_iso,
            config_path=self.config_path,
            timeframe=self.candidate_timeframes[0],
        )

        async def load_one(symbol: str, timeframe: str) -> pd.DataFrame:
            cached = self._load_covering_cache(symbol, timeframe)
            if cached is not None and not cached.empty:
                print(f"[*] Memuat cache gabungan {symbol} {timeframe} ({len(cached)} candle)")
                return cached
            return await loader.fetch_historical_data(timeframe, symbol=symbol)

        print('[*] Memuat master data walk-forward...')
        self.data[f'{self.symbol}|{self.htf}'] = await load_one(self.symbol, self.htf)
        for timeframe in self.candidate_timeframes:
            self.data[f'{self.symbol}|{timeframe}'] = await load_one(self.symbol, timeframe)

        if self.symbol != self.config.strategy.btc_filter_symbol:
            btc_symbol = self.config.strategy.btc_filter_symbol
            self.data[f'{btc_symbol}|{self.htf}'] = await load_one(btc_symbol, self.htf)

    def _load_covering_cache(self, symbol: str, timeframe: str) -> Optional[pd.DataFrame]:
        """Mencari/merge cache CSV yang menutup seluruh rentang walk-forward."""
        data_dir = Path('backtest_data')
        prefix = symbol.replace('/', '_') + f'_{timeframe}_'
        segments = []
        for path in data_dir.glob(prefix + '*.csv'):
            parts = path.stem.split('_')
            if len(parts) < 4:
                continue
            try:
                segment_start = parse_utc(parts[-2])
                # Nama file memakai tanggal akhir sebagai batas inklusif.
                segment_end = parse_utc(parts[-1]) + pd.Timedelta(days=1)
            except Exception:
                continue
            if segment_end >= self.start and segment_start <= self.end:
                segments.append((segment_start, segment_end, path))

        if not segments:
            return None

        segments.sort(key=lambda item: item[0])
        frames = [
            pd.read_csv(path, parse_dates=['timestamp'])
            for _, _, path in segments
        ]
        merged = pd.concat(frames, ignore_index=True)
        merged['timestamp'] = pd.to_datetime(merged['timestamp'], utc=True)
        merged = merged.drop_duplicates(subset=['timestamp']).sort_values('timestamp').reset_index(drop=True)

        if merged.empty:
            return None
        if merged['timestamp'].iloc[0] > self.start or merged['timestamp'].iloc[-1] < self.end:
            return None
        return merged

    def _get_frame(self, symbol: str, timeframe: str) -> pd.DataFrame:
        return self.data.get(f'{symbol}|{timeframe}', pd.DataFrame())

    def _select_timeframe(
        self,
        train_start: pd.Timestamp,
        train_end: pd.Timestamp,
    ) -> Tuple[str, Dict[str, TimeframeMetrics]]:
        htf_raw = slice_frame(self._get_frame(self.symbol, self.htf), train_start, train_end)
        htf = indicators.prepare_htf_frame(
            htf_raw,
            ema_period=self.config.strategy.ema_period,
            tema_period=self.config.strategy.tema_period,
        )
        candidate_frames = {
            timeframe: slice_frame(
                self._get_frame(self.symbol, timeframe), train_start, train_end
            )
            for timeframe in self.candidate_timeframes
        }

        btc_htf = None
        if self.symbol != self.config.strategy.btc_filter_symbol:
            btc_raw = slice_frame(
                self._get_frame(self.config.strategy.btc_filter_symbol, self.htf),
                train_start,
                train_end,
            )
            btc_htf = indicators.prepare_htf_frame(
                btc_raw,
                ema_period=self.config.strategy.ema_period,
                tema_period=self.config.strategy.tema_period,
            )

        selector = DynamicTimeframeSelector(
            strategy_config=self.config.strategy,
            risk_config=self.config.risk,
            candidate_timeframes=self.candidate_timeframes,
            evaluation_bars=self.selector_bars,
            min_trades=getattr(self.config.strategy, 'dynamic_timeframe_min_trades', 5),
            min_profit_factor=getattr(self.config.strategy, 'dynamic_timeframe_min_profit_factor', 1.05),
            min_expectancy_pct=getattr(self.config.strategy, 'dynamic_timeframe_min_expectancy_pct', 0.0),
            switch_margin=getattr(self.config.strategy, 'dynamic_timeframe_switch_margin', 0.10),
            cache_minutes=1,
        )
        fallback = self.config.strategy.get_ltf_for_symbol(self.symbol)
        if fallback not in self.candidate_timeframes:
            fallback = self.candidate_timeframes[0]
        return selector.choose(
            symbol=self.symbol,
            df_htf=htf,
            candidate_frames=candidate_frames,
            df_btc_htf=btc_htf,
            fallback=fallback,
            now=train_end.to_pydatetime(),
        )

    async def _run_test_fold(
        self,
        train_start: pd.Timestamp,
        test_start: pd.Timestamp,
        test_end: pd.Timestamp,
        timeframe: str,
    ) -> Tuple[dict, List[dict]]:
        """Jalankan Backtester pada test fold dengan warm-up dari training fold."""
        source_start = train_start
        bt = Backtester(
            symbol=self.symbol,
            start_date=source_start.isoformat(),
            end_date=test_end.isoformat(),
            config_path=self.config_path,
            timeframe=timeframe,
        )
        bt.initial_capital = self.capital
        bt.current_capital = self.capital
        bt.trade_start_date = test_start
        bt.start_date = test_start.isoformat()
        bt.end_date = test_end.isoformat()

        async def fetch_from_master(requested_timeframe: str, symbol: str = None) -> pd.DataFrame:
            requested_symbol = symbol or self.symbol
            frame = self._get_frame(requested_symbol, requested_timeframe)
            return slice_frame(frame, source_start, test_end)

        # Backtester lama tetap menjadi mesin eksekusi tunggal; walk-forward hanya
        # mengganti sumber datanya agar tidak mengulang logika fill/SL/TP.
        bt.fetch_historical_data = fetch_from_master
        await bt.run()

        fold_metrics = summarize_trades(
            bt.trades_history,
            capital_before=self.capital,
            capital_after=bt.current_capital,
        )
        self.capital = bt.current_capital
        self.all_trades.extend(bt.trades_history)
        return fold_metrics, bt.trades_history

    async def run(self) -> dict:
        await self.load_master_data()
        folds = build_folds(
            self.start,
            self.end,
            self.train_days,
            self.test_days,
            self.step_days,
        )
        if not folds:
            raise ValueError('Rentang tanggal tidak cukup untuk membentuk satu fold.')

        for index, (train_start, test_start, test_end) in enumerate(folds, start=1):
            selected, selection_metrics = self._select_timeframe(train_start, test_start)
            capital_before = self.capital
            fold_metrics, _ = await self._run_test_fold(
                train_start=train_start,
                test_start=test_start,
                test_end=test_end,
                timeframe=selected,
            )
            fold_result = {
                'fold': index,
                'train_start': train_start.isoformat(),
                'train_end': test_start.isoformat(),
                'test_start': test_start.isoformat(),
                'test_end': test_end.isoformat(),
                'selected_timeframe': selected,
                'selection_metrics': {
                    timeframe: metrics.to_dict()
                    for timeframe, metrics in selection_metrics.items()
                },
                'test_metrics': fold_metrics,
                'capital_before': capital_before,
                'capital_after': self.capital,
            }
            self.fold_results.append(fold_result)
            print(
                f"[WF {index}/{len(folds)}] {test_start.date()} -> {test_end.date()} | "
                f"LTF={selected} | trades={fold_metrics['trades']} | "
                f"PnL=${fold_metrics['net_pnl']:+.2f} | "
                f"PF={fold_metrics['profit_factor']:.2f}"
            )

        return self._build_report()

    def _build_report(self) -> dict:
        all_pnls = [float(t.get('pnl', 0.0)) for t in self.all_trades]
        all_wins = [p for p in all_pnls if p > 0]
        all_losses = [p for p in all_pnls if p < 0]
        gross_profit = sum(all_wins)
        gross_loss = abs(sum(all_losses))
        selected_counts: Dict[str, int] = {}
        for fold in self.fold_results:
            tf = fold['selected_timeframe']
            selected_counts[tf] = selected_counts.get(tf, 0) + 1

        equity = [float(self.config.execution.paper_cash)]
        equity.extend(float(t.get('capital_after', equity[-1])) for t in self.all_trades)
        equity_series = pd.Series(equity, dtype=float)
        drawdown = (equity_series - equity_series.cummax()) / equity_series.cummax() * 100.0

        return {
            'symbol': self.symbol,
            'start': self.start.isoformat(),
            'end': self.end.isoformat(),
            'train_days': self.train_days,
            'test_days': self.test_days,
            'step_days': self.step_days,
            'candidate_timeframes': self.candidate_timeframes,
            'folds': len(self.fold_results),
            'selected_timeframes': selected_counts,
            'overall': {
                'trades': len(all_pnls),
                'wins': len(all_wins),
                'losses': len(all_losses),
                'win_rate_pct': (len(all_wins) / len(all_pnls) * 100.0) if all_pnls else 0.0,
                'expectancy_pct': sum(float(t.get('pnl_pct', 0.0)) for t in self.all_trades) / len(all_pnls) if all_pnls else 0.0,
                'profit_factor': gross_profit / gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0),
                'net_pnl': sum(all_pnls),
                'return_pct': ((self.capital - self.config.execution.paper_cash) / self.config.execution.paper_cash) * 100.0,
                'max_drawdown_pct': float(drawdown.min()),
                'capital_start': float(self.config.execution.paper_cash),
                'capital_end': float(self.capital),
            },
            'fold_results': self.fold_results,
        }


async def async_main(args) -> dict:
    runner = WalkForwardRunner(
        symbol=args.symbol,
        start=parse_utc(args.start),
        end=parse_utc(args.end),
        config_path=args.config,
        train_days=args.train_days,
        test_days=args.test_days,
        step_days=args.step_days,
        selector_bars=args.selector_bars,
        candidate_timeframes=args.timeframes,
    )
    report = await runner.run()
    with open(args.output, 'w', encoding='utf-8') as file:
        json.dump(report, file, indent=2, default=str)
    print(f"\n[+] Report walk-forward disimpan di {args.output}")
    print(json.dumps(report['overall'], indent=2))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description='Walk-forward backtest Dynamic Sweet-Spot Timeframe')
    parser.add_argument('--symbol', default='BTC/USDT')
    parser.add_argument('--start', default='2024-05-01T00:00:00Z')
    parser.add_argument('--end', default='2025-11-01T00:00:00Z')
    parser.add_argument('--train-days', type=int, default=90)
    parser.add_argument('--test-days', type=int, default=30)
    parser.add_argument('--step-days', type=int, default=30)
    parser.add_argument('--selector-bars', type=int, default=5000)
    parser.add_argument('--timeframes', nargs='+', default=None, help='Contoh: 5m 15m')
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--output', default='walk_forward_results.json')
    args = parser.parse_args()
    asyncio.run(async_main(args))


if __name__ == '__main__':
    main()
