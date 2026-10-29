# update_data.py
import os
import sys
import io
import time
import sqlite3
import pandas as pd
import yfinance as yf
from app import load_data_from_excel, fetch_avanza_history, save_history_to_db, load_history_from_db, EXCEL_FILE, DB_FILE

EUROPEAN_COUNTRIES = [
    "Sverige", "Norge", "Danmark", "Finland", "Tyskland", 
    "Storbritannien", "Frankrike", "Italien", "Spanien", "Nederländerna", "Belgien"
]

def update_european_stocks():
    print("🚀 Startar automatisk uppdatering för europeiska länder...")
    all_stocks = load_data_from_excel(EXCEL_FILE)
    if not all_stocks:
        print("❌ Hittade inga aktier i Excel-filen.")
        return

    history_dict = load_history_from_db()
    
    # Filtrera ut europeiska länder
    target_stocks = [s for s in all_stocks if s.get("Land") in EUROPEAN_COUNTRIES]
    print(f"📊 Totalt {len(target_stocks)} aktier att uppdatera från Europa.")

    batch_size = 40
    for i in range(0, len(target_stocks), batch_size):
        batch = target_stocks[i:i+batch_size]
        tickers = [s["Yahoo_Ticker"] for s in batch]
        
        # Tyst nedladdning via yfinance
        try:
            data = yf.download(tickers, period="40d", group_by='ticker', threads=True, progress=False)
        except Exception as e:
            print(f"Fel vid hämtning av batch {i}: {e}")
            continue

        for info in batch:
            y_ticker = info["Yahoo_Ticker"]
            avanza_id = info.get("Avanza_Id")
            c_prefix = str(info.get("Land", "")).lower()

            df_new = pd.DataFrame()
            success = False

            if not data.empty:
                try:
                    if len(tickers) > 1 and y_ticker in data:
                        df_new = data[y_ticker].dropna(how='all')
                    elif len(tickers) == 1:
                        df_new = data.dropna(how='all')
                    if not df_new.empty and "Close" in df_new.columns:
                        success = True
                except Exception:
                    pass

            # Avanza-reserv för Sverige
            if "sverige" in c_prefix and avanza_id:
                try:
                    df_av = fetch_avanza_history(avanza_id)
                    if success and not df_av.empty:
                        y_last = pd.to_datetime(df_new.index.max()).tz_localize(None)
                        extra = df_av[df_av.index > y_last]
                        if not extra.empty:
                            df_new = pd.concat([df_new, extra[["Open", "High", "Low", "Close", "Volume"]]])
                    elif not success and not df_av.empty:
                        df_new = df_av
                        success = True
                except Exception:
                    pass

            if success and not df_new.empty:
                if getattr(df_new.index, "tz", None) is not None:
                    df_new.index = df_new.index.tz_localize(None)

                if y_ticker in history_dict:
                    df_combined = pd.concat([history_dict[y_ticker], df_new])
                    df_combined = df_combined[~df_combined.index.duplicated(keep='last')].sort_index()
                    save_history_to_db(y_ticker, df_combined)
                else:
                    save_history_to_db(y_ticker, df_new)

        time.sleep(0.3)

    print("✅ Uppdateringen av alla europeiska länder är klar!")

if __name__ == "__main__":
    update_european_stocks()
