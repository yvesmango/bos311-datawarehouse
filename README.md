# BOS311 Data Warehouse

Boston 311 civic data warehouse inspired by The Public Ledger.

## What this is

This project turns Boston 311 service request data into a clean, queryable warehouse that can support both public-facing maps and analytics-ready datasets.

## Current baseline

- Python project scaffolded with `uv`
- Local secrets kept in `.env`
- Git repository initialized locally on `main`

## Next milestones

1. Build the ingestion pipeline from the Boston CKAN API.
2. Load data into PostgreSQL/Supabase.
3. Stand up Metabase as the primary exploration layer.

## Exploring with Metabase

See [`docs/metabase-setup.md`](docs/metabase-setup.md) for the local Docker command,
Supabase Session Pooler connection format, and the five starter questions in the
"BOS311 Explorer" collection.

The warehouse remains the source of truth; Metabase is the read-only analysis
surface for maps, charts, tables, and future embeds.
