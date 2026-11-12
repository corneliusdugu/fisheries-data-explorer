"""Fisheries Data Explorer — GFW AIS-based apparent fishing effort exports."""

import asyncio
import json
import re
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import requests
import streamlit as st
from gfwapiclient import Client

from vms import render_vms

DATASET = "public-global-fishing-effort:latest"
REGION_DATASET = "public-eez-areas"
GLOBAL_STATS_URL = "https://gateway.api.globalfishingwatch.org/v3/4wings/stats/"

st.set_page_config(page_title="Fisheries Data Explorer", page_icon=None, layout="wide")


def filename_part(text: str) -> str:
    """Create a short, predictable, filename-safe region label."""
    cleaned = re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")
    return cleaned[:24].rstrip("_") or "region"


def make_client(token: str) -> Client:
    return Client(access_token=token)


@st.cache_data(ttl=86400, show_spinner=False)
def load_eez_regions(_token: str) -> pd.DataFrame:
    """Download and cache the public EEZ reference list for one day."""

    async def fetch():
        return (await make_client(_token).references.get_eez_regions()).df()

    result = asyncio.run(fetch())
    if not {"id", "label"}.issubset(result.columns):
        raise ValueError("GFW region list is missing 'id' or 'label'.")
    result = result.dropna(subset=["id", "label"]).copy()
    result["id"] = result["id"].astype(str)
    return result


async def fetch_regional(token, start, end, spatial, region_id, temporal):
    result = await make_client(token).fourwings.create_report(
        datasets=[DATASET],
        spatial_resolution=spatial,
        temporal_resolution=temporal,
        group_by="GEARTYPE",
        start_date=start.isoformat(),
        end_date=(end + timedelta(days=1)).isoformat(),
        region={"dataset": REGION_DATASET, "id": str(region_id)},
    )
    df = result.df()
    if not isinstance(df, pd.DataFrame):
        raise ValueError("The GFW report could not be converted to a table.")
    return df


def fetch_global(token, start, end):
    """Worldwide aggregate — NOT the same spatial/temporal granularity as regional rows."""
    response = requests.get(
        GLOBAL_STATS_URL,
        params={
            "datasets[0]": DATASET,
            "fields": "FLAGS,VESSEL-IDS,ACTIVITY-HOURS",
            "date-range": f"{start.isoformat()},{(end + timedelta(days=1)).isoformat()}",
        },
        headers={"Authorization": f"Bearer {token}"},
        timeout=90,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list) or not payload:
        raise ValueError("GFW returned an unexpected global statistics response.")
    df = pd.DataFrame(payload)
    if "activityHours" not in df.columns:
        raise ValueError("Global statistics response does not include activityHours.")
    df["start_date"] = start.isoformat()
    df["end_date"] = end.isoformat()
    df["scope"] = "worldwide_aggregate"
    return df


def dataframe_quality(df):
    """Checks only; do not silently discard or change scientific observations."""
    return {
        "records": int(len(df)),
        "columns": list(map(str, df.columns)),
        "exact_duplicate_rows": int(df.duplicated().sum()),
        "missing_values_by_column": {str(k): int(v) for k, v in df.isna().sum().items()},
    }


def export_metadata(info, regional, global_df, quality):
    return {
        "application": "Fisheries Data Explorer",
        "source": "Global Fishing Watch public API",
        "dataset_requested": DATASET,
        "dataset_version_note": "'latest' is a moving alias, not a fixed version number",
        "data_type": "AIS-derived apparent fishing effort; not VMS and not fish catch",
        "retrieved_at_utc": info["retrieved_at"].isoformat(),
        "geographical_area": info["area"],
        "region_dataset": REGION_DATASET,
        "region_id": info["region_id"],
        "start_date_inclusive": info["start"].isoformat(),
        "end_date_inclusive": info["end"].isoformat(),
        "api_end_date_exclusive": (info["end"] + timedelta(days=1)).isoformat(),
        "spatial_resolution": info["spatial"],
        "temporal_resolution": info["temporal"],
        "regional_grouping": "GEARTYPE",
        "regional_data_quality_checks": quality,
        "global_comparison_requested": info["include_global"],
        "global_statistics_returned": global_df is not None,
        "global_statistics_note": "Worldwide aggregate over the full date window; no matching regional grid cells or temporal breakdown",
        "limitations": [
            "GFW apparent fishing effort is estimated from vessel tracking and classification algorithms.",
            "AIS coverage varies; fishing activity without usable AIS may be absent.",
            "Reported fishing hours are not the quantity of fish caught.",
            "Vessel counts and flags in worldwide statistics are distinct counts, not additive row-level regional counts.",
            "Regional summed hours and worldwide activityHours should not be treated as formally validated comparable metrics without further methodology checks.",
        ],
    }


