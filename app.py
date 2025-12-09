import streamlit as st

# Show something on screen ASAP (so we never get a blank page)
st.set_page_config(page_title="Philadelphia Assessment Lookup", layout="wide")
st.title("Philadelphia Assessment Lookup")
st.write("Paste addresses or upload a CSV, then click **Run lookup**.")

import textwrap

# Try to load pandas and requests and show a clear error if they fail
try:
    import pandas as pd
    import requests
except Exception as e:
    st.error(
        "Problem loading required Python packages "
        "(pandas / requests). Please make sure `requirements.txt` "
        "contains:\n\n"
        "streamlit\npandas\nrequests\n\n"
        f"Technical error: {e}"
    )
    st.stop()


# ---------------------------------------------------------
# CONFIG
# ---------------------------------------------------------

CARTO_SQL_URL = "https://phl.carto.com/api/v2/sql"
PROPERTIES_TABLE = "opa_properties_public_pde"   # base property table
ASSESSMENTS_TABLE = "assessments"               # assessment history table


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

    Assumptions about the City tables:
    - `opa_properties_public_pde` has `full_address`, `parcel_number`, `zip_code`
    - `assessments` has `year`, `market_value`, `exempt_land`,
      `exempt_improvement`, `market_value_date`
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

    cols_order = [
        "input_address",
        "parcel_number",
        "full_address",
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
# UI LAYOUT
# ---------------------------------------------------------

st.markdown(
    """
Bulk lookup tool for **Philadelphia OPA** assessments.

**How to use:**

1. Paste addresses (one per line) OR upload a CSV with a column named `address`.
2. Choose the tax years you want (2025, 2026).
3. Click **Run lookup** to pull values from the City's open-data API.
"""
)

st.divider()

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
        "Paste addresses here",
        height=220,
        help="Paste straight from Excel or Google Sheets.",
        placeholder=sample_hint,
    )

    st.caption("OR upload a CSV with a column named `address`:")
    file = st.file_uploader("Upload CSV", type=["csv"], label_visibility="collapsed")

with col_right:
    st.subheader("2. Choose tax years")

    year_2025 = st.checkbox("2025", value=True)
    year_2026 = st.checkbox("2026", value=True)

    run_button = st.button("🔍 Run lookup", type="primary")


# ---------------------------------------------------------
# RUN LOOKUP
# ---------------------------------------------------------

if run_button:
    years = []
    if year_2025:
        years.append(2025)
    if year_2026:
        years.append(2026)

    if not years:
        st.warning("Select at least one tax year.")
        st.stop()

    addresses = []

    if text_addresses.strip():
        addresses.extend([line for line in text_addresses.splitlines() if line.strip()])

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

    seen = set()
    unique = []
    for a in addresses:
        ca = clean_address(a)
        if ca and ca not in seen:
            unique.append(a)
            seen.add(ca)

    if not unique:
        st.warning("Please enter at least one address.")
        st.stop()

    st.info(f"Looking up **{len(unique)}** unique addresses…")

    results = lookup_many_addresses(unique, years)

    if results.empty:
        st.warning("No results found. Check that the address format matches the City's data.")
        st.stop()

    st.success("Lookup complete!")

    st.subheader("Results")
    st.dataframe(results, use_container_width=True)

    csv_bytes = results.to_csv(index=False).encode("utf-8")
    st.download_button(
        "⬇️ Download results as CSV",
        data=csv_bytes,
        file_name="philly_assessments_lookup.csv",
        mime="text/csv",
    )
else:
    st.info("Paste addresses or upload a CSV, then click **Run lookup**.")
