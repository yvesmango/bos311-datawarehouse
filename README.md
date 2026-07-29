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
3. Add a lightweight dashboard for exploring service requests.
