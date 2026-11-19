"""Fisheries Data Explorer — GFW AIS-based apparent fishing effort exports."""

import asyncio
import json
import math
import re
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import requests
import streamlit as st
from gfwapiclient import Client

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


def filter_coordinates(df, bounds):
    """Keep source grid centres inside inclusive coordinate bounds."""
    if not {"lat", "lon"}.issubset(df.columns):
        raise ValueError("Coordinate filtering requires latitude and longitude.")
    south, north, west, east = bounds
    lat = pd.to_numeric(df["lat"], errors="coerce")
    lon = pd.to_numeric(df["lon"], errors="coerce")
    if not (lat.between(-90, 90).all() and lon.between(-180, 180).all()):
        raise ValueError("Source data contains missing or invalid coordinates.")
    return df.loc[lat.between(south, north) & lon.between(west, east)].copy()


def aggregate_grid(df, grid_size):
    """Sum fishing hours by date, gear and custom grid cell."""
    # SDK exposes gear_type rather than geartype.
    if "gear_type" not in df and "geartype" in df:
        df = df.rename(columns={"geartype": "gear_type"})
    required = {"lat", "lon", "hours", "date", "gear_type"}
    if not required.issubset(df.columns):
        raise ValueError("Custom grid requires latitude, longitude, hours, date and gear type.")
    data = df[list(sorted(required))].copy()
    if data["date"].isna().any():
        raise ValueError("Custom grid requires a valid reporting date.")
    for column in ("lat", "lon", "hours"):
        data[column] = pd.to_numeric(data[column], errors="coerce")
    if data[["lat", "lon", "hours"]].isna().any().any():
        raise ValueError("Custom grid cannot aggregate missing coordinates or hours.")
    if not (data["lat"].between(-90, 90).all() and data["lon"].between(-180, 180).all()
            and data["hours"].map(math.isfinite).all() and data["hours"].ge(0).all()):
        raise ValueError("Invalid coordinates or fishing hours in source data.")

    # Assign source centres to cells.
    # Integer hundredths avoid floating-point boundary errors.
    step = round(grid_size * 100)
    lat = (data["lat"] * 100).round().astype(int).clip(-9000, 8999)
    lon = (data["lon"] * 100).round().astype(int).clip(-18000, 17999)
    data["lat_cell"] = (lat + 9000) // step
    data["lon_cell"] = (lon + 18000) // step
    result = data.groupby(
        ["date", "gear_type", "lat_cell", "lon_cell"], dropna=False, as_index=False
    )["hours"].sum()

    # Calculate custom grid cell centres.
    result["lat"] = (-9000 + result["lat_cell"] * step + step / 2) / 100
    result["lon"] = (-18000 + result["lon_cell"] * step + step / 2) / 100
    result = result[["date", "gear_type", "lat", "lon", "hours"]]
    if not math.isclose(float(result["hours"].sum()), float(data["hours"].sum()), rel_tol=1e-9, abs_tol=1e-9):
        raise ValueError("Custom grid did not preserve total fishing hours.")
    return result


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


def remove_empty_columns(df):
    """Remove only entirely missing columns."""
    removed = []
    for column in df.columns:
        values = df[column]
        missing = values.isna()
        if pd.api.types.is_object_dtype(values) or pd.api.types.is_string_dtype(values):
            placeholders = values.astype("string").str.strip().str.lower().isin(
                ["", "none", "null", "nan", "<na>"]
            )
            missing = missing | placeholders
        if missing.all():
            removed.append(column)
    return df.drop(columns=removed).copy(), removed


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
        "coordinate_bounds": dict(zip(["south", "north", "west", "east"], info["bounds"])) if info.get("bounds") else None,
        "coordinate_filter_method": "Inclusive source grid-centre filter within selected EEZ, applied locally before custom aggregation; not an exact polygon intersection." if info.get("bounds") else None,
        "source_rows_before_coordinate_filter": info.get("source_rows"),
        "source_rows_after_coordinate_filter": info.get("filtered_rows"),
        "start_date_inclusive": info["start"].isoformat(),
        "end_date_inclusive": info["end"].isoformat(),
        "api_end_date_exclusive": (info["end"] + timedelta(days=1)).isoformat(),
        "spatial_resolution": info["spatial"],
        "custom_grid_degrees": info.get("grid_size"),
        "source_spatial_resolution": "LOW" if info.get("grid_size") else info["spatial"],
        "custom_grid_method": "Source cell centres assigned to degree grid anchored at (-90, -180); hours summed by date and gear. Not area-weighted. Vessel counts are not summed." if info.get("grid_size") else None,
        "temporal_resolution": info["temporal"],
        "regional_grouping": "GEARTYPE",
        "regional_data_quality_checks": quality,
        "regional_columns_before_cleaning": info.get("columns_before_cleaning", len(regional.columns)),
        "regional_empty_columns_removed": info.get("empty_columns_removed", []),
        "regional_cleaning_method": "Remove entirely missing columns only (null values and common textual null placeholders). No rows removed by this step; partially populated columns retained.",
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


