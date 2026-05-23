# run_once_add_governorate.py
from sqlalchemy import text
from app.database import engine

def ensure_governorate_column():
    with engine.begin() as conn:
        # اقرأ أعمدة جدول invoices
        cols = [row[1] for row in conn.execute(text("PRAGMA table_info(invoices)"))]
        if "governorate" in cols:
            print("✅ 'governorate' موجود بالفعل في invoices.")
            return

        # أضف العمود (Null مسموح)
        conn.execute(text("ALTER TABLE invoices ADD COLUMN governorate VARCHAR"))
        print("✅ تم إضافة العمود 'governorate' لجدول invoices.")

if __name__ == "__main__":
    ensure_governorate_column()