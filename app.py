"""Fisheries Data Explorer — GFW AIS-based apparent fishing effort exports."""

# Run background API tasks
import asyncio
# Work with JSON files
import json
# Find and replace text
import re
# Handle dates and times
from datetime import date, datetime, timedelta, timezone

# Work with data tables
import pandas as pd
# Send requests over internet
import requests
# Build the web interface
import streamlit as st
# Connect to fishing data
from gfwapiclient import Client

# Choose fishing activity dataset
DATASET = "public-global-fishing-effort:latest"
# Choose sea area dataset
REGION_DATASET = "public-eez-areas"
# Set worldwide statistics address
GLOBAL_STATS_URL = "https://gateway.api.globalfishingwatch.org/v3/4wings/stats/"

# Set page appearance
st.set_page_config(page_title="Fisheries Data Explorer", page_icon=None, layout="wide")


# Make safe download filenames
def filename_part(text: str) -> str:
    """Create a short, predictable, filename-safe region label."""
    # Remove unsafe filename characters
    cleaned = re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")
    # Keep filenames short
    return cleaned[:24].rstrip("_") or "region"

# Create fishing API connection
def make_client(token: str) -> Client:
    # Use provided access token
    return Client(access_token=token)


# Save region choices temporarily
@st.cache_data(ttl=86400, show_spinner=False)
# Load available sea regions
def load_eez_regions(_token: str) -> pd.DataFrame:
    """Download and cache the public EEZ reference list for one day."""

    # Request region list online
    async def fetch():
        # Convert regions into table
        return (await make_client(_token).references.get_eez_regions()).df()

    # Run the region request
    result = asyncio.run(fetch())
    # Check required columns exist
    if not {"id", "label"}.issubset(result.columns):
        # Reject incomplete region list
        raise ValueError("GFW region list is missing 'id' or 'label'.")
    # Remove regions without names
    result = result.dropna(subset=["id", "label"]).copy()
    # Keep region IDs as text
    result["id"] = result["id"].astype(str)
    # Return valid sea regions
    return result


# Request regional fishing activity
async def fetch_regional(token, start, end, spatial, region_id, temporal):
    # Create a fishing report
    result = await make_client(token).fourwings.create_report(
        # Choose fishing activity dataset
        datasets=[DATASET],
        # Choose map detail level
        spatial_resolution=spatial,
        # Choose report time interval
        temporal_resolution=temporal,
        # Group results by gear
        group_by="GEARTYPE",
        # Set first report day
        start_date=start.isoformat(),
        # Include the final day
        end_date=(end + timedelta(days=1)).isoformat(),
        # Choose selected sea area
        region={"dataset": REGION_DATASET, "id": str(region_id)},
    )
    # Convert report into table
    df = result.df()
    # Check table format
    if not isinstance(df, pd.DataFrame):
        # Reject unexpected report data
        raise ValueError("The GFW report could not be converted to a table.")
    # Return fishing data table
    return df

# Measure downloaded data quality
def dataframe_quality(df):
    """Checks only; do not silently discard or change scientific observations."""
    # Collect simple quality checks
    return {
        # Count total data rows
        "records": int(len(df)),
        # List downloaded table columns
        "columns": list(map(str, df.columns)),
        # Count identical data rows
        "exact_duplicate_rows": int(df.duplicated().sum()),
        # Count empty column values
        "missing_values_by_column": {str(k): int(v) for k, v in df.isna().sum().items()},
    }

# Load private API credentials
# Try reading API key
try:
    # Get locally stored key
    TOKEN = st.secrets["GFW_API_TOKEN"]
    # Check API key exists
    if not TOKEN:
        # Reject empty API key
        raise ValueError("The API token is empty.")
# Handle missing API credentials
except (KeyError, FileNotFoundError, ValueError):
    # Show instructions for fixes
    st.error("Global Fishing Watch API access is not configured. Add GFW_API_TOKEN to .streamlit/secrets.toml.")
    # Stop without valid credentials
    st.stop()

# Display application title
st.title(" Fisheries Data Explorer")
# Explain what users can do
st.caption("Select an area and reporting period, retrieve AIS-derived apparent fishing effort, and export CSV files for offline research.")

# Try loading sea regions
try:
    # Show progress while loading
    with st.spinner("Loading available fishing regions..."):
        # Download available region choices
        regions = load_eez_regions(TOKEN)
# Handle loading problems
except Exception:
    # Explain the connection problem
    st.error("The geographical area list could not be loaded from Global Fishing Watch. Check the connection and try refreshing.")
    # Stop when regions unavailable
    st.stop()

# Remember the last download
# Look for previous results
if "last_result" not in st.session_state:
    # Start with empty results
    st.session_state.last_result = None

# Make area choices distinct
# Sort regions by name
regions = regions.sort_values(["label", "id"], kind="stable")
# Link names to region IDs
region_labels = {f"{row.label} (EEZ #{row.id})": str(row.id) for row in regions.itertuples(index=False)}
# Create list of area names
labels = list(region_labels)

