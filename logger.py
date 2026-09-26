import logging
import sys
import os
import csv
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Union, Dict, Any

def setup_logger(level: str, log_file: str) -> logging.Logger:
    """Konfigurasi Root Logger agar semua modul terhubung ke satu file bot.log"""
    # Konversi string level (INFO/DEBUG) menjadi konstanta bawaan Python
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    
    # Ambil ROOT logger (logger tertinggi yang membawahi semua file)
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)
    
    # Bersihkan handler lama (mencegah log ganda jika bot direstart)
    if root_logger.hasHandlers():
        root_logger.handlers.clear()
        
    # Format penulisan log yang rapi: [Waktu] LEVEL - NamaFile - Pesan
    formatter = logging.Formatter(
        '[%(asctime)s] %(levelname)s - %(name)s - %(message)s', 
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # 1. Handler untuk menulis ke File (bot.log)
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)
    
    # 2. Handler untuk menampilkan di Layar Terminal
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)
    
    # Nonaktifkan log bawaan ccxt/asyncio yang terlalu berisik
    logging.getLogger('ccxt').setLevel(logging.WARNING)
    logging.getLogger('asyncio').setLevel(logging.WARNING)
    
    # Bungkam modul yang sering spam, pastikan hanya mencetak jika ada ERROR
    logging.getLogger('strategy').setLevel(logging.WARNING)
    logging.getLogger('data_fetcher').setLevel(logging.WARNING)
    logging.getLogger('zone_detection').setLevel(logging.WARNING)
    
    return logging.getLogger("BotCore")
class TradeJournal:
    """
    Jurnal trading berbasis CSV dengan implementasi thread-safe.
    Digunakan untuk mencatat semua event trading (signal, entry, exit, error).
    """
    
    # Field CSV wajib sesuai urutan
    FIELDS = [
        "timestamp", "event", "symbol", "side", "price", "quantity",
        "stop_loss", "take_profit", "notional", "pnl", "pnl_pct",
        "reason", "zone_id", "order_id", "mode", "notes"
    ]

    def __init__(self, filepath: Union[str, Path] = "trades.csv"):
        self.filepath = Path(filepath)
        self._lock = threading.Lock()
        
        # Buat file beserta header jika file belum eksis
        self._initialize_csv()

    def _initialize_csv(self) -> None:
        """Membuat file CSV dan menulis header jika belum ada."""
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        if not self.filepath.exists():
            with self._lock:
                # Double check inside lock
                if not self.filepath.exists():
                    with open(self.filepath, mode='w', newline='', encoding='utf-8') as f:
                        writer = csv.DictWriter(f, fieldnames=self.FIELDS)
                        writer.writeheader()

    def _write_row(self, data: Dict[str, Any]) -> None:
        """Menulis satu baris ke dalam CSV secara thread-safe."""
        # Validasi dan filtering agar data sesuai dengan header FIELDS
        row = {field: data.get(field, "") for field in self.FIELDS}
        
        # Berikan timestamp otomatis jika tidak disediakan
        if not row["timestamp"]:
            row["timestamp"] = datetime.now(timezone.utc).isoformat()

        with self._lock:
            with open(self.filepath, mode='a', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=self.FIELDS)
                writer.writerow(row)

    def log_signal(self, symbol: str, side: str, price: float, reason: str, mode: str = "dry_run", **kwargs: Any) -> None:
        """Mencatat sinyal trading yang terdeteksi oleh sistem."""
        data = {
            "event": "SIGNAL",
            "symbol": symbol,
            "side": side.upper(),
            "price": price,
            "reason": reason,
            "mode": mode,
            **kwargs
        }
        self._write_row(data)

    def log_entry(self, symbol: str, side: str, price: float, quantity: float, 
                  notional: float, stop_loss: float, take_profit: float, 
                  order_id: str, reason: str, mode: str = "dry_run", **kwargs: Any) -> None:
        """Mencatat eksekusi entry pasar yang berhasil."""
        data = {
            "event": "ENTRY",
            "symbol": symbol,
            "side": side.upper(),
            "price": price,
            "quantity": quantity,
            "notional": notional,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "order_id": order_id,
            "reason": reason,
            "mode": mode,
            **kwargs
        }
        self._write_row(data)

    def log_exit(self, symbol: str, side: str, price: float, quantity: float, 
                 pnl: float, pnl_pct: float, reason: str, order_id: str, 
                 mode: str = "dry_run", **kwargs: Any) -> None:
        """Mencatat eksekusi exit pasar (baik kena SL, TP, atau manual)."""
        data = {
            "event": "EXIT",
            "symbol": symbol,
            "side": side.upper(),
            "price": price,
            "quantity": quantity,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "reason": reason,
            "order_id": order_id,
            "mode": mode,
            **kwargs
        }
        self._write_row(data)

    def log_error(self, symbol: str, reason: str, notes: str, mode: str = "system", **kwargs: Any) -> None:
        """Mencatat error yang terjadi pada flow eksekusi order."""
        data = {
            "event": "ERROR",
            "symbol": symbol,
            "reason": reason,
            "notes": notes,
            "mode": mode,
            **kwargs
        }
        self._write_row(data)