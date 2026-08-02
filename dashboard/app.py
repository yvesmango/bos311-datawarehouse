"""Streamlit dashboard for Boston 311 service requests.

The dashboard reads from the cleaned `tickets` table in Supabase/PostgreSQL
and provides a citizen-facing overview with spatial and analytical views.
"""

from __future__ import annotations

import os
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import folium
import pandas as pd
import psycopg2
import streamlit as st
from dotenv import load_dotenv
from folium.plugins import MarkerCluster


APP_TITLE = "BOS311 Data Warehouse"
APP_SUBTITLE = "Boston 311 tickets, cleaned and ready for civic exploration."
DEFAULT_CENTER = (42.3601, -71.0589)
STATUS_COLORS = {
    "Open": "#d97706",
    "Closed": "#15803d",
    "In Progress": "#2563eb",
    "Pending": "#7c3aed",
}


st.set_page_config(page_title=APP_TITLE, page_icon="🗺️", layout="wide")


def load_database_url() -> str:
    load_dotenv()
    database_url = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is missing. Add it to .env before launching the dashboard.")
    return database_url


def get_connection():
    return psycopg2.connect(load_database_url())


@st.cache_data(ttl=300)
def fetch_dataframe(sql: str, params: tuple[Any, ...] | list[Any] | None = None) -> pd.DataFrame:
    with closing(get_connection()) as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
    return pd.DataFrame(rows, columns=columns)


@st.cache_data(ttl=300)
def get_filter_options() -> dict[str, list[str]]:
    query = """
        SELECT
            COALESCE(neighborhood, 'Unknown') AS neighborhood,
            COALESCE(source, 'Unknown') AS department,
            COALESCE(case_status, 'Unknown') AS case_status
        FROM tickets
    """
    df = fetch_dataframe(query)
    return {
        "neighborhoods": sorted(value for value in df["neighborhood"].dropna().unique().tolist() if value),
        "departments": sorted(value for value in df["department"].dropna().unique().tolist() if value),
        "statuses": sorted(value for value in df["case_status"].dropna().unique().tolist() if value),
    }


@st.cache_data(ttl=300)
def get_date_bounds() -> tuple[date, date]:
    df = fetch_dataframe(
        """
        SELECT
            MIN(open_dt)::date AS min_open_date,
            MAX(open_dt)::date AS max_open_date
        FROM tickets
        WHERE open_dt IS NOT NULL
        """
    )
    if df.empty or pd.isna(df.loc[0, "min_open_date"]) or pd.isna(df.loc[0, "max_open_date"]):
        today = date.today()
        return today - timedelta(days=30), today
    return df.loc[0, "min_open_date"], df.loc[0, "max_open_date"]


def build_filtered_query(filters: dict[str, Any], recent_limit: int) -> tuple[str, tuple[Any, ...]]:
    where_clauses = ["1 = 1"]
    params: list[Any] = []

    if filters["neighborhoods"]:
        where_clauses.append("COALESCE(neighborhood, 'Unknown') = ANY(%s)")
        params.append(filters["neighborhoods"])
    if filters["departments"]:
        where_clauses.append("COALESCE(source, 'Unknown') = ANY(%s)")
        params.append(filters["departments"])
    if filters["statuses"]:
        where_clauses.append("COALESCE(case_status, 'Unknown') = ANY(%s)")
        params.append(filters["statuses"])
    if filters["date_range"]:
        start_date, end_date = filters["date_range"]
        where_clauses.append("open_dt::date BETWEEN %s AND %s")
        params.extend([start_date, end_date])

    query = f"""
        SELECT
            case_enquiry_id,
            case_status,
            COALESCE(source, 'Unknown') AS department,
            COALESCE(neighborhood, 'Unknown') AS neighborhood,
            street_name,
            open_dt,
            closed_dt,
            sla_target_dt,
            due_date,
            latitude,
            longitude,
            city_council_district,
            description,
            subject,
            request_type,
            ROUND(EXTRACT(EPOCH FROM (closed_dt - open_dt)) / 3600.0, 2) AS response_hours
        FROM tickets
        WHERE {" AND ".join(where_clauses)}
        ORDER BY open_dt DESC NULLS LAST, case_enquiry_id DESC
        LIMIT %s
    """
    params.append(recent_limit)
    return query, tuple(params)


def load_filtered_tickets(filters: dict[str, Any], recent_limit: int) -> pd.DataFrame:
    query, params = build_filtered_query(filters, recent_limit)
    return fetch_dataframe(query, params)


def load_district_response_times(filters: dict[str, Any]) -> pd.DataFrame:
    where_clauses = ["open_dt IS NOT NULL", "closed_dt IS NOT NULL"]
    params: list[Any] = []

    if filters["neighborhoods"]:
        where_clauses.append("COALESCE(neighborhood, 'Unknown') = ANY(%s)")
        params.append(filters["neighborhoods"])
    if filters["departments"]:
        where_clauses.append("COALESCE(source, 'Unknown') = ANY(%s)")
        params.append(filters["departments"])
    if filters["statuses"]:
        where_clauses.append("COALESCE(case_status, 'Unknown') = ANY(%s)")
        params.append(filters["statuses"])
    if filters["date_range"]:
        start_date, end_date = filters["date_range"]
        where_clauses.append("open_dt::date BETWEEN %s AND %s")
        params.extend([start_date, end_date])

    query = f"""
        SELECT
            COALESCE(city_council_district, 'Unknown') AS city_council_district,
            ROUND(AVG(EXTRACT(EPOCH FROM (closed_dt - open_dt)) / 3600.0), 2) AS avg_response_hours,
            COUNT(*) AS ticket_count
        FROM tickets
        WHERE {" AND ".join(where_clauses)}
        GROUP BY 1
        HAVING COUNT(*) > 0
        ORDER BY avg_response_hours DESC NULLS LAST
    """
    return fetch_dataframe(query, tuple(params))


