# app/routers/settlements.py
from datetime import datetime, date, time
from fastapi import APIRouter, Request, Depends, Query, Form
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, or_
from fastapi.templating import Jinja2Templates

from app.database import SessionLocal
from app import models

router = APIRouter(prefix="/settlements", tags=["Daily Settlements"])
templates = Jinja2Templates(directory="app/templates")

# ----------------- DB -----------------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ----------------- Helpers -----------------
def _parse_date_str(s: str):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except:
        return None

def _day_range(yyyy_mm_dd: str):
    """يرجع بداية/نهاية اليوم كـ datetime"""
    d = _parse_date_str(yyyy_mm_dd)
    if not d:
        d = date.today()
    else:
        d = d.date()
    start_dt = datetime.combine(d, time.min)
    end_dt   = datetime.combine(d, time.max)
    return start_dt, end_dt

def _today_str():
    return date.today().strftime("%Y-%m-%d")

# فلتر يستبعد تحويلات إنستاباي/المحفظة من مصروفات التشغيل
def _exclude_instapay_wallet():
    return ~or_(
        models.FinanceEntry.category.ilike("%instapay%"),
        models.FinanceEntry.category.ilike("%wallet%"),
        models.FinanceEntry.category.ilike("%انستا%"),
        models.FinanceEntry.category.ilike("%محفظة%"),
        models.FinanceEntry.note.ilike("%instapay%"),
        models.FinanceEntry.note.ilike("%wallet%"),
        models.FinanceEntry.note.ilike("%انستا%"),
        models.FinanceEntry.note.ilike("%محفظة%"),
    )

# طرق دفع مباشرة لا تدخل ضمن COD
DIRECT_METHODS = {"InstaPay", "محفظة إلكترونية"}

