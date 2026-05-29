import csv
import os
from pathlib import Path

import oracledb
import requests
from dotenv import load_dotenv

load_dotenv()

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
ETF_CSV_PATH = Path("nasdaq_etf.csv")


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


DB_USER = require_env("DB_USER")
DB_PASSWORD = require_env("DB_PASSWORD")
DB_DSN = require_env("DB_DSN")

HEADERS = {
    "User-Agent": "Tomcrest support@tomcrest.com",
    "Accept-Encoding": "gzip, deflate",
    "Host": "www.sec.gov",
}


def fetch_sec_tickers():
    resp = requests.get(
        SEC_TICKERS_URL,
        headers=HEADERS,
        timeout=30,
    )

    resp.raise_for_status()

    data = resp.json()

    rows = {}

    for _, item in data.items():
        symbol = item.get("ticker", "").strip().upper()
        title = item.get("title", "").strip()

        if not symbol:
            continue

        rows[symbol] = {
            "symbol": symbol[:16],
            "title": title[:255],
            "asset_type": "STOCK",
        }

    print(f"Fetched {len(rows)} SEC tickers.")

    return rows


def fetch_etf_csv():
    rows = {}

    with open(ETF_CSV_PATH, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)

        for row in reader:
            symbol = row.get("SYMBOL", "").strip().upper()
            name = row.get("NAME", "").strip()

            if not symbol:
                continue

            rows[symbol] = {
                "symbol": symbol[:16],
                "title": name[:255],
                "asset_type": "ETF",
            }

    print(f"Loaded {len(rows)} ETF rows from CSV.")

    return rows


def merge_sources():
    sec_rows = fetch_sec_tickers()
    etf_rows = fetch_etf_csv()

    sec_rows.update(etf_rows)

    merged = list(sec_rows.values())

    print(f"Total merged symbols: {len(merged)}")

    return merged


def load_tickers_to_oracle():
    rows = merge_sources()

    with oracledb.connect(
        user=DB_USER,
        password=DB_PASSWORD,
        dsn=DB_DSN,
    ) as conn:

        with conn.cursor() as cur:

            merge_sql = """
                MERGE INTO ticker_symbols t
                USING (
                    SELECT
                        :symbol AS symbol,
                        :title AS title,
                        :asset_type AS asset_type
                    FROM dual
                ) src
                ON (t.symbol = src.symbol)

                WHEN MATCHED THEN
                    UPDATE SET
                        t.title = src.title,
                        t.asset_type = src.asset_type

                WHEN NOT MATCHED THEN
                    INSERT (
                        symbol,
                        title,
                        asset_type
                    )
                    VALUES (
                        src.symbol,
                        src.title,
                        src.asset_type
                    )
            """

            batch_size = 1000

            for i in range(0, len(rows), batch_size):
                batch = rows[i : i + batch_size]

                cur.executemany(
                    merge_sql,
                    batch,
                )

                print(f"Inserted batch {i} - {i + len(batch)}")

            conn.commit()

    print(f"Synced {len(rows)} symbols into ticker_symbols.")


if __name__ == "__main__":
    load_tickers_to_oracle()
