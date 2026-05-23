from fastapi import APIRouter, Request, Depends, Form, Query, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import date, datetime, time
from typing import Optional, List, Dict, Tuple

from app.database import SessionLocal
from app import models
from fastapi.templating import Jinja2Templates

router = APIRouter(prefix="/shipping", tags=["Shipping"])
templates = Jinja2Templates(directory="app/templates")

# طرق الدفع التي تعتبر "مدفوع مباشرة" ولا تُطالب بها شركة الشحن
DIRECT_METHODS = {"InstaPay", "محفظة إلكترونية"}

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

def distinct_companies(db: Session) -> List[str]:
    rows = (
        db.query(models.Invoice.shipping_company)
        .filter(
            models.Invoice.shipping_company.isnot(None),
            models.Invoice.shipping_company != "",
        )
        .distinct()
        .order_by(models.Invoice.shipping_company.asc())
        .all()
    )
    return [r[0] for r in rows]

# ===== Helpers: allocations & returns linking =====

def allocations_sum_for_invoice(db: Session, invoice_id: int) -> float:
    s = db.query(func.coalesce(func.sum(models.ShippingAllocation.amount), 0.0))\
          .filter(models.ShippingAllocation.invoice_id == invoice_id)\
          .scalar()
    return float(s or 0.0)

def allocations_sum_for_payment(db: Session, payment_id: int) -> float:
    s = db.query(func.coalesce(func.sum(models.ShippingAllocation.amount), 0.0))\
          .filter(models.ShippingAllocation.payment_id == payment_id)\
          .scalar()
    return float(s or 0.0)

def payment_available_amount(db: Session, pay: models.ShippingPayment) -> float:
    used = allocations_sum_for_payment(db, pay.id)
    return max(0.0, float(pay.amount or 0) - used)

def linked_returns_totals(db: Session, sale_id: int) -> Tuple[float, float]:
    """إجمالي المرتجعات المرتبطة بفاتورة بيع محددة (قيمة المرتجع + رسومه)."""
    rets = db.query(models.Invoice).filter(
        models.Invoice.type == "R",
        models.Invoice.original_sale_id == sale_id
    ).all()
    total_ret = sum(float(r.total or 0) for r in rets)
    total_ret_ship = sum(float(r.return_shipping_fee or 0) for r in rets)
    return total_ret, total_ret_ship

def _cash_link_for_payment(db: Session, pay_id: int) -> Optional[models.ShippingPaymentCashMap]:
    return db.query(models.ShippingPaymentCashMap)\
             .filter(models.ShippingPaymentCashMap.payment_id == int(pay_id)).first()

def _create_finance_entry_for_payment(db: Session, pay: models.ShippingPayment) -> int:
    fe = models.FinanceEntry(
        date=pay.date or datetime.now().strftime("%Y-%m-%d"),
        type="IN",
        category=f"توريد شركات شحن - {pay.company or 'غير محدد'}",
        amount=float(pay.amount or 0),
        note=f"دفعة شحن #{pay.id}",
        created_at=datetime.now()
    )
    db.add(fe); db.commit(); db.refresh(fe)
    return fe.id

# ===== NEW: تغطية إدارية =====
def is_admin_covered(db: Session, invoice_id: int, company: Optional[str]) -> bool:
    if not company:
        return False
    return db.query(models.ShippingAdminCover)\
             .filter(models.ShippingAdminCover.invoice_id == int(invoice_id),
                     models.ShippingAdminCover.company == company)\
             .first() is not None

