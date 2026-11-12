"""Norwegian VMS positions (Fiskeridirektoratet open data) searched from locally downloaded files.

Source: https://www.fiskeridir.no/statistikk-tall-og-analyse/data-og-statistikk-om-yrkesfiske/apne-data-posisjonsrapportering-vms
Files are yearly, semicolon-separated, UTF-8 CSVs, usually zipped. The user downloads them once into a
folder; this module scans them in chunks so multi-GB years never have to fit in memory.
"""

import json
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st

VMS_PAGE = "https://www.fiskeridir.no/statistikk-tall-og-analyse/data-og-statistikk-om-yrkesfiske/apne-data-posisjonsrapportering-vms"
VMS_DOCS = VMS_PAGE + "/dokumentasjon-fiskerisporing"

CHUNK_ROWS = 500_000
MAX_RAW_ROWS = 1_000_000  # keep the in-memory table and the download button usable
GRID_DEGREES = {"LOW": 0.1, "HIGH": 0.01}  # same cell sizes GFW uses for LOW / HIGH
PERIOD_FORMAT = {"DAILY": "%Y-%m-%d", "MONTHLY": "%Y-%m", "YEARLY": "%Y"}

# Documented variable names (Norwegian), normalised by _norm(); first match wins.
COLUMN_ALIASES = {
    "time": ["tidspunktutc", "tidspunkt", "timestamp"],
    "lon": ["lengdegrad", "longitude", "lon"],
    "lat": ["breddegrad", "latitude", "lat"],
    "speed": ["fart", "speed"],
    "callsign": ["radiokallesignal", "callsign"],
    "regmark": ["registreringsmerke"],
    "name": ["fartøynavn", "fartoynavn"],
    "length": ["størstelengde", "storstelengde"],
}
REQUIRED_COLUMNS = ("time", "lon", "lat")


class VmsTooLarge(Exception):
    """Raw result would exceed MAX_RAW_ROWS."""


@dataclass
class VmsQuery:
    start: date
    end: date
    bbox: tuple | None  # (min_lat, max_lat, min_lon, max_lon)
    vessel_terms: list
    min_length: float | None
    max_length: float | None
    output: str  # "RAW" or "GRID"
    spatial: str  # LOW / HIGH (GRID only)
    temporal: str  # DAILY / MONTHLY / YEARLY (GRID only)


def _norm(name) -> str:
    s = unicodedata.normalize("NFKC", str(name)).strip().lower()
    return re.sub(r"[^\w]+", "", s)


def resolve_columns(columns) -> dict:
    lookup = {_norm(c): c for c in columns}
    found = {}
    for key, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in lookup:
                found[key] = lookup[alias]
                break
    missing = [k for k in REQUIRED_COLUMNS if k not in found]
    if missing:
        raise ValueError(
            f"Could not find the {', '.join(missing)} column(s) in the VMS file. "
            f"Columns present: {', '.join(map(str, columns))}"
        )
    return found


def find_vms_files(folder) -> dict:
    """Map year -> list of .zip/.csv files, taking the year from the file name."""
    path = Path(str(folder)).expanduser()
    if not path.is_dir():
        return {}
    found: dict = {}
    for f in sorted(path.iterdir()):
        if f.is_file() and f.suffix.lower() in {".zip", ".csv"}:
            m = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", f.name)
            if m:
                found.setdefault(int(m.group(1)), []).append(f)
    return found


def _read_csv(fh):
    yield from pd.read_csv(fh, sep=";", dtype=str, encoding="utf-8-sig", chunksize=CHUNK_ROWS)


def iter_chunks(path: Path):
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            members = [i for i in zf.infolist() if not i.is_dir() and i.filename.lower().endswith(".csv")]
            if not members:
                raise ValueError(f"{path.name} contains no CSV file.")
            for info in members:
                with zf.open(info) as fh:
                    yield from _read_csv(fh)
    else:
        with open(path, "rb") as fh:
            yield from _read_csv(fh)


