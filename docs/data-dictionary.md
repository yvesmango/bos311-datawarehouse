# Data Dictionary

## raw_311_tickets

- `id`: surrogate primary key
- `case_enquiry_id`: raw external ticket identifier
- `payload`: exact CKAN JSON record
- `ingested_at`: timestamp for the ingest event

## departments

- `id`: surrogate primary key
- `name`: canonical department name
- `created_at`, `updated_at`: audit timestamps

## categories

- `id`: surrogate primary key
- `name`: canonical category name
- `department_id`: optional owning department
- `created_at`, `updated_at`: audit timestamps

## tickets

- `case_enquiry_id`: natural key used for upserts
- `raw_payload_id`: lineage back to the raw payload
- `department_id`, `category_id`: normalized joins
- `geo_point`: PostGIS point for map rendering and spatial analysis
- `open_dt`, `closed_dt`, `sla_target_dt`, `due_date`: timeline fields
- `on_time`: SLA compliance flag