# ===== أساس الحسابات على مستوى الشركة باستخدام المتبقي الحقيقي لكل فاتورة =====
def totals_for_company(
    db: Session,
    company: Optional[str],
    s_dt: Optional[datetime],
    e_dt: Optional[datetime],
) -> Dict[str, float]:
    """
    نحسب الإجماليات بالطريقة الأدق:
      - المبيعات المحتسبة = فواتير البيع غير المباشرة فقط (مش InstaPay/محفظة)
      - المدفوع = مجموع التخصيصات على فواتير الشركة (allocations) — داخل نفس الفلترة
      - صافي المستحق = مجموع المتبقي لكل فاتورة (outstanding) بعد خصم:
            مصاريف الشحن الفعلية + المرتجعات ورسومها + التخصيصات،
        مع تجاهل الفواتير المباشرة، واستبعاد الفواتير المغطاة إداريًا.
      - النتيجة لازم تطابق مجموع عمود "outstanding" المعروض في جدول "فواتير مستحقة".
    """
    inv_filters = []
    if company:
        inv_filters.append(models.Invoice.shipping_company == company)
    else:
        inv_filters.append(models.Invoice.shipping_company.isnot(None))
        inv_filters.append(models.Invoice.shipping_company != "")

    if s_dt:
        inv_filters.append(models.Invoice.created_at >= s_dt)
    if e_dt:
        inv_filters.append(models.Invoice.created_at <= e_dt)

    # كل الفواتير (بيع/مرتجع) لهذه الشركة/الفترة
    sales = db.query(models.Invoice).filter(models.Invoice.type == "S", *inv_filters).all()
    rets  = db.query(models.Invoice).filter(models.Invoice.type == "R", *inv_filters).all()

    # تقسيم مباشر/غير مباشر
    sales_non_direct = [i for i in sales if (i.payment_method or "").strip() not in DIRECT_METHODS]

    # إجماليات عرضية (للمعلومات أعلى الصفحة)
    sales_sum_non_direct = sum(float(i.total or 0) for i in sales_non_direct)
    actual_ship_sum_all  = sum(float(i.actual_shipping_cost or 0) for i in sales)
    returns_sum          = sum(float(i.total or 0) for i in rets)
    returns_ship_fees    = sum(float(i.return_shipping_fee or 0) for i in rets)

    # إجمالي التخصيصات المرتبطة بالفواتير ضمن نفس الفلترة
    sale_ids = [i.id for i in sales_non_direct]
    if sale_ids:
        paid_sum_alloc = db.query(func.coalesce(func.sum(models.ShippingAllocation.amount), 0.0))\
                           .filter(models.ShippingAllocation.invoice_id.in_(sale_ids))\
                           .scalar() or 0.0
    else:
        paid_sum_alloc = 0.0

    # صافي المستحق الحقيقي = مجموع المتبقي لكل فاتورة غير مباشرة، مع استبعاد المغطاة إداريًا دائمًا
    net_due = 0.0
    for inv in sales_non_direct:
        if is_admin_covered(db, inv.id, inv.shipping_company):
            continue  # مستبعد إداريًا سواء كنا بنجمّع لكل الشركات أو لشركة بعينها
        base_due = float(inv.total or 0) - float(inv.actual_shipping_cost or 0)
        ret_total, ret_ship = linked_returns_totals(db, inv.id)
        due_after_returns = base_due - ret_total - ret_ship
        paid_on_inv = allocations_sum_for_invoice(db, inv.id)
        outstanding = round(due_after_returns - paid_on_inv, 2)
        if outstanding > 0.009:
            net_due += outstanding

    return {
        "sales_sum": float(sales_sum_non_direct),
        "actual_ship_sum": float(actual_ship_sum_all),
        "returns_sum": float(returns_sum),
        "returns_ship_fees": float(returns_ship_fees),
        "paid_sum": float(paid_sum_alloc),   # ◀️ مجموع التخصيصات الفعلية
        "net_due": float(round(net_due, 2)), # ◀️ يساوي مجموع المتبقي بالجدول
        "sales": sales,
        "rets": rets,
        "payments": [],
    }

# ========================= UI =========================

