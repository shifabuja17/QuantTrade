import asyncio
import re
import sys

coins = ["BTC/USDT", "LINK/USDT", "ADA/USDT", "ETH/USDT", "AVAX/USDT", 
         "SOL/USDT", "XRP/USDT", "POL/USDT", "DOT/USDT", "ATOM/USDT", 
         "XLM/USDT", "ALGO/USDT"]

async def run_backtest(coin):
    print(f"[*] Menjalankan backtest untuk {coin} (harap tunggu, mengunduh data jika belum ada di cache)...")
    # Menggunakan sys.executable agar menyesuaikan dengan (python / python3) milik sistem Anda
    cmd = [sys.executable, "backtest.py", "--symbol", coin, 
           "--start", "2025-01-01T00:00:00Z", "--end", "2025-12-31T00:00:00Z"]
    
    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await process.communicate()
    
    if process.returncode == 0:
        print(f"[+] Selesai: {coin}")
        return stdout.decode()
    else:
        print(f"[!] Error pada {coin}:\n{stderr.decode()}")
        return ""

async def main():
    results = []
    # Jalankan secara berurutan agar Binance tidak memblokir IP kita (Rate Limit)
    for c in coins:
        res = await run_backtest(c)
        results.append(res)
        
    print("\n\n| Coin | Trades | Win Rate (%) | Profit Factor | Net PnL ($) | Max Drawdown (%) |")
    print("|---|---|---|---|---|---|")
    for coin, out in zip(coins, results):
        if not out: continue
        trades = re.search(r"Total Trades\s+:\s+(\d+)", out)
        if not trades or int(trades.group(1)) == 0: 
            print(f"| {coin} | 0 | 0.0 | 0.0 | 0.0 | 0.0 |")
            continue
            
        wr = re.search(r"Win Rate\s+:\s+([0-9.]+)%", out).group(1)
        pf_match = re.search(r"Profit Factor\s+:\s+([0-9.]+)", out)
        pf = pf_match.group(1) if (pf_match and pf_match.group(1) != 'inf') else "999.0"
        pnl = re.search(r"Total Net PnL\s+:\s+\$([0-9.-]+)", out).group(1)
        dd = re.search(r"Max Drawdown\s+:\s+([0-9.-]+)%", out).group(1)
        
        print(f"| {coin} | {trades.group(1)} | {wr} | {pf} | {pnl} | {dd} |")

if __name__ == "__main__":
    asyncio.run(main())