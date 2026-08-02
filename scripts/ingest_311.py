"""Ingest Boston 311 ticket data from CKAN into Supabase/PostgreSQL.

The script is designed to be idempotent:
- raw CKAN payloads are stored for auditability
- lookup tables are upserted by natural key
- ticket rows are upserted by `case_enquiry_id`

Required environment variables:
- `DATABASE_URL`: PostgreSQL connection string for Supabase

Optional environment variables:
- `CKAN_SQL_ENDPOINT`: override the Boston CKAN SQL endpoint
- `CKAN_RESOURCE_ID`: override the CKAN datastore resource id
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import psycopg2
import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from psycopg2.extras import Json
from urllib3.util.retry import Retry


LOGGER = logging.getLogger("bos311.ingest")
DEFAULT_CKAN_SQL_ENDPOINT = "https://data.boston.gov/api/3/action/datastore_search_sql"
DEFAULT_CKAN_RESOURCE_ID = "1a0b420d-99f1-4887-9851-990b2a5a6e17"
DEFAULT_PAGE_SIZE = 500
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 2.0


@dataclass(frozen=True)
class IngestionConfig:
    database_url: str
    ckan_sql_endpoint: str
    ckan_resource_id: str
    page_size: int
    max_rows: int | None
    apply_schema: bool
    full_refresh: bool
    retry_attempts: int
    retry_backoff_seconds: float


@dataclass(frozen=True)
class Watermark:
    open_dt: datetime
    case_enquiry_id: int


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit",
        "--page-size",
        dest="page_size",
        type=int,
        default=DEFAULT_PAGE_SIZE,
        help="Rows to fetch per CKAN page.",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=None,
        help="Optional cap on the total number of rows fetched in a run.",
    )
    parser.add_argument(
        "--apply-schema",
        action="store_true",
        help="Apply sql/schema_v1.sql before ingesting.",
    )
    parser.add_argument(
        "--full-refresh",
        action="store_true",
        help="Ignore the stored watermark and rebuild from the beginning of the source dataset.",
    )
    parser.add_argument(
        "--retry-attempts",
        type=int,
        default=DEFAULT_RETRY_ATTEMPTS,
        help="Number of attempts for transient request or database failures.",
    )
    parser.add_argument(
        "--retry-backoff-seconds",
        type=float,
        default=DEFAULT_RETRY_BACKOFF_SECONDS,
        help="Base backoff delay between retry attempts.",
    )
    return parser


def load_config(args: argparse.Namespace) -> IngestionConfig:
    load_dotenv()

    database_url = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DATABASE_URL")
    if not database_url:
        raise RuntimeError(
            "DATABASE_URL is missing. Set it in your .env file to the Supabase connection string."
        )

    return IngestionConfig(
        database_url=database_url,
        ckan_sql_endpoint=os.getenv("CKAN_SQL_ENDPOINT", DEFAULT_CKAN_SQL_ENDPOINT),
        ckan_resource_id=os.getenv("CKAN_RESOURCE_ID", DEFAULT_CKAN_RESOURCE_ID),
        page_size=args.page_size,
        max_rows=args.max_rows,
        apply_schema=args.apply_schema,
        full_refresh=args.full_refresh,
        retry_attempts=args.retry_attempts,
        retry_backoff_seconds=args.retry_backoff_seconds,
    )


def build_http_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=5,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def timestamp_literal(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("'", "''")


def build_sql_query(
    resource_id: str,
    page_size: int,
    offset: int,
    watermark: Watermark | None = None,
) -> str:
    where_clause = ""
    if watermark is not None:
        watermark_dt = timestamp_literal(watermark.open_dt)
        where_clause = (
            "WHERE open_dt IS NULL "
            f"OR open_dt > TIMESTAMPTZ '{watermark_dt}' "
            f"OR (open_dt = TIMESTAMPTZ '{watermark_dt}' AND case_enquiry_id > {watermark.case_enquiry_id})"
        )
    return (
        f'SELECT * FROM "{resource_id}" '
        f"{where_clause} "
        "ORDER BY open_dt ASC NULLS FIRST, case_enquiry_id ASC "
        f"LIMIT {page_size} OFFSET {offset}"
    )


def fetch_ckan_page(
    session: requests.Session,
    endpoint: str,
    resource_id: str,
    page_size: int,
    offset: int,
    watermark: Watermark | None,
) -> list[dict[str, Any]]:
    query = build_sql_query(resource_id, page_size, offset, watermark)
    url = f"{endpoint}?{urlencode({'sql': query})}"
    LOGGER.info("Fetching CKAN page", extra={"offset": offset, "page_size": page_size})
    response = session.get(url, timeout=120)
    response.raise_for_status()
    payload = response.json()

    if not payload.get("success"):
        raise RuntimeError(f"CKAN request failed: {json.dumps(payload, ensure_ascii=False)}")

    records = payload.get("result", {}).get("records", [])
    if not isinstance(records, list):
        raise RuntimeError("CKAN response did not contain a record list.")
    return records


def fetch_ckan_rows(
    session: requests.Session,
    endpoint: str,
    resource_id: str,
    page_size: int,
    watermark: Watermark | None = None,
    max_rows: int | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        remaining = None if max_rows is None else max_rows - len(rows)
        if remaining is not None and remaining <= 0:
            break
        current_page_size = page_size if remaining is None else min(page_size, remaining)
        page = fetch_ckan_page(session, endpoint, resource_id, current_page_size, offset, watermark)
        rows.extend(page)
        if len(page) < current_page_size:
            break
        offset += current_page_size
    return rows


def to_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        normalized = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    return None


def to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_bool(value: Any) -> bool | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"t", "true", "1", "yes", "y"}:
            return True
        if normalized in {"f", "false", "0", "no", "n"}:
            return False
    return None


def normalize_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def make_geo_point_sql(latitude: Any, longitude: Any) -> tuple[float | None, float | None]:
    lat = to_float(latitude)
    lon = to_float(longitude)
    if lat is None or lon is None:
        return None, None
    return lat, lon


def execute_schema(conn) -> None:
    schema_path = Path(__file__).resolve().parents[1] / "sql" / "schema_v1.sql"
    with schema_path.open("r", encoding="utf-8") as handle:
        schema_sql = handle.read()
    with conn.cursor() as cursor:
        for statement in split_sql_statements(schema_sql):
            LOGGER.info("Applying schema statement", extra={"statement": statement.splitlines()[0][:120]})
            cursor.execute(statement)


def split_sql_statements(sql_text: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    in_dollar_block = False
    for line in sql_text.splitlines():
        stripped = line.strip()
        if stripped.count("$$") % 2 == 1:
            in_dollar_block = not in_dollar_block
        current.append(line)
        if not in_dollar_block and stripped.endswith(";"):
            statement = "\n".join(current).strip()
            if statement and not statement.startswith("--"):
                statements.append(statement)
            current = []
    remainder = "\n".join(current).strip()
    if remainder:
        statements.append(remainder)
    return statements


def upsert_lookup(cursor, table: str, name: str, extra: dict[str, Any] | None = None) -> int | None:
    if not name:
        return None
    extra = extra or {}
    columns = ["name", *extra.keys()]
    values = [name, *extra.values()]
    update_clause = ", ".join(f"{column} = EXCLUDED.{column}" for column in extra.keys())
    if update_clause:
        update_clause = f"DO UPDATE SET {update_clause}"
    else:
        update_clause = "DO UPDATE SET updated_at = NOW()"
    sql = f"""
        INSERT INTO {table} ({", ".join(columns)})
        VALUES ({", ".join(["%s"] * len(values))})
        ON CONFLICT (name)
        {update_clause}
        RETURNING id
    """
    cursor.execute(sql, values)
    row = cursor.fetchone()
    return row[0] if row else None


def upsert_ticket(cursor, ticket: dict[str, Any], raw_payload_id: int, department_id: int | None, category_id: int | None) -> None:
    latitude, longitude = make_geo_point_sql(ticket.get("latitude"), ticket.get("longitude"))
    geo_point_sql = (
        "ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography" if latitude is not None and longitude is not None else "NULL"
    )
    params: list[Any] = [
        ticket.get("case_enquiry_id"),
        raw_payload_id,
        department_id,
        category_id,
        normalize_text(ticket.get("case_status")),
        normalize_text(ticket.get("street_name")),
        normalize_text(ticket.get("neighborhood")),
        normalize_text(ticket.get("ward")),
        normalize_text(ticket.get("precinct")),
        normalize_text(ticket.get("city_council_district")),
        normalize_text(ticket.get("source")),
        normalize_text(ticket.get("description")),
        normalize_text(ticket.get("subject")),
        normalize_text(ticket.get("request_type")),
        to_datetime(ticket.get("open_dt")),
        to_datetime(ticket.get("closed_dt")),
        to_datetime(ticket.get("sla_target_dt")),
        to_datetime(ticket.get("due_date")),
        to_bool(ticket.get("on_time")),
        latitude,
        longitude,
    ]
    if latitude is not None and longitude is not None:
        params.extend([longitude, latitude])

    sql = f"""
        INSERT INTO tickets (
            case_enquiry_id,
            raw_payload_id,
            department_id,
            category_id,
            case_status,
            street_name,
            neighborhood,
            ward,
            precinct,
            city_council_district,
            source,
            description,
            subject,
            request_type,
            open_dt,
            closed_dt,
            sla_target_dt,
            due_date,
            on_time,
            latitude,
            longitude,
            geo_point
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            {geo_point_sql}
        )
        ON CONFLICT (case_enquiry_id)
        DO UPDATE SET
            raw_payload_id = EXCLUDED.raw_payload_id,
            department_id = EXCLUDED.department_id,
            category_id = EXCLUDED.category_id,
            case_status = EXCLUDED.case_status,
            street_name = EXCLUDED.street_name,
            neighborhood = EXCLUDED.neighborhood,
            ward = EXCLUDED.ward,
            precinct = EXCLUDED.precinct,
            city_council_district = EXCLUDED.city_council_district,
            source = EXCLUDED.source,
            description = EXCLUDED.description,
            subject = EXCLUDED.subject,
            request_type = EXCLUDED.request_type,
            open_dt = EXCLUDED.open_dt,
            closed_dt = EXCLUDED.closed_dt,
            sla_target_dt = EXCLUDED.sla_target_dt,
            due_date = EXCLUDED.due_date,
            on_time = EXCLUDED.on_time,
            latitude = EXCLUDED.latitude,
            longitude = EXCLUDED.longitude,
            geo_point = EXCLUDED.geo_point
    """
    cursor.execute(sql, params)


def load_payload(cursor, ticket: dict[str, Any]) -> int:
    case_enquiry_id = ticket.get("case_enquiry_id")
    if case_enquiry_id in (None, ""):
        raise ValueError("CKAN ticket is missing case_enquiry_id.")
    cursor.execute(
        """
        INSERT INTO raw_311_tickets (case_enquiry_id, payload)
        VALUES (%s, %s)
        ON CONFLICT (case_enquiry_id)
        DO UPDATE SET payload = EXCLUDED.payload
        RETURNING id
        """,
        (int(case_enquiry_id), Json(ticket)),
    )
    row = cursor.fetchone()
    if not row:
        raise RuntimeError("Failed to insert raw payload.")
    return int(row[0])


def ingest_rows(conn, rows: Iterable[dict[str, Any]]) -> list[int]:
    ingested_case_ids: list[int] = []
    with conn.cursor() as cursor:
        for ticket in rows:
            case_enquiry_id = ticket.get("case_enquiry_id")
            if case_enquiry_id in (None, ""):
                raise ValueError("CKAN ticket is missing case_enquiry_id.")
            case_enquiry_id = int(case_enquiry_id)
            raw_payload_id = load_payload(cursor, ticket)
            department_name = normalize_text(ticket.get("department"))
            category_name = normalize_text(ticket.get("case_title") or ticket.get("subject"))
            department_id = upsert_lookup(cursor, "departments", department_name) if department_name else None
            category_id = None
            if category_name:
                category_id = upsert_lookup(
                    cursor,
                    "categories",
                    category_name,
                    extra={"department_id": department_id} if department_id else None,
                )
            upsert_ticket(cursor, ticket, raw_payload_id, department_id, category_id)
            ingested_case_ids.append(case_enquiry_id)
    return ingested_case_ids


def get_watermark(conn) -> Watermark | None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT open_dt, case_enquiry_id
            FROM tickets
            WHERE open_dt IS NOT NULL
            ORDER BY open_dt DESC, case_enquiry_id DESC
            LIMIT 1
            """
        )
        row = cursor.fetchone()
    if not row:
        return None
    open_dt, case_enquiry_id = row
    if open_dt is None or case_enquiry_id is None:
        return None
    return Watermark(open_dt=open_dt, case_enquiry_id=int(case_enquiry_id))


