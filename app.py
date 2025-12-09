import textwrap
import pandas as pd
import requests
import streamlit as st


# ---------------------------------------------------------
# CONFIG
# ---------------------------------------------------------

# Philadelphia Carto SQL API
CARTO_SQL_URL = "https://phl.carto.com/api/v2/sql"

# Main tables used by the City for OPA data
PROPERTIES_TABLE = "opa_properties_public_pde"   # property base data
ASSESSMENTS_TABLE = "assessments"                # assessment history


# ---------------------------------------------------------
# HELPER FUNCTIONS
# ---------------------------------------------------------

def clean_address(raw: str) -> str:
    """Normalize an address string a bit."""
    if not isinstance(raw, str):
        return ""
    return " ".join(raw.strip().upper().split())


def carto_sql(sql: str) -> pd.DataFrame:
    """Run a SQL query against the Carto API and return a DataFrame."""
    params = {"q": sql, "format": "json"}
    resp = requests.get(CARTO_SQL_URL, params=params, timeout=30)

    if resp.status_code != 200:
        st.error(f"Carto API error {resp.status_code}: {resp.text[:300]}")
        return pd.DataFrame()

    data = resp.json()
    rows = data.get("rows", [])
    return pd.DataFrame(rows)


def lookup_one_address(address: str, years: list[int]) -> pd.DataFrame:
    """
    Look up a single address in the OPA data for the selected tax years.

    This version assumes:
    - `opa_properties_public_pde` has a column called `full_address`
    - assessment history table has a column called `year`
    """

    addr = clean_address(address)
    if not addr:
        return pd.DataFrame()

    addr_sql = addr.replace("'", "''")
    years_list = ", ".join(str(y) for y in years)

    sql = f"""
        SELECT
            p.parcel_number,
            p.full_address,
            p.zip_code,
            a.year AS tax_year,
            a.market_value,
            a.exempt_land,
            a.exempt_improvement,
            a.market_value_date
        FROM {PROPERTIES_TABLE} AS p
        JOIN {ASSESSMENTS_TABLE} AS a
          ON p.parcel_number = a.parcel_number
        WHERE UPPER(p.full_address) = '{addr_sql}'
          AND a.year IN ({years_list})
        ORDER BY a.year
    """

    return carto_sql(sql)


def lookup_many_addresses(addresses: list[str], years: list[int]) -> pd.DataFrame:
    """Loop over many addresses and stack the results."""
    frames = []
    total = len(addresses)

    for i, addr in enumerate(addresses, start=1):
        if not addr.strip():
            continue

        with st.spinner(f"Looking up {addr} ({i}/{total})…"):
            df = lookup_one_address(addr, years)

            if df.empty:
                frames.append(
                    pd.DataFrame(
                        {
                            "input_address": [addr],
                            "parcel_number": [None],
                            "full_address": [None],
                            "zip_code": [None],
                            "tax_year": [None],
                            "market_value": [None],
                            "note": ["No match found"],
                        }
                    )
                )
            else:
                df["input_address"] = addr
                frames.append(df)

    if not frames:
        return pd.DataFrame()

    out = pd.concat(frames, ignore_index=True)