_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def _parse_oracle_style(s: pd.Series) -> pd.Series:
    """Parse e.g. '01-JAN-24 15.40.00.000000000' (the format used in the Fiskeridirektoratet VMS files).

    Month names are mapped by hand so the result does not depend on the computer's language settings.
    """
    p = s.str.extract(r"^\s*(\d{1,2})-([A-Za-z]{3})-(\d{2,4})[ T](\d{1,2})[.:](\d{2})[.:](\d{2})")
    year = pd.to_numeric(p[2], errors="coerce")
    year = year.where(year >= 100, year + 2000 - 100 * (year > 69))  # 00-69 -> 20xx, 70-99 -> 19xx
    parts = pd.DataFrame({
        "year": year,
        "month": p[1].str.upper().map(_MONTHS),
        "day": pd.to_numeric(p[0], errors="coerce"),
        "hour": pd.to_numeric(p[3], errors="coerce"),
        "minute": pd.to_numeric(p[4], errors="coerce"),
        "second": pd.to_numeric(p[5], errors="coerce"),
    })
    return pd.to_datetime(parts, errors="coerce")


class _TimeParser:
    """Detects the timestamp format on the first chunk (ISO 8601, day-first, then mixed), then sticks with it."""

    def __init__(self):
        self.mode = None

    def _parse(self, s, mode):
        s = s.str.strip()
        if mode == "DD-MON-YY HH.MI.SS":
            return _parse_oracle_style(s)
        if mode == "ISO 8601":
            out = pd.to_datetime(s, format="ISO8601", errors="coerce", utc=True)
        elif mode == "day-first":
            out = pd.to_datetime(s, dayfirst=True, errors="coerce", utc=True)
        else:
            out = pd.to_datetime(s, format="mixed", dayfirst=True, errors="coerce", utc=True)
        return out.dt.tz_localize(None)

    def __call__(self, s):
        if self.mode is None:
            best, best_share = None, -1.0
            for mode in ("DD-MON-YY HH.MI.SS", "ISO 8601", "day-first", "mixed"):
                parsed = self._parse(s, mode)
                share = parsed.notna().mean() if len(parsed) else 0.0
                if share >= 0.5:
                    self.mode = mode
                    return parsed
                if share > best_share:
                    best, best_share = mode, share
            self.mode = best  # nothing parsed well; keep the best attempt and report it
        return self._parse(s, self.mode)

    @property
    def label(self):
        return self.mode or "undetermined"


def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s.str.replace(",", ".", regex=False), errors="coerce")


def _clean(s: pd.Series) -> pd.Series:
    return s.where(s.str.strip() != "")


class _GridAccumulator:
    KEYS = ["period", "cell_lat_sw", "cell_lon_sw"]

    def __init__(self, query: VmsQuery):
        self.size = GRID_DEGREES[query.spatial]
        self.fmt = PERIOD_FORMAT[query.temporal]
        self.parts, self.pairs = [], []

    def add(self, frame: pd.DataFrame):
        f = pd.DataFrame({
            "period": frame["ts"].dt.strftime(self.fmt),
            "cell_lat_sw": (np.floor(np.round(frame["lat"] / self.size, 6)) * self.size).round(4),
            "cell_lon_sw": (np.floor(np.round(frame["lon"] / self.size, 6)) * self.size).round(4),
            "speed": frame["speed"],
            "vid": frame["vid"],
        })
        part = f.groupby(self.KEYS, sort=False).agg(
            position_reports=("speed", "size"), speed_sum=("speed", "sum"), speed_n=("speed", "count")
        ).reset_index()
        self.parts.append(part)
        self.pairs.append(f.dropna(subset=["vid"])[self.KEYS + ["vid"]].drop_duplicates())
        if len(self.parts) >= 20:
            self._compress()

    def _compress(self):
        if len(self.parts) > 1:
            self.parts = [pd.concat(self.parts).groupby(self.KEYS, as_index=False).sum()]
        if len(self.pairs) > 1:
            self.pairs = [pd.concat(self.pairs).drop_duplicates()]

    def result(self) -> pd.DataFrame:
        if not self.parts:
            return pd.DataFrame()
        self._compress()
        agg = self.parts[0]
        distinct = self.pairs[0].groupby(self.KEYS).size().rename("distinct_vessels").reset_index()
        agg = agg.merge(distinct, on=self.KEYS, how="left")
        agg["distinct_vessels"] = agg["distinct_vessels"].fillna(0).astype(int)
        agg["position_reports"] = agg["position_reports"].astype(int)
        agg["speed_values_count"] = agg["speed_n"].astype(int)
        agg["mean_speed_reported"] = (agg["speed_sum"] / agg["speed_n"].where(agg["speed_n"] > 0)).round(3)
        cols = self.KEYS + ["position_reports", "distinct_vessels", "mean_speed_reported", "speed_values_count"]
        return agg[cols].sort_values(self.KEYS, kind="stable").reset_index(drop=True)