@router.get("", response_class=HTMLResponse)
def shipping_accounts(
    request: Request,
    company: Optional[str] = Query(None),
    dfrom: Optional[str] = Query(None),
    dto: Optional[str] = Query(None),

    # NEW: بحث مستقل لكل جدول
    unpaid_q: str = Query("", description="بحث الفواتير المستحقة (كود/موبايل)"),
    paid_q: str = Query("", description="بحث الفواتير المسددة (كود/موبايل)"),

    # Pagination للجداول
    unpaid_page: int = Query(1),
    paid_page: int = Query(1),
    unpaid_page_size: int = Query(10),
    paid_page_size: int = Query(10),

    # Pagination لدفعات الشركة
    payments_page: int = Query(1),
    payments_page_size: int = Query(5),

    # توافقًا مع القديم (fallback)
    page_size: int = Query(20),

    db: Session = Depends(get_db),
):
    companies = distinct_companies(db)

    # تواريخ اختيارية فقط
    df = parse_date(dfrom)
    dt = parse_date(dto)
    s_dt = start_of_day(df) if df else None
    e_dt = end_of_day(dt) if dt else None

    if not company:
        # صفحة إجمالية: نعرض ملخص إجمالي + لكل شركة بقيم net_due الحقيقية
        overall = totals_for_company(db, None, s_dt, e_dt)
        per_company = []
        for comp in companies:
            t = totals_for_company(db, comp, s_dt, e_dt)
            per_company.append({
                "company": comp,
                "sales_sum": t["sales_sum"],
                "actual_ship_sum": t["actual_ship_sum"],
                "returns_sum": t["returns_sum"],
                "returns_ship_fees": t["returns_ship_fees"],
                "paid_sum": t["paid_sum"],
                "net_due": t["net_due"],
            })
        ctx = {
            "request": request,
            "companies": companies,
            "company": "",
            "dfrom": dfrom or "",
            "dto": dto or "",
            "has_selection": False,
            "sales_sum": overall["sales_sum"],
            "actual_ship_sum": overall["actual_ship_sum"],
            "returns_sum": overall["returns_sum"],
            "returns_ship_fees": overall["returns_ship_fees"],
            "paid_sum": overall["paid_sum"],
            "net_due": overall["net_due"],
            "per_company": per_company,
            "sales": [], "rets": [], "payments": [],
            "payments_with_avail": [],
            "invoices_unpaid": [], "invoices_paid": [],
            "unpaid_page": 1, "unpaid_pages": 1, "unpaid_page_size": 10, "unpaid_q": "",
            "paid_page": 1, "paid_pages": 1, "paid_page_size": 10, "paid_q": "",
            "payments_table": [], "payments_page": 1, "payments_pages": 1, "payments_page_size": 5,
            "payments_select": [],
        }
        return templates.TemplateResponse("shipping_accounts.html", ctx)

    # شركة محددة → فواتير الشركة خلال الفترة (أو كل الوقت)
    inv_filters = [models.Invoice.shipping_company == company]
    if s_dt: inv_filters.append(models.Invoice.created_at >= s_dt)
    if e_dt: inv_filters.append(models.Invoice.created_at <= e_dt)

    invoices_all = db.query(models.Invoice).filter(*inv_filters).order_by(models.Invoice.id.desc()).all()

    # ملخص مستوى الشركة (بالطريقة الجديدة المتوافقة مع الجدول)
    t = totals_for_company(db, company, s_dt, e_dt)

    # تقسيم الفواتير: نعرض للتحصيل فقط فواتير S
    invoices_unpaid_all, invoices_paid_all = [], []
    for inv in invoices_all:
        if inv.type != "S":
            continue  # المرتجعات لا تدخل في جداول التحصيل

        method = (inv.payment_method or "").strip()
        is_direct = method in DIRECT_METHODS

        # مستحق أساسًا من البيع
        base_due = float(inv.total or 0) - float(inv.actual_shipping_cost or 0)

        # خصم المرتجعات المرتبطة بهذه الفاتورة
        ret_total, ret_ship = linked_returns_totals(db, inv.id)
        due_after_returns = base_due - ret_total - ret_ship

        # تخصيصات دفعات على هذه الفاتورة
        paid_on_inv = allocations_sum_for_invoice(db, inv.id)

        # للفواتير المباشرة: اعتبرها مسددة تمامًا
        if is_direct:
            rec = {
                "id": inv.id,
                "invoice_code": inv.invoice_code,
                "type": inv.type,
                "total": float(inv.total or 0),
                "actual_shipping_cost": float(inv.actual_shipping_cost or 0),
                "created_at": inv.created_at,
                "due": 0.0, "paid": 0.0, "outstanding": 0.0,
                "linked_returns_total": round(ret_total, 2),
                "linked_returns_ship": round(ret_ship, 2),
                "direct_paid": True,
                "payment_method": method,
                "admin_covered": False,
                "customer_name": inv.customer_name or "-",
                "customer_phone": inv.customer_phone or "",
            }
            invoices_paid_all.append(rec)
            continue

        outstanding = round(due_after_returns - paid_on_inv, 2)

        # NEW: هل مغطّاة إداريًا؟
        covered = is_admin_covered(db, inv.id, company)

        rec = {
            "id": inv.id,
            "invoice_code": inv.invoice_code,
            "type": inv.type,
            "total": float(inv.total or 0),
            "actual_shipping_cost": float(inv.actual_shipping_cost or 0),
            "created_at": inv.created_at,
            "due": round(due_after_returns, 2),
            "paid": round(paid_on_inv, 2),
            "outstanding": outstanding,
            "linked_returns_total": round(ret_total, 2),
            "linked_returns_ship": round(ret_ship, 2),
            "direct_paid": False,
            "payment_method": method,
            "admin_covered": covered,
            "customer_name": inv.customer_name or "-",
            "customer_phone": inv.customer_phone or "",
        }
        # لو مسدد تمامًا أو مغطّى إداريًا → يتحط في "المسددة"
        if abs(outstanding) < 0.01 or covered:
            invoices_paid_all.append(rec)
        else:
            invoices_unpaid_all.append(rec)

    # ======= NEW: بحث مستقل لكل جدول =======
    def _matches_invoice(rec, q):
        if not q:
            return True
        _q = q.strip().lower()
        return (_q in (rec.get("invoice_code","") or "").lower()) or (_q in (rec.get("customer_phone","") or "").lower())

    if unpaid_q:
        invoices_unpaid_all = [r for r in invoices_unpaid_all if _matches_invoice(r, unpaid_q)]

    if paid_q:
        invoices_paid_all = [r for r in invoices_paid_all if _matches_invoice(r, paid_q)]
    # =======================================

    # تقسيم صفحات (نفس الدالة تستخدم للجداول والدفعات)
    def _paginate(lst, page, size):
        total = len(lst)
        pages = (total + size - 1) // size if size > 0 else 1
        page = max(1, min(page, max(1, pages)))
        start = (page - 1) * size
        end = start + size
        return lst[start:end], page, pages

    # أحجام الصفحات مع توافق القديم
    unpaid_page_size = int(unpaid_page_size or 0) or int(page_size or 10)
    paid_page_size   = int(paid_page_size or 0)   or int(page_size or 10)
    unpaid_page_size = max(1, unpaid_page_size)
    paid_page_size   = max(1, paid_page_size)

    unpaid_slice, unpaid_page, unpaid_pages = _paginate(invoices_unpaid_all, int(unpaid_page or 1), unpaid_page_size)
    paid_slice,   paid_page,   paid_pages   = _paginate(invoices_paid_all,   int(paid_page or 1),   paid_page_size)

    # دفعات الشركة + الأرصدة المتاحة (نعرضها كما هي)
    pay_filters = [models.ShippingPayment.company == company]
    if dfrom: pay_filters.append(models.ShippingPayment.date >= dfrom)
    if dto:   pay_filters.append(models.ShippingPayment.date <= dto)
    payments = db.query(models.ShippingPayment).filter(*pay_filters).order_by(models.ShippingPayment.id.desc()).all()

    # ضم معلومات الربط بالخزنة
    payments_with_avail = []
    for p in payments:
        link = _cash_link_for_payment(db, p.id)
        payments_with_avail.append({
            "id": p.id, "date": p.date,
            "amount": float(p.amount or 0),
            "note": p.note,
            "available": payment_available_amount(db, p),
            "linked_cash": True if link else False,
            "finance_entry_id": link.finance_entry_id if link else None
        })

    # ======= NEW: دفعات الشركة Pagination + لائحة للاختيارات المتاحة فقط =======
    def _paginate(lst, page, size):
        total = len(lst)
        pages = (total + size - 1) // size if size > 0 else 1
        page = max(1, min(int(page or 1), max(1, pages)))
        start = (page - 1) * size
        end = start + size
        return lst[start:end], page, pages

    payments_page_size = int(payments_page_size or 5)
    payments_page_size = max(1, payments_page_size)
    payments_page = max(1, int(payments_page or 1))

    payments_slice, payments_page, payments_pages = _paginate(payments_with_avail, payments_page, payments_page_size)

    # لستة الدفعات التي بها رصيد متاح فقط (لاختيارات التخصيص في الفورم)
    payments_select = [p for p in payments_with_avail if (p.get("available", 0) or 0) > 0.0009]
    # =========================================================

    ctx = {
        "request": request,
        "companies": companies,
        "company": company,
        "dfrom": dfrom or "",
        "dto": dto or "",
        "has_selection": True,

        # الإجماليات بالطريقة الجديدة
        "sales_sum": t["sales_sum"],
        "actual_ship_sum": t["actual_ship_sum"],
        "returns_sum": t["returns_sum"],
        "returns_ship_fees": t["returns_ship_fees"],
        "paid_sum": t["paid_sum"],
        "net_due": t["net_due"],

        "sales": t["sales"],
        "rets": t["rets"],

        # دفعات
        "payments": payments,
        "payments_with_avail": payments_with_avail,
        "payments_table": payments_slice,
        "payments_page": payments_page,
        "payments_pages": payments_pages,
        "payments_page_size": payments_page_size,
        "payments_select": payments_select,

        # جداول الفواتير
        "invoices_unpaid": unpaid_slice,
        "invoices_paid": paid_slice,

        # Pagination + بحث
        "unpaid_page": unpaid_page,
        "unpaid_pages": unpaid_pages,
        "paid_page": paid_page,
        "paid_pages": paid_pages,
        "unpaid_page_size": unpaid_page_size,
        "paid_page_size": paid_page_size,
        "unpaid_q": unpaid_q,
        "paid_q": paid_q,

        # احتفاظًا بالقيم القديمة للتوافق
        "page_size": page_size,
    }
    return templates.TemplateResponse("shipping_accounts.html", ctx)

