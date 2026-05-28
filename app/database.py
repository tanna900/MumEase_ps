from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

SQLALCHEMY_DATABASE_URL = "sqlite:///./pos.db"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False}
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

Base = declarative_base()

with engine.connect() as conn:
    try:
        conn.execute(text("ALTER TABLE products ADD COLUMN short_name TEXT"))
        conn.commit()
    except:
        pass

    for sql in (
        "ALTER TABLE invoices ADD COLUMN shopify_order_id TEXT",
        "ALTER TABLE invoices ADD COLUMN shopify_order_name TEXT",
        "ALTER TABLE invoices ADD COLUMN shopify_fulfilled_at DATETIME",
        "ALTER TABLE invoices ADD COLUMN shopify_paid_at DATETIME",
        "ALTER TABLE invoices ADD COLUMN shopify_sync_note TEXT",
    ):
        try:
            conn.execute(text(sql))
            conn.commit()
        except:
            pass