# Credentials and regional reference data
try:
    TOKEN = st.secrets["GFW_API_TOKEN"]
    if not TOKEN:
        raise ValueError("The API token is empty.")
except (KeyError, FileNotFoundError, ValueError):
    st.error("Global Fishing Watch API access is not configured. Add GFW_API_TOKEN to .streamlit/secrets.toml.")
    st.stop()

st.title(" Fisheries Data Explorer")
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
        help="Choose an EEZ; optionally restrict its returned grid centres using coordinate bounds below.",
    )
    restrict_area = st.checkbox("Restrict EEZ by latitude and longitude", value=False)
    st.caption("Optional bounds in decimal degrees. Applied only when the checkbox is selected. Negative values mean south or west.")
    lat_col, lon_col = st.columns(2)
    with lat_col:
        south = st.number_input("Minimum latitude (south)", -90.0, 90.0, 58.0, step=0.1, format="%.6f")
        north = st.number_input("Maximum latitude (north)", -90.0, 90.0, 62.0, step=0.1, format="%.6f")
    with lon_col:
        west = st.number_input("Minimum longitude (west)", -180.0, 180.0, 3.0, step=0.1, format="%.6f")
        east = st.number_input("Maximum longitude (east)", -180.0, 180.0, 8.0, step=0.1, format="%.6f")
    bounds = (south, north, west, east) if restrict_area else None
    st.caption("Bounds filter the returned EEZ grid centres locally; they do not reduce the API request size. Boxes crossing the antimeridian are not supported.")
    c1, c2 = st.columns(2)
    with c1:
        start = st.date_input("Start date", value=date(2024, 1, 1), max_value=date.today())
    with c2:
        end = st.date_input("End date", value=date(2024, 12, 31), max_value=date.today())
    c3, c4 = st.columns(2)
    with c3:
        spatial = st.selectbox(
            "Geographical detail",
            ["LOW", "HIGH", "CUSTOM"],
            format_func=lambda s: {"LOW": "Low · 0.1°", "HIGH": "High · 0.01°", "CUSTOM": "Custom · numerical grid"}[s],
        )
    with c4:
        temporal = st.selectbox(
            "Time interval",
            ["DAILY", "MONTHLY", "YEARLY"],
            index=1,
            format_func=lambda t: {"DAILY": "Daily", "MONTHLY": "Monthly", "YEARLY": "Yearly"}[t],
        )
    grid_size = st.number_input(
        "Custom grid size (degrees)", min_value=0.1, max_value=10.0,
        value=0.5, step=0.1, format="%.1f",
        help="Used only with Custom. Enter a multiple of 0.1° that divides 180° evenly. Source data is retrieved at LOW resolution and aggregated locally.",
    )
    include_global = st.checkbox(
        "Also retrieve worldwide summary statistics (separate CSV)", value=False,
        help="The global export has one aggregate for the chosen date range, not comparable grid-level rows.",
    )
    submitted = st.form_submit_button("Retrieve fishing data", type="primary", use_container_width=True)