# ====== عمليات الدفعات ======

@router.post("/pay")
def add_payment(
    company: str = Form(...),
    date_: str = Form(...),
    amount: float = Form(...),
    note: str = Form(""),
    q_dfrom: str = Form("", alias="q_dfrom"),
    q_dto: str = Form("", alias="q_dto"),
    unpaid_page: int = Form(1),
    paid_page: int = Form(1),
    page_size: int = Form(20),
    db: Session = Depends(get_db),
):
    p = models.ShippingPayment(
        company=company.strip(),
        date=date_.strip(),
        amount=float(amount or 0),
        note=note.strip() or None,
    )
    db.add(p)
    db.commit()

    url = f"/shipping?company={company}&unpaid_page={unpaid_page}&paid_page={paid_page}&page_size={page_size}"
    if q_dfrom: url += f"&dfrom={q_dfrom}"
    if q_dto:   url += f"&dto={q_dto}"
    return RedirectResponse(url=url, status_code=303)

@router.post("/allocate")
def allocate_payment(
    company: str = Form(...),
    invoice_id: int = Form(...),
    payment_id: int = Form(...),
    amount: float = Form(...),
    q_dfrom: str = Form("", alias="q_dfrom"),
    q_dto: str = Form("", alias="q_dto"),
    unpaid_page: int = Form(1),
    paid_page: int = Form(1),
    page_size: int = Form(20),
    db: Session = Depends(get_db),
):
    inv = db.query(models.Invoice).get(invoice_id)
    pay = db.query(models.ShippingPayment).get(payment_id)

    # فحص صحة البيانات
    if not inv or not pay or inv.shipping_company != company or pay.company != company:
        url = f"/shipping?company={company}&unpaid_page={unpaid_page}&paid_page={paid_page}&page_size={page_size}"
        if q_dfrom: url += f"&dfrom={q_dfrom}"
        if q_dto:   url += f"&dto={q_dto}"
        return RedirectResponse(url=url + "&msg=⚠️ بيانات غير صحيحة", status_code=303)

    # منع تخصيص دفعات على المرتجع
    if inv.type == "R":
        url = f"/shipping?company={company}&unpaid_page={unpaid_page}&paid_page={paid_page}&page_size={page_size}"
        if q_dfrom: url += f"&dfrom={q_dfrom}"
        if q_dto:   url += f"&dto={q_dto}"
        return RedirectResponse(url=url + "&msg=⚠️ لا يمكن تخصيص دفعة لفاتورة مرتجع", status_code=303)

    # للفواتير المدفوعة مباشرة: لا يجوز تخصيص دفعات
    method = (inv.payment_method or "").strip()
    if method in DIRECT_METHODS:
        url = f"/shipping?company={company}&unpaid_page={unpaid_page}&paid_page={paid_page}&page_size={page_size}"
        if q_dfrom: url += f"&dfrom={q_dfrom}"
        if q_dto:   url += f"&dto={q_dto}"
        return RedirectResponse(url=url + "&msg=⚠️ الفاتورة مدفوعة مباشرة، لا تحتاج تخصيص", status_code=303)

    # حساب المتبقي بعد خصم المرتجعات المرتبطة
    base_due = float(inv.total or 0) - float(inv.actual_shipping_cost or 0)
    ret_total, ret_ship = linked_returns_totals(db, inv.id)
    due_after_returns = base_due - ret_total - ret_ship
    paid_on_inv = allocations_sum_for_invoice(db, inv.id)
    outstanding = round(due_after_returns - paid_on_inv, 2)

    avail = payment_available_amount(db, pay)

    amount = float(amount or 0)
    max_allowed = min(max(outstanding, 0.0), avail)
    if amount <= 0 or amount - max_allowed > 1e-6:
        url = f"/shipping?company={company}&unpaid_page={unpaid_page}&paid_page={paid_page}&page_size={page_size}"
        if q_dfrom: url += f"&dfrom={q_dfrom}"
        if q_dto:   url += f"&dto={q_dto}"
        return RedirectResponse(url=url + "&msg=⚠️ مبلغ غير صالح", status_code=303)

    db.add(models.ShippingAllocation(payment_id=pay.id, invoice_id=inv.id, amount=amount))
    db.commit()

    url = f"/shipping?company={company}&unpaid_page={unpaid_page}&paid_page={paid_page}&page_size={page_size}"
    if q_dfrom: url += f"&dfrom={q_dfrom}"
    if q_dto:   url += f"&dto={q_dto}"
    return RedirectResponse(url=url + "&msg=✅ تم تخصيص الدفعة", status_code=303)

