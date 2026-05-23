from fastapi import APIRouter, Request, Depends, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session
from sqlalchemy import func, or_
from datetime import date, datetime, time
from typing import Optional, List, Dict

from app.database import SessionLocal
from app import models
from fastapi.templating import Jinja2Templates

router = APIRouter(prefix="/marketers", tags=["Marketers"])
templates = Jinja2Templates(directory="app/templates")


# ---------- DB ----------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------- Date helpers ----------
def parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d").date()

def start_of_day(d: date) -> datetime:
    return datetime.combine(d, time.min)

def end_of_day(d: date) -> datetime:
    return datetime.combine(d, time.max)


# =========================================================
# إدارة المسوّقين: عرض / إضافة / تعديل / حذف
# =========================================================

@router.get("", response_class=HTMLResponse)
def marketers_list(request: Request, db: Session = Depends(get_db)):
    rows = db.query(models.Marketer).order_by(models.Marketer.name.asc()).all()
    return templates.TemplateResponse("marketers_list.html", {"request": request, "rows": rows})

@router.post("/add")
def marketers_add(
    name: str = Form(...),
    commission_rate: float = Form(0.0),
    db: Session = Depends(get_db),
):
    name = (name or "").strip()
    if not name:
        return RedirectResponse(url="/marketers?msg=⚠️ الاسم مطلوب", status_code=303)
    m = models.Marketer(name=name, commission_rate=float(commission_rate or 0))
    db.add(m); db.commit()
    return RedirectResponse(url="/marketers?msg=✓ تم الإضافة", status_code=303)

@router.post("/update")
def marketers_update(
    mid: int = Form(...),
    name: str = Form(...),
    commission_rate: float = Form(0.0),
    db: Session = Depends(get_db),
):
    m = db.query(models.Marketer).get(int(mid))
    if not m:
        return RedirectResponse(url="/marketers?msg=⚠️ غير موجود", status_code=303)
    m.name = (name or "").strip()
    m.commission_rate = float(commission_rate or 0)
    db.commit()
    return RedirectResponse(url="/marketers?msg=✓ تم الحفظ", status_code=303)

@router.post("/delete")
def marketers_delete(mid: int = Form(...), db: Session = Depends(get_db)):
    m = db.query(models.Marketer).get(int(mid))
    if not m:
        return RedirectResponse(url="/marketers?msg=⚠️ غير موجود", status_code=303)
    db.delete(m); db.commit()
    return RedirectResponse(url="/marketers?msg=✓ تم الحذف", status_code=303)


# =========================================================
# حساب العمولات + عرض دفعات المسوّق + التصدير CSV
# =========================================================

def _note_matches_marketer(note: str, marketer_id: int, marketer_name: str) -> bool:
    """
    يربط قيد الخزنة بالمسوّق بإحدى طريقتين:
      1) لو الملاحظة تحتوي التوكن: [MKT:<id>]
      2) أو الملاحظة تحتوي اسم المسوّق كنص (case-insensitive)
    """
    if not note:
        return False
    try:
        n = note.strip()
    except Exception:
        n = str(note or "")
    token = f"[MKT:{int(marketer_id)}]"
    if token in n:
        return True
    # طابق بالاسم كخطة احتياطية
    return (marketer_name or "").lower() in n.lower()

