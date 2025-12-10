import io
from typing import List, Dict, Tuple, Optional

import requests
import pandas as pd
import streamlit as st

# Try to import reportlab for PDF export
try:
    from reportlab.platypus import (
        SimpleDocTemplate,
        Table,
        TableStyle,
        Paragraph,
        Spacer,
    )
    from reportlab.lib.pagesizes import letter, landscape
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

    REPORTLAB_AVAILABLE = True
except Exception:
    REPORTLAB_AVAILABLE = False

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


def find_parcel_for_address(address: str) -> Optional[Dict]:
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

    We use SELECT * so we don't have to know the exact column names
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

    # Put key columns first if they exist
    preferred_order = [
        "input_address",
        "parcel_number",
        "full_address",
        "zip_code",
        "year",          # assessments table's year
        "note",
    ]
    cols = list(df.columns)
    front = [c for c in preferred_order if c in cols]
    rest = [c for c in cols if c not in front]
    df = df[front + rest]

    return df, errors


def make_pdf_from_dataframe(df_display: pd.DataFrame,
                            grand_total: Optional[float]) -> bytes:
    """
    Create a PDF report from a *display* DataFrame and return it as bytes.

    To keep things fitting on the page, we only include a subset of columns:
    - input_address
    - full_address
    - year
    - market_value
    - taxable_land
    - taxable_building

    (6 columns total – this fits nicely on a landscape letter page.)
    """
    buffer = io.BytesIO()

    page_size = landscape(letter)
    doc = SimpleDocTemplate(
        buffer,
        pagesize=page_size,
        leftMargin=30,
        rightMargin=30,
        topMargin=30,
        bottomMargin=30,
    )

    styles = getSampleStyleSheet()
    cell_style = ParagraphStyle(
        "CellStyle",
        parent=styles["BodyText"],
        fontSize=7,
        leading=8,
    )

    elements = []

    # Title
    elements.append(Paragraph("Philadelphia Assessment Lookup", styles["Title"]))
    elements.append(Spacer(1, 8))

    # Grand total line
    if grand_total is not None:
        total_text = (
            f"Grand total market value (all properties & selected years): "
            f"$ {grand_total:,.0f}"
        )
        elements.append(Paragraph(total_text, styles["Heading3"]))
        elements.append(Spacer(1, 8))

    # Columns to include in PDF
    pdf_cols_preferred = [
        "input_address",
        "full_address",
        "year",
        "market_value",
        "taxable_land",
        "taxable_building",
    ]
    available_cols = [c for c in pdf_cols_preferred if c in df_display.columns]
    if not available_cols:
        available_cols = list(df_display.columns)

    df_pdf = df_display[available_cols].copy()

    # Limit rows so PDF doesn't get huge
    if len(df_pdf) > 300:
        df_pdf = df_pdf.head(300)

    # Header row
    header_row = [Paragraph(str(col), cell_style) for col in df_pdf.columns]

    # Data rows
    data_rows = [
        [Paragraph(str(val), cell_style) for val in row]
        for row in df_pdf.astype(str).values.tolist()
    ]

    table_data = [header_row] + data_rows

    # Column widths: split evenly
    total_width = page_size[0] - doc.leftMargin - doc.rightMargin
    num_cols = len(available_cols)
    col_width = total_width / num_cols if num_cols > 0 else total_width
    col_widths = [col_width] * num_cols

    table = Table(table_data, repeatRows=1, colWidths=col_widths)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#333333")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 4),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
    ]))

    elements.append(table)
    doc.build(elements)

    pdf_bytes = buffer.getvalue()
    buffer.close()
    return pdf_bytes


# ---------- Streamlit UI ----------

st.set_page_config(
    page_title="Philadelphia Assessment Lookup",
    layout="wide",
)

st.title("Philadelphia Assessment Lookup")

st.write(
    "Paste a list of **Philadelphia property addresses** or upload a CSV with an "
    "`address` column to look up **assessment records for 2023–2026** in bulk.\n\n"
    "- The main dollar field is **`market_value`** (assessed market value per year).\n"
    "- The app also returns `taxable_land` and `taxable_building` from the City.\n"
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

# -----------------------------------------------------
# RUN LOOKUP BUTTON
# -----------------------------------------------------

run_search = st.button("🔍 Run lookup", type="primary")

if run_search:
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
        st.write("No results found for these addresses and years.")
    else:
        # ---------- GRAND TOTAL (CLEAR LABEL) ----------
        grand_total: Optional[float] = None
        if "market_value" in results_df.columns and pd.api.types.is_numeric_dtype(results_df["market_value"]):
            grand_total = results_df["market_value"].dropna().astype(float).sum()
            st.markdown(
                f"### 🧮 Grand Total Property Value\n"
                f"**Grand total market value (all properties & selected years): "
                f"$ {grand_total:,.0f}**"
            )
        else:
            st.markdown(
                "### 🧮 Grand Total Property Value\n"
                "_Column `market_value` not found or not numeric; total cannot be calculated._"
            )

        # ---------- FORMAT NUMBERS FOR DISPLAY ----------
        df_display = results_df.copy()

        value_cols = [
            c for c in df_display.columns
            if any(term in c.lower() for term in ["value", "taxable"])
            and pd.api.types.is_numeric_dtype(df_display[c])
        ]

        for col in value_cols:
            df_display[col] = df_display[col].apply(
                lambda x: f"${x:,.0f}" if pd.notnull(x) else ""
            )

        st.dataframe(df_display, use_container_width=True)

        # ---------- CSV DOWNLOAD (RAW DATA) ----------
        csv_bytes = results_df.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="📥 Download results as CSV",
            data=csv_bytes,
            file_name="philly_assessments_results.csv",
            mime="text/csv",
        )

        # ---------- PDF DOWNLOAD (TRIMMED COLUMNS, FITS PAGE) ----------
        if REPORTLAB_AVAILABLE:
            pdf_bytes = make_pdf_from_dataframe(df_display, grand_total)
            st.download_button(
                label="📄 Download results as PDF",
                data=pdf_bytes,
                file_name="philly_assessments_results.pdf",
                mime="application/pdf",
            )
        else:
            st.info(
                "PDF download is disabled because the `reportlab` package is not installed. "
                "Add `reportlab` to your `requirements.txt` to enable PDF export."
            )

else:
    st.info(
        "Paste addresses or upload a CSV, select years, then click **Run lookup**."
    )
