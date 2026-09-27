import re

with open("execution.py", "r") as f:
    content = f.read()

# Add consecutive_losses to __init__
content = re.sub(
    r'(self\.cooldowns: Dict\[str, datetime\] = \{\})',
    r'\1\n        self.consecutive_losses: Dict[str, int] = {}',
    content
)

# Update _save_state
save_state_patch = """
        state_data = {
            "current_day": self.current_day,
            "daily_pnl": self.daily_pnl,
            "circuit_breaker_active": self.circuit_breaker_active,
            "active_trades": {},
            "pending_orders": {},
            "cooldowns": {sym: cd.isoformat() for sym, cd in self.cooldowns.items()},
            "consecutive_losses": self.consecutive_losses
        }
"""
content = re.sub(
    r'        state_data = \{\n.*?"pending_orders": \{\}\n        \}',
    save_state_patch.strip(),
    content,
    flags=re.DOTALL
)

# Update _load_state
load_state_patch = """
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

            # Load Cooldowns & Consecutive Losses
            cooldowns_data = data.get("cooldowns", {})
            for symbol, cd_iso in cooldowns_data.items():
                self.cooldowns[symbol] = datetime.fromisoformat(cd_iso)
            self.consecutive_losses = data.get("consecutive_losses", {})

            logger.info(f"Berhasil memuat state: {len(self.active_trades)} posisi aktif & {len(self.pending_orders)} pending limit order dipulihkan.")
"""
content = re.sub(
    r'            # Load Active Trades.*?            logger\.info\(f"Berhasil memuat state:',
    load_state_patch.strip('\n') + '\n            logger.info(f"Berhasil memuat state:',
    content,
    flags=re.DOTALL
)

with open("execution.py", "w") as f:
    f.write(content)
