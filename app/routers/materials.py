# app/routers/materials.py
from fastapi import APIRouter, Request, Depends, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, text, inspect
from datetime import datetime as _dt

from app.database import SessionLocal
from app import models
from fastapi.templating import Jinja2Templates

router = APIRouter(prefix="/materials", tags=["Materials"])
templates = Jinja2Templates(directory="app/templates")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

_MAT_MIGRATED = False

def _ensure_materials_schema(db: Session):
    """Auto create table لو مش موجودة (مفيد للـ sqlite)."""
    global _MAT_MIGRATED
    if _MAT_MIGRATED:
        return

    try:
        engine = db.get_bind()
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        if "production_material_entries" not in tables:
            # أغلب استخدامك sqlite، فهنعمل CREATE TABLE بشكل آمن
            db.execute(text("""
            CREATE TABLE IF NOT EXISTS production_material_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT,
                type TEXT,
                amount REAL DEFAULT 0,
                note TEXT,
                finance_entry_id INTEGER,
                ref_type TEXT,
                ref_id INTEGER,
                created_at TEXT
            )
            """))
            db.execute(text("CREATE INDEX IF NOT EXISTS ix_pme_date ON production_material_entries(date)"))
            db.execute(text("CREATE INDEX IF NOT EXISTS ix_pme_type ON production_material_entries(type)"))
            db.execute(text("CREATE INDEX IF NOT EXISTS ix_pme_ref ON production_material_entries(ref_type, ref_id)"))
            db.execute(text("CREATE INDEX IF NOT EXISTS ix_pme_fin ON production_material_entries(finance_entry_id)"))
            db.commit()

        _MAT_MIGRATED = True
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        _MAT_MIGRATED = True

def _today():
    return _dt.now().strftime("%Y-%m-%d")

def _materials_balance(db: Session) -> float:
    _ensure_materials_schema(db)

    ins = float(
        db.query(func.coalesce(func.sum(models.ProductionMaterialEntry.amount), 0.0))
          .filter(models.ProductionMaterialEntry.type == "IN")
          .scalar() or 0.0
    )
    outs = float(
        db.query(func.coalesce(func.sum(models.ProductionMaterialEntry.amount), 0.0))
          .filter(models.ProductionMaterialEntry.type == "OUT")
          .scalar() or 0.0
    )
    return round(ins - outs, 2)

@router.get("", response_class=HTMLResponse)
def materials_home(
    request: Request,
    q: str = Query(""),
    date_from: str = "",
    date_to: str = "",
    page: int = Query(1, ge=1),
    per_page: int = Query(15, ge=1, le=200),
    db: Session = Depends(get_db),
):
    _ensure_materials_schema(db)

    base_q = db.query(models.ProductionMaterialEntry)

    if q:
        like = f"%{q}%"
        base_q = base_q.filter(models.ProductionMaterialEntry.note.like(like))

    if date_from:
        base_q = base_q.filter(models.ProductionMaterialEntry.date >= date_from)
    if date_to:
        base_q = base_q.filter(models.ProductionMaterialEntry.date <= date_to)

    total_count = base_q.count()
    pages = max(1, (total_count + per_page - 1) // per_page)
    page = min(max(1, page), pages)
    offset = (page - 1) * per_page

    rows = (base_q
            .order_by(models.ProductionMaterialEntry.date.desc(), models.ProductionMaterialEntry.id.desc())
            .offset(offset).limit(per_page).all())

    bal = _materials_balance(db)

    return templates.TemplateResponse("materials.html", {
        "request": request,
        "rows": rows,
        "balance": bal,
        "q": q,
        "date_from": date_from,
        "date_to": date_to,
        "page": page,
        "per_page": per_page,
        "pages": pages,
        "total_count": total_count,
    })

@router.post("/transfer-from-cash")
def transfer_from_cash(
    amount: float = Form(...),
    date: str = Form(""),
    note: str = Form(""),
    db: Session = Depends(get_db),
):
    """
    تحويل من الخزنة -> خامات انتاج:
      FinanceEntry: OUT category="خامات انتاج"
      ProductionMaterialEntry: IN
    """
    _ensure_materials_schema(db)

    try:
        amt = float(amount or 0)
    except Exception:
        amt = 0

    if amt <= 0:
        return RedirectResponse(url="/materials?msg=⚠️ المبلغ غير صحيح", status_code=303)

    if not date:
        date = _today()

    # 1) OUT من الخزنة
    fe = models.FinanceEntry(
        date=date,
        type="OUT",
        category="خامات انتاج",
        amount=amt,
        note=(note or "تحويل إلى خامات انتاج").strip() or None
    )
    db.add(fe)
    db.flush()

    # 2) IN في خامات انتاج
    me = models.ProductionMaterialEntry(
        date=date,
        type="IN",
        amount=amt,
        note=(note or "تحويل من الخزنة").strip() or None,
        finance_entry_id=int(fe.id),
        ref_type="Cash",
        ref_id=int(fe.id),
    )
    db.add(me)

    db.commit()
    return RedirectResponse(url="/materials?msg=✓ تم التحويل من الخزنة لخامات الانتاج", status_code=303)

@router.post("/adjust")
def materials_adjust(
    type: str = Form(...),   # IN/OUT
    amount: float = Form(...),
    date: str = Form(""),
    note: str = Form(""),
    db: Session = Depends(get_db),
):
    _ensure_materials_schema(db)

    if type not in ("IN", "OUT"):
        return RedirectResponse(url="/materials?msg=⚠️ نوع الحركة غير صحيح", status_code=303)

    try:
        amt = float(amount or 0)
    except Exception:
        amt = 0

    if amt <= 0:
        return RedirectResponse(url="/materials?msg=⚠️ المبلغ غير صحيح", status_code=303)

    if not date:
        date = _today()

    db.add(models.ProductionMaterialEntry(
        date=date, type=type, amount=amt,
        note=(note or "").strip() or None
    ))
    db.commit()
    return RedirectResponse(url="/materials?msg=✓ تم تسجيل الحركة", status_code=303)

@router.post("/delete")
def materials_delete(eid: int = Form(...), db: Session = Depends(get_db)):
    _ensure_materials_schema(db)
    row = db.query(models.ProductionMaterialEntry).get(int(eid))
    if row:
        db.delete(row)
        db.commit()
    return RedirectResponse(url="/materials?msg=✓ تم الحذف", status_code=303)