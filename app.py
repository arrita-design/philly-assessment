import textwrap
import urllib.parse

import pandas as pd
import requests
import streamlit as st

# ---------------------------------------------------------
# CONFIG
# ---------------------------------------------------------

# Philadelphia Carto SQL API
CARTO_SQL_URL = "https://phl.carto.com/api/v2/sql"

# Main tables used by the City for OPA data
PROPERTIES_TABLE = "opa_properties_public_pde"   # property characteristics
ASSESSMENTS_TABLE = "assessments"               # assessment history


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
    # Carto expects the query in the `q` parameter
    params = {"q": sql, "format": "json"}
    # We keep this super simple – one GET per query
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

    NOTE:
    - This assumes the properties table has a column called `address_std`.
    - If that column name is different, you’ll need to change it below.
    """
    addr = clean_address(address)
    if not addr:
        return pd.DataFrame()

    # Escape single quotes for SQL
    addr_sql = addr.replace("'", "''")
    years_list = ", ".join(str(y) for y in years)

    sql = f"""
        SELECT
            p.parcel_number,
            p.address_std,
            p.zip_code,
            a.tax_year,
            a.market_value,
            a.exempt_land,
            a.exempt_improvement,
            a.market_value_date
        FROM {PROPERTIES_TABLE} AS p
        JOIN {ASSESSMENTS_TABLE} AS a
          ON p.parcel_number = a.parcel_number
        WHERE UPPER(p.address_std) = '{addr_sql}'
          AND a.tax_year IN ({years_list})
        ORDER BY a.tax_year
    """

    return carto_sql(sql)


def lookup_many_addresses(addresses: list[str], years: list[int]) -> pd.DataFrame:
    """Loop over many addresses and stack the results."""
    frames = []
    for i, addr in enumerate(addresses, start=1):
        if not addr.strip():
            continue
        with st.spinner(f"Looking up {addr} ({i}/{len(addresses)})…"):
            df = lookup_one_address(addr, years)
            if df.empty:
                frames.append(
                    pd.DataFrame(
                        {
                            "input_address": [addr],
                            "parcel_number": [None],
                            "address_std": [None],
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

    # Reorder columns a bit
    cols_order = [
        "input_address",
        "parcel_number",
        "address_std",
        "zip_code",
        "tax_year",
        "market_value",
        "exempt_land",
        "exempt_improvement",
        "market_value_date",
        "note",
    ]
    for c in cols_order:
        if c not in out.columns:
            out[c] = None

    return out[cols_order]


# ---------------------------------------------------------
# STREAMLIT UI
# ---------------------------------------------------------

st.set_page_config(
    page_title="Philadelphia Assessment Lookup",
    layout="wide",
)

st.title("Philadelphia Assessment Lookup")

st.markdown(
    """
Bulk-lookup tool for **Philadelphia Office of Property Assessment** data  
(using the City’s official OpenDataPhilly / Carto API).

**What it does**

- Paste a list of Philadelphia mailing addresses, or upload a CSV.
- Choose tax years (e.g. 2025 and 2026).
- Get parcel numbers + market values in a downloadable table.

> ⚠️ This is an unofficial helper tool.  
> Data comes directly from the City’s open data services.
"""
)

st.divider()

# --- INPUT COLUMN (LEFT) & OPTIONS (RIGHT) ---
col_left, col_right = st.columns([2, 1])

with col_left:
    st.subheader("1. Enter addresses")

    sample_hint = textwrap.dedent(
        """\
        One address per line, for example:

        0373 Sloan Street
        0711 N. 40th Street
        3905 Aspen Street
        """
    )

    text_addresses = st.text_area(
        "Paste addresses here (one per line)",
        height=220,
        help="You can paste straight from Excel / Google Sheets.",
        placeholder=sample_hint,
    )

    st.caption("OR upload a CSV file with an `address` column:")

    file = st.file_uploader("Upload CSV", type=["csv"], label_visibility="collapsed")

with col_right:
    st.subheader("2. Choose tax years")

    year_2025 = st.checkbox("2025", value=True)
    year_2026 = st.checkbox("2026", value=True)

    if not (year_2025 or year_2026):
        st.warning("Select at least one tax year (2025 / 2026).")

    run_button = st.button("🔍 Run lookup", type="primary")

# ---------------------------------------------------------
# PROCESS INPUT
# ---------------------------------------------------------

if run_button:
    years = []
    if year_2025:
        years.append(2025)
    if year_2026:
        years.append(2026)

    if not years:
        st.stop()

    # Collect addresses from textarea
    addresses = []
    if text_addresses.strip():
        addresses.extend(
            [line for line in text_addresses.splitlines() if line.strip()]
        )

    # Collect addresses from CSV
    if file is not None:
        try:
            df_in = pd.read_csv(file)
            if "address" not in df_in.columns:
                st.error("Your CSV must have a column named **address**.")
                st.stop()
            csv_addresses = df_in["address"].dropna().astype(str).tolist()
            addresses.extend(csv_addresses)
        except Exception as e:
            st.error(f"Could not read CSV: {e}")
            st.stop()

    # Remove duplicates while preserving order
    seen = set()
    unique_addresses = []
    for a in addresses:
        ca = clean_address(a)
        if ca and ca not in seen:
            unique_addresses.append(a)
            seen.add(ca)

    if not unique_addresses:
        st.warning("Please enter at least one address.")
        st.stop()

    st.info(f"Looking up **{len(unique_addresses)}** unique addresses…")

    results = lookup_many_addresses(unique_addresses, years)

    if results.empty:
        st.warning("No results found. You may need to adjust the address format.")
        st.stop()

    st.success("Done! Preview below.")

    st.subheader("Results")
    st.dataframe(results, use_container_width=True)

    # Download as CSV
    csv_bytes = results.to_csv(index=False).encode("utf-8")
    st.download_button(
        "⬇️ Download results as CSV",
        data=csv_bytes,
        file_name="philly_assessments_lookup.csv",
        mime="text/csv",
    )

    st.caption(
        "Tip: Open the CSV in Excel / Google Sheets and join it back to your master list."
    )

else:
    st.info("Enter addresses and click **Run lookup** to get started.")


