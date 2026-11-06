"""Fisheries Data Explorer — GFW AIS-based apparent fishing effort exports."""  # Describe the application

import asyncio  # Run asynchronous tasks
import json  # Work with JSON
import re  # Match text patterns
from datetime import date, datetime, timedelta, timezone  # Handle dates and times

import pandas as pd  # Handle table data
import requests  # Send web requests
import streamlit as st  # Build the web interface
from gfwapiclient import Client  # Connect to fishing API

DATASET = "public-global-fishing-effort:latest"  # Choose fishing dataset
REGION_DATASET = "public-eez-areas"  # Choose region dataset
GLOBAL_STATS_URL = "https://gateway.api.globalfishingwatch.org/v3/4wings/stats/"  # Set worldwide API address

st.set_page_config(page_title="Fisheries Data Explorer", page_icon=None, layout="wide")  # Set page appearance


def filename_part(text: str) -> str:  # Define filename helper
    """Create a short, predictable, filename-safe region label."""  # Describe filename helper
    cleaned = re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")  # Remove unsafe characters
    return cleaned[:24].rstrip("_") or "region"  # Return short filename


def make_client(token: str) -> Client:  # Define API client helper
    return Client(access_token=token)  # Create authenticated client


@st.cache_data(ttl=86400, show_spinner=False)  # Cache for one day
def load_eez_regions(_token: str) -> pd.DataFrame:  # Define region loader
    """Download and cache the public EEZ reference list for one day."""  # Describe region caching

    async def fetch():  # Define asynchronous fetch
        return (await make_client(_token).references.get_eez_regions()).df()  # Get available fishing regions

    result = asyncio.run(fetch())  # Run region request
    if not {"id", "label"}.issubset(result.columns):  # Check required columns
        raise ValueError("GFW region list is missing 'id' or 'label'.")  # Report missing columns
    result = result.dropna(subset=["id", "label"]).copy()  # Remove incomplete regions
    result["id"] = result["id"].astype(str)  # Convert identifiers to text
    return result  # Return valid regions


async def fetch_regional(token, start, end, spatial, region_id, temporal):  # Define regional data request
    result = await make_client(token).fourwings.create_report(  # Request fishing report
        datasets=[DATASET],  # Select fishing dataset
        spatial_resolution=spatial,  # Set geographical detail
        temporal_resolution=temporal,  # Set time interval
        group_by="GEARTYPE",  # Group by fishing gear
        start_date=start.isoformat(),  # Set starting date
        end_date=(end + timedelta(days=1)).isoformat(),  # Include final day
        region={"dataset": REGION_DATASET, "id": str(region_id)},  # Choose selected region
    )  # Finish report request
    df = result.df()  # Convert report into table
    if not isinstance(df, pd.DataFrame):  # Check table format
        raise ValueError("The GFW report could not be converted to a table.")  # Report invalid table
    return df  # Return regional records


def fetch_global(token, start, end):  # Define worldwide request
    """Worldwide aggregate — NOT the same spatial/temporal granularity as regional rows."""  # Explain worldwide data
    response = requests.get(  # Send worldwide API request
        GLOBAL_STATS_URL,  # Use worldwide API address
        params={  # Set request options
            "datasets[0]": DATASET,  # Choose fishing dataset
            "fields": "FLAGS,VESSEL-IDS,ACTIVITY-HOURS",  # Choose summary fields
            "date-range": f"{start.isoformat()},{(end + timedelta(days=1)).isoformat()}",  # Set date range
        },  # Finish request options
        headers={"Authorization": f"Bearer {token}"},  # Send access token
        timeout=90,  # Set request timeout
    )  # Finish web request
    response.raise_for_status()  # Check request success
    payload = response.json()  # Read JSON response
    if not isinstance(payload, list) or not payload:  # Check response contents
        raise ValueError("GFW returned an unexpected global statistics response.")  # Report unexpected response
    df = pd.DataFrame(payload)  # Create worldwide table
    if "activityHours" not in df.columns:  # Check fishing hours
        raise ValueError("Global statistics response does not include activityHours.")  # Report missing hours
    df["start_date"] = start.isoformat()  # Record starting date
    df["end_date"] = end.isoformat()  # Record ending date
    df["scope"] = "worldwide_aggregate"  # Mark worldwide scope
    return df  # Return worldwide data