def verify_ingest(database_url: str, case_enquiry_ids: list[int]) -> None:
    if not case_enquiry_ids:
        LOGGER.warning("No rows were ingested; skipping verification.")
        return

    id_list = list(dict.fromkeys(case_enquiry_ids))
    with psycopg2.connect(database_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM raw_311_tickets WHERE case_enquiry_id = ANY(%s)",
                (id_list,),
            )
            raw_count = int(cursor.fetchone()[0])

            cursor.execute(
                "SELECT count(*) FROM tickets WHERE case_enquiry_id = ANY(%s)",
                (id_list,),
            )
            ticket_count = int(cursor.fetchone()[0])

            cursor.execute(
                """
                SELECT count(*)
                FROM tickets
                WHERE case_enquiry_id = ANY(%s)
                  AND latitude IS NOT NULL
                  AND longitude IS NOT NULL
                  AND geo_point IS NULL
                """,
                (id_list,),
            )
            missing_geo_count = int(cursor.fetchone()[0])

            cursor.execute(
                """
                SELECT count(*)
                FROM tickets
                WHERE case_enquiry_id = ANY(%s)
                  AND raw_payload_id IS NULL
                """,
                (id_list,),
            )
            missing_lineage_count = int(cursor.fetchone()[0])

    expected = len(id_list)
    if raw_count != expected:
        raise RuntimeError(f"Verification failed: expected {expected} raw rows, found {raw_count}.")
    if ticket_count != expected:
        raise RuntimeError(f"Verification failed: expected {expected} ticket rows, found {ticket_count}.")
    if missing_geo_count:
        raise RuntimeError(f"Verification failed: {missing_geo_count} rows have coordinates but no geo_point.")
    if missing_lineage_count:
        raise RuntimeError(f"Verification failed: {missing_lineage_count} ticket rows are missing raw lineage.")

    with psycopg2.connect(database_url) as conn:
        with conn.cursor() as cursor:
            counts: dict[str, int] = {}
            for table in ("raw_311_tickets", "tickets", "departments", "categories"):
                cursor.execute(f"SELECT count(*) FROM {table}")
                counts[table] = int(cursor.fetchone()[0])
    LOGGER.info("Post-run table counts", extra=counts)