def search_vms(folder, q: VmsQuery, progress=None):
    """Scan the local files for the requested years. Returns (DataFrame, stats)."""
    files = find_vms_files(folder)
    years = list(range(q.start.year, q.end.year + 1))
    todo = [(y, p) for y in years for p in files.get(y, [])]
    if not todo:
        raise FileNotFoundError(
            f"No VMS files for {years[0]}–{years[-1]} found in '{folder}'. "
            f"Expected names such as {years[0]}-VMS.csv.zip."
        )
    start_ts = pd.Timestamp(q.start)
    end_ts = pd.Timestamp(q.end) + pd.Timedelta(days=1)

    stats = {
        "files_read": [p.name for _, p in todo],
        "years_without_local_file": [y for y in years if y not in files],
        "rows_scanned": 0,
        "rows_dropped_invalid_time_or_position": 0,
        "rows_matched": 0,
        "positions_without_vessel_identifier": 0,
        "timestamp_format_by_file": {},
        "first_matched_timestamp_utc": None,
        "last_matched_timestamp_utc": None,
        "columns_used": {},
        "sample_raw_values": {},
        "readable_timestamp_range_in_files": [None, None],
    }
    seen_lo = seen_hi = None
    raw_parts, raw_rows = [], 0
    grid = _GridAccumulator(q) if q.output == "GRID" else None
    first_ts = last_ts = None

    for i, (_, path) in enumerate(todo):
        parser, colcache = _TimeParser(), {}
        for chunk in iter_chunks(path):
            if chunk.empty:
                continue
            key = tuple(chunk.columns)
            if key not in colcache:
                colcache[key] = resolve_columns(chunk.columns)
            cols = colcache[key]
            if (q.min_length or q.max_length) and "length" not in cols:
                raise ValueError("A vessel length filter was set but the file has no vessel length column.")
            if q.vessel_terms and not any(k in cols for k in ("callsign", "regmark", "name")):
                raise ValueError("A vessel filter was set but the file has no call sign, registration or name column.")

            stats["rows_scanned"] += len(chunk)
            ts = parser(chunk[cols["time"]])
            lon, lat = _num(chunk[cols["lon"]]), _num(chunk[cols["lat"]])
            valid = ts.notna() & lon.between(-180, 180) & lat.between(-90, 90)
            stats["rows_dropped_invalid_time_or_position"] += int((~valid).sum())
            if not stats["columns_used"]:
                stats["columns_used"] = {k: str(v) for k, v in cols.items()}
                stats["sample_raw_values"] = {
                    k: chunk[cols[k]].head(3).tolist() for k in ("time", "lon", "lat")
                }
            if ts.notna().any():
                lo_all, hi_all = ts.min(), ts.max()
                seen_lo = lo_all if seen_lo is None else min(seen_lo, lo_all)
                seen_hi = hi_all if seen_hi is None else max(seen_hi, hi_all)

            keep = valid & (ts >= start_ts) & (ts < end_ts)
            if q.bbox:
                min_lat, max_lat, min_lon, max_lon = q.bbox
                keep &= lat.between(min_lat, max_lat) & lon.between(min_lon, max_lon)
            if q.vessel_terms:
                hit = pd.Series(False, index=chunk.index)
                for k in ("callsign", "regmark", "name"):
                    if k in cols:
                        text = chunk[cols[k]].fillna("").str.lower()
                        for term in q.vessel_terms:
                            hit |= text.str.contains(term, regex=False)
                keep &= hit
            if q.min_length or q.max_length:
                length = _num(chunk[cols["length"]])
                if q.min_length:
                    keep &= length >= q.min_length
                if q.max_length:
                    keep &= length <= q.max_length

            n = int(keep.sum())
            if progress:
                progress(i / len(todo), f"Reading {path.name} ({i + 1}/{len(todo)}) · {stats['rows_scanned']:,} rows scanned · {stats['rows_matched'] + n:,} matched")
            if n == 0:
                continue
            stats["rows_matched"] += n
            kept_ts = ts[keep]
            lo, hi = kept_ts.min(), kept_ts.max()
            first_ts = lo if first_ts is None else min(first_ts, lo)
            last_ts = hi if last_ts is None else max(last_ts, hi)

            if q.output == "RAW":
                raw_rows += n
                if raw_rows > MAX_RAW_ROWS:
                    raise VmsTooLarge(
                        f"More than {MAX_RAW_ROWS:,} matching position reports. Narrow the area, period or vessels, "
                        "or choose the gridded summary."
                    )
                sub = chunk.loc[keep].copy()
                sub["timestamp_utc_iso"] = kept_ts.dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                raw_parts.append(sub)
            else:
                vid = pd.Series(pd.NA, index=chunk.index, dtype="object")
                for k in ("callsign", "regmark", "name"):
                    if k in cols:
                        vid = vid.combine_first(_clean(chunk[cols[k]]))
                speed = _num(chunk[cols["speed"]]) if "speed" in cols else pd.Series(np.nan, index=chunk.index)
                stats["positions_without_vessel_identifier"] += int(vid[keep].isna().sum())
                grid.add(pd.DataFrame({"ts": kept_ts, "lat": lat[keep], "lon": lon[keep], "speed": speed[keep], "vid": vid[keep]}))
        stats["timestamp_format_by_file"][path.name] = parser.label

    if seen_lo is not None:
        stats["readable_timestamp_range_in_files"] = [seen_lo.isoformat(), seen_hi.isoformat()]
    if first_ts is not None:
        stats["first_matched_timestamp_utc"] = first_ts.isoformat()
        stats["last_matched_timestamp_utc"] = last_ts.isoformat()
    if progress:
        progress(1.0, "Finished reading files")

    if q.output == "RAW":
        df = pd.concat(raw_parts, ignore_index=True) if raw_parts else pd.DataFrame()
    else:
        df = grid.result()
    return df, stats


