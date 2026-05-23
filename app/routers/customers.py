from typing import Optional
from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session
from sqlalchemy import func, case
from app.database import SessionLocal
from app import models
from fastapi.templating import Jinja2Templates
from datetime import datetime

router = APIRouter(prefix="/customers", tags=["Customers"])
templates = Jinja2Templates(directory="app/templates")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.get("/ledger", response_class=HTMLResponse)
def customers_ledger(
    request: Request,
    q: str = Query("", description="فلترة بالاسم أو الهاتف (اختياري)"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """
    سجل العملاء المجمّع من جدول الفواتير (group by name+phone) + ترقيم صفحات
    """
    # CASE expressions
    sale_cnt = func.sum(case((models.Invoice.type == "S", 1), else_=0)).label("sale_count")
    sale_sum = func.sum(case((models.Invoice.type == "S", models.Invoice.total), else_=0)).label("sale_total")
    ret_cnt  = func.sum(case((models.Invoice.type == "R", 1), else_=0)).label("ret_count")
    ret_sum  = func.sum(case((models.Invoice.type == "R", models.Invoice.total), else_=0)).label("ret_total")

    base_q = (
        db.query(
            models.Invoice.customer_name.label("name"),
            models.Invoice.customer_phone.label("phone"),
            sale_cnt, sale_sum, ret_cnt, ret_sum
        )
        .group_by(models.Invoice.customer_name, models.Invoice.customer_phone)
    )

    if q:
        like = f"%{q}%"
        base_q = base_q.having(
            (models.Invoice.customer_name.like(like)) | (models.Invoice.customer_phone.like(like))
        )

    # إجمالي عدد العملاء (بعد الفلترة)
    total_rows = db.query(func.count()).select_from(base_q.subquery()).scalar() or 0

    # ترتيب + صفحة
    offset = (page - 1) * per_page
    rows_page = (
        base_q
        .order_by(func.coalesce(sale_sum, 0).desc(), models.Invoice.customer_name.asc())
        .offset(offset)
        .limit(per_page)
        .all()
    )

    # جهّز صفوف العرض
    data = []
    for i, r in enumerate(rows_page, start=1):
        data.append({
            "idx": offset + i,  # ترقيم متسلسل عبر الصفحات
            "name": r.name or "-",
            "phone": r.phone or "-",
            "sale_count": int(r.sale_count or 0),
            "sale_total": float(r.sale_total or 0),
            "ret_count": int(r.ret_count or 0),
            "ret_total": float(r.ret_total or 0),
        })

    pages_count = (total_rows + per_page - 1) // per_page if per_page else 1

    return templates.TemplateResponse("customer_ledger.html", {
        "request": request,
        "q": q,
        "rows": data,
        "page": page,
        "per_page": per_page,
        "total_rows": total_rows,
        "pages_count": pages_count,
    })

@router.get("/ledger/detail", response_class=HTMLResponse)
def customer_detail(
    request: Request,
    name: str = Query(...),
    phone: str = Query(...),
    db: Session = Depends(get_db),
):
    """
    تفاصيل معاملات عميل واحد (كل فواتير البيع والمرتجع)
    """
    invs = (
        db.query(models.Invoice)
        .filter(
            models.Invoice.customer_name == name,
            models.Invoice.customer_phone == phone,
        )
        .order_by(models.Invoice.created_at.desc(), models.Invoice.id.desc())
        .all()
    )

    # تقسيم لبيع / مرتجع + إجماليات
    sales = [i for i in invs if i.type == "S"]
    rets  = [i for i in invs if i.type == "R"]

    ctx = {
        "request": request,
        "name": name,
        "phone": phone,
        "sales": sales,
        "rets": rets,
        "sales_count": len(sales),
        "sales_sum": sum(float(i.total or 0) for i in sales),
        "rets_count": len(rets),
        "rets_sum": sum(float(i.total or 0) for i in rets),
    }
    return templates.TemplateResponse("customer_detail.html", ctx)

# =============== تصدير CSV ===============
@router.get("/ledger/export")
def customers_ledger_export(
    q: str = Query("", description="فلترة بالاسم أو الهاتف (اختياري)"),
    db: Session = Depends(get_db),
):
    """
    تصدير CSV لكل البيانات المطابقة للفلتر (بدون تقطيع صفحات)
    """
    sale_cnt = func.sum(case((models.Invoice.type == "S", 1), else_=0)).label("sale_count")
    sale_sum = func.sum(case((models.Invoice.type == "S", models.Invoice.total), else_=0)).label("sale_total")
    ret_cnt  = func.sum(case((models.Invoice.type == "R", 1), else_=0)).label("ret_count")
    ret_sum  = func.sum(case((models.Invoice.type == "R", models.Invoice.total), else_=0)).label("ret_total")

    qry = (
        db.query(
            models.Invoice.customer_name.label("name"),
            models.Invoice.customer_phone.label("phone"),
            sale_cnt, sale_sum, ret_cnt, ret_sum
        )
        .group_by(models.Invoice.customer_name, models.Invoice.customer_phone)
    )

    if q:
        like = f"%{q}%"
        qry = qry.having(
            (models.Invoice.customer_name.like(like)) | (models.Invoice.customer_phone.like(like))
        )

    rows = (
        qry
        .order_by(func.coalesce(sale_sum, 0).desc(), models.Invoice.customer_name.asc())
        .all()
    )

    # جهّز CSV (UTF-8 BOM)
    lines = []
    lines.append("الاسم,الموبايل,عدد_فواتير_البيع,إجمالي_البيع,عدد_المرتجعات,إجمالي_المرتجعات,صافي_المبيعات\n")
    for r in rows:
        sale_total = float(r.sale_total or 0)
        ret_total  = float(r.ret_total or 0)
        net = sale_total - ret_total
        lines.append(f"\"{(r.name or '').replace('\"','')}\",{r.phone or ''},{int(r.sale_count or 0)},{sale_total:.2f},{int(r.ret_count or 0)},{ret_total:.2f},{net:.2f}\n")

    csv_data = ("\ufeff" + "".join(lines)).encode("utf-8")
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    headers = {
        "Content-Disposition": f'attachment; filename="customers_ledger_{ts}.csv"',
        "Cache-Control": "no-store",
    }
    return Response(content=csv_data, media_type="text/csv; charset=utf-8", headers=headers)