st.title(" Fisheries Data Explorer")
data_source = st.radio(
    "Data source",
    ["GFW AIS · apparent fishing effort", "Norwegian VMS · position reports (local files)"],
    horizontal=True,
)
if data_source.startswith("Norwegian"):
    # VMS reads files already downloaded to disk, so it needs no GFW token or network access.
    render_vms()
    st.stop()

# Credentials and regional reference data
try:
    TOKEN = st.secrets["GFW_API_TOKEN"]
    if not TOKEN:
        raise ValueError("The API token is empty.")
except (KeyError, FileNotFoundError, ValueError):
    st.error("Global Fishing Watch API access is not configured. Add GFW_API_TOKEN to .streamlit/secrets.toml.")
    st.stop()

st.caption("Select an area and reporting period, retrieve AIS-derived apparent fishing effort, and export CSV files for offline research.")

try:
    with st.spinner("Loading available fishing regions..."):
        regions = load_eez_regions(TOKEN)
except Exception:
    st.error("The geographical area list could not be loaded from Global Fishing Watch. Check the connection and try refreshing.")
    st.stop()

# Keep the original data and query details together in one session object.
if "last_result" not in st.session_state:
    st.session_state.last_result = None

# Keep distinct IDs even if labels coincide.
regions = regions.sort_values(["label", "id"], kind="stable")
region_labels = {f"{row.label} (EEZ #{row.id})": str(row.id) for row in regions.itertuples(index=False)}
labels = list(region_labels)

with st.form("fishing_request", border=True):
    st.subheader("1 · Choose your data")
    selected_label = st.selectbox(
        "Geographical area · Exclusive Economic Zones (EEZs)",
        options=labels,
        index=None,
        placeholder="Search for Norway or another EEZ",
        help="Only the EEZ regions provided by GFW are listed. Arbitrary drawn areas are not currently supported.",
    )
    c1, c2 = st.columns(2)
    with c1:
        start = st.date_input("Start date", value=date(2024, 1, 1), max_value=date.today())
    with c2:
        end = st.date_input("End date", value=date(2024, 12, 31), max_value=date.today())
    c3, c4 = st.columns(2)
    with c3:
        spatial = st.selectbox(
            "Geographical detail",
            ["LOW", "HIGH"],
            format_func=lambda s: {"LOW": "Low · smaller output", "HIGH": "High · finer grid"}[s],
        )
    with c4:
        temporal = st.selectbox(
            "Time interval",
            ["DAILY", "MONTHLY", "YEARLY"],
            index=1,
            format_func=lambda t: {"DAILY": "Daily", "MONTHLY": "Monthly", "YEARLY": "Yearly"}[t],
        )
    include_global = st.checkbox(
        "Also retrieve worldwide summary statistics (separate CSV)", value=False,
        help="The global export has one aggregate for the chosen date range, not comparable grid-level rows.",
    )
    submitted = st.form_submit_button("Retrieve fishing data", type="primary", use_container_width=True)

if submitted:
    if selected_label is None:
        st.error("Choose a geographical area before retrieving data.")
    elif start > end:
        st.error("The start date must be on or before the end date.")
    else:
        region_id = region_labels[selected_label]
        region_name = selected_label.rsplit(" (EEZ #", 1)[0]
        if spatial == "HIGH" and temporal == "DAILY" and (end - start).days > 31:
            st.warning("High-resolution daily requests over long periods may be very large and slow. Consider a shorter period if the request fails.")
        with st.spinner("Requesting fishing activity from Global Fishing Watch..."):
            try:
                regional = asyncio.run(fetch_regional(TOKEN, start, end, spatial, region_id, temporal))
                if regional.empty:
                    st.warning("The selected request returned no fishing activity rows. Try another region or time period. This does not prove that no fishing occurred.")
                else:
                    worldwide = None
                    global_error = None
                    if include_global:
                        try:
                            worldwide = fetch_global(TOKEN, start, end)
                        except (requests.RequestException, ValueError, TypeError):
                            global_error = "The global summary request did not complete. Regional data is still available; try again later."
                    details = {
                        "area": region_name,
                        "region_id": region_id,
                        "start": start,
                        "end": end,
                        "spatial": spatial,
                        "temporal": temporal,
                        "include_global": include_global,
                        "retrieved_at": datetime.now(timezone.utc),
                    }
                    st.session_state.last_result = {
                        "regional": regional,
                        "global": worldwide,
                        "global_error": global_error,
                        "info": details,
                    }
                    st.success("Regional fishing data retrieved successfully.")
            except Exception:
                st.error("Unable to retrieve regional fishing data. Check your selection, network or API access and try again. Previously retrieved data, if any, is still available below.")