def dataframe_quality(df):  # Define quality checks
    """Checks only; do not silently discard or change scientific observations."""  # Explain data checks
    return {  # Build quality summary
        "records": int(len(df)),  # Count data rows
        "columns": list(map(str, df.columns)),  # List table columns
        "exact_duplicate_rows": int(df.duplicated().sum()),  # Count matching rows
        "missing_values_by_column": {str(k): int(v) for k, v in df.isna().sum().items()},  # Count missing values
    }  # Finish quality summary


def export_metadata(info, regional, global_df, quality):  # Define metadata builder
    return {  # Build metadata record
        "application": "Fisheries Data Explorer",  # Name the application
        "source": "Global Fishing Watch public API",  # Identify data source
        "dataset_requested": DATASET,  # Record requested dataset
        "dataset_version_note": "'latest' is a moving alias, not a fixed version number",  # Explain changing dataset version
        "data_type": "AIS-derived apparent fishing effort; not VMS and not fish catch",  # Describe fishing measurements
        "retrieved_at_utc": info["retrieved_at"].isoformat(),  # Record retrieval time
        "geographical_area": info["area"],  # Record selected area
        "region_dataset": REGION_DATASET,  # Record region dataset
        "region_id": info["region_id"],  # Record region identifier
        "start_date_inclusive": info["start"].isoformat(),  # Record first date
        "end_date_inclusive": info["end"].isoformat(),  # Record final date
        "api_end_date_exclusive": (info["end"] + timedelta(days=1)).isoformat(),  # Record excluded end date
        "spatial_resolution": info["spatial"],  # Record geographical detail
        "temporal_resolution": info["temporal"],  # Record time interval
        "regional_grouping": "GEARTYPE",  # Record fishing gear grouping
        "regional_data_quality_checks": quality,  # Include quality results
        "global_comparison_requested": info["include_global"],  # Record worldwide request choice
        "global_statistics_returned": global_df is not None,  # Mark worldwide data availability
        "global_statistics_note": "Worldwide aggregate over the full date window; no matching regional grid cells or temporal breakdown",  # Explain worldwide summary limits
        "limitations": [  # List data limitations
            "GFW apparent fishing effort is estimated from vessel tracking and classification algorithms.",  # Explain estimated fishing effort
            "AIS coverage varies; fishing activity without usable AIS may be absent.",  # Explain missing tracking coverage
            "Reported fishing hours are not the quantity of fish caught.",  # Distinguish hours from catch
            "Vessel counts and flags in worldwide statistics are distinct counts, not additive row-level regional counts.",  # Explain distinct vessel counts
            "Regional summed hours and worldwide activityHours should not be treated as formally validated comparable metrics without further methodology checks.",  # Warn about differing measurements
        ],  # Finish limitations list
    }  # Finish metadata record


# Load API credentials
try:  # Try loading access token
    TOKEN = st.secrets["GFW_API_TOKEN"]  # Read secret access token
    if not TOKEN:  # Check token exists
        raise ValueError("The API token is empty.")  # Report empty token
except (KeyError, FileNotFoundError, ValueError):  # Handle missing credentials
    st.error("Global Fishing Watch API access is not configured. Add GFW_API_TOKEN to .streamlit/secrets.toml.")  # Show credential error
    st.stop()  # Stop application execution

st.title(" Fisheries Data Explorer")  # Display application title
st.caption("Select an area and reporting period, retrieve AIS-derived apparent fishing effort, and export CSV files for offline research.")  # Explain application purpose

try:  # Try loading regions
    with st.spinner("Loading available fishing regions..."):  # Show loading indicator
        regions = load_eez_regions(TOKEN)  # Fetch available regions
except Exception:  # Handle region loading failure
    st.error("The geographical area list could not be loaded from Global Fishing Watch. Check the connection and try refreshing.")  # Show loading error
    st.stop()  # Stop application execution

# Preserve previous query results
if "last_result" not in st.session_state:  # Check for saved results
    st.session_state.last_result = None  # Start with no results