# Build fishing request form
with st.form("fishing_request", border=True):
    # Display form heading
    st.subheader("1 · Choose your data")
    # Let users choose area
    selected_label = st.selectbox(
        "Geographical area · Exclusive Economic Zones (EEZs)",
        options=labels,
        index=None,
        placeholder="Search for Norway or another EEZ",
        help="Only the EEZ regions provided by GFW are listed. Arbitrary drawn areas are not currently supported.",
    )
    # Place dates side by side
    c1, c2 = st.columns(2)
    # Position start date field
    with c1:
        # Let users choose start
        start = st.date_input("Start date", value=date(2024, 1, 1), max_value=date.today())
    # Position end date field
    with c2:
        # Let users choose end
        end = st.date_input("End date", value=date(2024, 12, 31), max_value=date.today())
    # Place detail options together
    c3, c4 = st.columns(2)
    # Position map detail selector
    with c3:
        # Let users choose detail
        spatial = st.selectbox(
            "Geographical detail",
            ["LOW", "HIGH"],
            format_func=lambda s: {"LOW": "Low · smaller output", "HIGH": "High · finer grid"}[s],
        )
    # Position time interval selector
    with c4:
        # Let users choose interval
        temporal = st.selectbox(
            "Time interval",
            ["DAILY", "MONTHLY", "YEARLY"],
            index=1,
            format_func=lambda t: {"DAILY": "Daily", "MONTHLY": "Monthly", "YEARLY": "Yearly"}[t],
        )
    # Add request submission button
    submitted = st.form_submit_button("Retrieve fishing data", type="primary", use_container_width=True)


# Start after button click
if submitted:
    # Check area was selected
    if selected_label is None:
        # Warn when area missing
        st.error("Choose a geographical area before retrieving data.")
    # Check date order
    elif start > end:
        # Explain incorrect date order
        st.error("The start date must be on or before the end date.")
    # Continue with valid choices
    else:
        # Find selected region identifier
        region_id = region_labels[selected_label]
        # Extract selected region name
        region_name = selected_label.rsplit(" (EEZ #", 1)[0]
        # Check for oversized requests
        if spatial == "HIGH" and temporal == "DAILY" and (end - start).days > 31:
            # Suggest smaller requests
            st.warning("High-resolution daily requests over long periods may be very large and slow. Consider a shorter period if the request fails.")
        # Show request progress
        with st.spinner("Requesting fishing activity from Global Fishing Watch..."):
            # Handle unexpected request errors
            try:
                # Download regional fishing activity
                regional = asyncio.run(fetch_regional(TOKEN, start, end, spatial, region_id, temporal))
                # Check for empty results
                if regional.empty:
                    # Explain empty data results
                    st.warning("The selected request returned no fishing activity rows. Try another region or time period. This does not prove that no fishing occurred.")
                # Continue when data exists
                else:
                    # Record search settings
                    details = {
                        # Save area name
                        "area": region_name,
                        # Save area identifier
                        "region_id": region_id,
                        # Save chosen starting date
                        "start": start,
                        # Save chosen ending date
                        "end": end,
                        # Save chosen map detail
                        "spatial": spatial,
                        # Save chosen time interval
                        "temporal": temporal,
                        # Record download time
                        "retrieved_at": datetime.now(timezone.utc),
                    }
                    # Store the latest results
                    st.session_state.last_result = {
                        # Keep regional data table
                        "regional": regional,
                        # Keep original search choices
                        "info": details,
                    }
                    # Confirm data was downloaded
                    st.success("Regional fishing data retrieved successfully.")
            # Handle regional request errors
            except Exception:
                # Explain why request failed
                st.error("Unable to retrieve regional fishing data. Check your selection, network or API access and try again. Previously retrieved data, if any, is still available below.")

# Load any saved results
saved = st.session_state.last_result
# Check saved results exist
if saved is not None:
    # Restore original search choices
    info = saved["info"]
    # Restore regional activity table
    regional = saved["regional"]
    # Check regional table quality
    quality = dataframe_quality(regional)

    # Separate form and results
    st.divider()
    # Show results section heading
    st.subheader("2 · Review retrieved data")
    # Check changed search choices
    if selected_label is None or region_labels.get(selected_label) != info["region_id"] or (
        start != info["start"] or end != info["end"] or spatial != info["spatial"]
        or temporal != info["temporal"]
    ):
        # Warn about older results
        st.warning("Current form selections differ from the saved result below. Submit a new request to update it; these downloads still refer to the previous request.")

    # Create three summary boxes
    a, b, c = st.columns(3)
    # Show number of rows
    a.metric("Regional observations", f"{len(regional):,}")
    # Show count of duplicates
    b.metric("Exact duplicate rows", f"{quality['exact_duplicate_rows']:,}")
    # Show number of columns
    c.metric("Columns", f"{len(regional.columns):,}")
    # Display original search details
    st.write(
        f"**Region:** {info['area']} (EEZ #{info['region_id']})  |  "
        f"**Period:** {info['start']} to {info['end']}  |  "
        f"**Spatial:** {info['spatial']}  |  **Temporal:** {info['temporal']}"
    )
    # Explain the preview limit
    st.caption("Preview: first 10 rows only. The downloaded regional CSV contains the entire returned DataFrame without automatic filtering or removal of rows.")
    # Preview first ten rows
    st.dataframe(regional.head(10), use_container_width=True, hide_index=True)

    # Create regional CSV filename
    regional_name = (
        f"fishing_{filename_part(info['area'])}_{info['region_id']}_"
        f"{info['start']:%Y%m%d}-{info['end']:%Y%m%d}_"
        f"{info['spatial'][0]}_{info['temporal'][0]}.csv"
    )
    # Show CSV download heading
    st.subheader("3 · Download for offline research")
    # Create regional download button
    st.download_button(
        "Download complete regional CSV",
        # Turn regional table into CSV
        data=regional.to_csv(index=False).encode("utf-8"),
        # Set downloaded CSV filename
        file_name=regional_name, mime="text/csv", use_container_width=True,
    )
