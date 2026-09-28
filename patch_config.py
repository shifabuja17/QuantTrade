import json

with open("config.json", "r") as f:
    data = json.load(f)

# The user explicitly asked to "ubah target Take Profit 2 (TP2) pada config.json".
# Currently it's hardcoded in config.py. Let's add it to config.json under 'risk'
# if it's not there.
if "symbol_tp2_multiplier" not in data["risk"]:
    data["risk"]["symbol_tp2_multiplier"] = {
        "SOL/USDT": 1.8,
        "BTC/USDT": 2.2
    }

with open("config.json", "w") as f:
    json.dump(data, f, indent=2)