# Keep each region unique
regions = regions.sort_values(["label", "id"], kind="stable")  # Sort available regions
region_labels = {f"{row.label} (EEZ #{row.id})": str(row.id) for row in regions.itertuples(index=False)}  # Match labels to identifiers
labels = list(region_labels)  # List region labels

with st.form("fishing_request", border=True):  # Create selection form
    st.subheader("1 · Choose your data")  # Show selection heading
    selected_label = st.selectbox(  # Create region selector
        "Geographical area · Exclusive Economic Zones (EEZs)",  # Label region selector
        options=labels,  # List available regions
        index=None,  # Start without selection
        placeholder="Search for Norway or another EEZ",  # Show search example
        help="Only the EEZ regions provided by GFW are listed. Arbitrary drawn areas are not currently supported.",  # Explain region selection limits
    )  # Finish region selector
    c1, c2 = st.columns(2)  # Create date columns
    with c1:  # Use first date column
        start = st.date_input("Start date", value=date(2024, 1, 1), max_value=date.today())  # Choose starting date
    with c2:  # Use second date column
        end = st.date_input("End date", value=date(2024, 12, 31), max_value=date.today())  # Choose ending date
    c3, c4 = st.columns(2)  # Create detail columns
    with c3:  # Use first detail column
        spatial = st.selectbox(  # Create geographical detail selector
            "Geographical detail",  # Label geographical detail
            ["LOW", "HIGH"],  # Offer detail choices
            format_func=lambda s: {"LOW": "Low · smaller output", "HIGH": "High · finer grid"}[s],  # Explain detail choices
        )  # Finish detail selector
    with c4:  # Use second detail column
        temporal = st.selectbox(  # Create time interval selector
            "Time interval",  # Label time interval
            ["DAILY", "MONTHLY", "YEARLY"],  # Offer time intervals
            index=1,  # Choose monthly default
            format_func=lambda t: {"DAILY": "Daily", "MONTHLY": "Monthly", "YEARLY": "Yearly"}[t],  # Display interval names
        )  # Finish interval selector
    include_global = st.checkbox(  # Create worldwide summary checkbox
        "Also retrieve worldwide summary statistics (separate CSV)", value=False,  # Offer worldwide statistics
        help="The global export has one aggregate for the chosen date range, not comparable grid-level rows.",  # Explain worldwide data limits
    )  # Finish worldwide checkbox
    submitted = st.form_submit_button("Retrieve fishing data", type="primary", use_container_width=True)  # Create request button

if submitted:  # Handle submitted form
    if selected_label is None:  # Check region selection
        st.error("Choose a geographical area before retrieving data.")  # Ask for selected region
    elif start > end:  # Check date order
        st.error("The start date must be on or before the end date.")  # Show date order error
    else:  # Process valid selections
        region_id = region_labels[selected_label]  # Find selected region identifier
        region_name = selected_label.rsplit(" (EEZ #", 1)[0]  # Extract clean region name
        if spatial == "HIGH" and temporal == "DAILY" and (end - start).days > 31:  # Check large request size
            st.warning("High-resolution daily requests over long periods may be very large and slow. Consider a shorter period if the request fails.")  # Warn about large requests
        with st.spinner("Requesting fishing activity from Global Fishing Watch..."):  # Show request progress
            try:  # Try fetching report
                regional = asyncio.run(fetch_regional(TOKEN, start, end, spatial, region_id, temporal))  # Fetch regional fishing records
                if regional.empty:  # Check for empty results
                    st.warning("The selected request returned no fishing activity rows. Try another region or time period. This does not prove that no fishing occurred.")  # Explain empty report
                else:  # Handle returned records
                    worldwide = None  # Start without worldwide data
                    global_error = None  # Start without worldwide error
                    if include_global:  # Check worldwide option
                        try:  # Try worldwide request
                            worldwide = fetch_global(TOKEN, start, end)  # Fetch worldwide summary
                        except (requests.RequestException, ValueError, TypeError):  # Handle worldwide request failure
                            global_error = "The global summary request did not complete. Regional data is still available; try again later."  # Save worldwide error message
                    details = {  # Collect request details
                        "area": region_name,  # Save area name
                        "region_id": region_id,  # Save region identifier
                        "start": start,  # Save starting date
                        "end": end,  # Save ending date
                        "spatial": spatial,  # Save geographical detail
                        "temporal": temporal,  # Save time interval
                        "include_global": include_global,  # Save worldwide request choice
                        "retrieved_at": datetime.now(timezone.utc),  # Save retrieval timestamp
                    }  # Finish request details
                    st.session_state.last_result = {  # Save request results
                        "regional": regional,  # Store regional records
                        "global": worldwide,  # Store worldwide statistics
                        "global_error": global_error,  # Store worldwide error
                        "info": details,  # Store request settings
                    }  # Finish saved results
                    st.success("Regional fishing data retrieved successfully.")  # Show successful retrieval
            except Exception:  # Handle regional request failure
                st.error("Unable to retrieve regional fishing data. Check your selection, network or API access and try again. Previously retrieved data, if any, is still available below.")  # Show regional request error