if submitted:
    if selected_label is None:
        st.error("Choose a geographical area before retrieving data.")
    elif bounds is not None and (south >= north or west >= east):
        st.error("Minimum latitude and longitude must be smaller than their respective maximum values.")
    elif start > end:
        st.error("The start date must be on or before the end date.")
    elif spatial == "CUSTOM" and (not math.isclose(grid_size * 10, round(grid_size * 10), abs_tol=1e-8) or 1800 % round(grid_size * 10) != 0):
        st.error("Choose a grid size in 0.1° increments that divides 180° evenly, such as 0.2°, 0.5°, 1° or 2°.")
    else:
        region_id = region_labels[selected_label]
        region_name = selected_label.rsplit(" (EEZ #", 1)[0]
        if spatial == "HIGH" and temporal == "DAILY" and (end - start).days > 31:
            st.warning("High-resolution daily requests over long periods may be very large and slow. Consider a shorter period if the request fails.")
        with st.spinner("Requesting fishing activity from Global Fishing Watch..."):
            try:
                regional = asyncio.run(fetch_regional(TOKEN, start, end, "LOW" if spatial == "CUSTOM" else spatial, region_id, temporal))
                source_rows = len(regional)
                if bounds is not None and not regional.empty:
                    regional = filter_coordinates(regional, bounds)
                filtered_rows = len(regional)
                if spatial == "CUSTOM" and not regional.empty:
                    regional = aggregate_grid(regional, grid_size)
                if regional.empty:
                    st.warning("The selected request returned no fishing activity rows. Try another region or time period. This does not prove that no fishing occurred.")
                else:
                    # Remove entirely empty columns.
                    columns_before_cleaning = len(regional.columns)
                    regional, empty_columns_removed = remove_empty_columns(regional)
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
                        "bounds": bounds,
                        "source_rows": source_rows,
                        "filtered_rows": filtered_rows,
                        "columns_before_cleaning": columns_before_cleaning,
                        "empty_columns_removed": [str(c) for c in empty_columns_removed],
                        "start": start,
                        "end": end,
                        "spatial": spatial,
                        "grid_size": grid_size if spatial == "CUSTOM" else None,
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
                st.error("Unable to retrieve or aggregate regional fishing data. Check your selection, network or API access and try again. Previously retrieved data, if any, is still available below.")

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
        or bounds != info.get("bounds")
        or (spatial == "CUSTOM" and grid_size != info.get("grid_size"))
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
    if info.get("bounds"):
        s, n, w, e = info["bounds"]
        st.write(f"**Coordinate bounds:** latitude {s:g}° to {n:g}°, longitude {w:g}° to {e:g}°")
        st.caption(f"Source rows within bounds: {info['filtered_rows']:,} of {info['source_rows']:,}. Selection uses source cell centres, not exact cell boundaries.")
    if info.get("grid_size"):
        st.info(f"Custom {info['grid_size']:g}° grid: LOW (0.1°) source cells assigned by centre; hours summed by date and gear. Coordinates are custom cell centres, not vessel positions; after coordinate filtering, custom cell centres may fall outside the bounds. Vessel counts are omitted because they cannot be safely summed.")
    removed = info.get("empty_columns_removed", [])
    before = info.get("columns_before_cleaning", len(regional.columns))
    st.caption(f"Dataset cleaning: {before} columns before cleaning; {len(removed)} entirely empty columns removed. No observations removed by cleaning.")
    if removed:
        with st.expander("View removed empty columns"):
            st.write(", ".join(removed))
    st.caption("Preview: first 10 rows only. The CSV contains all rows of the displayed dataset (locally aggregated when Custom is selected).")
    st.dataframe(regional.head(10), use_container_width=True, hide_index=True)

    spatial_code = f"custom_{info['grid_size']:g}deg" if info.get("grid_size") else info["spatial"][0]
    bounds_code = ""
    if info.get("bounds"):
        bounds_code = "_box_" + "_".join(f"{v:.6f}".rstrip("0").rstrip(".") for v in info["bounds"])
    regional_name = (
        f"fishing_{filename_part(info['area'])}_{info['region_id']}_"
        f"{info['start']:%Y%m%d}-{info['end']:%Y%m%d}_"
        f"{spatial_code}_{info['temporal'][0]}{bounds_code}.csv"
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
        x.metric("Selected area sum of reported hours", f"{regional_hours:,.1f}" if pd.notna(regional_hours) else "Not available")
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
        "with entirely empty columns removed from the selected report. "
        "**Worldwide CSV:** one aggregated statistics record for the requested date range. "
        "**Metadata JSON:** original settings, timestamp, quality checks and interpretation caveats. "
        "The global and regional exports are NOT matching row-level datasets. "
        "For research use, check GFW's definitions, licence, data caveats and attribution requirements."
    )
    st.markdown("[GFW API documentation](https://globalfishingwatch.org/our-apis/documentation/)")