saved = st.session_state.last_result
if saved is not None:
    info = saved["info"]
    regional = saved["regional"]
    worldwide = saved["global"]
    quality = dataframe_quality(regional)

    st.divider()
    st.subheader("2 · Review retrieved data")
    if selected_label is None or region_labels.get(selected_label) != info["region_id"] or (
        start != info["start"] or end != info["end"] or spatial != info["spatial"]
        or temporal != info["temporal"] or include_global != info["include_global"]
    ):
        st.warning("Current form selections differ from the saved result below. Submit a new request to update it; these downloads still refer to the previous request.")

    a, b, c = st.columns(3)
    a.metric("Regional observations", f"{len(regional):,}")
    b.metric("Exact duplicate rows", f"{quality['exact_duplicate_rows']:,}")
    c.metric("Columns", f"{len(regional.columns):,}")
    st.write(
        f"**Region:** {info['area']} (EEZ #{info['region_id']})  |  "
        f"**Period:** {info['start']} to {info['end']}  |  "
        f"**Spatial:** {info['spatial']}  |  **Temporal:** {info['temporal']}"
    )
    st.caption("Preview: first 10 rows only. The downloaded regional CSV contains the entire returned DataFrame without automatic filtering or removal of rows.")
    st.dataframe(regional.head(10), use_container_width=True, hide_index=True)

    regional_name = (
        f"fishing_{filename_part(info['area'])}_{info['region_id']}_"
        f"{info['start']:%Y%m%d}-{info['end']:%Y%m%d}_"
        f"{info['spatial'][0]}_{info['temporal'][0]}.csv"
    )
    st.subheader("3 · Download for offline research")
    st.download_button(
        "Download complete regional CSV",
        data=regional.to_csv(index=False).encode("utf-8"),
        file_name=regional_name, mime="text/csv", use_container_width=True,
    )

    if saved["global_error"]:
        st.warning(saved["global_error"])

    if worldwide is not None:
        global_name = f"fishing_global_{info['start']:%Y%m%d}-{info['end']:%Y%m%d}.csv"
        st.download_button(
            "Download worldwide summary CSV",
            data=worldwide.to_csv(index=False).encode("utf-8"),
            file_name=global_name, mime="text/csv", use_container_width=True,
        )
        st.info("The worldwide statistics CSV contains aggregate activityHours, vesselIds and flags for the whole reporting period. It does not include the region's daily/monthly grid observations or spatial/temporal resolution.")

        st.subheader("Interpretation and comparison")
        global_hours = pd.to_numeric(worldwide.get("activityHours"), errors="coerce").sum(min_count=1)
        regional_hours = (
            pd.to_numeric(regional["hours"], errors="coerce").sum(min_count=1)
            if "hours" in regional else float("nan")
        )
        x, y = st.columns(2)
        x.metric("Regional sum of reported hours", f"{regional_hours:,.1f}" if pd.notna(regional_hours) else "Not available")
        y.metric("Worldwide reported activity hours", f"{global_hours:,.1f}" if pd.notna(global_hours) else "Not available")
        st.warning(
            "These two figures come from DIFFERENT GFW endpoints/aggregations. "
            "They share a requested AIS dataset and date range, but comparable measurement "
            "definitions, aggregation and overlap have not been independently validated. "
            "Do not interpret their ratio as the region's verified share of worldwide fishing effort. "
            "Worldwide vessel/flag counts are distinct counts, not row-by-row equivalents."
        )

    metadata = export_metadata(info, regional, worldwide, quality)
    st.download_button(
        "Download research metadata (JSON)",
        data=json.dumps(metadata, indent=2, ensure_ascii=False).encode("utf-8"),
        file_name=regional_name.replace(".csv", "_metadata.json"),
        mime="application/json", use_container_width=True,
    )

    with st.expander("Data quality checks and coverage limitations"):
        st.write("**Missing values in returned columns**")
        st.dataframe(
            pd.DataFrame([
                {"Column": col, "Missing values": count}
                for col, count in quality["missing_values_by_column"].items()
            ]),
            use_container_width=True, hide_index=True,
        )
        st.caption("Duplicate rows are flagged, not automatically removed: repeated observations may require scientific interpretation.")
        st.write(
            "These exports concern **AIS-derived apparent fishing effort**, not VMS data or fish catch. "
            "Coverage depends on vessel transmissions and GFW classification; some activity is unobserved. "
            "The selected spatial and temporal resolutions affect the aggregation and interpretation of records."
        )

with st.expander("About the datasets and how to use them"):
    st.write(
        "**Regional CSV:** detailed AIS-based apparent fishing-effort estimates grouped by gear type, "
        "with the fields returned by the selected GFW report. "
        "**Worldwide CSV:** one aggregated statistics record for the requested date range. "
        "**Metadata JSON:** original settings, timestamp, quality checks and interpretation caveats. "
        "The global and regional exports are NOT matching row-level datasets. "
        "For research use, check GFW's definitions, licence, data caveats and attribution requirements."
    )
    st.markdown("[GFW API documentation](https://globalfishingwatch.org/our-apis/documentation/)")
