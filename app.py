import streamlit as st
import yfinance as yf
import pandas as pd
import os
import time
import sys
import io
import sqlite3
import requests
import re
import logging

logging.getLogger('yfinance').setLevel(logging.CRITICAL)  # tysta 'No data found'-meddelanden

st.set_page_config(page_title="Internationell Börs-Screener", layout="wide")

st.markdown("""
    <style>
        .block-container {
            padding-top: 1rem;
            padding-bottom: 1rem;
            padding-left: 2rem;
            padding-right: 2rem;
        }
        div.stButton > button {
            margin-top: 0px;
        }
        section[data-testid="stSidebar"] .block-container {
            padding-top: 1.5rem;
            gap: 0.5rem;
        }
        section[data-testid="stSidebar"] div[data-testid="stVerticalBlock"] {
            gap: 0.4rem;
        }
    </style>
""", unsafe_allow_html=True)

st.title("Internationell Börs-Screener & Trendanalys")

EXCEL_FILE = "Borsdata_2026-10-19 alla tickers pa borsdata.xlsx"
DB_FILE = "stock_database.db"


# --- HJÄLPFUNKTION FÖR STRÄNGTVÄTT (NORMERING) ---
def normalize_str(s):
    """Tar bort alla specialtecken, mellanslag och gör gemener för exakt matchning."""
    if not s or pd.isna(s):
        return ""
    return re.sub(r'[^a-zA-Z0-9]', '', str(s)).lower()


# --- DATABASFUNKTIONER ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS stock_prices (
            ticker TEXT,
            date TEXT,
            Open REAL,
            High REAL,
            Low REAL,
            Close REAL,
            Volume INTEGER,
            PRIMARY KEY (ticker, date)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS stock_isin (
            ticker TEXT PRIMARY KEY,
            isin TEXT
        )
    ''')
    conn.commit()
    conn.close()


def load_isin_cache():
    init_db()
    conn = sqlite3.connect(DB_FILE)
    df_isin = pd.read_sql("SELECT ticker, isin FROM stock_isin", conn)
    conn.close()
    if not df_isin.empty:
        return dict(zip(df_isin['ticker'], df_isin['isin']))
    return {}


def save_isin_cache_batch(isin_dict):
    if not isin_dict:
        return
    conn = sqlite3.connect(DB_FILE)
    data = [(k, v) for k, v in isin_dict.items() if v]
    conn.executemany("INSERT OR REPLACE INTO stock_isin (ticker, isin) VALUES (?, ?)", data)
    conn.commit()
    conn.close()


def load_history_from_db():
    init_db()
    conn = sqlite3.connect(DB_FILE)
    query = "SELECT ticker, date, Open, High, Low, Close, Volume FROM stock_prices"
    df_all = pd.read_sql(query, conn)
    conn.close()

    history_dict = {}
    if not df_all.empty:
        df_all['date'] = pd.to_datetime(df_all['date'])
        for ticker, group in df_all.groupby('ticker'):
            df_t = group.set_index('date').sort_index()
            history_dict[ticker] = df_t[['Open', 'High', 'Low', 'Close', 'Volume']]
    return history_dict


def save_history_to_db(ticker, df):
    conn = sqlite3.connect(DB_FILE)
    df_to_save = df.reset_index()
    date_col = df_to_save.columns[0]
    df_to_save = df_to_save.rename(columns={date_col: 'date'})
    df_to_save['date'] = pd.to_datetime(df_to_save['date']).dt.strftime('%Y-%m-%d')
    df_to_save['ticker'] = ticker

    cols = ['ticker', 'date', 'Open', 'High', 'Low', 'Close', 'Volume']
    for c in cols:
        if c not in df_to_save.columns:
            df_to_save[c] = None

    df_subset = df_to_save[cols].dropna(subset=['date', 'Close'])
    data_tuples = [tuple(x) for x in df_subset.to_numpy()]

    conn.executemany('''
        INSERT OR REPLACE INTO stock_prices (ticker, date, Open, High, Low, Close, Volume)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', data_tuples)
    conn.commit()
    conn.close()