# ====== Edit/Delete payment & link/unlink cashbook ======

@router.post("/payments/{pid}/edit")
def edit_payment(
    pid: int,
    company: str = Form(...),
    date_: str = Form(...),
    amount: float = Form(...),
    note: str = Form(""),
    link_to_cash: Optional[str] = Form(None),
    q_dfrom: str = Form("", alias="q_dfrom"),
    q_dto: str = Form("", alias="q_dto"),
    unpaid_page: int = Form(1),
    paid_page: int = Form(1),
    page_size: int = Form(20),
    db: Session = Depends(get_db),
):
    p = db.query(models.ShippingPayment).get(int(pid))
    if not p or p.company != company:
        url = f"/shipping?company={company}&unpaid_page={unpaid_page}&paid_page={paid_page}&page_size={page_size}"
        if q_dfrom: url += f"&dfrom={q_dfrom}"
        if q_dto:   url += f"&dto={q_dto}"
        return RedirectResponse(url=url + "&msg=⚠️ دفعة غير موجودة", status_code=303)

    used = allocations_sum_for_payment(db, p.id)
    amount = float(amount or 0)
    if amount < used - 1e-6:
        url = f"/shipping?company={company}&unpaid_page={unpaid_page}&paid_page={paid_page}&page_size={page_size}"
        if q_dfrom: url += f"&dfrom={q_dfrom}"
        if q_dto:   url += f"&dto={q_dto}"
        return RedirectResponse(url=url + f"&msg=⚠️ لا يمكن تقليل المبلغ عن المستخدَم ({used:.2f})", status_code=303)

    # تحديث الدفعة
    p.date = date_.strip()
    p.amount = amount
    p.note = note.strip() or None
    db.commit()

    # ربط/تحديث قيد الخزنة
    link = _cash_link_for_payment(db, p.id)
    want_link = link_to_cash is not None

    if want_link and not link:
        fe_id = _create_finance_entry_for_payment(db, p)
        db.add(models.ShippingPaymentCashMap(payment_id=p.id, finance_entry_id=fe_id, created_at=datetime.now()))
        db.commit()
    elif want_link and link:
        fe = db.query(models.FinanceEntry).get(int(link.finance_entry_id))
        if fe:
            fe.date = p.date
            fe.amount = p.amount
            fe.category = f"توريد شركات شحن - {p.company or 'غير محدد'}"
            fe.note = f"دفعة شحن #{p.id}"
            db.commit()
    elif not want_link and link:
        fe = db.query(models.FinanceEntry).get(int(link.finance_entry_id))
        if fe: db.delete(fe)
        db.delete(link)
        db.commit()

    url = f"/shipping?company={company}&unpaid_page={unpaid_page}&paid_page={paid_page}&page_size={page_size}"
    if q_dfrom: url += f"&dfrom={q_dfrom}"
    if q_dto:   url += f"&dto={q_dto}"
    return RedirectResponse(url=url + "&msg=✓ تم التعديل", status_code=303)