def _quality(df: pd.DataFrame, raw: bool) -> dict:
    q = {
        "records": int(len(df)),
        "columns": list(map(str, df.columns)),
        "missing_values_by_column": {str(k): int(v) for k, v in df.isna().sum().items()},
    }
    if raw:
        q["exact_duplicate_rows"] = int(df.duplicated().sum())
    return q


def _safe(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_") or "x"


def _metadata(info: dict, stats: dict, quality: dict) -> dict:
    return {
        "application": "Fisheries Data Explorer",
        "source": "Fiskeridirektoratet (Norwegian Directorate of Fisheries) open VMS position data, local copy",
        "source_page": VMS_PAGE,
        "documentation": VMS_DOCS,
        "licence": "Norsk lisens for offentlige data (NLOD); attribute Fiskeridirektoratet",
        "data_type": "VMS position reports (POS messages) from Norwegian vessels; not AIS, not fishing effort, not catch",
        "retrieved_at_utc": info["retrieved_at"].isoformat(),
        "local_folder": info["folder"],
        "geographical_filter": info["area"],
        "start_date_inclusive": info["start"].isoformat(),
        "end_date_inclusive": info["end"].isoformat(),
        "vessel_filter_terms": info["vessel_terms"],
        "min_length_m": info["min_length"],
        "max_length_m": info["max_length"],
        "output_type": "raw position reports" if info["output"] == "RAW" else "gridded summary",
        "spatial_resolution": info["spatial"] if info["output"] == "GRID" else None,
        "grid_cell_degrees": GRID_DEGREES[info["spatial"]] if info["output"] == "GRID" else None,
        "grid_cell_reference": "cell_lat_sw / cell_lon_sw are the south-west corner of each cell" if info["output"] == "GRID" else None,
        "temporal_resolution": info["temporal"] if info["output"] == "GRID" else None,
        "processing_statistics": stats,
        "result_data_quality_checks": quality,
        "limitations": [
            "Covers Norwegian fishing and catch vessels reporting by VMS (15 m and over; 12 m in Skagerrak from 2013). Foreign vessels and smaller vessels are not included.",
            "These are position reports, not fishing effort: they include transit, port and idle periods, and the files carry no gear type.",
            "Reporting is nominally hourly, but some vessel types report every 10 or 30 minutes, and satellite shadow areas cause gaps. Position counts are not time spent and are not comparable to GFW apparent fishing hours.",
            "Only standard position messages (POS) are included; other message types were removed by the source.",
            "Distinct vessel counts use call sign, then registration mark, then vessel name. Identifiers can be missing, and registration marks can be reused after two years.",
            "A bounding box is a rectangle, not an EEZ boundary: it can include other nations' waters and exclude parts of an EEZ.",
            "Mean speed is as reported in the source ('Fart'); check the source documentation for its unit and precision.",
            "Years without a local file are not searched; see processing_statistics.years_without_local_file.",
        ],
    }


def render_vms():
    st.caption(
        "Search the Norwegian VMS position data you have already downloaded from "
        "[Fiskeridirektoratet](" + VMS_PAGE + "). Select an area and reporting period, then export CSV files for offline research."
    )
    folder = st.text_input(
        "Folder containing the downloaded VMS files",
        value="vms_data",
        key="vms_folder",
        help="Yearly files such as 2024-VMS.csv.zip or VMS-2022.zip. The year is read from the file name. Files stay zipped.",
    )
    files = find_vms_files(folder)
    if files:
        st.caption("Local files found for: " + ", ".join(f"{y}" + (f" ({len(v)} files)" if len(v) > 1 else "") for y, v in sorted(files.items())))
    else:
        st.warning(f"No yearly VMS files found in '{folder}'. Download them from the Fiskeridirektoratet page linked above and put them in this folder.")

    if "last_vms" not in st.session_state:
        st.session_state.last_vms = None

    with st.form("vms_request", border=True):
        st.subheader("1 · Choose your data")
        c1, c2 = st.columns(2)
        start = c1.date_input("Start date", value=date(2024, 1, 1), min_value=date(2011, 1, 1), max_value=date.today(), key="vms_start")
        end = c2.date_input("End date", value=date(2024, 12, 31), min_value=date(2011, 1, 1), max_value=date.today(), key="vms_end")
        output = st.selectbox(
            "Output",
            ["GRID", "RAW"],
            format_func=lambda o: {"GRID": "Gridded summary (counts per cell and period)", "RAW": "Raw position reports (max 1,000,000 rows)"}[o],
        )
        c3, c4 = st.columns(2)
        spatial = c3.selectbox(
            "Geographical detail (gridded only)",
            ["LOW", "HIGH"],
            format_func=lambda s: {"LOW": "Low · 0.1° cells", "HIGH": "High · 0.01° cells"}[s],
        )
        temporal = c4.selectbox(
            "Time interval (gridded only)",
            ["DAILY", "MONTHLY", "YEARLY"],
            index=1,
            format_func=lambda t: {"DAILY": "Daily", "MONTHLY": "Monthly", "YEARLY": "Yearly"}[t],
        )
        submitted = st.form_submit_button("Search VMS data", type="primary", use_container_width=True)

    # The search is by date range only: no area, vessel or length filters.
    use_box, terms, min_len, max_len = False, [], 0.0, 0.0
    min_lat = max_lat = min_lon = max_lon = 0.0
    params = {
        "folder": folder, "bbox": None, "start": start, "end": end, "terms": terms,
        "min_len": min_len, "max_len": max_len,
        "output": output, "spatial": spatial, "temporal": temporal,
    }

    if submitted:
        if start > end:
            st.error("The start date must be on or before the end date.")
        elif use_box and (min_lat >= max_lat or min_lon >= max_lon):
            st.error("The bounding box minimums must be smaller than the maximums.")
        elif not files:
            st.error("No VMS files found in the selected folder.")
        else:
            query = VmsQuery(
                start=start, end=end, bbox=params["bbox"], vessel_terms=terms,
                min_length=min_len or None, max_length=max_len or None,
                output=output, spatial=spatial, temporal=temporal,
            )
            bar = st.progress(0.0, text="Starting…")
            try:
                df, stats = search_vms(folder, query, progress=lambda f, t: bar.progress(min(max(f, 0.0), 1.0), text=t))
                bar.empty()
                if df.empty:
                    st.warning(
                        "No position reports matched the selected dates. If the files should cover them, the file format may "
                        "not be what the reader expects; the details below show what it saw."
                    )
                    st.json({
                        "rows_scanned": stats["rows_scanned"],
                        "rows_with_unreadable_time_or_position": stats["rows_dropped_invalid_time_or_position"],
                        "timestamp_format_detected": stats["timestamp_format_by_file"],
                        "timestamp_range_readable_in_files": stats["readable_timestamp_range_in_files"],
                        "columns_used": stats["columns_used"],
                        "first_raw_values": stats["sample_raw_values"],
                        "files_read": stats["files_read"],
                    })
                else:
                    area = (
                        f"bounding box lat {min_lat:g} to {max_lat:g}, lon {min_lon:g} to {max_lon:g}"
                        if use_box else "no area filter"
                    )
                    st.session_state.last_vms = {
                        "data": df, "stats": stats, "params": params,
                        "info": {
                            "folder": str(folder), "area": area, "start": start, "end": end,
                            "area_tag": _safe(f"bbox {min_lat:g} {max_lat:g} {min_lon:g} {max_lon:g}".replace("-", "m")) if use_box else "all",
                            "vessel_terms": terms, "min_length": min_len or None, "max_length": max_len or None,
                            "output": output, "spatial": spatial, "temporal": temporal,
                            "retrieved_at": datetime.now(timezone.utc),
                        },
                    }
                    st.success("VMS data retrieved successfully.")
            except VmsTooLarge as exc:
                bar.empty()
                st.error(str(exc))
            except (FileNotFoundError, ValueError) as exc:
                bar.empty()
                st.error(str(exc))
            except Exception as exc:
                bar.empty()
                st.error(f"Unable to read the VMS files ({type(exc).__name__}: {exc}). Previously retrieved data, if any, is still available below.")

    saved = st.session_state.last_vms
    if saved is None:
        return
    info, stats, df = saved["info"], saved["stats"], saved["data"]
    raw = info["output"] == "RAW"
    quality = _quality(df, raw)

    st.divider()
    st.subheader("2 · Review retrieved data")
    if saved["params"] != params:
        st.warning("Current form selections differ from the saved result below. Submit a new search to update it; these downloads still refer to the previous search.")
    if stats["years_without_local_file"]:
        st.warning("No local file for: " + ", ".join(map(str, stats["years_without_local_file"])) + ". Those years are missing from this result.")

    a, b, c = st.columns(3)
    a.metric("Matching position reports", f"{stats['rows_matched']:,}")
    b.metric("Rows in result", f"{len(df):,}")
    c.metric("Rows scanned", f"{stats['rows_scanned']:,}")
    output_label = "raw positions" if raw else "grid " + info["spatial"] + " / " + info["temporal"]
    st.write(f"**Area:** {info['area']}  |  **Period:** {info['start']} to {info['end']}  |  **Output:** {output_label}")
    st.caption("Preview: first 10 rows only. The downloaded CSV contains the entire result without further filtering.")
    st.dataframe(df.head(10), use_container_width=True, hide_index=True)

    area_tag = info["area_tag"]
    kind = "raw" if raw else f"grid_{info['spatial'][0]}_{info['temporal'][0]}"
    name = f"vms_{area_tag}_{info['start']:%Y%m%d}-{info['end']:%Y%m%d}_{kind}.csv"

    st.subheader("3 · Download for offline research")
    st.download_button("Download complete VMS CSV", data=df.to_csv(index=False).encode("utf-8"), file_name=name, mime="text/csv", use_container_width=True)
    st.download_button(
        "Download research metadata (JSON)",
        data=json.dumps(_metadata(info, stats, quality), indent=2, ensure_ascii=False).encode("utf-8"),
        file_name=name.replace(".csv", "_metadata.json"), mime="application/json", use_container_width=True,
    )

    with st.expander("Data quality checks and coverage limitations"):
        st.write(
            f"Rows dropped before filtering because of an unreadable timestamp or position: "
            f"**{stats['rows_dropped_invalid_time_or_position']:,}**"
        )
        if not raw:
            st.write(f"Matching positions without a vessel identifier (excluded from distinct-vessel counts): **{stats['positions_without_vessel_identifier']:,}**")
        if raw:
            st.write(f"Exact duplicate rows in result: **{quality['exact_duplicate_rows']:,}** (flagged, not removed)")
        st.write("Timestamp format detected per file: " + ", ".join(f"{k}: {v}" for k, v in stats["timestamp_format_by_file"].items()))
        st.dataframe(
            pd.DataFrame([{"Column": k, "Missing values": v} for k, v in quality["missing_values_by_column"].items()]),
            use_container_width=True, hide_index=True,
        )
        st.write(
            "VMS covers **Norwegian vessels of 15 m and over** (12 m in Skagerrak from 2013). Each record is a position report, "
            "**not fishing effort or catch**, and the data has no gear type. Reporting is roughly hourly but irregular, "
            "so position counts are not time spent. See the "
            f"[source documentation]({VMS_DOCS}) and attribute Fiskeridirektoratet (NLOD licence)."
        )
