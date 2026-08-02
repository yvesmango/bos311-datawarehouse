# Architecture Notes

## Pipeline shape

1. Pull Boston 311 records from the CKAN `datastore_search_sql` endpoint.
2. Store the exact payload in `raw_311_tickets`.
3. Resolve departments and categories into lookup tables.
4. Upsert cleaned rows into `tickets`.
5. Preserve status changes in `ticket_status_history` as that layer is added.

## Operational rules

- Keep secrets in `.env`.
- Use `DATABASE_URL` for Supabase connectivity.
- Re-run the pipeline safely; it should update existing rows instead of duplicating them.
- Keep the schema idempotent so the warehouse can be rebuilt when needed.

## What is still pending

- A first ingestion run against Supabase.
- A small dashboard layer for public exploration.
- Additional analytics endpoints once the warehouse is stable.
