# SQL reference

- Authoritative schema documentation: [../docs/DATABASE.md](../docs/DATABASE.md)
- Add **Alembic** here (`sql/migrations`) when you are ready for versioned migrations.

## Bootstrap (local)

```bash
psql "$DATABASE_URL" -f docs/DATABASE.md
```

(Or paste the DDL from `DATABASE.md` into your migration tool.)
