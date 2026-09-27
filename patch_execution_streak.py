import re

with open("execution.py", "r") as f:
    content = f.read()

# Update close_trade to update streak
close_trade_patch = """
            # Dynamic Cooldown & Streak Breaker
            if total_net_pnl < 0:
                self.consecutive_losses[symbol] = self.consecutive_losses.get(symbol, 0) + 1
            else:
                self.consecutive_losses[symbol] = 0

            max_losses = getattr(self.risk_config, "max_consecutive_losses", 2)
            streak_cd = getattr(self.risk_config, "streak_cooldown_hours", 24)

            if self.consecutive_losses.get(symbol, 0) >= max_losses:
                cd_minutes = streak_cd * 60
                logger.warning(f"[{symbol}] 🔴 STREAK BREAKER AKTIF! {max_losses} loss berturut-turut. Cooldown {streak_cd} jam.")
                # Reset streak after triggering cooldown
                self.consecutive_losses[symbol] = 0
            elif "STOP_LOSS" in reason:
                cd_minutes = self.exec_config.sl_cooldown_minutes
            else:
                cd_minutes = self.exec_config.cooldown_minutes

            self.cooldowns[symbol] = datetime.now(timezone.utc) + timedelta(minutes=cd_minutes)
            self._save_state()
"""
content = re.sub(
    r'            # Dynamic Cooldown\n            if "STOP_LOSS" in reason:\n                cd_minutes = self\.exec_config\.sl_cooldown_minutes\n            else:\n                cd_minutes = self\.exec_config\.cooldown_minutes\n                \n            self\.cooldowns\[symbol\] = datetime\.now\(timezone\.utc\) \+ timedelta\(minutes=cd_minutes\)\n            self\._save_state\(\)',
    close_trade_patch.strip('\n'),
    content
)

with open("execution.py", "w") as f:
    f.write(content)