def render_map(df: pd.DataFrame) -> None:
    map_df = df.dropna(subset=["latitude", "longitude"]).copy()
    if map_df.empty:
        st.info("No geocoded tickets match the current filters.")
        return

    center_lat = float(map_df["latitude"].mean()) if not pd.isna(map_df["latitude"].mean()) else DEFAULT_CENTER[0]
    center_lon = float(map_df["longitude"].mean()) if not pd.isna(map_df["longitude"].mean()) else DEFAULT_CENTER[1]
    map_ = folium.Map(location=(center_lat, center_lon), zoom_start=12, tiles="CartoDB positron")
    cluster = MarkerCluster().add_to(map_)

    for _, row in map_df.iterrows():
        status = row.get("case_status") or "Unknown"
        color = STATUS_COLORS.get(status, "#334155")
        popup = folium.Popup(
            html=(
                f"<strong>{row.get('case_enquiry_id')}</strong><br>"
                f"{row.get('subject') or 'Boston 311 ticket'}<br>"
                f"Status: {status}<br>"
                f"Neighborhood: {row.get('neighborhood') or 'Unknown'}<br>"
                f"Open: {row.get('open_dt') or 'Unknown'}"
            ),
            max_width=320,
        )
        folium.CircleMarker(
            location=(float(row["latitude"]), float(row["longitude"])),
            radius=5,
            weight=1,
            color=color,
            fill=True,
            fill_color=color,
            fill_opacity=0.85,
            popup=popup,
        ).add_to(cluster)

    st.components.v1.html(map_._repr_html_(), height=600)


def render_metric_row(df: pd.DataFrame) -> None:
    total_tickets = len(df)
    open_tickets = int((df["case_status"] == "Open").sum()) if "case_status" in df else 0
    geocoded_tickets = int(df[["latitude", "longitude"]].notna().all(axis=1).sum())
    avg_response_hours = (
        round(float(df["response_hours"].dropna().mean()), 1)
        if "response_hours" in df and not df["response_hours"].dropna().empty
        else None
    )

    cols = st.columns(4)
    cols[0].metric("Tickets", f"{total_tickets:,}")
    cols[1].metric("Open", f"{open_tickets:,}")
    cols[2].metric("Geocoded", f"{geocoded_tickets:,}")
    cols[3].metric("Avg response", f"{avg_response_hours:.1f} h" if avg_response_hours is not None else "N/A")


def render_district_chart(df: pd.DataFrame) -> None:
    if df.empty:
        st.info("No completed tickets match the current filters, so the district chart is empty.")
        return
    chart_df = df.rename(columns={"avg_response_hours": "Average response hours", "city_council_district": "District"})
    chart_df = chart_df.set_index("District")[["Average response hours"]]
    st.bar_chart(chart_df, horizontal=True)


def main() -> None:
    st.title(APP_TITLE)
    st.caption(APP_SUBTITLE)
    st.write("Read-only exploration of the cleaned Boston 311 warehouse.")

    options = get_filter_options()
    min_date, max_date = get_date_bounds()

    with st.sidebar:
        st.header("Filters")
        neighborhoods = st.multiselect("Neighborhoods", options["neighborhoods"])
        departments = st.multiselect("Departments", options["departments"])
        statuses = st.multiselect("Statuses", options["statuses"])
        date_range = st.date_input("Open date range", value=(min_date, max_date), min_value=min_date, max_value=max_date)
        recent_limit = st.slider("Recent ticket limit", min_value=10, max_value=200, value=50, step=10)

    if isinstance(date_range, tuple) and len(date_range) == 2:
        selected_range = date_range
    else:
        selected_range = (min_date, max_date)

    filters = {
        "neighborhoods": neighborhoods,
        "departments": departments,
        "statuses": statuses,
        "date_range": selected_range,
    }

    tickets_df = load_filtered_tickets(filters, recent_limit)
    district_df = load_district_response_times(filters)

    render_metric_row(tickets_df)

    left, right = st.columns([1.25, 0.75], gap="large")
    with left:
        st.subheader("Map")
        render_map(tickets_df)
    with right:
        st.subheader("Average response time by district")
        render_district_chart(district_df)
        st.dataframe(district_df, use_container_width=True, hide_index=True)

    st.subheader("Most recent tickets")
    if tickets_df.empty:
        st.info("No tickets match the current filters.")
    else:
        st.dataframe(
            tickets_df[
                [
                    "case_enquiry_id",
                    "case_status",
                    "department",
                    "neighborhood",
                    "street_name",
                    "open_dt",
                    "closed_dt",
                    "response_hours",
                ]
            ],
            use_container_width=True,
            hide_index=True,
        )


if __name__ == "__main__":
    main()