@router.post("/payments/{pid}/delete")
def delete_payment(
    pid: int,
    company: str = Form(...),
    q_dfrom: str = Form("", alias="q_dfrom"),
    q_dto: str = Form("", alias="q_dto"),
    unpaid_page: int = Form(1),
    paid_page: int = Form(1),
    page_size: int = Form(20),
    db: Session = Depends(get_db),
):
    p = db.query(models.ShippingPayment).get(int(pid))
    if not p or p.company != company:
        url = f"/shipping?company={company}&unpaid_page={unpaid_page}&paid_page={paid_page}&page_size={page_size}"
        if q_dfrom: url += f"&dfrom={q_dfrom}"
        if q_dto:   url += f"&dto={q_dto}"
        return RedirectResponse(url=url + "&msg=⚠️ دفعة غير موجودة", status_code=303)

    # احذف التخصيصات
    db.query(models.ShippingAllocation).filter(models.ShippingAllocation.payment_id == p.id).delete()

    # احذف ربط الخزنة + القيد
    link = _cash_link_for_payment(db, p.id)
    if link:
        fe = db.query(models.FinanceEntry).get(int(link.finance_entry_id))
        if fe: db.delete(fe)
        db.delete(link)

    db.delete(p)
    db.commit()

    url = f"/shipping?company={company}&unpaid_page={unpaid_page}&paid_page={paid_page}&page_size={page_size}"
    if q_dfrom: url += f"&dfrom={q_dfrom}"
    if q_dto:   url += f"&dto={q_dto}"
    return RedirectResponse(url=url + "&msg=✓ تم الحذف", status_code=303)

