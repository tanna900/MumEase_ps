from sqlalchemy.orm import Session
from app import models

def get_next_invoice_code(db: Session, t: str) -> str:
    # t in {"S","R"}
    key = f"seq_{t}"
    s = db.query(models.Setting).filter(models.Setting.key == key).first()
    if not s:
        s = models.Setting(key=key, value="1114")  # سيبدأ من 1115
        db.add(s); db.commit(); db.refresh(s)
    n = int(s.value) + 1
    s.value = str(n)
    db.commit()
    return f"INV {t}-{n}"