# run_once_add_users.py
from sqlalchemy import text
from app.database import engine
from app.auth import User, hash_password
from app.database import Base, SessionLocal

def run():
    # إنشاء جدول users
    Base.metadata.create_all(bind=engine)
    print("✅ تم إنشاء جدول users")

    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.username == "admin").first()
        if not existing:
            admin = User(
                username="admin",
                display_name="المدير",
                password_hash=hash_password("admin123"),
                is_admin=True,
                is_active=True,
                permissions="[]",
            )
            db.add(admin)
            db.commit()
            print("✅ تم إنشاء مستخدم admin — الباسورد: admin123")
            print("⚠️  غير الباسورد فوراً من صفحة تغيير الباسورد!")
        else:
            print("ℹ️  مستخدم admin موجود بالفعل")
    finally:
        db.close()

if __name__ == "__main__":
    run()