# --- AVANZA API FÖR SVENSKA BOLAG OCH ORDERBOKS-ID ---
@st.cache_data(ttl=600, show_spinner="Hämtar Avanza-metadata...")
def get_avanza_stocks_metadata():
    url_filter = 'https://www.avanza.se/_api/market-stock-filter/stocks'
    headers = {
        'Content-Type': 'application/json',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'Referer': 'https://www.avanza.se/aktier/handla.html/screener',
        'X-SecurityToken': '-'
    }
    avanza_data = {}
    log = []

    def add_stock(s):
        orderbook_id = str(s.get("id", "") or s.get("orderbookId", ""))
        ticker = str(s.get("ticker", "")).strip()
        name = str(s.get("name", "")).strip()
        isin = str(s.get("isin", "")).strip()
        yahoo_t = str(s.get("yahooTicker", "") or "").strip()
        if not orderbook_id:
            return
        item = {
            "orderbookId": orderbook_id,
            "ticker": ticker,
            "yahooTicker": yahoo_t,
            "isin": isin,
            "name": name
        }
        if ticker:
            avanza_data[normalize_str(ticker)] = item
        if name:
            avanza_data[normalize_str(name)] = item
        if isin:
            avanza_data[isin] = item

    def fetch_pages(market_places, label, max_pages=60):
        offset, limit, antal, pages = 0, 100, 0, 0
        while pages < max_pages:
            payload = {
                "filter": {"sectors": [], "marketPlaces": market_places},
                "offset": offset,
                "limit": limit,
                "sortBy": {"order": "desc", "field": "numberOfOwners"}
            }
            try:
                resp = requests.post(url_filter, json=payload, headers=headers, timeout=15)
            except Exception as e:
                log.append(f"{label}: NÄTVERKSFEL {e}")
                break
            if resp.status_code != 200:
                log.append(f"{label}: HTTP {resp.status_code} – {resp.text[:150]}")
                break
            data = resp.json()
            stocks = data.get("stocks", [])
            if not stocks:
                break
            for s in stocks:
                add_stock(s)
            antal += len(stocks)
            pages += 1
            offset += limit
            if offset >= data.get("totalNumberOfStocks", 0):
                break
        log.append(f"{label}: {antal} aktier")
        return antal

    any_zero = False
    for m_code in ["se", "firstnorthse", "spotlightse", "ngm"]:
        if fetch_pages([m_code], m_code) == 0:
            any_zero = True

    if any_zero:
        fetch_pages([], "UTAN FILTER (reserv)")

    return avanza_data, log


def _parse_avanza_ts(ts):
    if ts is None:
        return pd.NaT
    if isinstance(ts, (int, float)):
        return (pd.to_datetime(ts, unit="ms", utc=True, errors="coerce")
                .tz_convert("Europe/Stockholm").tz_localize(None))
    return pd.to_datetime(ts, errors="coerce")


def fetch_avanza_history(orderbook_id):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'Referer': 'https://www.avanza.se/',
        'X-SecurityToken': '-'
    }
    urls = [
        f"https://www.avanza.se/_api/price-chart/stock/{orderbook_id}?timePeriod=three_months&resolution=day",
        f"https://www.avanza.se/_api/price-chart/stock/{orderbook_id}?timePeriod=one_month&resolution=day",
        f"https://www.avanza.se/_api/historic/chart/orderbook/{orderbook_id}?timePeriod=THREE_MONTHS",
    ]
    for url in urls:
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code != 200:
                continue
            js = resp.json()
        except Exception:
            continue

        points = js.get("ohlc") or js.get("chartPoints") or js.get("prices") or []
        rows = []
        for p in points:
            if isinstance(p, dict):
                dt = _parse_avanza_ts(p.get("timestamp") or p.get("date") or p.get("timeInMillis"))
                close_val = p.get("close") if p.get("close") is not None else p.get("last")
                open_val = p.get("open", close_val)
                high_val = p.get("high", close_val)
                low_val = p.get("low", close_val)
                vol_val = p.get("totalVolumeTraded") or p.get("volume") or 0
            elif isinstance(p, (list, tuple)) and len(p) >= 2:
                dt = _parse_avanza_ts(p[0])
                close_val = p[1]
                open_val = high_val = low_val = close_val
                vol_val = 0
            else:
                continue

            if pd.isna(dt) or close_val is None or float(close_val) <= 0:
                continue
            rows.append({
                "date": dt.normalize(),
                "Open": float(open_val if open_val is not None else close_val),
                "High": float(high_val if high_val is not None else close_val),
                "Low": float(low_val if low_val is not None else close_val),
                "Close": float(close_val),
                "Volume": int(vol_val) if vol_val else 0
            })

        if rows:
            df_av = pd.DataFrame(rows).drop_duplicates(subset=['date'], keep='last')
            return df_av.set_index('date').sort_index()
    return pd.DataFrame()


def find_avanza_id_by_search(isin, name):
    headers = {
        'Content-Type': 'application/json',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'Referer': 'https://www.avanza.se/',
        'X-SecurityToken': '-'
    }
    url = "https://www.avanza.se/_api/search/filtered-search"
    for query in [q for q in (isin, name) if q]:
        try:
            payload = {"query": query,
                       "searchFilter": {"types": ["STOCK"]},
                       "pagination": {"from": 0, "size": 5}}
            resp = requests.post(url, json=payload, headers=headers, timeout=10)
            if resp.status_code != 200:
                continue
            js = resp.json()
            hits = js.get("hits") or js.get("searchResults") or js.get("results") or []
            for h in hits:
                if not isinstance(h, dict):
                    continue
                oid = h.get("orderbookId") or h.get("orderBookId") or h.get("id")
                if oid:
                    return str(oid)
        except Exception:
            continue
    return None


def yahoo_history_by_isin(isin):
    try:
        quotes = yf.Search(isin, max_results=5).quotes
    except Exception:
        return pd.DataFrame()
    for q in quotes or []:
        sym = q.get("symbol", "")
        if not sym.endswith(".ST"):
            continue
        try:
            df = yf.Ticker(sym).history(period="40d")
            if df is not None and not df.empty:
                return df
        except Exception:
            continue
    return pd.DataFrame()


