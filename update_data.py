import os
import pandas as pd
from app import load_data_from_excel, fetch_avanza_history, save_history_to_db, load_history_from_db, EXCEL_FILE

def run_daily_update():
    print("Startar daglig uppdatering av aktiedata per land...")
    
    # Ladda in alla aktier från Excel
    all_stocks = load_data_from_excel(EXCEL_FILE)
    if not all_stocks:
        print("Inga aktier hittades i Excel-filen.")
        return

    # Hämta unika länder
    available_countries = sorted(list(set([s["Land"] for s in all_stocks if s["Land"] != "nan"])))

    for country in available_countries:
        print(f"Uppdaterar land: {country}...")
        history_dict = load_history_from_db(country)
        target_stocks = [s for s in all_stocks if s["Land"] == country]

        for info in target_stocks:
            y_ticker = info["Yahoo_Ticker"]
            avanza_id = info.get("Avanza_Id")

            # Hämta historik (exempel via Yahoo / Avanza beroende på din logik)
            # Här kan du anropa dina befintliga nedladdningsfunktioner för respektive ticker
            try:
                import yfinance as yf
                df_new = yf.download(y_ticker, period="5d", progress=False)
                if not df_new.empty:
                    if isinstance(df_new.columns, pd.MultiIndex):
                        df_new = df_new.droplevel(1, axis=1)
                    
                    if getattr(df_new.index, "tz", None) is not None:
                        df_new.index = df_new.index.tz_localize(None)

                    # Kombinera och spara till landets egna databas
                    if y_ticker in history_dict:
                        df_old = history_dict[y_ticker]
                        df_combined = pd.concat([df_old, df_new])
                        df_combined = df_combined[~df_combined.index.duplicated(keep='last')]
                        df_combined.sort_index(inplace=True)
                        save_history_to_db(country, y_ticker, df_combined)
                    else:
                        save_history_to_db(country, y_ticker, df_new)
            except Exception as e:
                print(f"Kunde inte uppdatera {y_ticker}: {e}")

    print("Daglig uppdatering klar!")

if __name__ == "__main__":
    run_daily_update()