saved = st.session_state.last_result  # Read saved results
if saved is not None:  # Check saved results exist
    info = saved["info"]  # Read saved settings
    regional = saved["regional"]  # Read regional records
    worldwide = saved["global"]  # Read worldwide statistics
    quality = dataframe_quality(regional)  # Check regional data quality

    st.divider()  # Add page divider
    st.subheader("2 · Review retrieved data")  # Show results heading
    if selected_label is None or region_labels.get(selected_label) != info["region_id"] or (  # Check region selection changed
        start != info["start"] or end != info["end"] or spatial != info["spatial"]  # Check dates and detail
        or temporal != info["temporal"] or include_global != info["include_global"]  # Check remaining selections
    ):  # Finish change check
        st.warning("Current form selections differ from the saved result below. Submit a new request to update it; these downloads still refer to the previous request.")  # Warn about previous results

    a, b, c = st.columns(3)  # Create summary columns
    a.metric("Regional observations", f"{len(regional):,}")  # Show regional record count
    b.metric("Exact duplicate rows", f"{quality['exact_duplicate_rows']:,}")  # Show duplicate row count
    c.metric("Columns", f"{len(regional.columns):,}")  # Show column count
    st.write(  # Display request description
        f"**Region:** {info['area']} (EEZ #{info['region_id']})  |  "  # Show chosen region
        f"**Period:** {info['start']} to {info['end']}  |  "  # Show chosen dates
        f"**Spatial:** {info['spatial']}  |  **Temporal:** {info['temporal']}"  # Show chosen resolutions
    )  # Finish request description
    st.caption("Preview: first 10 rows only. The downloaded regional CSV contains the entire returned DataFrame without automatic filtering or removal of rows.")  # Explain data preview
    st.dataframe(regional.head(10), use_container_width=True, hide_index=True)  # Preview first ten rows

    regional_name = (  # Build regional download filename
        f"fishing_{filename_part(info['area'])}_{info['region_id']}_"  # Include region in filename
        f"{info['start']:%Y%m%d}-{info['end']:%Y%m%d}_"  # Include dates in filename
        f"{info['spatial'][0]}_{info['temporal'][0]}.csv"  # Include resolutions in filename
    )  # Finish regional filename
    st.subheader("3 · Download for offline research")  # Show download heading
    st.download_button(  # Create regional download
        "Download complete regional CSV",  # Label regional download
        data=regional.to_csv(index=False).encode("utf-8"),  # Convert table to CSV
        file_name=regional_name, mime="text/csv", use_container_width=True,  # Set regional download options
    )  # Finish regional download

    if saved["global_error"]:  # Check worldwide request error
        st.warning(saved["global_error"])  # Show worldwide request warning

    if worldwide is not None:  # Check worldwide data exists
        global_name = f"fishing_global_{info['start']:%Y%m%d}-{info['end']:%Y%m%d}.csv"  # Build worldwide download filename
        st.download_button(  # Create worldwide download
            "Download worldwide summary CSV",  # Label worldwide download
            data=worldwide.to_csv(index=False).encode("utf-8"),  # Convert summary to CSV
            file_name=global_name, mime="text/csv", use_container_width=True,  # Set worldwide download options
        )  # Finish worldwide download
        st.info("The worldwide statistics CSV contains aggregate activityHours, vesselIds and flags for the whole reporting period. It does not include the region's daily/monthly grid observations or spatial/temporal resolution.")  # Explain worldwide CSV contents

        st.subheader("Interpretation and comparison")  # Show comparison heading
        global_hours = pd.to_numeric(worldwide.get("activityHours"), errors="coerce").sum(min_count=1)  # Total worldwide fishing hours
        regional_hours = (  # Calculate regional fishing hours
            pd.to_numeric(regional["hours"], errors="coerce").sum(min_count=1)  # Total regional fishing hours
            if "hours" in regional else float("nan")  # Handle missing hours column
        )  # Finish regional hours calculation
        x, y = st.columns(2)  # Create comparison columns
        x.metric("Regional sum of reported hours", f"{regional_hours:,.1f}" if pd.notna(regional_hours) else "Not available")  # Display regional fishing hours
        y.metric("Worldwide reported activity hours", f"{global_hours:,.1f}" if pd.notna(global_hours) else "Not available")  # Display worldwide fishing hours
        st.warning(  # Show comparison warning
            "These two figures come from DIFFERENT GFW endpoints/aggregations. "  # Explain different API sources
            "They share a requested AIS dataset and date range, but comparable measurement "  # Explain shared request settings
            "definitions, aggregation and overlap have not been independently validated. "  # Explain unverified measurement differences
            "Do not interpret their ratio as the region's verified share of worldwide fishing effort. "  # Avoid unsupported percentage claims
            "Worldwide vessel/flag counts are distinct counts, not row-by-row equivalents."  # Explain distinct vessel counts
        )  # Finish comparison warning

    metadata = export_metadata(info, regional, worldwide, quality)  # Prepare research metadata
    st.download_button(  # Create metadata download
        "Download research metadata (JSON)",  # Label metadata download
        data=json.dumps(metadata, indent=2, ensure_ascii=False).encode("utf-8"),  # Convert metadata to JSON
        file_name=regional_name.replace(".csv", "_metadata.json"),  # Set metadata filename
        mime="application/json", use_container_width=True,  # Set metadata download options
    )  # Finish metadata download

    with st.expander("Data quality checks and coverage limitations"):  # Open quality information panel
        st.write("**Missing values in returned columns**")  # Show missing values heading
        st.dataframe(  # Display missing values table
            pd.DataFrame([  # Build missing values table
                {"Column": col, "Missing values": count}  # Name missing value columns
                for col, count in quality["missing_values_by_column"].items()  # Read each missing count
            ]),  # Finish table creation
            use_container_width=True, hide_index=True,  # Set table display options
        )  # Finish table display
        st.caption("Duplicate rows are flagged, not automatically removed: repeated observations may require scientific interpretation.")  # Explain duplicate handling
        st.write(  # Show research limitations
            "These exports concern **AIS-derived apparent fishing effort**, not VMS data or fish catch. "  # Explain fishing effort data
            "Coverage depends on vessel transmissions and GFW classification; some activity is unobserved. "  # Explain tracking coverage gaps
            "The selected spatial and temporal resolutions affect the aggregation and interpretation of records."  # Explain resolution effects
        )  # Finish limitation text

with st.expander("About the datasets and how to use them"):  # Open dataset help panel
    st.write(  # Show dataset descriptions
        "**Regional CSV:** detailed AIS-based apparent fishing-effort estimates grouped by gear type, "  # Explain regional data
        "with the fields returned by the selected GFW report. "  # Explain returned regional fields
        "**Worldwide CSV:** one aggregated statistics record for the requested date range. "  # Explain worldwide summary
        "**Metadata JSON:** original settings, timestamp, quality checks and interpretation caveats. "  # Explain metadata contents
        "The global and regional exports are NOT matching row-level datasets. "  # Explain dataset differences
        "For research use, check GFW's definitions, licence, data caveats and attribution requirements."  # Recommend checking official guidance
    )  # Finish dataset descriptions
    st.markdown("[GFW API documentation](https://globalfishingwatch.org/our-apis/documentation/)")  # Link official API documentation