# --- INLÄSNING FRÅN EXCEL OCH CACHE ---
def load_data_from_excel(filepath):
    if not os.path.exists(filepath):
        st.error(f"Kunde inte hitta Excel-filen '{filepath}' i mappen!")
        return []

    df_excel = pd.read_excel(filepath, sheet_name="Export")
    isin_cache = load_isin_cache()

    avanza_meta = {}
    try:
        avanza_meta, avanza_log = get_avanza_stocks_metadata()
        st.session_state["avanza_log"] = avanza_log
    except Exception as e:
        st.session_state["avanza_log"] = [f"Fel vid hämtning: {e}"]

    stock_list = []
    isin_batch = {}

    for _, r in df_excel.iterrows():
        ticker_raw = str(r.get("Info - Ticker", "")).strip()
        yahoo_ticker = str(r.get("Info - Yahoo", "")).strip()
        country = str(r.get("Info - Land", "")).strip()
        currency = str(r.get("Info - Kursvaluta", "")).strip()
        name = str(r.get("Bolagsnamn", "")).strip()
        branch = str(r.get("Info - Bransch", "")).strip()
        sector = str(r.get("Info - Sektor", "")).strip()
        lista = str(r.get("Info - Lista", "")).strip()
        isin = str(r.get("Info - ISIN", "")).strip()

        c_prefix = country.lower()

        if isin == "nan" or not isin:
            isin = isin_cache.get(f"{c_prefix}_{ticker_raw}",
                   isin_cache.get(f"{c_prefix}_{normalize_str(ticker_raw)}",
                   isin_cache.get(f"{c_prefix}_{normalize_str(name)}",
                   isin_cache.get(ticker_raw,
                   isin_cache.get(normalize_str(ticker_raw),
                   isin_cache.get(normalize_str(name), ""))))))

        if not ticker_raw or ticker_raw == "nan":
            continue

        if yahoo_ticker and yahoo_ticker != "nan":
            y_t = yahoo_ticker.replace(" ", "-")
            if "sverige" in c_prefix and "." not in y_t:
                y_t += ".ST"
        elif "sverige" in c_prefix:
            y_t = ticker_raw.replace(" ", "-") + ".ST"
        else:
            y_t = ticker_raw

        avanza_item = None
        if "sverige" in c_prefix:
            avanza_item = (
                (avanza_meta.get(isin) if isin and isin != "nan" else None)
                or avanza_meta.get(normalize_str(ticker_raw))
                or avanza_meta.get(normalize_str(name))
                or avanza_meta.get(normalize_str(y_t.replace(".ST", "")))
            )

        if isin and isin != "nan":
            isin_batch[f"{c_prefix}_{ticker_raw}"] = isin
            isin_batch[f"{c_prefix}_{normalize_str(ticker_raw)}"] = isin
            isin_batch[f"{c_prefix}_{normalize_str(name)}"] = isin

        stock_list.append({
            "Yahoo_Ticker": y_t,
            "Original_Ticker": ticker_raw,
            "Avanza_Id": avanza_item.get("orderbookId") if avanza_item else None,
            "ISIN": isin if isin != "nan" else "",
            "Bolagsnamn": name,
            "Land": country if country else "Okänt",
            "Lista": lista if lista != "nan" else "Okänd lista",
            "Kursvaluta": currency,
            "Bransch": branch if branch != "nan" else "Okänd bransch",
            "Sektor": sector if sector != "nan" else "Okänd sektor"
        })

    save_isin_cache_batch(isin_batch)
    return stock_list


all_stocks = load_data_from_excel(EXCEL_FILE)

