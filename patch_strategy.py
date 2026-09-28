with open("strategy.py", "r") as f:
    content = f.read()

patch_code = """
        # === FILTER HTF KHUSUS LINK ===
        # Memastikan bahwa LINK hanya akan ditradingkan jika harga Close 1H > EMA 50 1H.
        if "LINK" in symbol.upper():
            if df_htf is not None and not df_htf.empty:
                htf_last_row = df_htf.iloc[-1]
                if 'ema' in htf_last_row and not pd.isna(htf_last_row['ema']):
                    if htf_last_row['close'] <= htf_last_row['ema']:
                        logger.debug(f"[{symbol} LTF] Sinyal LINK dibatalkan: Harga Close HTF ({htf_last_row['close']:.4f}) <= EMA50 HTF ({htf_last_row['ema']:.4f}).")
                        return None
"""

# Replace the generic trend check with the specific LINK rule to be cleaner, or just add it below.
# Looking at the code:
#             # FILTER TREN MANDIRI ASET PADA HTF
#             # Mencegah entry pada saat kondisi tren HTF koin itu sendiri sudah hancur (di bawah EMA 50)
#             if df_htf is not None and not df_htf.empty:
#                 htf_last_row = df_htf.iloc[-1]
#                 if 'ema' in htf_last_row and not pd.isna(htf_last_row['ema']):
#                     if htf_last_row['close'] < htf_last_row['ema']:

# Oh wait, the existing code ALREADY blocks ALL assets if Close HTF < EMA50 HTF!
# Wait, let me check that closely.