# ----------------- Index (قائمة التسويات) -----------------
@router.get("", response_class=HTMLResponse)
def settlements_index(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=1, le=200),
    date_from: str = Query("", description="YYYY-MM-DD"),
    date_to: str = Query("", description="YYYY-MM-DD"),
    export: str = Query("", description="csv للتصدير"),
    db: Session = Depends(get_db)
):
    """
    قائمة اليوميات المقفولة مع:
      - ترقيم صفحات page/per_page (افتراضيًا 10 في الصفحة)
      - فلترة اختيارية بين تاريخين
      - تصدير CSV لنفس النتائج المفلترة (كل النتائج، مش الصفحة فقط)
    """
    base_q = db.query(models.DailySettlement)

    # فلترة بالتاريخ (الحقل string بصيغة YYYY-MM-DD)
    if date_from:
        base_q = base_q.filter(models.DailySettlement.date >= date_from)
    if date_to:
        base_q = base_q.filter(models.DailySettlement.date <= date_to)

    # إجمالي بعد الفلترة
    total_count = base_q.count()

    # ترتيب: الأحدث أولًا
    base_q = base_q.order_by(models.DailySettlement.date.desc(), models.DailySettlement.id.desc())

    # تصدير CSV (لكل النتائج المفلترة)
    if (export or "").lower() == "csv":
        import io, csv
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["ID", "Date", "Sales Total", "Returns Total", "Other Expenses", "Net Total"])
        for r in base_q.all():
            w.writerow([
                r.id,
                r.date,
                float(r.sales_total or 0),
                float(r.returns_total or 0),
                float(r.expenses_total or 0),
                float(r.net_total or 0),
            ])
        buf.seek(0)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        return StreamingResponse(
            iter([("\ufeff" + buf.getvalue()).encode("utf-8")]),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="settlements_{ts}.csv"'}
        )

    # ترقيم الصفحات
    total_pages = max(1, (total_count + per_page - 1) // per_page)
    page = min(page, total_pages)
    offset = (page - 1) * per_page

    rows_db = base_q.offset(offset).limit(per_page).all()

    # تجهيز صفوف العرض (مع serial_str المستخدمة في القالب)
    rows = []
    for r in rows_db:
        rows.append({
            "id": r.id,
            "date": r.date,
            "sales_total": float(r.sales_total or 0),
            "returns_total": float(r.returns_total or 0),
            "expenses_total": float(r.expenses_total or 0),
            "net_total": float(r.net_total or 0),
            "serial_str": f"{r.id:06d}",
        })

    return templates.TemplateResponse("settlements_list.html", {
        "request": request,
        "rows": rows,
        "today": _today_str(),

        # ترقيم الصفحات
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "total_count": total_count,

        # الفلاتر
        "date_from": date_from or "",
        "date_to": date_to or "",
    })

# ----------------- شاشة تقفيل اليوم -----------------
@router.get("/close", response_class=HTMLResponse)
def close_page(
    request: Request,
    date: str = Query("", description="YYYY-MM-DD"),
    db: Session = Depends(get_db)
):
    day = date or _today_str()
    start_dt, end_dt = _day_range(day)

    # هل اليوم مقفول؟
    already = db.query(models.DailySettlement).filter(models.DailySettlement.date == day).first()

    # ---- استعلامات اليوم (قوائم + إجماليات + أعداد) ----
    sales_q = db.query(models.Invoice).filter(
        models.Invoice.type == "S",
        models.Invoice.created_at >= start_dt,
        models.Invoice.created_at <= end_dt
    )
    returns_q = db.query(models.Invoice).filter(
        models.Invoice.type == "R",
        models.Invoice.created_at >= start_dt,
        models.Invoice.created_at <= end_dt
    )

    inv_sales = sales_q.order_by(models.Invoice.id.asc()).all()
    inv_returns = returns_q.order_by(models.Invoice.id.asc()).all()

    sales_total = float(
        db.query(func.coalesce(func.sum(models.Invoice.total), 0.0))
          .filter(models.Invoice.type == "S",
                  models.Invoice.created_at >= start_dt,
                  models.Invoice.created_at <= end_dt)
          .scalar() or 0.0
    )
    returns_total = float(
        db.query(func.coalesce(func.sum(models.Invoice.total), 0.0))
          .filter(models.Invoice.type == "R",
                  models.Invoice.created_at >= start_dt,
                  models.Invoice.created_at <= end_dt)
          .scalar() or 0.0
    )

    sales_count = sales_q.count()
    returns_count = returns_q.count()

    # إيراد الشحن المحمَّل على العميل (للإظهار فقط)
    shipping_income = float(
        db.query(func.coalesce(func.sum(models.Invoice.shipping_cost), 0.0))
          .filter(models.Invoice.type == "S",
                  models.Invoice.created_at >= start_dt,
                  models.Invoice.created_at <= end_dt)
          .scalar() or 0.0
    )

    # مصروف الشحن الفعلي (بيع) + رسوم شحن المرتجع
    shipping_expense_actual = float(
        db.query(func.coalesce(func.sum(models.Invoice.actual_shipping_cost), 0.0))
          .filter(models.Invoice.type == "S",
                  models.Invoice.created_at >= start_dt,
                  models.Invoice.created_at <= end_dt)
          .scalar() or 0.0
    )
    return_shipping_fees = float(
        db.query(func.coalesce(func.sum(models.Invoice.return_shipping_fee), 0.0))
          .filter(models.Invoice.type == "R",
                  models.Invoice.created_at >= start_dt,
                  models.Invoice.created_at <= end_dt)
          .scalar() or 0.0
    )
    shipping_total_expense = shipping_expense_actual + return_shipping_fees

    # ==== COD (تحصيل عند الاستلام) لفواتير اليوم غير المباشرة ====
    cod_total = 0.0
    for inv in inv_sales:
        method = (inv.payment_method or "").strip()
        if method in DIRECT_METHODS:
            continue
        cod_total += float(inv.total or 0) - float(inv.actual_shipping_cost or 0)
    cod_total = round(cod_total, 2)

    # ---- COGS (تكلفة البضاعة المباعة) ----
    try:
        cogs_sales = float(
            db.query(func.coalesce(func.sum(models.InvoiceItem.qty * models.InvoiceItem.cost_price), 0.0))
              .join(models.Invoice, models.Invoice.id == models.InvoiceItem.invoice_id)
              .filter(models.Invoice.type == "S",
                      models.Invoice.created_at >= start_dt,
                      models.Invoice.created_at <= end_dt)
              .scalar() or 0.0
        )
        cogs_returns = float(
            db.query(func.coalesce(func.sum(models.InvoiceItem.qty * models.InvoiceItem.cost_price), 0.0))
              .join(models.Invoice, models.Invoice.id == models.InvoiceItem.invoice_id)
              .filter(models.Invoice.type == "R",
                      models.Invoice.created_at >= start_dt,
                      models.Invoice.created_at <= end_dt)
              .scalar() or 0.0
        )
    except Exception:
        cogs_sales = float(
            db.query(func.coalesce(func.sum(models.InvoiceItem.qty * models.Product.cost_price), 0.0))
              .join(models.Invoice, models.Invoice.id == models.InvoiceItem.invoice_id)
              .join(models.Product, models.Product.id == models.InvoiceItem.product_id)
              .filter(models.Invoice.type == "S",
                      models.Invoice.created_at >= start_dt,
                      models.Invoice.created_at <= end_dt)
              .scalar() or 0.0
        )
        cogs_returns = float(
            db.query(func.coalesce(func.sum(models.InvoiceItem.qty * models.Product.cost_price), 0.0))
              .join(models.Invoice, models.Invoice.id == models.InvoiceItem.invoice_id)
              .join(models.Product, models.Product.id == models.InvoiceItem.product_id)
              .filter(models.Invoice.type == "R",
                      models.Invoice.created_at >= start_dt,
                      models.Invoice.created_at <= end_dt)
              .scalar() or 0.0
        )
    cogs = cogs_sales - cogs_returns

    # ---- مصروفات تشغيلية (OUT) مع استثناء إنستاباي/محفظة ----
    operating_expenses = float(
        db.query(func.coalesce(func.sum(models.FinanceEntry.amount), 0.0))
          .filter(models.FinanceEntry.type == "OUT",
                  models.FinanceEntry.date == day,
                  _exclude_instapay_wallet())
          .scalar() or 0.0
    )

    # ---- دفعات شحن غير مُرحّلة ----
    try:
        subq = db.query(models.ShippingPaymentCashMap.payment_id)
        unlinked = db.query(models.ShippingPayment)\
                     .filter(models.ShippingPayment.date == day)\
                     .filter(~models.ShippingPayment.id.in_(subq))\
                     .order_by(models.ShippingPayment.id.asc()).all()
        unlinked_count = len(unlinked)
        unlinked_sum = float(sum(float(p.amount or 0) for p in unlinked))
    except Exception:
        unlinked = []
        unlinked_count = 0
        unlinked_sum = 0.0

    # ---- إجماليات العرض ----
    expenses_total = operating_expenses + shipping_total_expense
    net_total = sales_total - returns_total - expenses_total

    ctx = {
        "request": request,
        "day": day,
        "already": already,

        "sales_total": sales_total,
        "returns_total": returns_total,
        "shipping_income": shipping_income,

        "sales_count": sales_count,
        "returns_count": returns_count,

        "shipping_expense_actual": shipping_expense_actual,
        "return_shipping_fees": return_shipping_fees,
        "shipping_total_expense": shipping_total_expense,

        "operating_expenses": operating_expenses,
        "expenses_total": expenses_total,

        "cogs_sales": cogs_sales,
        "cogs_returns": cogs_returns,
        "cogs": cogs,

        "cod_total": cod_total,

        "inv_sales": inv_sales,
        "inv_returns": inv_returns,

        "unlinked": unlinked,
        "unlinked_count": unlinked_count,
        "unlinked_sum": unlinked_sum,

        "net_total": net_total,
    }
    return templates.TemplateResponse("settlements_close.html", ctx)

# ----------------- إضافة مصروف نثري سريع -----------------
@router.post("/add-expense")
def add_quick_expense(
    day: str = Form(...),
    category: str = Form(...),
    amount: float = Form(...),
    note: str = Form(""),
    db: Session = Depends(get_db)
):
    category = (category or "").strip()
    try:
        val = float(amount or 0)
    except:
        val = 0.0
    if not category or val <= 0:
        return RedirectResponse(url=f"/settlements/close?date={day}&msg=⚠️ تحقق من البند/المبلغ", status_code=303)

    db.add(models.FinanceEntry(
        type="OUT",
        category=category,
        amount=val,
        date=day,
        note=(note or "").strip() or None,
        created_at=datetime.now()
    ))
    db.commit()
    return RedirectResponse(url=f"/settlements/close?date={day}&msg=✓ تم تسجيل مصروف نثري", status_code=303)

# ----------------- ترحيل دفعات الشحن لليوم المحدد وربطها بالخزنة -----------------
@router.post("/link-shipping")
def link_shipping_for_day(
    day: str = Form(...),
    db: Session = Depends(get_db)
):
    try:
        subq = db.query(models.ShippingPaymentCashMap.payment_id)
        payments = db.query(models.ShippingPayment)\
                     .filter(models.ShippingPayment.date == day)\
                     .filter(~models.ShippingPayment.id.in_(subq)).all()
    except Exception:
        payments = []

    if not payments:
        return RedirectResponse(url=f"/settlements/close?date={day}&msg=ℹ️ لا توجد دفعات غير مُرحّلة", status_code=303)

    created = 0
    for p in payments:
        amt = float(p.amount or 0)
        if amt <= 0:
            continue
        # أنشئ قيد خزنة (IN)
        fe = models.FinanceEntry(
            date=day,
            type="IN",
            category=f"توريد شركات شحن - {p.company or 'غير محدد'}",
            amount=amt,
            note=f"دفعة شحن #{p.id}",
            created_at=datetime.now()
        )
        db.add(fe); db.commit(); db.refresh(fe)
        # أنشئ ربط
        link = models.ShippingPaymentCashMap(payment_id=p.id, finance_entry_id=fe.id, created_at=datetime.now())
        db.add(link); db.commit()
        created += 1

    return RedirectResponse(url=f"/settlements/close?date={day}&msg=✓ تم ترحيل {created} دفعة شحن للخزنة", status_code=303)

# ----------------- تأكيد التقفيل (إنشاء DailySettlement) -----------------
@router.post("/confirm")
def confirm_close(
    day: str = Form(...),
    db: Session = Depends(get_db)
):
    # لو موجود مسبقًا، ما نكررش
    exists = db.query(models.DailySettlement).filter(models.DailySettlement.date == day).first()
    if exists:
        return RedirectResponse(url=f"/settlements?msg=ℹ️ اليوم {day} مقفول بالفعل", status_code=303)

    # احسب الأرقام تاني بسرعة (نفس منطق شاشة التقفيل)
    start_dt, end_dt = _day_range(day)

    sales_total = float(
        db.query(func.coalesce(func.sum(models.Invoice.total), 0.0))
          .filter(models.Invoice.type == "S",
                  models.Invoice.created_at >= start_dt,
                  models.Invoice.created_at <= end_dt)
          .scalar() or 0.0
    )
    returns_total = float(
        db.query(func.coalesce(func.sum(models.Invoice.total), 0.0))
          .filter(models.Invoice.type == "R",
                  models.Invoice.created_at >= start_dt,
                  models.Invoice.created_at <= end_dt)
          .scalar() or 0.0
    )
    shipping_expense_actual = float(
        db.query(func.coalesce(func.sum(models.Invoice.actual_shipping_cost), 0.0))
          .filter(models.Invoice.type == "S",
                  models.Invoice.created_at >= start_dt,
                  models.Invoice.created_at <= end_dt)
          .scalar() or 0.0
    )
    return_shipping_fees = float(
        db.query(func.coalesce(func.sum(models.Invoice.return_shipping_fee), 0.0))
          .filter(models.Invoice.type == "R",
                  models.Invoice.created_at >= start_dt,
                  models.Invoice.created_at <= end_dt)
          .scalar() or 0.0
    )
    shipping_total_expense = shipping_expense_actual + return_shipping_fees

    # استثناء إنستاباي/محفظة في التأكيد برضه
    operating_expenses = float(
        db.query(func.coalesce(func.sum(models.FinanceEntry.amount), 0.0))
          .filter(models.FinanceEntry.type == "OUT",
                  models.FinanceEntry.date == day,
                  _exclude_instapay_wallet())
          .scalar() or 0.0
    )

    expenses_total = shipping_total_expense + operating_expenses
    net_total = sales_total - returns_total - expenses_total

    row = models.DailySettlement(
        date=day,
        sales_total=sales_total,
        returns_total=returns_total,
        expenses_total=expenses_total,
        net_total=net_total,
        created_at=datetime.now()
    )
    db.add(row); db.commit()
    return RedirectResponse(url="/settlements?msg=✓ تم تقفيل اليومية", status_code=303)

# ----------------- عرض يومية معينة بالـ ID (إعادة توجيه) -----------------
@router.get("/{sid}", response_class=HTMLResponse)
def settlement_show(sid: int, request: Request, db: Session = Depends(get_db)):
    row = db.query(models.DailySettlement).get(int(sid))
    if not row:
        return HTMLResponse("<h3>اليومية غير موجودة</h3>", status_code=404)
    # نعيد التوجيه لشاشة التقفيل لنفس التاريخ لعرض التفاصيل
    return RedirectResponse(url=f"/settlements/close?date={row.date}", status_code=307)

# ----------------- (اختياري) حذف تسوية -----------------
@router.post("/delete")
def delete_close(sid: int = Form(...), db: Session = Depends(get_db)):
    row = db.query(models.DailySettlement).get(int(sid))
    if row:
        db.delete(row); db.commit()
    return RedirectResponse(url="/settlements?msg=✓ تم حذف التسوية", status_code=303)

# ===== [AUTO-CLOSE HELPERS] =====
def _compute_settlement(day: str, db: Session):
    """يرجع قيم اليومية (نفس منطق confirm_close) بدون إنشاء سجل."""
    start_dt, end_dt = _day_range(day)

    sales_total = float(
        db.query(func.coalesce(func.sum(models.Invoice.total), 0.0))
          .filter(models.Invoice.type == "S",
                  models.Invoice.created_at >= start_dt,
                  models.Invoice.created_at <= end_dt)
          .scalar() or 0.0
    )
    returns_total = float(
        db.query(func.coalesce(func.sum(models.Invoice.total), 0.0))
          .filter(models.Invoice.type == "R",
                  models.Invoice.created_at >= start_dt,
                  models.Invoice.created_at <= end_dt)
          .scalar() or 0.0
    )
    shipping_expense_actual = float(
        db.query(func.coalesce(func.sum(models.Invoice.actual_shipping_cost), 0.0))
          .filter(models.Invoice.type == "S",
                  models.Invoice.created_at >= start_dt,
                  models.Invoice.created_at <= end_dt)
          .scalar() or 0.0
    )
    return_shipping_fees = float(
        db.query(func.coalesce(func.sum(models.Invoice.return_shipping_fee), 0.0))
          .filter(models.Invoice.type == "R",
                  models.Invoice.created_at >= start_dt,
                  models.Invoice.created_at <= end_dt)
          .scalar() or 0.0
    )
    shipping_total_expense = shipping_expense_actual + return_shipping_fees

    operating_expenses = float(
        db.query(func.coalesce(func.sum(models.FinanceEntry.amount), 0.0))
          .filter(models.FinanceEntry.type == "OUT",
                  models.FinanceEntry.date == day,
                  _exclude_instapay_wallet())
          .scalar() or 0.0
    )

    expenses_total = shipping_total_expense + operating_expenses
    net_total = sales_total - returns_total - expenses_total

    return {
        "sales_total": sales_total,
        "returns_total": returns_total,
        "expenses_total": expenses_total,
        "net_total": net_total,
    }

def create_settlement_if_missing(day: str, db: Session) -> bool:
    """
    ينشئ DailySettlement لليوم المحدد لو مش موجود.
    يرجّع True لو اتعمل جديد، False لو كان موجود بالفعل.
    """
    exists = db.query(models.DailySettlement).filter(models.DailySettlement.date == day).first()
    if exists:
        return False

    vals = _compute_settlement(day, db)
    row = models.DailySettlement(
        date=day,
        sales_total=vals["sales_total"],
        returns_total=vals["returns_total"],
        expenses_total=vals["expenses_total"],
        net_total=vals["net_total"],
        created_at=datetime.now()
    )
    db.add(row)
    db.commit()
    return True
# ===== [/AUTO-CLOSE HELPERS] =====