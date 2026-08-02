# Metabase Setup for BOS311 Explorer

This project now uses Metabase as the primary exploration layer for Boston 311
analytics. The warehouse remains the source of truth in Supabase/PostgreSQL.

## Local Docker setup

Start a local Metabase instance with Docker:

```bash
docker run -d \
  --name bos311-metabase \
  -p 3000:3000 \
  -v bos311-metabase-data:/metabase-data \
  -e MB_DB_FILE=/metabase-data/metabase.db \
  metabase/metabase:latest
```

Open Metabase at `http://localhost:3000`.

## Supabase Session Pooler connection

Use the Supabase Session Pooler when Metabase connects to the database.

Connection string format:

```text
postgresql://postgres.<project-ref>:[YOUR-PASSWORD]@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require
```

Recommended environment variables:

```bash
export SUPABASE_DB_HOST=aws-0-ca-central-1.pooler.supabase.com
export SUPABASE_DB_PORT=5432
export SUPABASE_DB_NAME=postgres
export SUPABASE_DB_USER=postgres.<project-ref>
export SUPABASE_DB_PASSWORD=***
export SUPABASE_DB_SSLMODE=require
```

In Metabase, enter the same values through the PostgreSQL database connection
form. Keep the password in a local `.env` file or platform secret store. Never
commit credentials.

## BOS311 Explorer collection

Create a collection named `BOS311 Explorer` and save these five questions in it:

1. `Map: Open Tickets by Location`
   - Filter: `case_status = 'open'`
   - Visualization: map with pin markers
   - Color by: `case_title` or `department`

2. `Bar Chart: Avg Response Time by City Council District`
   - Metric: `AVG(closed_dt - open_dt)` in hours
   - Group by: `city_council_district`
   - Filter: `case_status = 'closed'` and `closed_dt IS NOT NULL`

3. `Time Series: Ticket Volume Over Time`
   - X-axis: `open_dt` grouped by week
   - Y-axis: count of `case_enquiry_id`
   - Filter: last 90 days

4. `Table: Late Tickets (SLA Violations)`
   - Filter: `on_time = false` and `case_status = 'open'`
   - Columns: `case_enquiry_id`, `case_title`, `neighborhood`, `open_dt`, `sla_target_dt`
   - Sort: `open_dt DESC`

5. `Donut Chart: Tickets by Department`
   - Join: `tickets` -> `departments`
   - Group by: `department.name`
   - Metric: count of tickets

## Configuration screenshots

Capture the native Metabase configuration screen for each question and store the
images under `docs/assets/metabase/`:

- `docs/assets/metabase/01-map-open-tickets.png`
- `docs/assets/metabase/02-bar-response-by-district.png`
- `docs/assets/metabase/03-time-series-volume.png`
- `docs/assets/metabase/04-table-late-tickets.png`
- `docs/assets/metabase/05-donut-by-department.png`

These screenshots are intentionally left as placeholders for manual capture so
you can decide the exact visual state to document. When the images exist, embed
them below:

<!--
![Map: Open Tickets by Location](./assets/metabase/01-map-open-tickets.png)
![Bar Chart: Avg Response Time by City Council District](./assets/metabase/02-bar-response-by-district.png)
![Time Series: Ticket Volume Over Time](./assets/metabase/03-time-series-volume.png)
![Table: Late Tickets (SLA Violations)](./assets/metabase/04-table-late-tickets.png)
![Donut Chart: Tickets by Department](./assets/metabase/05-donut-by-department.png)
-->

## Embedding dashboards later

For a future public-facing site:

1. Keep the dashboard in a locked-down Metabase collection.
2. Use Metabase public embedding or signed embedding, depending on the
   deployment model.
3. Create a read-only database user or read-only connection in Supabase.
4. Restrict embedded dashboards to the curated BOS311 questions only.
5. Store embed secrets in environment variables or platform secrets.

## Notes

- Metabase should only read from the warehouse.
- Keep local Docker state out of git.
- Prefer the Session Pooler for this project’s long-lived BI connection.