def retry_with_backoff(action, attempts: int, backoff_seconds: float):
    for attempt in range(1, attempts + 1):
        try:
            return action()
        except (requests.RequestException, psycopg2.OperationalError) as exc:
            if isinstance(exc, requests.HTTPError):
                status_code = getattr(exc.response, "status_code", None)
                if status_code not in {429, 500, 502, 503, 504}:
                    raise
            if attempt >= attempts:
                raise
            sleep_for = backoff_seconds * attempt
            LOGGER.warning(
                "Transient failure, retrying",
                extra={"attempt": attempt, "sleep_seconds": sleep_for, "error": str(exc)},
            )
            time.sleep(sleep_for)


def run_ingestion_cycle(config: IngestionConfig, session: requests.Session) -> list[int]:
    conn = psycopg2.connect(config.database_url)
    try:
        conn.autocommit = False
        if config.apply_schema:
            execute_schema(conn)

        watermark = None if config.full_refresh else get_watermark(conn)
        if watermark is None:
            LOGGER.info("No incremental watermark found; starting from the beginning of the source dataset.")
        else:
            LOGGER.info(
                "Using incremental watermark",
                extra={
                    "open_dt": watermark.open_dt.isoformat(),
                    "case_enquiry_id": watermark.case_enquiry_id,
                },
            )

        rows = fetch_ckan_rows(
            session,
            config.ckan_sql_endpoint,
            config.ckan_resource_id,
            config.page_size,
            watermark=watermark,
            max_rows=config.max_rows,
        )
        LOGGER.info("Fetched rows", extra={"count": len(rows)})

        case_ids = ingest_rows(conn, rows)
        conn.commit()
        return case_ids
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = load_config(args)

    session = build_http_session()

    def run_once() -> list[int]:
        return run_ingestion_cycle(config, session)

    case_ids = retry_with_backoff(run_once, config.retry_attempts, config.retry_backoff_seconds)
    verify_ingest(config.database_url, case_ids)
    LOGGER.info("Ingestion complete", extra={"rows": len(case_ids)})


if __name__ == "__main__":
    main()
