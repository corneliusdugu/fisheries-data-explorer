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


if submitted:
    # Explain the upcoming feature
    st.info("The selection form is ready. Fishing-data retrieval is introduced in the next stage.")
