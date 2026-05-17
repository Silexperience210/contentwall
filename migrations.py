from lnbits.db import Connection


async def m001_initial(db: Connection):
    # Use IF NOT EXISTS everywhere so this migration is idempotent.
    # This matters when a previous install attempt (e.g. v1.0.6) crashed
    # mid-migration after partially creating tables; without IF NOT EXISTS
    # the retry would fail with "table already exists".
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
        "CREATE INDEX IF NOT EXISTS idx_contentwall_items_wallet ON items(wallet);"
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
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_contentwall_payments_hash ON payments(item_id, payment_hash);"
    )
