import re

with open("execution.py", "r") as f:
    content = f.read()

# 1. Disable real-time trailing check BEFORE TP1.
# The user wants trailing stop to ONLY activate AFTER TP1.
# We will comment out or remove the "3. Real-time Trailing check sebelum TP1" block entirely,
# or wrap it in a strict condition (though it explicitly says "sebelum TP1", the user requested:
# "Pastikan trailing stop baru aktif setelah TP1 (1.3R atau 1.5R) tercapai, bukan aktif di awal entri agar harga leluasa bernapas.")

# So, we will remove block 3 in `monitor_open_trades`
pattern_realtime = r"(\s+# 3\. Real-time Trailing check sebelum TP1.*?)(?=\s+else:)"
content = re.sub(pattern_realtime, "", content, flags=re.DOTALL)

# 2. Fix FASE 1 trailing in `update_structural_trailing`
# Change the condition so it only trails if `trade.tp1_executed` is True.
pattern_fase1 = r"(# --- FASE 1: TRAILING PROTECTION ---\n\s+bep_mult = getattr\(self\.risk_config, 'bep_trigger_atr_multiplier', 1\.5\)\n\s+bep_profit_pct = getattr\(self\.risk_config, 'bep_profit_pct', 0\.15\)\n\s+if )not trade\.bep_activated and current_profit >= \(stop_distance \* bep_mult\):"
replace_fase1 = r"\1trade.tp1_executed and not trade.bep_activated and current_profit >= (stop_distance * bep_mult):"
content = re.sub(pattern_fase1, replace_fase1, content)

with open("execution.py", "w") as f:
    f.write(content)
