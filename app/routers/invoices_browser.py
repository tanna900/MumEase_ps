# app/routers/invoices_browser.py
from fastapi import APIRouter, Request, Query, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from sqlalchemy import or_, func
from datetime import datetime, date, time
from typing import Optional

from app.database import SessionLocal
from app import models
from fastapi.templating import Jinja2Templates

router = APIRouter(prefix="/invoices", tags=["Invoices (Browse)"])
templates = Jinja2Templates(directory="app/templates")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d").date()

def start_of_day(d: date) -> datetime:
    return datetime.combine(d, time.min)

def end_of_day(d: date) -> datetime:
    return datetime.combine(d, time.max)

@router.get("/browse", response_class=HTMLResponse)
def browse_invoices(
    request: Request,
    q: str | None = Query(None, description="بحث عام: اسم/موبايل/جزء من كود الفاتورة"),
    dfrom: str | None = Query(None),
    dto: str | None = Query(None),
    t: str | None = Query(None, description="نوع الفاتورة: S بيع / R مرتجع"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=10, le=500),
    db: Session = Depends(get_db),
):
    """
    يعرض كل الفواتير مع بحث حر:
    - q يطابق: customer_name / customer_phone / invoice_code (جزئي)
    - فلترة بالتاريخ (من/إلى) واختيار نوع الفاتورة (S/R)
    - ترتيب تنازلي بالتاريخ
    - Pagination بسيط
    """
    qry = db.query(models.Invoice)

    # فلترة النوع اختياري
    if t in ("S", "R"):
        qry = qry.filter(models.Invoice.type == t)

    # فلترة التاريخ اختيارية
    df = parse_date(dfrom)
    dt = parse_date(dto)
    if df:
        qry = qry.filter(models.Invoice.created_at >= start_of_day(df))
    if dt:
        qry = qry.filter(models.Invoice.created_at <= end_of_day(dt))

    # بحث عام (case-insensitive)
    if q:
        like = f"%{q.strip()}%"
        qry = qry.filter(
            or_(
                func.lower(models.Invoice.customer_name).like(func.lower(like)),
                func.lower(models.Invoice.customer_phone).like(func.lower(like)),
                func.lower(models.Invoice.invoice_code).like(func.lower(like)),
            )
        )

    # عدّ إجمالي النتائج قبل الـ pagination
    total_count = qry.count()

    # ترتيب وحدود الصفحة
    qry = qry.order_by(models.Invoice.created_at.desc(), models.Invoice.id.desc())
    offset = (page - 1) * page_size
    rows = qry.offset(offset).limit(page_size).all()

    # أرقام الصفحات
    total_pages = max(1, (total_count + page_size - 1) // page_size)

    ctx = {
        "request": request,
        "q": q or "",
        "dfrom": dfrom or "",
        "dto": dto or "",
        "t": t or "",
        "page": page,
        "page_size": page_size,
        "total_count": total_count,
        "total_pages": total_pages,
        "invoices": rows,
    }
    return templates.TemplateResponse("invoices_browse.html", ctx)