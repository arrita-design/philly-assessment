import requests
import pandas as pd
import streamlit as st
from typing import List, Tuple, Dict

# CARTO SQL endpoint for Philly open data
CARTO_SQL_URL = "https://phl.carto.com/api/v2/sql"


# ---------- Helper functions ----------

def call_carto(sql: str) -> Dict:
    """Run a SQL query against the CARTO API."""
    resp = requests.get(CARTO_SQL_URL, params={"q": sql})
    if resp.status_code != 200:
        raise RuntimeError(f"Carto API error {resp.status_code}: {resp.text}")
    return resp.json()


def normalize_address_for_search(address: str) -> str:
    """
    Clean user address and turn it into a pattern we can use with ILIKE
    against p.location in opa_properties_public_pde.
    """
    if not address:
        return ""

    # Only use first part if they paste "780 Union Street, Philadelphia, PA"
    a = address.strip().split(",")[0]
    if not a:
        return ""

    a = a.upper()

    # Strip leading zeros from the house number
    parts = a.split()
    if parts and parts[0].isdigit():
        try:
            parts[0] = str(int(parts[0]))  # "0780" -> "780"
        except ValueError:
            pass
    a = " ".join(parts)

    suffix_map = {
        " STREET": " ST",
        " AVENUE": " AVE",
        " BOULEVARD": " BLVD",
        " ROAD": " RD",
        " DRIVE": " DR",
        " PLACE": " PL",
        " COURT": " CT",
        " LANE": " LN",
        " TERRACE": " TER",
    }
    for long_suffix, short_suffix in suffix_map.items():
        if a.endswith(long_suffix):
            a = a[: -len(long_suffix)] + short_suffix
            break

    a = a.replace("'", "''")
    return f"%{a}%"   # e.g. "%780 UNION ST%"


def find_parcel_for_address(address: str) -> Dict | None:
    """
    Step 1: Find a parcel in opa_properties_public_pde that matches this address.
    Returns a single row dict with parcel_number, location, and zip_code,
    or None if nothing matches.
    """
    pattern = normalize_address_for_search(address)
    if not pattern:
        return None

    sql = f"""
        SELECT
            parcel_number,
            location AS full_address,
            zip_code
        FROM opa_properties_public_pde
        WHERE location ILIKE '{pattern}'
        ORDER BY parcel_number
        LIMIT 1
    """

    data = call_carto(sql)
    rows = data.get("rows", [])
    return rows[0] if rows else None


def get_assessments_for_parcel(parcel_number: str, years: List[int]) -> List[Dict]:
    """
    Step 2: Given a parcel_number, pull assessment rows from the assessments table.

    IMPORTANT: we use SELECT * so we don't have to know the exact column names
    (e.g. whatever they call the value fields).
    """
    if not parcel_number:
        return []

    years_clause = ", ".join(str(y) for y in sorted(set(years)))

    sql = f"""
        SELECT *
        FROM assessments
        WHERE parcel_number = '{parcel_number}'
          AND year IN ({years_clause})
        ORDER BY year
    """

    data = call_carto(sql)
    return data.get("rows", [])


def lookup_single_address(address: str, years: List[int]) -> List[Dict]:
    """
    Full lookup for a single address:
    - find parcel in properties table
    - then fetch assessments for that parcel
    - return combined rows (one per year), or a single "note" row
    """
    # Step 1: try to get parcel information
    parcel_row = None
    try:
        parcel_row = find_parcel_for_address(address)
    except Exception as e:
        return [{
            "input_address": address,
            "note": f"Error looking up parcel: {e}",
        }]

    if not parcel_row:
        return [{
            "input_address": address,
            "note": "No parcel found for this address",
        }]

    parcel_number = parcel_row.get("parcel_number")
    full_address = parcel_row.get("full_address")
    zip_code = parcel_row.get("zip_code")

    # Step 2: get assessment rows for this parcel
    try:
        assessments = get_assessments_for_parcel(parcel_number, years)
    except Exception as e:
        return [{
            "input_address": address,
            "parcel_number": parcel_number,
            "full_address": full_address,
            "zip_code": zip_code,
            "note": f"Error looking up assessments: {e}",
        }]

    if not assessments:
        return [{
            "input_address": address,
            "parcel_number": parcel_number,
            "full_address": full_address,
            "zip_code": zip_code,
            "note": "Parcel found, but no assessment records for selected years",
        }]

    # Combine parcel info with each assessment row
    out_rows: List[Dict] = []
    for a in assessments:
        rec = dict(a)
        rec["input_address"] = address
        rec["parcel_number"] = parcel_number
        rec["full_address"] = full_address
        rec["zip_code"] = zip_code
        out_rows.append(rec)

    return out_rows


