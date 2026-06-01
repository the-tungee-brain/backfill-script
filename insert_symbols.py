import csv
import os
import re
from pathlib import Path

import oracledb
import requests
from dotenv import load_dotenv

load_dotenv()

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
ETF_CSV_PATH = Path("nasdaq_etf.csv")
STOCK_LOGO_URL_TEMPLATE = (
    "https://raw.githubusercontent.com/the-tungee-brain/stock-icons/main/ticker_icons/{symbol}.png"
)
UPPERCASE_TOKENS = {
    "adr",
    "ag",
    "apa",
    "etf",
    "eu",
    "lp",
    "llc",
    "nv",
    "plc",
    "reit",
    "sa",
    "se",
    "uk",
    "us",
}
BRAND_OVERRIDES = {
    "jpmorgan": "JPMorgan",
}
WORD_PARTS = re.compile(r"^([^\w]*)([\w&]+)([^\w]*)$")
SEC_TITLE_JUNK = re.compile(r"\s*/\w+/?")


def format_word(word: str) -> str:
    match = WORD_PARTS.match(word)
    if not match:
        return word

    prefix, core, suffix = match.groups()
    if not core:
        return word

    lower_core = core.lower()
    if core != core.upper() and core != core.lower():
        return word
    if lower_core in BRAND_OVERRIDES:
        formatted = BRAND_OVERRIDES[lower_core]
    elif lower_core in UPPERCASE_TOKENS:
        formatted = core.upper()
    else:
        formatted = lower_core.capitalize()

    return f"{prefix}{formatted}{suffix}"


def format_title(title: str) -> str:
    cleaned = SEC_TITLE_JUNK.sub("", title.strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return " ".join(format_word(word) for word in cleaned.split())


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value or not value.strip():
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value.strip()


def connect_db():
    try:
        return oracledb.connect(
            user=require_env("DB_USER"),
            password=require_env("DB_PASSWORD"),
            dsn=require_env("DB_DSN"),
        )
    except oracledb.OperationalError as exc:
        message = str(exc)
        if "12506" in message or "DPY-6000" in message:
            raise RuntimeError(
                "Oracle listener refused the connection (ORA-12506). "
                "Your local machine can reach the DB, but GitHub Actions runners "
                "use different public IPs. In OCI Console open your Autonomous "
                "Database → Network → Access Control List and allow access from "
                "all public IPs (0.0.0.0/0), then re-run the workflow."
            ) from exc
        raise

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
            "title": format_title(title)[:255],
            "asset_type": "STOCK",
            "logo_url": STOCK_LOGO_URL_TEMPLATE.format(symbol=symbol[:16]),
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
                "title": format_title(name)[:255],
                "asset_type": "ETF",
                "logo_url": None,
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

    with connect_db() as conn:

        with conn.cursor() as cur:

            merge_sql = """
                MERGE INTO ticker_symbols t
                USING (
                    SELECT
                        :symbol AS symbol,
                        :title AS title,
                        :asset_type AS asset_type,
                        :logo_url AS logo_url
                    FROM dual
                ) src
                ON (t.symbol = src.symbol)

                WHEN MATCHED THEN
                    UPDATE SET
                        t.title = src.title,
                        t.asset_type = src.asset_type,
                        t.logo_url = src.logo_url

                WHEN NOT MATCHED THEN
                    INSERT (
                        symbol,
                        title,
                        asset_type,
                        logo_url
                    )
                    VALUES (
                        src.symbol,
                        src.title,
                        src.asset_type,
                        src.logo_url
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