@router.post("/payments/{pid}/link-cash")
def link_cash(
    pid: int,
    company: str = Form(...),
    q_dfrom: str = Form("", alias="q_dfrom"),
    q_dto: str = Form("", alias="q_dto"),
    unpaid_page: int = Form(1),
    paid_page: int = Form(1),
    page_size: int = Form(20),
    db: Session = Depends(get_db),
):
    p = db.query(models.ShippingPayment).get(int(pid))
    if not p or p.company != company:
        url = f"/shipping?company={company}&unpaid_page={unpaid_page}&paid_page={paid_page}&page_size={page_size}"
        if q_dfrom: url += f"&dfrom={q_dfrom}"
        if q_dto:   url += f"&dto={q_dto}"
        return RedirectResponse(url=url + "&msg=⚠️ دفعة غير موجودة", status_code=303)

    link = _cash_link_for_payment(db, p.id)
    if not link:
        fe_id = _create_finance_entry_for_payment(db, p)
        db.add(models.ShippingPaymentCashMap(payment_id=p.id, finance_entry_id=fe_id, created_at=datetime.now()))
        db.commit()

    url = f"/shipping?company={company}&unpaid_page={unpaid_page}&paid_page={paid_page}&page_size={page_size}"
    if q_dfrom: url += f"&dfrom={q_dfrom}"
    if q_dto:   url += f"&dto={q_dto}"
    return RedirectResponse(url=url + "&msg=✓ تم الربط بالخزنة", status_code=303)

@router.post("/payments/{pid}/unlink-cash")
def unlink_cash(
    pid: int,
    company: str = Form(...),
    q_dfrom: str = Form("", alias="q_dfrom"),
    q_dto: str = Form("", alias="q_dto"),
    unpaid_page: int = Form(1),
    paid_page: int = Form(1),
    page_size: int = Form(20),
    db: Session = Depends(get_db),
):
    p = db.query(models.ShippingPayment).get(int(pid))
    if not p or p.company != company:
        url = f"/shipping?company={company}&unpaid_page={unpaid_page}&paid_page={paid_page}&page_size={page_size}"
        if q_dfrom: url += f"&dfrom={q_dfrom}"
        if q_dto:   url += f"&dto={q_dto}"
        return RedirectResponse(url=url + "&msg=⚠️ دفعة غير موجودة", status_code=303)

    link = db.query(models.ShippingPaymentCashMap).filter(
        models.ShippingPaymentCashMap.payment_id == int(pid)
    ).first()
    if link:
        fe = db.query(models.FinanceEntry).get(int(link.finance_entry_id))
        if fe: db.delete(fe)
        db.delete(link)
        db.commit()

    url = f"/shipping?company={company}&unpaid_page={unpaid_page}&paid_page={paid_page}&page_size={page_size}"
    if q_dfrom: url += f"&dfrom={q_dfrom}"
    if q_dto:   url += f"&dto={q_dto}"
    return RedirectResponse(url=url + "&msg=✓ تم إلغاء الربط", status_code=303)

