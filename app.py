import io
import requests
import pandas as pd
import streamlit as st

# CARTO SQL endpoint for Philly open data
CARTO_SQL_URL = "https://phl.carto.com/api/v2/sql"

# ---------- Helper functions ----------

def call_carto(sql: str) -> dict:
    """Run a SQL query against the CARTO API."""
    resp = requests.get(CARTO_SQL_URL, params={"q": sql})
    if resp.status_code != 200:
        # Bubble the error up so we can show it in the UI
        raise RuntimeError(f"Carto API error {resp.status_code}: {resp.text}")
    return resp.json()


def normalize_address_for_search(address: str) -> str | None:
    """
    Clean user address and turn it into a pattern we can use with ILIKE
    against p.location in opa_properties_public_pde.
    """
    if not address:
        return None

    # Only use first part if they paste "780 Union Street, Philadelphia, PA"
    a = address.strip().split(",")[0]

    if not a:
        return None

    # Uppercase for consistency
    a = a.upper()

    # Strip leading zeros from the house number
    parts = a.split()
    if parts and parts[0].isdigit():
        try:
            parts[0] = str(int(parts[0]))  # "0780" -> "780"
        except ValueError:
            pass
    a = " ".join(parts)

    # Normalize common street suffixes to match OPA's abbreviations
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

    # Escape single quotes for SQL and turn into an ILIKE pattern
    a = a.replace("'", "''")
    return a + "%"   # e.g. "780 UNION ST%"


def lookup_single_address(address: str, years: list[int]) -> list[dict]:
    """
    Look up one address for the selected tax years.
    Returns a list of rows (dicts) from the joined OPA + assessments tables.
    """
    pattern = normalize_address_for_search(address)
    if not pattern:
        return []

    years_clause = ", ".join(str(y) for y in sorted(set(years)))

    # NOTE: key fixes here:
    #  - p.location AS full_address   (NOT p.full_address)
    #  - a.year AS tax_year           (NOT a.tax_year)
    sql = f"""
        SELECT
            p.parcel_number,
            p.location AS full_address,
            p.zip_code,
            a.year AS tax_year,
            a.market_value,
            a.exempt_land,
            a.exempt_building,
            a.taxable_land,
            a.taxable_building,
            a.market_value_date
        FROM opa_properties_public_pde p
        JOIN assessments a
          ON p.parcel_number = a.parcel_number
        WHERE a.year IN ({years_clause})
          AND p.location ILIKE '{pattern}'
        ORDER BY a.year
    """

    data = call_carto(sql)
    return data.get("rows", [])


def build_results(addresses: list[str], years: list[int]) -> tuple[pd.DataFrame, list[str]]:
    """
    Run the lookup for a list of addresses and return:
    - a DataFrame of results
    - a list of error messages (if any)
    """
    rows = []
    errors = []

    unique_addresses = [a for a in dict.fromkeys(a.strip() for a in addresses) if a.strip()]

    progress_text = "Looking up {} unique addresses…".format(len(unique_addresses))
    progress = st.progress(0, text=progress_text)

    for idx, addr in enumerate(unique_addresses, start=1):
        try:
            matches = lookup_single_address(addr, years)
        except Exception as e:
            errors.append(f"{addr}: {e}")
            matches = []

        if matches:
            for m in matches:
                m = dict(m)
                m["input_address"] = addr
                rows.append(m)
        else:
            # No match found – add a placeholder row
            rows.append({
                "input_address": addr,
                "parcel_number": None,
                "full_address": None,
                "zip_code": None,
                "tax_year": ", ".join(str(y) for y in years),
                "market_value": None,
                "exempt_land": None,
                "exempt_building": None,
                "taxable_land": None,
                "taxable_building": None,
                "market_value_date": None,
                "note": "No match found",
            })

        progress.progress(idx / len(unique_addresses), text=progress_text)

    progress.empty()

    if rows:
        df = pd.DataFrame(rows)
        # Order columns a bit nicer if they exist
        col_order = [
            "input_address",
            "parcel_number",
            "full_address",
            "zip_code",
            "tax_year",
            "market_value",
            "exempt_land",
            "exempt_building",
            "taxable_land",
            "taxable_building",
            "market_value_date",
            "note",
        ]
        df = df[[c for c in col_order if c in df.columns]]
    else:
        df = pd.DataFrame()

    return df, errors


# ---------- Streamlit UI ----------

st.set_page_config(
    page_title="Philadelphia Assessment Lookup",
    layout="wide",
)

st.title("Philadelphia Assessment Lookup")

st.write(
    "Paste a list of **Philadelphia property addresses** or upload a CSV with an "
    " `address` column to look up **market values for 2025 and 2026** in bulk."
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

# Year selection
col_y1, col_y2 = st.columns(2)
with col_y1:
    year_2025 = st.checkbox("2025", value=True)
with col_y2:
    year_2026 = st.checkbox("2026", value=True)

years = []
if year_2025:
    years.append(2025)
if year_2026:
    years.append(2026)

if not years:
    st.warning("Please select at least one tax year.")
    st.stop()

# Build address list
addresses = []

# From text area
if addr_text.strip():
    addresses.extend([line.strip() for line in addr_text.splitlines() if line.strip()])

# From CSV
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
