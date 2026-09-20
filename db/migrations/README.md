# Database migrations

`db/schema.sql` is the bootstrap baseline used by `db:init`. Schema changes after that baseline must be added here as immutable, ordered SQL migrations.

Naming:

```text
0001_baseline.sql
0002_short_description.sql
0003_another_change.sql
```

Rules:

- Never edit a migration after it has been released; applied-file checksums are verified.
- Keep migrations forward-only and safe to run exactly once.
- Prefer one structural change per file. MySQL implicitly commits many DDL statements, so a failed multi-DDL file can leave partial structural changes even though the migration version is not recorded.
- Update application code and migration SQL together; do not rely on `CREATE TABLE IF NOT EXISTS` in `schema.sql` to upgrade an existing database.
- Use `yahoo-stock-mcp db:migrate` for an existing database. `db:init` applies the bootstrap schema and then all pending migrations.