@router.get("/commission", response_class=HTMLResponse)
def marketers_commission(
    request: Request,
    marketer_id: Optional[int] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    # نستقبلها كنص ونفس المنطق تحت
    override_rate: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    marketers = db.query(models.Marketer).order_by(models.Marketer.name.asc()).all()

    calc = None
    marketer = None
    payments: List[Dict] = []
    payments_total = 0.0
    net_after_payments = None

    if marketer_id:
        marketer = db.query(models.Marketer).get(int(marketer_id))

        df = parse_date(date_from)
        dt = parse_date(date_to)
        s_dt = start_of_day(df) if df else None
        e_dt = end_of_day(dt) if dt else None

        # فواتير بيع تخص المسوّق (مستخدمة لاحقًا لضم المرتجعات القديمة حتى لو marketer_id فيها NULL)
        sales_q = db.query(models.Invoice.id).filter(
            models.Invoice.type == "S",
            models.Invoice.marketer_id == marketer_id
        )
        if s_dt:
            sales_q = sales_q.filter(models.Invoice.created_at >= s_dt)
        if e_dt:
            sales_q = sales_q.filter(models.Invoice.created_at <= e_dt)

        # إجمالي مبيعات (S)
        inv_filters_sales = [models.Invoice.type == "S", models.Invoice.marketer_id == marketer_id]
        if s_dt: inv_filters_sales.append(models.Invoice.created_at >= s_dt)
        if e_dt: inv_filters_sales.append(models.Invoice.created_at <= e_dt)

        sales_total = float(
            db.query(func.coalesce(func.sum(models.Invoice.total), 0.0)).filter(*inv_filters_sales).scalar() or 0.0
        )

        # مصاريف الشحن الفعلية على المبيعات (S)
        shipping_total = float(
            db.query(func.coalesce(func.sum(models.Invoice.actual_shipping_cost), 0.0)).filter(*inv_filters_sales).scalar() or 0.0
        )

        # ==== المرتجعات (R): marketer_id مباشر OR الأصل من مبيعاته ====
        inv_filters_returns = [models.Invoice.type == "R"]
        if s_dt: inv_filters_returns.append(models.Invoice.created_at >= s_dt)
        if e_dt: inv_filters_returns.append(models.Invoice.created_at <= e_dt)

        returns_total = float(
            db.query(func.coalesce(func.sum(models.Invoice.total), 0.0))
              .filter(*inv_filters_returns)
              .filter(
                  (models.Invoice.marketer_id == marketer_id) |
                  (models.Invoice.original_sale_id.in_(sales_q.subquery()))
              )
              .scalar() or 0.0
        )

        returns_ship_fees = float(
            db.query(func.coalesce(func.sum(models.Invoice.return_shipping_fee), 0.0))
              .filter(*inv_filters_returns)
              .filter(
                  (models.Invoice.marketer_id == marketer_id) |
                  (models.Invoice.original_sale_id.in_(sales_q.subquery()))
              )
              .scalar() or 0.0
        )
        # ==========================================

        base = sales_total - shipping_total - returns_total - returns_ship_fees

        # نسبة العمولة
        if override_rate is not None and str(override_rate).strip() != "":
            try:
                rate = float(override_rate)
            except ValueError:
                rate = float((marketer and marketer.commission_rate) or 0.0)
        else:
            rate = float((marketer and marketer.commission_rate) or 0.0)

        commission = (base * rate / 100.0) if base > 0 else 0.0

        class _Calc:  # هيكل بسيط للعرض في الجينجا
            pass
        c = _Calc()
        c.sales_total = round(sales_total, 2)
        c.returns_total = round(returns_total, 2)
        c.shipping_total = round(shipping_total, 2)
        c.returns_ship_fees = round(returns_ship_fees, 2)
        c.base = round(base, 2)
        c.rate = round(rate, 2)
        c.commission = round(commission, 2)
        calc = c

        # ====== تجميع الدفعات من الخزنة (OUT / category = عموله تسويق) ======
        # ملاحظة: FinanceEntry.date عبارة عن نص "YYYY-MM-DD" فهنفلتر نصيًا.
        cat_variants = ["عموله تسويق", "عمولة تسويق", "عمولات تسويق", "Commission", "Commission Fee"]
        q = db.query(models.FinanceEntry).filter(models.FinanceEntry.type == "OUT")
        q = q.filter(models.FinanceEntry.category.in_(cat_variants))
        if date_from:
            q = q.filter(models.FinanceEntry.date >= date_from)
        if date_to:
            q = q.filter(models.FinanceEntry.date <= date_to)

        rows: List[models.FinanceEntry] = q.order_by(models.FinanceEntry.date.asc(), models.FinanceEntry.id.asc()).all()
        mname = marketer.name if marketer else ""
        for r in rows:
            note = r.note or ""
            if _note_matches_marketer(note, int(marketer_id), mname):
                payments.append({
                    "date": r.date,
                    "amount": float(r.amount or 0.0),
                    "note": note,
                    "id": r.id,
                })
                payments_total += float(r.amount or 0.0)

        payments_total = round(payments_total, 2)
        net_after_payments = round(c.commission - payments_total, 2)

    ctx = {
        "request": request,
        "marketers": marketers,
        "marketer": marketer,
        "marketer_id": marketer_id,
        "date_from": date_from or "",
        "date_to": date_to or "",
        # خليه يرجع فاضي لو المستخدم ما دخلش حاجة
        "override_rate": "" if (override_rate is None) else override_rate,
        "calc": calc,
        # NEW: دفعات المسوّق
        "payments": payments,
        "payments_total": payments_total,
        "net_after_payments": net_after_payments,
    }
    return templates.TemplateResponse("marketers_comission.html", ctx)


@router.get("/commission/export")
def marketers_commission_export(
    marketer_id: int = Query(...),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    override_rate: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    df = parse_date(date_from)
    dt = parse_date(date_to)
    s_dt = start_of_day(df) if df else None
    e_dt = end_of_day(dt) if dt else None

    # فواتير بيع تخص المسوّق (لاستخدامها مع مرتجعات الأصل)
    sales_q = db.query(models.Invoice.id).filter(
        models.Invoice.type == "S",
        models.Invoice.marketer_id == marketer_id
    )
    if s_dt:
        sales_q = sales_q.filter(models.Invoice.created_at >= s_dt)
    if e_dt:
        sales_q = sales_q.filter(models.Invoice.created_at <= e_dt)

    inv_filters_sales = [models.Invoice.type == "S", models.Invoice.marketer_id == marketer_id]
    if s_dt: inv_filters_sales.append(models.Invoice.created_at >= s_dt)
    if e_dt: inv_filters_sales.append(models.Invoice.created_at <= e_dt)

    inv_filters_returns = [models.Invoice.type == "R"]
    if s_dt: inv_filters_returns.append(models.Invoice.created_at >= s_dt)
    if e_dt: inv_filters_returns.append(models.Invoice.created_at <= e_dt)

    sales = db.query(models.Invoice).filter(*inv_filters_sales).order_by(models.Invoice.id.asc()).all()
    rets  = db.query(models.Invoice)\
              .filter(*inv_filters_returns)\
              .filter(
                  (models.Invoice.marketer_id == marketer_id) |
                  (models.Invoice.original_sale_id.in_(sales_q.subquery()))
              )\
              .order_by(models.Invoice.id.asc()).all()

    sales_total = sum(float(i.total or 0) for i in sales)
    shipping_total = sum(float(i.actual_shipping_cost or 0) for i in sales)
    returns_total = sum(float(i.total or 0) for i in rets)
    returns_ship_fees = sum(float(i.return_shipping_fee or 0) for i in rets)
    base = sales_total - shipping_total - returns_total - returns_ship_fees

    marketer = db.query(models.Marketer).get(int(marketer_id))

    if override_rate is not None and str(override_rate).strip() != "":
        try:
            rate = float(override_rate)
        except ValueError:
            rate = float((marketer.commission_rate if marketer else 0.0))
    else:
        rate = float((marketer.commission_rate if marketer else 0.0))

    commission = (base * rate / 100.0) if base > 0 else 0.0

    # CSV (UTF-8 with BOM)
    lines = []
    lines.append("نوع,كود الفاتورة,التاريخ,الإجمالي,مصروف_الشحن_الفعلي,رسوم_شحن_مرتجع\n")
    for i in sales:
        lines.append(f"S,{i.invoice_code or ''},{i.created_at.date() if i.created_at else ''},{(i.total or 0):.2f},{(i.actual_shipping_cost or 0):.2f},0.00\n")
    for r in rets:
        lines.append(f"R,{r.invoice_code or ''},{r.created_at.date() if r.created_at else ''},{(r.total or 0):.2f},0.00,{(r.return_shipping_fee or 0):.2f}\n")
    lines.append("\n")
    lines.append(f"إجمالي مبيعات,,,{sales_total:.2f},,\n")
    lines.append(f"(-) مصاريف الشحن,,,{shipping_total:.2f},,\n")
    lines.append(f"(-) المرتجعات,,,{returns_total:.2f},,\n")
    lines.append(f"(-) رسوم شحن المرتجعات,,,{returns_ship_fees:.2f},,\n")
    lines.append(f"أساس العمولة,,,{(base):.2f},,\n")
    lines.append(f"نسبة العمولة,,,{(rate):.2f}%,,\n")
    lines.append(f"عمولة مستحقة,,,{(commission):.2f},,\n")

    csv_data = ("\ufeff" + "".join(lines)).encode("utf-8")
    headers = {
        "Content-Disposition": 'attachment; filename="marketer_commission.csv"',
        "Cache-Control": "no-store",
    }
    return Response(content=csv_data, media_type="text/csv; charset=utf-8", headers=headers)


# =========================================================
# (اختياري) تعويض المرتجعات القديمة بملء marketer_id مرة واحدة
# =========================================================
@router.post("/util/backfill-returns-marketer")
def backfill_returns_marketer(db: Session = Depends(get_db)):
    """
    يملأ marketer_id للمرتجعات القديمة من فاتورة البيع الأصلية
    (للقيود التي type='R' و marketer_id IS NULL و original_sale_id IS NOT NULL)
    """
    rets = db.query(models.Invoice).filter(
        models.Invoice.type == "R",
        models.Invoice.marketer_id.is_(None),
        models.Invoice.original_sale_id.isnot(None)
    ).all()
    count = 0
    for r in rets:
        sale = db.query(models.Invoice).get(int(r.original_sale_id))
        if sale and sale.marketer_id:
            r.marketer_id = sale.marketer_id
            count += 1
    db.commit()
    return {"updated": count}