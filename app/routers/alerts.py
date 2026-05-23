# app/routers/alerts.py
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session, aliased
from sqlalchemy import func, exists, and_

from app.database import SessionLocal
from app import models
from fastapi.templating import Jinja2Templates

router = APIRouter(tags=["Alerts"])
templates = Jinja2Templates(directory="app/templates")

# إعدادات
LOW_STOCK_THRESHOLD = 4         # حد تنبيه المخزون
DAYS_NO_ALLOCATION   = 10       # فواتير بيع مرّ عليها X يوم بدون تخصيص دفعات

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ---------- Helpers ----------
def _low_stock_list(db: Session, threshold:int=LOW_STOCK_THRESHOLD) -> List[Dict]:
    """أصناف منخفضة المخزون غير متجاهَلة."""
    P = models.Product
    D = models.DismissedAlert
    q = (
        db.query(P)
          .filter((P.stock == None) | (P.stock < int(threshold)))
          .filter(~exists().where(and_(D.kind == "low_stock", D.ref_id == P.id)))
          .order_by(P.stock.asc().nullsfirst(), P.name.asc())
    )
    out = []
    for p in q.all():
        out.append({
            "id": p.id,
            "name": f"{p.name}{f' ({p.size})' if p.size else ''}{f' {p.color}' if p.color else ''}",
            "stock": int(p.stock or 0),
        })
    return out

def _overdue_sales_without_allocations(db: Session, days:int=DAYS_NO_ALLOCATION) -> List[Dict]:
    """
    فواتير بيع S مرّ عليها +days يوم ولم تُخصّص لها أي دفعات شحن،
    مع استبعاد أي فاتورة بيع لها مرتجع (R) مربوط بـ original_sale_id،
    واستبعاد الفواتير المتجاهَلة.
    """
    Inv = models.Invoice
    Alloc = models.ShippingAllocation
    D = models.DismissedAlert
    cutoff = datetime.now() - timedelta(days=days)

    # مجموع التخصيصات لكل فاتورة
    alloc_sum_subq = (
        db.query(
            Alloc.invoice_id.label("inv_id"),
            func.coalesce(func.sum(Alloc.amount), 0.0).label("alloc_sum")
        )
        .group_by(Alloc.invoice_id)
        .subquery()
    )

    # alias لفواتير المرتجع
    Ret = aliased(Inv)

    q = (
        db.query(
            Inv.id,
            Inv.invoice_code,
            Inv.customer_name,
            Inv.customer_phone,
            Inv.created_at,
            Inv.total,
            func.coalesce(alloc_sum_subq.c.alloc_sum, 0.0).label("alloc_sum"),
        )
        .outerjoin(alloc_sum_subq, alloc_sum_subq.c.inv_id == Inv.id)
        .filter(
            Inv.type == "S",
            Inv.created_at <= cutoff,
            func.coalesce(alloc_sum_subq.c.alloc_sum, 0.0) == 0.0,
            ~exists().where(and_(Ret.type == "R", Ret.original_sale_id == Inv.id)),
            ~exists().where(and_(D.kind == "overdue", D.ref_id == Inv.id)),
        )
        .order_by(Inv.created_at.asc(), Inv.id.asc())
    )

    rows = q.all()
    out = []
    for r in rows:
        age_days = (datetime.now() - (r.created_at or datetime.now())).days
        out.append({
            "id": r.id,
            "code": r.invoice_code or f"INV-{r.id}",
            "customer": r.customer_name or "-",
            "phone": r.customer_phone or "-",
            "created_at": r.created_at,
            "age_days": age_days,
            "total": float(r.total or 0.0),
        })
    return out

def _count_all(db: Session) -> int:
    """إجمالي عدد التنبيهات (غير المتجاهَلة)."""
    return len(_low_stock_list(db)) + len(_overdue_sales_without_allocations(db))

# ---------- APIs للجرس ----------
@router.get("/alerts/summary")
def alerts_summary(db: Session = Depends(get_db)):
    """
    API مختصر للجرس في النافبار: يرجع عدد كل التنبيهات + 3 عناصر نصية اختيارية.
    """
    low_list = _low_stock_list(db)
    overdue_list = _overdue_sales_without_allocations(db)

    total = len(low_list) + len(overdue_list)

    items = []
    if overdue_list:
        items.append({
            "icon": "bi-clock-history",
            "text": f"{len(overdue_list)} فاتورة بيع أقدم من {DAYS_NO_ALLOCATION} يوم بدون تخصيص دفعات",
            "href": "/alerts#overdue"
        })
    if low_list:
        items.append({
            "icon": "bi-exclamation-triangle",
            "text": f"{len(low_list)} صنف تحت حد المخزون ({LOW_STOCK_THRESHOLD})",
            "href": "/alerts#lowstock"
        })

    return JSONResponse({"count": total, "items": items[:3]})

# ---------- صفحة التنبيهات ----------
@router.get("/alerts", response_class=HTMLResponse)
def alerts_page(request: Request, db: Session = Depends(get_db)):
    low_list = _low_stock_list(db)
    overdue_list = _overdue_sales_without_allocations(db)
    ctx = {
        "request": request,
        "low_list": low_list,
        "overdue_list": overdue_list,
        "LOW_STOCK_THRESHOLD": LOW_STOCK_THRESHOLD,
        "DAYS_NO_ALLOCATION": DAYS_NO_ALLOCATION,
    }
    return templates.TemplateResponse("alerts.html", ctx)

# ---------- تجاهل عنصر ----------
@router.post("/alerts/ignore")
def alerts_ignore(kind: str = Form(...), ref_id: int = Form(...), db: Session = Depends(get_db)):
    kind = (kind or "").strip()
    if kind not in ("low_stock", "overdue"):
        return RedirectResponse(url="/alerts?msg=invalid_kind", status_code=303)

    # لو متسجّل قبل كده، منكررهوش
    exists_q = db.query(models.DismissedAlert).filter(
        models.DismissedAlert.kind == kind,
        models.DismissedAlert.ref_id == int(ref_id)
    ).first()
    if not exists_q:
        db.add(models.DismissedAlert(kind=kind, ref_id=int(ref_id), created_at=datetime.now()))
        db.commit()
    return RedirectResponse(url="/alerts?msg=dismissed", status_code=303)

# ---------- تجاهل الكل (للنطاق الحالي الظاهر) ----------
@router.post("/alerts/ignore-all")
def alerts_ignore_all(kind: str = Form(...), db: Session = Depends(get_db)):
    kind = (kind or "").strip()
    if kind not in ("low_stock", "overdue"):
        return RedirectResponse(url="/alerts?msg=invalid_kind", status_code=303)

    if kind == "low_stock":
        current_ids = [p["id"] for p in _low_stock_list(db)]
    else:
        current_ids = [i["id"] for i in _overdue_sales_without_allocations(db)]

    if current_ids:
        # جيب الموجودين فعلًا عشان ما نكررهمش
        existing = set(
            r.ref_id for r in db.query(models.DismissedAlert.ref_id)
            .filter(models.DismissedAlert.kind == kind,
                    models.DismissedAlert.ref_id.in_(current_ids))
            .all()
        )
        for rid in current_ids:
            if rid not in existing:
                db.add(models.DismissedAlert(kind=kind, ref_id=int(rid), created_at=datetime.now()))
        db.commit()

    return RedirectResponse(url="/alerts?msg=dismissed_all", status_code=303)