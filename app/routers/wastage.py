# app/routers/wastage.py
"""
مخزون الهالك (Wastage)
======================
- صرف من المخزون → مخزون هالك  (transfer)
- إرجاع من الهالك → المخزون     (recover)

يحتاج عمود `wastage_stock` في جدول products — شوف migration_wastage.py
"""

from fastapi import APIRouter, Request, Depends, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import or_, func, text
from app.database import SessionLocal
from app import models
from datetime import datetime

router = APIRouter(prefix="/wastage", tags=["Wastage"])
templates = Jinja2Templates(directory="app/templates")


# ──────────────────────────────────────────
# DB helper
# ──────────────────────────────────────────
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _ensure_wastage_column(db: Session):
    """يتأكد إن عمود wastage_stock موجود — fallback لو المايجريشن ما اتشغلش."""
    try:
        db.execute(text(
            "ALTER TABLE products ADD COLUMN wastage_stock INTEGER NOT NULL DEFAULT 0"
        ))
        db.commit()
    except Exception:
        pass  # العمود موجود بالفعل


# ──────────────────────────────────────────
# Pages
# ──────────────────────────────────────────
@router.get("", response_class=HTMLResponse)
def wastage_page(
    request: Request,
    q: str = Query(""),
    msg: str = Query(""),
    db: Session = Depends(get_db),
):
    _ensure_wastage_column(db)

    q_str = (q or "").strip()
    query = db.query(models.Product)

    if q_str:
        query = query.filter(
            or_(
                models.Product.name.ilike(f"%{q_str}%"),
                models.Product.barcode.ilike(f"%{q_str}%"),
            )
        )

    products = query.order_by(models.Product.name.asc()).all()

    # إجمالي الهالك (عدد قطع + قيمة بالتكلفة)
    total_wastage_qty = sum(int(getattr(p, "wastage_stock", 0) or 0) for p in products)
    total_wastage_value = sum(
        int(getattr(p, "wastage_stock", 0) or 0) * float(getattr(p, "cost_price", 0) or 0)
        for p in products
    )

    ctx = {
        "request": request,
        "products": products,
        "q": q_str,
        "msg": msg,
        "total_wastage_qty": total_wastage_qty,
        "total_wastage_value": total_wastage_value,
    }
    return templates.TemplateResponse("wastage.html", ctx)


# ──────────────────────────────────────────
# Actions
# ──────────────────────────────────────────
@router.post("/transfer")
def transfer_to_wastage(
    product_id: int = Form(...),
    qty: int = Form(...),
    note: str = Form(""),
    q: str = Form(""),
    next: str = Form(""),
    db: Session = Depends(get_db),
):
    """
    صرف qty قطعة من stock → wastage_stock
    """
    _ensure_wastage_column(db)

    p = db.query(models.Product).get(product_id)
    if not p:
        return RedirectResponse(url=f"/wastage?msg=⚠️+المنتج+غير+موجود&q={q}", status_code=303)

    qty = max(1, int(qty or 1))
    current_stock = int(p.stock or 0)

    if qty > current_stock:
        return RedirectResponse(
            url=f"/wastage?msg=⚠️+الكمية+المطلوبة+({qty})+أكبر+من+المخزون+الحالي+({current_stock})&q={q}",
            status_code=303,
        )

    # خصم من المخزون وإضافة للهالك
    p.stock = current_stock - qty
    current_wastage = int(getattr(p, "wastage_stock", 0) or 0)
    p.wastage_stock = current_wastage + qty

    # سجّل في ProductMovement فقط (لا تأثير على حساب المصنع)
    if hasattr(models, "ProductMovement"):
        note_txt = f"[WASTAGE] تحويل للهالك — {note}".strip(" —")
        try:
            db.add(models.ProductMovement(
                product_id=p.id,
                movement_type="wastage",
                qty=-qty,
                balance_after=p.stock,
                ref_type="Wastage",
                note=note_txt,
            ))
        except Exception:
            pass

    db.commit()

    product_name = getattr(p, "name", f"#{product_id}")
    # لو جاي من صفحة المخزون، ارجع لها
    redirect_url = next.strip() if next.strip() else f"/wastage?msg=✅+تم+تحويل+{qty}+قطعة+للهالك&q={q}"
    return RedirectResponse(url=redirect_url, status_code=303)


@router.post("/recover")
def recover_from_wastage(
    product_id: int = Form(...),
    qty: int = Form(...),
    note: str = Form(""),
    q: str = Form(""),
    db: Session = Depends(get_db),
):
    """
    إرجاع qty قطعة من wastage_stock → stock
    """
    _ensure_wastage_column(db)

    p = db.query(models.Product).get(product_id)
    if not p:
        return RedirectResponse(url=f"/wastage?msg=⚠️+المنتج+غير+موجود&q={q}", status_code=303)

    qty = max(1, int(qty or 1))
    current_wastage = int(getattr(p, "wastage_stock", 0) or 0)

    if qty > current_wastage:
        return RedirectResponse(
            url=f"/wastage?msg=⚠️+الكمية+({qty})+أكبر+من+مخزون+الهالك+({current_wastage})&q={q}",
            status_code=303,
        )

    # خصم من الهالك وإضافة للمخزون
    p.wastage_stock = current_wastage - qty
    p.stock = int(p.stock or 0) + qty

    # سجّل في ProductMovement فقط (لا تأثير على حساب المصنع)
    if hasattr(models, "ProductMovement"):
        note_txt = f"[WASTAGE-RECOVER] استرداد من الهالك — {note}".strip(" —")
        try:
            db.add(models.ProductMovement(
                product_id=p.id,
                movement_type="wastage_recover",
                qty=qty,
                balance_after=p.stock,
                ref_type="Wastage",
                note=note_txt,
            ))
        except Exception:
            pass

    db.commit()

    product_name = getattr(p, "name", f"#{product_id}")
    return RedirectResponse(
        url=f"/wastage?msg=✅+تم+استرداد+{qty}+قطعة+من+هالك+({product_name})+للمخزون&q={q}",
        status_code=303,
    )