def build_results(addresses: List[str], years: List[int]) -> Tuple[pd.DataFrame, List[str]]:
    """
    Run the lookup for a list of addresses and return:
    - a DataFrame of results
    - a list of error messages (if any)
    """
    rows: List[Dict] = []
    errors: List[str] = []

    # Deduplicate while preserving order
    unique_addresses = [a for a in dict.fromkeys(a.strip() for a in addresses) if a.strip()]

    if not unique_addresses:
        return pd.DataFrame(), ["No addresses provided"]

    progress_text = f"Looking up {len(unique_addresses)} addresses…"
    progress = st.progress(0, text=progress_text)

    for idx, addr in enumerate(unique_addresses, start=1):
        try:
            addr_rows = lookup_single_address(addr, years)
            rows.extend(addr_rows)
        except Exception as e:
            errors.append(f"{addr}: {e}")
            rows.append({
                "input_address": addr,
                "note": f"Unexpected error: {e}",
            })

        progress.progress(idx / len(unique_addresses), text=progress_text)

    progress.empty()

    if not rows:
        return pd.DataFrame(), errors

    df = pd.DataFrame(rows)

    # Try to put the most important columns first if they exist
    preferred_order = [
        "input_address",
        "parcel_number",
        "full_address",
        "zip_code",
        "year",          # assessments table's year
        "note",
    ]
    # Keep existing columns but re-order with preferred ones at the front
    cols = list(df.columns)
    front = [c for c in preferred_order if c in cols]
    rest = [c for c in cols if c not in front]
    df = df[front + rest]

    return df, errors


# ---------- Streamlit UI ----------

st.set_page_config(
    page_title="Philadelphia Assessment Lookup",
    layout="wide",
)

st.title("Philadelphia Assessment Lookup")

st.write(
    "Paste a list of **Philadelphia property addresses** or upload a CSV with an "
    "`address` column to look up **assessment records for 2023–2026** in bulk.\n\n"
    "Because the City’s assessment schema changes over time, this tool pulls **all** "
    "columns from the assessment table for each parcel/year. Look for columns whose "
    "names contain words like `VALUE` or `ASSESSMENT` to see the dollar amounts."
)

# Address input
addr_text = st.text_area(
    "Paste addresses here",
    height=200,
    placeholder="780 Union Street\n0373 Sloan Street\n0711 N. 40th Street\n…",
)

st.write("**OR** upload a CSV with a column named `address`:")
uploaded_file = st.file_uploader(
    "Drag and drop file here",
    type=["csv"],
    label_visibility="collapsed",
)

# ---- Year selection: 2023–2026 ----
col_y1, col_y2 = st.columns(2)
with col_y1:
    year_2023 = st.checkbox("2023", value=False)
    year_2024 = st.checkbox("2024", value=False)
with col_y2:
    year_2025 = st.checkbox("2025", value=True)
    year_2026 = st.checkbox("2026", value=True)

years: List[int] = []
if year_2023:
    years.append(2023)
if year_2024:
    years.append(2024)
if year_2025:
    years.append(2025)
if year_2026:
    years.append(2026)

if not years:
    st.warning("Please select at least one tax year.")
    st.stop()

# Build address list
addresses: List[str] = []

if addr_text.strip():
    addresses.extend([line.strip() for line in addr_text.splitlines() if line.strip()])

if uploaded_file is not None:
    try:
        df_upload = pd.read_csv(uploaded_file)
        if "address" in df_upload.columns:
            addresses.extend(
                [str(a).strip() for a in df_upload["address"].tolist() if str(a).strip()]
            )
        else:
            st.error("Uploaded CSV must have a column named `address`.")
    except Exception as e:
        st.error(f"Could not read CSV file: {e}")

# Run button
if st.button("🔍 Run lookup", type="primary"):
    if not addresses:
        st.warning("Please paste at least one address or upload a CSV.")
        st.stop()

    st.info(f"Looking up {len(addresses)} addresses…")

    results_df, error_list = build_results(addresses, years)

    if error_list:
        with st.expander("Show API errors (for debugging)"):
            for msg in error_list:
                st.error(msg)

    st.success("Lookup complete!")

    st.subheader("Results")

    if results_df.empty:
        st.write("No results returned.")
    else:
        st.dataframe(results_df, use_container_width=True)

        # Download as CSV
        csv_bytes = results_df.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="📥 Download results as CSV",
            data=csv_bytes,
            file_name="philly_assessments_results.csv",
            mime="text/csv",
        )
else:
    st.info("Paste addresses or upload a CSV, select years, then click **Run lookup**.")
