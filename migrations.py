from lnbits.db import Connection


async def m001_initial(db: Connection):
    """Initial schema (idempotent)."""
    await db.execute(
        f"""
        CREATE TABLE IF NOT EXISTS contentwall.items (
            id TEXT PRIMARY KEY,
            wallet TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            content_type TEXT NOT NULL,
            content_hash TEXT,
            amount {db.big_int} NOT NULL,
            currency TEXT NOT NULL DEFAULT 'sat',
            memo TEXT NOT NULL,
            remembers INTEGER DEFAULT 1,
            release_delay_seconds INTEGER DEFAULT 0,
            scheduled_at TEXT,
            onion_hostname TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT {db.timestamp_now}
        );
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_contentwall_items_wallet "
        "ON contentwall.items(wallet);"
    )

    await db.execute(
        f"""
        CREATE TABLE IF NOT EXISTS contentwall.payments (
            id TEXT PRIMARY KEY,
            item_id TEXT NOT NULL,
            payment_hash TEXT NOT NULL,
            amount_paid {db.big_int} NOT NULL,
            paid_at TIMESTAMP,
            created_at TIMESTAMP NOT NULL DEFAULT {db.timestamp_now}
        );
        """
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_contentwall_payments_hash "
        "ON contentwall.payments(item_id, payment_hash);"
    )


async def _column_exists(db: Connection, table: str, column: str) -> bool:
    """
    Cross-DB column existence check.
    LNbits supports both SQLite and Postgres. PRAGMA is SQLite-only; the
    information_schema fallback handles Postgres.
    """
    try:
        rows = await db.fetchall(f"PRAGMA table_info({table});")
        if rows:
            return any(r["name"] == column for r in rows)
    except Exception:
        pass

    try:
        row = await db.fetchone(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'contentwall'
              AND table_name = :table
              AND column_name = :column
            """,
            {"table": table, "column": column},
        )
        return row is not None
    except Exception:
        return False


async def _add_column_if_missing(
    db: Connection, table: str, column: str, ddl_type: str
) -> None:
    """Idempotent ALTER TABLE ... ADD COLUMN."""
    if await _column_exists(db, table, column):
        return
    await db.execute(
        f"ALTER TABLE contentwall.{table} ADD COLUMN {column} {ddl_type};"
    )


async def m002_extended_fields(db: Connection):
    """
    v1.1.0 extended schema. New columns introduced for:
      * Teaser preview          -> teaser_text, teaser_blur
      * Soft delete / archival  -> archived_at
      * Per-purchase access     -> access_duration_seconds, expires_at
      * Per-purchase view count -> views_count, max_views
      * Signed access URLs HMAC -> access_signing_key
      * Outbound webhooks       -> webhook_url
      * Multi-file bundles      -> item_files table
    """
    await _add_column_if_missing(db, "items", "teaser_text", "TEXT")
    await _add_column_if_missing(db, "items", "teaser_blur", "INTEGER DEFAULT 1")
    await _add_column_if_missing(db, "items", "archived_at", "TIMESTAMP")
    await _add_column_if_missing(
        db, "items", "access_duration_seconds", "INTEGER DEFAULT 0"
    )
    await _add_column_if_missing(db, "items", "access_signing_key", "TEXT")
    await _add_column_if_missing(db, "items", "webhook_url", "TEXT")
    await _add_column_if_missing(db, "items", "max_views", "INTEGER DEFAULT 0")
    await _add_column_if_missing(db, "payments", "expires_at", "TIMESTAMP")
    await _add_column_if_missing(db, "payments", "views_count", "INTEGER DEFAULT 0")

    await db.execute(
        f"""
        CREATE TABLE IF NOT EXISTS contentwall.item_files (
            id TEXT PRIMARY KEY,
            item_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            content_type TEXT NOT NULL,
            size {db.big_int} NOT NULL DEFAULT 0,
            content_hash TEXT,
            position INTEGER DEFAULT 0,
            created_at TIMESTAMP NOT NULL DEFAULT {db.timestamp_now}
        );
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_contentwall_files_item "
        "ON contentwall.item_files(item_id, position);"
    )


async def m003_distribution_features(db: Connection):
    """
    v1.2.0 schema additions:
      * Markdown rendering toggle -> items.markdown
      * Tip jar (post-purchase)   -> items.allow_tips + new table tips
      * Discount codes / coupons  -> new table coupons + payments.coupon_code
      * Embed widget tracking     -> nothing in DB
      * Audio / video content     -> content_type='audio'|'video' string
    """
    await _add_column_if_missing(db, "items", "markdown", "INTEGER DEFAULT 0")
    await _add_column_if_missing(db, "items", "allow_tips", "INTEGER DEFAULT 1")
    await _add_column_if_missing(db, "payments", "coupon_code", "TEXT")

    await db.execute(
        f"""
        CREATE TABLE IF NOT EXISTS contentwall.coupons (
            id TEXT PRIMARY KEY,
            item_id TEXT NOT NULL,
            code TEXT NOT NULL,
            discount_percent INTEGER DEFAULT 0,
            discount_fixed_sats {db.big_int} DEFAULT 0,
            uses_remaining INTEGER DEFAULT -1,
            uses_count INTEGER DEFAULT 0,
            expires_at TIMESTAMP,
            created_at TIMESTAMP NOT NULL DEFAULT {db.timestamp_now}
        );
        """
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_contentwall_coupons_code "
        "ON contentwall.coupons(item_id, code);"
    )

    await db.execute(
        f"""
        CREATE TABLE IF NOT EXISTS contentwall.tips (
            id TEXT PRIMARY KEY,
            item_id TEXT NOT NULL,
            paywall_payment_hash TEXT,
            tip_payment_hash TEXT NOT NULL,
            amount_sats {db.big_int} NOT NULL,
            paid_at TIMESTAMP,
            created_at TIMESTAMP NOT NULL DEFAULT {db.timestamp_now}
        );
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_contentwall_tips_item "
        "ON contentwall.tips(item_id);"
    )
