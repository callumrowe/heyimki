"""
Database connection and migration runner.
On startup, applies any .sql files in /app/migrations/ not yet recorded
in the schema_migrations table.
"""
import asyncio
import os
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

DATABASE_URL = os.environ["DATABASE_URL"]

engine = create_async_engine(DATABASE_URL, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


async def run_migrations() -> None:
    for attempt in range(10):
        try:
            async with engine.begin() as conn:
                break
        except Exception as e:
            if attempt == 9:
                raise
            wait = 2 ** attempt
            print(f"Postgres not ready (attempt {attempt + 1}/10), retrying in {wait}s... ({e})")
            await asyncio.sleep(wait)

    async with engine.begin() as conn:
        # Ensure the tracking table exists
        await conn.execute(sa.text("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ DEFAULT now()
            )
        """))

        # Find applied versions
        result = await conn.execute(
            sa.text("SELECT version FROM schema_migrations")
        )
        applied = {row[0] for row in result}

    # Apply each pending migration in its own transaction so a bad migration
    # logs a warning and is skipped rather than crashing the entire app.
    pending = [
        path for path in sorted(MIGRATIONS_DIR.glob("*.sql"))
        if path.name not in applied
    ]

    if not pending:
        print("[migrations] All migrations already applied — nothing to do.")
    else:
        print(f"[migrations] {len(pending)} pending migration(s): {[p.name for p in pending]}")

    failed = []
    for path in pending:
        version = path.name
        print(f"[migrations] Applying: {version}")
        try:
            async with engine.begin() as mconn:
                for statement in path.read_text().split(";"):
                    statement = statement.strip()
                    if statement:
                        await mconn.execute(sa.text(statement))
                await mconn.execute(
                    sa.text("INSERT INTO schema_migrations (version) VALUES (:v)"),
                    {"v": version},
                )
            print(f"[migrations] Applied:  {version} ✓")
        except Exception as e:
            print(f"[migrations] ERROR: {version} failed and was skipped: {e}")
            failed.append(version)

    if failed:
        raise RuntimeError(
            f"[migrations] FATAL: {len(failed)} migration(s) failed — cannot start with broken schema: {failed}. "
            "Fix the SQL in the failing migration file and redeploy."
        )
    elif pending:
        print(f"[migrations] All done — {len(pending)} migration(s) applied successfully.")