if all_stocks:
    history_dict = load_history_from_db()

    # --- LANDVAL OCH UPPDATERING I SIDOFÄLTET ---
    available_countries = sorted(list(set([s["Land"] for s in all_stocks if s["Land"] != "nan"])))

    with st.sidebar:
        st.markdown("### 🌍 Välj Land")
        selected_country = st.selectbox("Land att analysera:", available_countries, label_visibility="collapsed")

        st.markdown("---")
        st.markdown("### ⚙️ Databashantering")

        update_prices_btn = st.button("🔄 Uppdatera Priser & Hämta ISIN", use_container_width=True)

        if update_prices_btn:
            filtered_stocks_for_update = [s for s in all_stocks if s["Land"] == selected_country]
            progress_bar = st.progress(0)
            status_text = st.empty()

            batch_size = 40
            total_batches = (len(filtered_stocks_for_update) + batch_size - 1) // batch_size

            def silent_download(tickers):
                old_stderr = sys.stderr
                sys.stderr = io.StringIO()
                try:
                    data = yf.download(tickers, period="40d", group_by='ticker', threads=True, progress=False)
                except Exception:
                    data = pd.DataFrame()
                finally:
                    sys.stderr = old_stderr
                return data

            stats = {"avanza_tillagg": 0, "avanza_tomt": 0, "yahoo": 0, "avanza": 0, "avanza_sok": 0, "yahoo_isin": 0, "index_utan_data": 0, "misslyckade": []}

            def is_fresh(df):
                if df is None or df.empty or "Close" not in df.columns:
                    return False
                last = pd.to_datetime(df.index.max())
                if last.tzinfo is not None:
                    last = last.tz_localize(None)
                return last >= pd.Timestamp.today().normalize() - pd.Timedelta(days=5)

            isin_updates = {}
            for idx, i in enumerate(range(0, len(filtered_stocks_for_update), batch_size)):
                batch_stocks = filtered_stocks_for_update[i:i+batch_size]
                batch_tickers = [s["Yahoo_Ticker"] for s in batch_stocks]

                status_text.text(f"Uppdaterar priser (batch {idx+1} av {total_batches})...")
                data = silent_download(batch_tickers)

                for info in batch_stocks:
                    y_ticker = info["Yahoo_Ticker"]
                    orig_ticker = info["Original_Ticker"]
                    c_prefix = info["Land"].lower()
                    avanza_id = info.get("Avanza_Id")

                    df_new = pd.DataFrame()
                    success = False

                    try:
                        if len(batch_tickers) > 1:
                            if not data.empty and y_ticker in data:
                                df_new = data[y_ticker].dropna(how='all')
                        else:
                            if not data.empty:
                                df_new = data.dropna(how='all')
                                if isinstance(df_new.columns, pd.MultiIndex):
                                    if y_ticker in df_new.columns.get_level_values(0):
                                        df_new = df_new[y_ticker]
                                    else:
                                        df_new = df_new.droplevel(1, axis=1)
                        if is_fresh(df_new):
                            success = True
                            stats["yahoo"] += 1
                    except Exception:
                        pass

                    if success and avanza_id and "sverige" in c_prefix:
                        try:
                            df_av = fetch_avanza_history(avanza_id)
                            if df_av.empty:
                                stats["avanza_tomt"] += 1
                            else:
                                y_last = pd.to_datetime(df_new.index.max())
                                if y_last.tzinfo is not None:
                                    y_last = y_last.tz_localize(None)
                                extra = df_av[df_av.index > y_last.normalize()]
                                if not extra.empty:
                                    df_base = df_new.copy()
                                    if getattr(df_base.index, "tz", None) is not None:
                                        df_base.index = df_base.index.tz_localize(None)
                                    df_new = pd.concat([df_base, extra[["Open", "High", "Low", "Close", "Volume"]]])
                                    stats["avanza_tillagg"] += 1
                        except Exception:
                            pass

                    if not success and avanza_id and "sverige" in c_prefix:
                        try:
                            df_av = fetch_avanza_history(avanza_id)
                            if is_fresh(df_av):
                                df_new = df_av
                                success = True
                                stats["avanza"] += 1
                        except Exception:
                            pass

                    if not success and not avanza_id and "sverige" in c_prefix and info["Lista"] != "Index":
                        found_id = find_avanza_id_by_search(info["ISIN"], info["Bolagsnamn"])
                        if found_id:
                            avanza_id = found_id
                            df_av = fetch_avanza_history(found_id)
                            if is_fresh(df_av):
                                df_new = df_av
                                success = True
                                stats["avanza_sok"] += 1

                    if not success and info["ISIN"] and info["Lista"] != "Index":
                        df_y = yahoo_history_by_isin(info["ISIN"])
                        if is_fresh(df_y):
                            df_new = df_y
                            success = True
                            stats["yahoo_isin"] += 1

                    if not success and info["Lista"] == "Index":
                        stats["index_utan_data"] += 1
                    elif not success:
                        stats["misslyckade"].append(
                            f"{info['Bolagsnamn']} | {info['Lista']} | {y_ticker} | Avanza-ID: {avanza_id}"
                        )

                    if success:
                        if getattr(df_new.index, "tz", None) is not None:
                            df_new = df_new.copy()
                            df_new.index = df_new.index.tz_localize(None)

                        if y_ticker in history_dict:
                            df_old = history_dict[y_ticker]
                            df_combined = pd.concat([df_old, df_new])
                            df_combined = df_combined[~df_combined.index.duplicated(keep='last')]
                            df_combined.sort_index(inplace=True)
                            history_dict[y_ticker] = df_combined
                            save_history_to_db(y_ticker, df_combined)
                        else:
                            history_dict[y_ticker] = df_new
                            save_history_to_db(y_ticker, df_new)

                        if not info["ISIN"]:
                            try:
                                t_obj = yf.Ticker(y_ticker)
                                found_isin = t_obj.info.get("isin", "")
                                if found_isin:
                                    isin_updates[f"{c_prefix}_{orig_ticker}"] = found_isin
                                    isin_updates[f"{c_prefix}_{normalize_str(orig_ticker)}"] = found_isin
                                    isin_updates[f"{c_prefix}_{normalize_str(info['Bolagsnamn'])}"] = found_isin
                            except Exception:
                                pass

                time.sleep(0.3)
                progress_bar.progress((idx + 1) / total_batches)

            if isin_updates:
                save_isin_cache_batch(isin_updates)

            st.session_state["update_report"] = stats
            status_text.text("")
            st.cache_data.clear()
            st.success("Pris- och ISIN-uppdateringen klar!")
            st.rerun()

    target_country_stocks = [s for s in all_stocks if s["Land"] == selected_country]
    cached_count = sum(1 for s in target_country_stocks if s["Yahoo_Ticker"] in history_dict)
    isin_count = sum(1 for s in target_country_stocks if s.get("ISIN"))

    st.info(f"📍 **{selected_country}**: Hittade {len(target_country_stocks)} aktier i Börsdata-filen, varav **{cached_count}** har sparad prisdata och **{isin_count}** har ISIN.")

    with st.expander("🔎 Diagnostik (Avanza / uppdatering)"):
        st.write("**Avanza-metadata:**")
        for line in st.session_state.get("avanza_log", ["Ingen logg än"]):
            st.text(line)

        if "sverige" in selected_country.lower():
            df_cov = pd.DataFrame(target_country_stocks)
            df_cov["Har_Avanza_Id"] = df_cov["Avanza_Id"].notna()
            df_cov["Har_prisdata"] = df_cov["Yahoo_Ticker"].isin(history_dict.keys())
            st.write("**Täckning per lista:**")
            st.dataframe(df_cov.groupby("Lista")[["Har_Avanza_Id", "Har_prisdata"]].mean().round(2))

        rep = st.session_state.get("update_report")
        if rep:
            st.write(f"Senaste uppdatering: Yahoo {rep['yahoo']} st, Avanza {rep['avanza']} st, "
                     f"Avanza la till nyare dagar på Yahoo-data för {rep.get('avanza_tillagg', 0)} st "
                     f"(Avanza gav tom historik för {rep.get('avanza_tomt', 0)} st), "
                     f"Avanza-sök {rep.get('avanza_sok', 0)} st, Yahoo-ISIN-sök {rep.get('yahoo_isin', 0)} st, "
                     f"index utan data {rep.get('index_utan_data', 0)} st, "
                     f"misslyckades {len(rep['misslyckade'])} st")
            if rep["misslyckade"]:
                st.dataframe(pd.DataFrame({"Misslyckade": rep["misslyckade"]}))

    if history_dict:
        @st.cache_data(show_spinner=f"Analyserar data för {selected_country}...")
        def run_analysis(stocks_meta):
            analysis_rows = []

            date_counts = {}
            for info in stocks_meta:
                t = info["Yahoo_Ticker"]
                if t in history_dict:
                    h = history_dict[t]
                    h = h[(h.index <= pd.Timestamp.today()) & (h["Close"] > 0)]
                    for d in h.index[-30:]:
                        key = d.strftime('%Y-%m-%d')
                        date_counts[key] = date_counts.get(key, 0) + 1
            if date_counts:
                threshold = 0.3 * max(date_counts.values())
                table_dates = sorted(d for d, c in date_counts.items() if c >= threshold)[-5:]
            else:
                table_dates = []

            for info in stocks_meta:
                active_ticker = info["Yahoo_Ticker"]

                if active_ticker in history_dict:
                    df = history_dict[active_ticker]
                    df = df[(df.index <= pd.Timestamp.today()) & (df['Close'] > 0)].sort_index()

                    if len(df) >= 21:
                        df_21 = df.tail(21).copy()
                        closes_21 = df_21['Close'].tolist()
                        volumes_21 = df_21['Volume'].tolist()

                        price_diff = [closes_21[i] - closes_21[i-1] for i in range(1, 21)]
                        vol_diff = [volumes_21[i] - volumes_21[i-1] for i in range(1, 21)]

                        pos_days = 0
                        for p in reversed(price_diff):
                            if p > 0: pos_days += 1
                            else: break

                        neg_days = 0
                        for p in reversed(price_diff):
                            if p < 0: neg_days += 1
                            else: break

                        inc_vol = 0
                        for v in reversed(vol_diff):
                            if v > 0: inc_vol += 1
                            else: break

                        dec_vol = 0
                        for v in reversed(vol_diff):
                            if v < 0: dec_vol += 1
                            else: break

                        vol_up_price_up = 0
                        for v, p in reversed(list(zip(vol_diff, price_diff))):
                            if v > 0 and p > 0: vol_up_price_up += 1
                            else: break

                        vol_up_price_down = 0
                        for v, p in reversed(list(zip(vol_diff, price_diff))):
                            if v > 0 and p < 0: vol_up_price_down += 1
                            else: break

                        start_price = closes_21[1]
                        end_price = closes_21[-1]
                        pct_change = ((end_price - start_price) / start_price) * 100 if start_price else 0.0

                        prev_price = closes_21[-2]
                        last_price = closes_21[-1]
                        latest_day_pct = ((last_price - prev_price) / prev_price) * 100 if prev_price else 0.0
                        last_vol = int(volumes_21[-1])
                        last_turnover = int(last_price * last_vol)

                        df_tail20 = df_21.tail(20)
                        all_dates = [idx.strftime('%Y-%m-%d') for idx in df_tail20.index]
                        latest_date = all_dates[-1]

                        hl_pct_series = ((df_tail20['High'] - df_tail20['Low']) / df_tail20['Close']) * 100
                        avg_hl_pct = hl_pct_series.mean()

                        c_all = {idx.strftime('%Y-%m-%d'): round(val, 2) for idx, val in zip(df_tail20.index, df_tail20['Close'])}
                        v_all = {idx.strftime('%Y-%m-%d'): int(val) for idx, val in zip(df_tail20.index, df_tail20['Volume'])}
                        hl_all = {idx.strftime('%Y-%m-%d'): round(row['High'] - row['Low'], 2) for idx, row in df_tail20.iterrows()}
                        c_dict_5, v_dict_5, hl_dict_5 = c_all, v_all, hl_all

                        row_data = {
                            "Bolag": info["Bolagsnamn"],
                            "Ticker": info["Original_Ticker"],
                            "ISIN": info["ISIN"],
                            "Land": info["Land"],
                            "Lista": info["Lista"],
                            "Senaste Datum": latest_date,
                            "%-förändring (20d)": round(pct_change, 2),
                            "Snitt Intradag High-Low %": round(avg_hl_pct, 2),
                            "Senaste Dag %": round(latest_day_pct, 2),
                            "Senaste Volym": last_vol,
                            "Senaste Omsättning": last_turnover,
                            "Sektor": info["Sektor"],
                            "Bransch": info["Bransch"],
                            "Valuta": info["Kursvaluta"],
                            "Pos. Dagar": pos_days,
                            "Neg. Dagar": neg_days,
                            "Stigande Volym": inc_vol,
                            "Avtagande Volym": dec_vol,
                            "Volym Upp + Pris Upp": vol_up_price_up,
                            "Volym Upp + Pris Ned": vol_up_price_down,
                            "_price_diff": price_diff,
                            "_full_volumes": volumes_21
                        }

                        for d in table_dates:
                            row_data[f"{d} Pris"] = c_dict_5.get(d, None)
                        for d in table_dates:
                            row_data[f"{d} Volym"] = v_dict_5.get(d, None)
                        for d in table_dates:
                            row_data[f"{d} High-Low"] = hl_dict_5.get(d, None)

                        analysis_rows.append(row_data)
            return pd.DataFrame(analysis_rows)

        df_res = run_analysis(target_country_stocks)

        if not df_res.empty:
            st.sidebar.markdown("### 🎛️ Screener-filter")

            all_sectors = sorted(df_res["Sektor"].dropna().unique().tolist())
            with st.sidebar.expander("🏢 Sektor", expanded=False):
                selected_sectors = st.multiselect("Välj sektor:", all_sectors, default=all_sectors, label_visibility="collapsed")

            sub_df_sector = df_res[df_res["Sektor"].isin(selected_sectors)]

            all_branches = sorted(sub_df_sector["Bransch"].dropna().unique().tolist())
            with st.sidebar.expander("🏭 Bransch", expanded=False):
                selected_branches = st.multiselect("Välj bransch:", all_branches, default=all_branches, label_visibility="collapsed")

            with st.sidebar.expander("⚡ Intraday-rörelse (High-Low)", expanded=False):
                use_hl_filter = st.checkbox("Aktivera High-Low filter")
                min_avg_hl_pct = st.slider("Minst genomsnittlig High-Low (%)", 0.0, 20.0, 0.0, step=0.5)

            with st.sidebar.expander("📊 Volymfilter", expanded=False):
                use_vol_filter = st.checkbox("Aktivera volymfilter")
                vol_sma_days = st.slider("Jämför mot snitt antal dagar:", 1, 20, 4)
                min_vol_spike = st.slider("Minst volym vs snitt (%)", -100, 500, 0, step=10)

            with st.sidebar.expander("📉 Minsta Absoluta Omsättning", expanded=False):
                min_abs_to = st.number_input("Minsta absoluta omsättning", min_value=0, value=0, step=100000)

            with st.sidebar.expander("🔄 Mönsterigenkänning", expanded=False):
                pattern_choice = st.selectbox(
                    "Mönstertyp:",
                    ["Inget specifikt mönster", "Bottenvändning (Ner X dagar -> Upp Y dagar)", "Rekyl (Upp X dagar -> Ner Y dagar)"],
                    label_visibility="collapsed"
                )
                min_a_rev = 1
                min_b_rev = 0
                if pattern_choice.startswith("Bottenvändning"):
                    min_a_rev = st.slider("X (Dagar ner innan vändning):", 1, 5, 3)
                    min_b_rev = st.slider("Y (Dagar upp efter vändning):", 0, 4, 1)
                elif pattern_choice.startswith("Rekyl"):
                    min_a_rev = st.slider("X (Dagar upp före rekyl):", 1, 5, 3)
                    min_b_rev = st.slider("Y (Dagar ner i rekyl):", 0, 4, 1)

            with st.sidebar.expander("📈 Momentum-filter", expanded=False):
                min_pos = st.slider("Minst positiva dagar i rad", 0, 5, 0)
                min_neg = st.slider("Minst negativa dagar i rad", 0, 5, 0)
                min_inc_vol = st.slider("Minst dagar med stigande volym", 0, 5, 0)
                min_vol_p_up = st.slider("Minst dagar Volym Upp + Pris Upp", 0, 5, 0)

            def calc_vol_spike(row):
                vols = row["_full_volumes"]
                target_vols = vols[-(vol_sma_days + 1):-1]
                if not target_vols:
                    return 0.0
                avg_v = sum(target_vols) / len(target_vols)
                if avg_v <= 0:
                    return 0.0
                return ((row["Senaste Volym"] - avg_v) / avg_v) * 100

            def match_pattern(row):
                if pattern_choice == "Inget specifikt mönster":
                    return True

                p_diff = row["_price_diff"]
                x = min_a_rev
                y = min_b_rev

                if len(p_diff) < (x + y):
                    return False

                if pattern_choice.startswith("Bottenvändning"):
                    if y > 0:
                        recent_y = p_diff[-y:]
                        if not all(p > 0 for p in recent_y):
                            return False
                    prior_x = p_diff[-(x + y):-y] if y > 0 else p_diff[-x:]
                    if not all(p < 0 for p in prior_x):
                        return False
                    return True

                elif pattern_choice.startswith("Rekyl"):
                    if y > 0:
                        recent_y = p_diff[-y:]
                        if not all(p < 0 for p in recent_y):
                            return False
                    prior_x = p_diff[-(x + y):-y] if y > 0 else p_diff[-x:]
                    if not all(p > 0 for p in prior_x):
                        return False
                    return True

                return True

            df_res["Volym vs Snitt %"] = df_res.apply(calc_vol_spike, axis=1)

            filtered_df = df_res[
                (df_res["Sektor"].isin(selected_sectors)) &
                (df_res["Bransch"].isin(selected_branches)) &
                (df_res["Senaste Omsättning"] >= min_abs_to) &
                (df_res["Pos. Dagar"] >= min_pos) &
                (df_res["Neg. Dagar"] >= min_neg) &
                (df_res["Stigande Volym"] >= min_inc_vol) &
                (df_res["Volym Upp + Pris Upp"] >= min_vol_p_up)
            ]

            if use_hl_filter:
                filtered_df = filtered_df[filtered_df["Snitt Intradag High-Low %"] >= min_avg_hl_pct]

            if use_vol_filter:
                filtered_df = filtered_df[filtered_df["Volym vs Snitt %"] >= min_vol_spike]

            if pattern_choice != "Inget specifikt mönster":
                filtered_df = filtered_df[filtered_df.apply(match_pattern, axis=1)]

            display_df = filtered_df.drop(columns=["_full_volumes", "_price_diff"])
            display_df = display_df.rename(columns={"Volym vs Snitt %": f"Volym vs {vol_sma_days}d Snitt %"})

            # --- EXPORT FÖR INFRONT ---
            infront_export_df = pd.DataFrame({
                "ISIN": display_df["ISIN"],
                "Symbol": display_df["Ticker"],
                "Beskrivning": display_df["Bolag"]
            })

            excel_buffer = io.BytesIO()
            with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
                infront_export_df.to_excel(writer, index=False, sheet_name='Sheet1')
            excel_data = excel_buffer.getvalue()

            st.markdown(f"**Matchande bolag ({selected_country}):** {len(display_df)} st")
            st.download_button(
                label="📊 Ladda ner Infront Excel med ISIN (.xlsx)",
                data=excel_data,
                file_name=f"infront_isin_import_{selected_country.lower()}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True
            )

            # --- FLIKAR FÖR VYVAL ---
            tab_dashboard, tab1, tab2 = st.tabs(["🚀 Dashboard & Topplistor", "📊 Huvudvy & Screener", "📅 Senaste 5 dagarnas förändring"])

            with tab_dashboard:
                st.subheader("📊 Daglig Marknadsöversikt & Mönstertrigger")
                
                # Hjälpfunktion för att matcha specifika mönster baserat på prisdifferenser
                def check_exact_pattern(p_diff, pattern_type):
                    if len(p_diff) < 5:
                        return False
                    
                    if pattern_type == "ner_3_upp_2":
                        # Ner 3 dagar (index -5, -4, -3) följt av upp 2 dagar (index -2, -1)
                        return p_diff[-5] < 0 and p_diff[-4] < 0 and p_diff[-3] < 0 and p_diff[-2] > 0 and p_diff[-1] > 0
                    
                    elif pattern_type == "ner_4_upp_1":
                        # Ner 4 dagar följt av upp 1 dag
                        return p_diff[-5] < 0 and p_diff[-4] < 0 and p_diff[-3] < 0 and p_diff[-2] < 0 and p_diff[-1] > 0
                    
                    elif pattern_type == "upp_4_ner_1":
                        # Upp 4 dagar i rad sedan ner en dag
                        return p_diff[-5] > 0 and p_diff[-4] > 0 and p_diff[-3] > 0 and p_diff[-2] > 0 and p_diff[-1] < 0
                    
                    elif pattern_type == "upp_3_ner_2":
                        # Upp 3 dagar i rad sedan ner två dagar
                        return p_diff[-5] > 0 and p_diff[-4] > 0 and p_diff[-3] > 0 and p_diff[-2] < 0 and p_diff[-1] < 0
                    
                    elif pattern_type == "upp_5":
                        # Upp 5 dagar i sträck
                        return all(p > 0 for p in p_diff[-5:])
                    
                    elif pattern_type == "ner_5":
                        # Ner 5 dagar i sträck
                        return all(p < 0 for p in p_diff[-5:])
                        
                    return False

                # Skapa listor för respektive mönster
                m_ner3_upp2 = []
                m_ner4_upp1 = []
                m_upp4_ner1 = []
                m_upp3_ner2 = []
                m_upp5 = []
                m_ner5 = []

                for _, row in df_res.iterrows():
                    p_diff = row.get("_price_diff", [])
                    if check_exact_pattern(p_diff, "ner_3_upp_2"):
                        m_ner3_upp2.append(row)
                    if check_exact_pattern(p_diff, "ner_4_upp_1"):
                        m_ner4_upp1.append(row)
                    if check_exact_pattern(p_diff, "upp_4_ner_1"):
                        m_upp4_ner1.append(row)
                    if check_exact_pattern(p_diff, "upp_3_ner_2"):
                        m_upp3_ner2.append(row)
                    if check_exact_pattern(p_diff, "upp_5"):
                        m_upp5.append(row)
                    if check_exact_pattern(p_diff, "ner_5"):
                        m_ner5.append(row)

           # --- VISUALISERING I DASHBOARDEN ---
                
                # Valda kolumner att visa i tabellerna
                dashboard_cols = ["Bolag", "Sektor", "%-förändring (20d)", "Snitt Intradag High-Low %", "Senaste Dag %", "Senaste Omsättning"]

                # --- RAD 1: MOMENTUM & TOPPREKYLER (ÖVERST) ---
                st.markdown("### 📈 Momentum & Topprekyler")
                col1, col2, col3 = st.columns(3)

                with col1:
                    st.markdown(f"**Upp 4 dagar & Ner 1 dag** ({len(m_upp4_ner1)} st)")
                    if m_upp4_ner1:
                        st.dataframe(pd.DataFrame(m_upp4_ner1)[dashboard_cols], use_container_width=False, height=400)
                    else:
                        st.info("Inga träffar.")

                with col2:
                    st.markdown(f"**Upp 3 dagar & Ner 2 dagar** ({len(m_upp3_ner2)} st)")
                    if m_upp3_ner2:
                        st.dataframe(pd.DataFrame(m_upp3_ner2)[dashboard_cols], use_container_width=False, height=400)
                    else:
                        st.info("Inga träffar.")

                with col3:
                    st.markdown(f"**Upp 4 dagar i sträck** ({len(m_upp5)} st)")
                    if m_upp5:
                        st.dataframe(pd.DataFrame(m_upp5)[dashboard_cols], use_container_width=False, height=400)
                    else:
                        st.info("Inga träffar.")

                st.markdown("---")

                # --- RAD 2: BOTTENVÄNDNINGAR / REKYLER (UNDER) ---
                st.markdown("### 📉 Bottenvändningar / Rekyler")
                col4, col5, col6 = st.columns(3)

                with col4:
                    st.markdown(f"**Ner 3 dagar & Upp 2 dagar** ({len(m_ner3_upp2)} st)")
                    if m_ner3_upp2:
                        st.dataframe(pd.DataFrame(m_ner3_upp2)[dashboard_cols], use_container_width=False, height=400)
                    else:
                        st.info("Inga träffar.")

                with col5:
                    st.markdown(f"**Ner 4 dagar & Upp 1 dag** ({len(m_ner4_upp1)} st)")
                    if m_ner4_upp1:
                        st.dataframe(pd.DataFrame(m_ner4_upp1)[dashboard_cols], use_container_width=False, height=400)
                    else:
                        st.info("Inga träffar.")

                with col6:
                    st.markdown(f"**Ner 4 dagar i sträck** ({len(m_ner5)} st)")
                    if m_ner5:
                        st.dataframe(pd.DataFrame(m_ner5)[dashboard_cols], use_container_width=False, height=400)
                    else:
                        st.info("Inga träffar.")
        else:
            st.warning(f"Ingen historisk data tillgänglig för {selected_country}. Klicka på '🔄 Uppdatera Priser & Hämta ISIN' i sidofältet.")
    else:
        st.info("Ingen lokal databas hittad. Klicka på '🔄 Uppdatera Priser & Hämta ISIN' för att börja ladda ner!")
else:
    st.error(f"Kunde inte hitta filen '{EXCEL_FILE}' i mappen.")
