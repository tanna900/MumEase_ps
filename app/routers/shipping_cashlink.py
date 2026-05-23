# app/routers/shipping_cashlink.py
from datetime import datetime
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from sqlalchemy import func
from fastapi.templating import Jinja2Templates

from app.database import SessionLocal
from app import models

router = APIRouter(prefix="/shipping/cash-sync", tags=["Shipping ↔ Cash"])
templates = Jinja2Templates(directory="app/templates")

# ---------- DB ----------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ---------- Helpers ----------
def _payment_is_mapped(db: Session, payment_id: int) -> bool:
    return db.query(models.ShippingPaymentCashMap).filter(
        models.ShippingPaymentCashMap.payment_id == payment_id
    ).first() is not None

def _create_cash_entry_for_payment(db: Session, pay: models.ShippingPayment) -> int:
    """
    ينشئ قيد خزنة (IN) لدفعة الشحن ويرجع finance_entry_id
    """
    cat = f"توريد شركات شحن - {pay.company or 'غير محدد'}"
    note = f"دفعة شحن #{pay.id}"
    fe = models.FinanceEntry(
        date=pay.date or datetime.now().strftime("%Y-%m-%d"),
        type="IN",
        category=cat,
        amount=float(pay.amount or 0),
        note=note,
        created_at=datetime.now()
    )
    db.add(fe); db.commit(); db.refresh(fe)
    return fe.id

# ---------- UI ----------
@router.get("", response_class=HTMLResponse)
def sync_page(
    request: Request,
    db: Session = Depends(get_db)
):
    """
    تعرض:
    - دفعات شحن غير مُرحّلة للخزنة
    - دفعات شحن مُرحّلة (مع إمكانية إلغاء قيّد الخزنة)
    """
    # غير مُرحّلة
    unlinked = db.query(models.ShippingPayment).filter(
        ~models.ShippingPayment.id.in_(
            db.query(models.ShippingPaymentCashMap.payment_id)
        )
    ).order_by(models.ShippingPayment.id.desc()).all()

    # مُرحّلة
    linked = db.query(models.ShippingPayment, models.ShippingPaymentCashMap, models.FinanceEntry).\
        join(models.ShippingPaymentCashMap, models.ShippingPaymentCashMap.payment_id == models.ShippingPayment.id).\
        join(models.FinanceEntry, models.FinanceEntry.id == models.ShippingPaymentCashMap.finance_entry_id).\
        order_by(models.ShippingPayment.id.desc()).all()

    total_unlinked = float(sum(float(r.amount or 0) for r in unlinked))
    total_linked = float(sum(float(p.amount or 0) for (p, _, __) in linked))

    ctx = {
        "request": request,
        "unlinked": unlinked,
        "linked": linked,
        "total_unlinked": total_unlinked,
        "total_linked": total_linked
    }
    return templates.TemplateResponse("shipping_cash_sync.html", ctx)

@router.post("/link-one")
def link_one(payment_id: int = Form(...), db: Session = Depends(get_db)):
    pay = db.query(models.ShippingPayment).get(int(payment_id))
    if not pay:
        return RedirectResponse(url="/shipping/cash-sync?msg=⚠️ دفعة غير موجودة", status_code=303)
    if _payment_is_mapped(db, pay.id):
        return RedirectResponse(url="/shipping/cash-sync?msg=ℹ️ الدفعة مُرحّلة بالفعل", status_code=303)

    fe_id = _create_cash_entry_for_payment(db, pay)
    db.add(models.ShippingPaymentCashMap(payment_id=pay.id, finance_entry_id=fe_id, created_at=datetime.now()))
    db.commit()
    return RedirectResponse(url="/shipping/cash-sync?msg=✓ تم ترحيل الدفعة للخزنة", status_code=303)

@router.post("/link-bulk")
def link_bulk(db: Session = Depends(get_db)):
    unlinked = db.query(models.ShippingPayment).filter(
        ~models.ShippingPayment.id.in_(db.query(models.ShippingPaymentCashMap.payment_id))
    ).all()
    count = 0
    for pay in unlinked:
        if float(pay.amount or 0) <= 0:
            continue
        fe_id = _create_cash_entry_for_payment(db, pay)
        db.add(models.ShippingPaymentCashMap(payment_id=pay.id, finance_entry_id=fe_id, created_at=datetime.now()))
        count += 1
    db.commit()
    return RedirectResponse(url=f"/shipping/cash-sync?msg=✓ تم ترحيل {count} دفعة للخزنة", status_code=303)

@router.post("/unlink")
def unlink(payment_id: int = Form(...), db: Session = Depends(get_db)):
    link = db.query(models.ShippingPaymentCashMap).filter(
        models.ShippingPaymentCashMap.payment_id == int(payment_id)
    ).first()
    if not link:
        return RedirectResponse(url="/shipping/cash-sync?msg=⚠️ لا يوجد ربط لهذه الدفعة", status_code=303)

    # احذف قيّد الخزنة المرتبط
    fe = db.query(models.FinanceEntry).get(int(link.finance_entry_id))
    if fe:
        db.delete(fe)
    db.delete(link)
    db.commit()
    return RedirectResponse(url="/shipping/cash-sync?msg=✓ تم إلغاء قيد الخزنة لهذه الدفعة", status_code=303)