# ===== NEW: تغطية إدارية لفاتورة =====
@router.post("/cover")
def admin_cover_invoice(
    company: str = Form(...),
    invoice_id: int = Form(...),
    q_dfrom: str = Form(""),
    q_dto: str = Form(""),
    unpaid_page: int = Form(1),
    paid_page: int = Form(1),
    page_size: int = Form(20),
    db: Session = Depends(get_db),
):
    inv = db.query(models.Invoice).get(int(invoice_id))
    if not inv or inv.type != "S" or inv.shipping_company != company:
        url = f"/shipping?company={company}&unpaid_page={unpaid_page}&paid_page={paid_page}&page_size={page_size}"
        if q_dfrom: url += f"&dfrom={q_dfrom}"
        if q_dto:   url += f"&dto={q_dto}"
        return RedirectResponse(url=url + "&msg=⚠️ فاتورة غير صالحة", status_code=303)

    # لا نسمح بالتغطية لفاتورة مدفوعة مباشرة أو مغطّاة أصلاً
    method = (inv.payment_method or "").strip()
    if method in DIRECT_METHODS or is_admin_covered(db, inv.id, company):
        url = f"/shipping?company={company}&unpaid_page={unpaid_page}&paid_page={paid_page}&page_size={page_size}"
        if q_dfrom: url += f"&dfrom={q_dfrom}"
        if q_dto:   url += f"&dto={q_dto}"
        return RedirectResponse(url=url + "&msg=ℹ️ هذه الفاتورة ليست قابلة للتغطية", status_code=303)

    # احسب المتبقي وتأكد ≤ 100
    base_due = float(inv.total or 0) - float(inv.actual_shipping_cost or 0)
    ret_total, ret_ship = linked_returns_totals(db, inv.id)
    due_after_returns = base_due - ret_total - ret_ship
    paid_on_inv = allocations_sum_for_invoice(db, inv.id)
    outstanding = round(due_after_returns - paid_on_inv, 2)

    if outstanding > 100:
        url = f"/shipping?company={company}&unpaid_page={unpaid_page}&paid_page={paid_page}&page_size={page_size}"
        if q_dfrom: url += f"&dfrom={q_dfrom}"
        if q_dto:   url += f"&dto={q_dto}"
        return RedirectResponse(url=url + "&msg=⚠️ المتبقي أكبر من 100 — لا يمكن التغطية", status_code=303)

    db.add(models.ShippingAdminCover(invoice_id=inv.id, company=company, note="Covered admin"))
    db.commit()

    url = f"/shipping?company={company}&unpaid_page={unpaid_page}&paid_page={paid_page}&page_size={page_size}"
    if q_dfrom: url += f"&dfrom={q_dfrom}"
    if q_dto:   url += f"&dto={q_dto}"
    return RedirectResponse(url=url + "&msg=✓ تم وسم الفاتورة كمغطّاة إداريًا", status_code=303)

# ===== NEW: تفاصيل الفاتورة كـ Popup HTML =====
@router.get("/invoice/{invoice_id}", response_class=HTMLResponse)
def invoice_popup(invoice_id: int, db: Session = Depends(get_db)):
    inv = db.query(models.Invoice).get(int(invoice_id))
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")

    base_due = float(inv.total or 0) - float(inv.actual_shipping_cost or 0)
    ret_total, ret_ship = linked_returns_totals(db, inv.id)
    due_after_returns = base_due - ret_total - ret_ship
    paid_on_inv = allocations_sum_for_invoice(db, inv.id)
    outstanding = round(due_after_returns - paid_on_inv, 2)

    allocs = db.query(models.ShippingAllocation)\
               .filter(models.ShippingAllocation.invoice_id == inv.id)\
               .order_by(models.ShippingAllocation.id.desc()).all()

    rows = "".join(
        f"<tr><td>#{a.id}</td><td>{a.payment_id}</td><td class='text-end'>{float(a.amount or 0):.2f}</td></tr>"
        for a in allocs
    ) or "<tr><td colspan='3' class='text-center text-muted'>لا توجد تخصيصات</td></tr>"

    html = f"""
    <div class="row g-2">
      <div class="col-md-6">
        <div class="mb-1"><span class="text-muted small">كود:</span> <strong>{inv.invoice_code}</strong></div>
        <div class="mb-1"><span class="text-muted small">العميل:</span> {inv.customer_name or ''}</div>
        <div class="mb-1"><span class="text-muted small">موبايل:</span> {inv.customer_phone or ''}</div>
        <div class="mb-1"><span class="text-muted small">تاريخ:</span> {getattr(inv, 'created_at', '')}</div>
      </div>
      <div class="col-md-6">
        <div class="mb-1"><span class="text-muted small">إجمالي الفاتورة:</span> {float(inv.total or 0):.2f}</div>
        <div class="mb-1"><span class="text-muted small">مصاريف الشحن الفعلية:</span> {float(inv.actual_shipping_cost or 0):.2f}</div>
        <div class="mb-1"><span class="text-muted small">مرتجعات+رسوم:</span> {(ret_total + ret_ship):.2f}</div>
        <div class="mb-1"><span class="text-muted small">مدفوع (تخصيصات):</span> {float(paid_on_inv or 0):.2f}</div>
        <div class="mb-1"><span class="text-muted small">المتبقي:</span> <strong>{outstanding:.2f}</strong></div>
      </div>
    </div>
    <hr class="my-2"/>
    <div class="table-responsive">
      <table class="table table-sm table-bordered mb-0">
        <thead class="table-light">
          <tr><th>#</th><th>دفعة</th><th class="text-end">المبلغ</th></tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
    """
    return HTMLResponse(content=html)