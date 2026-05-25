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

    sales = db.query(models.Invoice).filter(models.Invoice.type == "S", *inv_filters).all()
    rets  = db.query(models.Invoice).filter(models.Invoice.type == "R", *inv_filters).all()

    sales_non_direct = [i for i in sales if (i.payment_method or "").strip() not in DIRECT_METHODS]

    sales_sum_non_direct = sum(float(i.total or 0) for i in sales_non_direct)
    actual_ship_sum_all  = sum(float(i.actual_shipping_cost or 0) for i in sales)
    returns_sum          = sum(float(i.total or 0) for i in rets)
    returns_ship_fees    = sum(float(i.return_shipping_fee or 0) for i in rets)

    sale_ids = [i.id for i in sales_non_direct]
    if sale_ids:
        paid_sum_alloc = db.query(func.coalesce(func.sum(models.ShippingAllocation.amount), 0.0))\
                           .filter(models.ShippingAllocation.invoice_id.in_(sale_ids))\
                           .scalar() or 0.0
    else:
        paid_sum_alloc = 0.0

    net_due = 0.0
    for inv in sales_non_direct:
        if is_admin_covered(db, inv.id, inv.shipping_company):
            continue
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
        "paid_sum": float(paid_sum_alloc),
        "net_due": float(round(net_due, 2)),
        "sales": sales,
        "rets": rets,
        "payments": [],
    }

# ===== NEW: حساب “صفحة الكل” بسرعة وبنفس نفس الأرقام (بدون N+1) =====
def totals_all_companies_bulk(
    db: Session,
    s_dt: Optional[datetime],
    e_dt: Optional[datetime],
) -> Tuple[Dict[str, Dict[str, float]], Dict[str, float]]:
    """
    يرجع:
      - per_company: قاموس لكل شركة بنفس الأعمدة (sales_sum, actual_ship_sum, returns_sum, returns_ship_fees, paid_sum, net_due)
      - overall: نفس الأعمدة للإجمالي العام
    بنفس منطق totals_for_company تمامًا لكن بدون تكرار لكل شركة وبدون Queries داخل loops.
    """
    Inv = models.Invoice
    Alloc = models.ShippingAllocation
    Cover = models.ShippingAdminCover

    inv_filters = [
        Inv.shipping_company.isnot(None),
        Inv.shipping_company != "",
    ]
    if s_dt:
        inv_filters.append(Inv.created_at >= s_dt)
    if e_dt:
        inv_filters.append(Inv.created_at <= e_dt)

    # 1) مبيعات S (كلها) لاستخراج actual_shipping_cost لكل شركة
    sales_all = db.query(
        Inv.id,
        Inv.shipping_company,
        Inv.total,
        Inv.actual_shipping_cost,
        Inv.payment_method,
    ).filter(Inv.type == "S", *inv_filters).all()

    # 2) مبيعات غير مباشرة فقط (IDs)
    sales_non_direct = []
    sale_ids = []
    for r in sales_all:
        method = (r.payment_method or "").strip()
        if method in DIRECT_METHODS:
            continue
        sales_non_direct.append(r)
        sale_ids.append(int(r.id))

    # 3) allocations sums مرة واحدة لكل الفواتير غير المباشرة
    alloc_map: Dict[int, float] = {}
    if sale_ids:
        rows = (
            db.query(Alloc.invoice_id, func.coalesce(func.sum(Alloc.amount), 0.0))
            .filter(Alloc.invoice_id.in_(sale_ids))
            .group_by(Alloc.invoice_id)
            .all()
        )
        alloc_map = {int(i): float(s or 0.0) for (i, s) in rows}

    # 4) returns المرتبطة بالفواتير غير المباشرة (group by original_sale_id) مرة واحدة
    ret_link_map: Dict[int, Tuple[float, float]] = {}
    if sale_ids:
        rows = (
            db.query(
                Inv.original_sale_id,
                func.coalesce(func.sum(Inv.total), 0.0),
                func.coalesce(func.sum(Inv.return_shipping_fee), 0.0),
            )
            .filter(Inv.type == "R", Inv.original_sale_id.in_(sale_ids))
            .group_by(Inv.original_sale_id)
            .all()
        )
        ret_link_map = {int(sid): (float(t or 0.0), float(f or 0.0)) for (sid, t, f) in rows}

    # 5) covers مرة واحدة
    covered_set = set()
    if sale_ids:
        rows = db.query(Cover.invoice_id).filter(Cover.invoice_id.in_(sale_ids)).all()
        covered_set = {int(x[0]) for x in rows}

    # 6) returns داخل الفترة لكل شركة (للعرض أعلى الجدول) — نفس منطق totals_for_company
    ret_company_rows = (
        db.query(
            Inv.shipping_company,
            func.coalesce(func.sum(Inv.total), 0.0),
            func.coalesce(func.sum(Inv.return_shipping_fee), 0.0),
        )
        .filter(Inv.type == "R", *inv_filters)
        .group_by(Inv.shipping_company)
        .all()
    )
    ret_company_map = {c: (float(t or 0.0), float(f or 0.0)) for (c, t, f) in ret_company_rows}

    # ========== تجميع per_company ==========
    per_company: Dict[str, Dict[str, float]] = {}

    # actual_ship_sum (لكل المبيعات S حتى المباشرة)
    for r in sales_all:
        comp = (r.shipping_company or "").strip()
        if not comp:
            continue
        pc = per_company.setdefault(comp, {
            "sales_sum": 0.0,
            "actual_ship_sum": 0.0,
            "returns_sum": 0.0,
            "returns_ship_fees": 0.0,
            "paid_sum": 0.0,
            "net_due": 0.0,
        })
        pc["actual_ship_sum"] += float(r.actual_shipping_cost or 0.0)

    # sales_sum + paid_sum + net_due (على الفواتير غير المباشرة)
    for r in sales_non_direct:
        comp = (r.shipping_company or "").strip()
        if not comp:
            continue
        inv_id = int(r.id)
        pc = per_company.setdefault(comp, {
            "sales_sum": 0.0,
            "actual_ship_sum": 0.0,
            "returns_sum": 0.0,
            "returns_ship_fees": 0.0,
            "paid_sum": 0.0,
            "net_due": 0.0,
        })

        pc["sales_sum"] += float(r.total or 0.0)

        paid_on_inv = float(alloc_map.get(inv_id, 0.0))
        pc["paid_sum"] += paid_on_inv

        # استبعاد المغطاة إداريًا
        if inv_id in covered_set:
            continue

        base_due = float(r.total or 0.0) - float(r.actual_shipping_cost or 0.0)
        ret_total, ret_ship = ret_link_map.get(inv_id, (0.0, 0.0))
        due_after_returns = base_due - ret_total - ret_ship
        outstanding = round(due_after_returns - paid_on_inv, 2)
        if outstanding > 0.009:
            pc["net_due"] += outstanding

    # returns_sum + returns_ship_fees (كل المرتجعات داخل الفترة لنفس الشركة)
    for comp, (rt, rf) in ret_company_map.items():
        if not comp:
            continue
        pc = per_company.setdefault(comp, {
            "sales_sum": 0.0,
            "actual_ship_sum": 0.0,
            "returns_sum": 0.0,
            "returns_ship_fees": 0.0,
            "paid_sum": 0.0,
            "net_due": 0.0,
        })
        pc["returns_sum"] = float(rt or 0.0)
        pc["returns_ship_fees"] = float(rf or 0.0)

    # تقريب القيم (كما كنت تعمل)
    for comp in list(per_company.keys()):
        per_company[comp]["sales_sum"] = float(per_company[comp]["sales_sum"] or 0.0)
        per_company[comp]["actual_ship_sum"] = float(per_company[comp]["actual_ship_sum"] or 0.0)
        per_company[comp]["returns_sum"] = float(per_company[comp]["returns_sum"] or 0.0)
        per_company[comp]["returns_ship_fees"] = float(per_company[comp]["returns_ship_fees"] or 0.0)
        per_company[comp]["paid_sum"] = float(per_company[comp]["paid_sum"] or 0.0)
        per_company[comp]["net_due"] = float(round(per_company[comp]["net_due"] or 0.0, 2))

    # overall = مجموع الشركات (نفس الأعمدة)
    overall = {
        "sales_sum": 0.0,
        "actual_ship_sum": 0.0,
        "returns_sum": 0.0,
        "returns_ship_fees": 0.0,
        "paid_sum": 0.0,
        "net_due": 0.0,
    }
    for _, pc in per_company.items():
        overall["sales_sum"] += float(pc["sales_sum"] or 0.0)
        overall["actual_ship_sum"] += float(pc["actual_ship_sum"] or 0.0)
        overall["returns_sum"] += float(pc["returns_sum"] or 0.0)
        overall["returns_ship_fees"] += float(pc["returns_ship_fees"] or 0.0)
        overall["paid_sum"] += float(pc["paid_sum"] or 0.0)
        overall["net_due"] += float(pc["net_due"] or 0.0)

    overall["net_due"] = float(round(overall["net_due"] or 0.0, 2))
    return per_company, overall



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

    # Default period: from first day of current month until today
    today = date.today()

    if not dfrom:
        dfrom = today.replace(day=1).strftime("%Y-%m-%d")

    if not dto:
        dto = today.strftime("%Y-%m-%d")

    df = parse_date(dfrom)
    dt = parse_date(dto)

    s_dt = start_of_day(df) if df else None
    e_dt = end_of_day(dt) if dt else None

    if not company:
        # ✅ بدل totals_for_company لكل شركة (البطء الكبير)
        # نحسب نفس النتائج لكن بـ Bulk Queries قليلة جدًا
        per_company_map, overall = totals_all_companies_bulk(db, s_dt, e_dt)

        per_company = []
        for comp in companies:
            t = per_company_map.get(comp, {
                "sales_sum": 0.0,
                "actual_ship_sum": 0.0,
                "returns_sum": 0.0,
                "returns_ship_fees": 0.0,
                "paid_sum": 0.0,
                "net_due": 0.0,
            })
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

            "page_size": page_size,
        }
        return templates.TemplateResponse("shipping_accounts.html", ctx)

    # شركة محددة → فواتير الشركة خلال الفترة (أو كل الوقت)
    # شركة محددة → فواتير الشركة خلال الفترة (أو كل الوقت)
    inv_filters = [models.Invoice.shipping_company == company]
    if s_dt: inv_filters.append(models.Invoice.created_at >= s_dt)
    if e_dt: inv_filters.append(models.Invoice.created_at <= e_dt)

    # ✅ نجيب الفواتير S مرة واحدة (بدون Invoice objects كاملة)
    Inv = models.Invoice
    sales_rows = db.query(
        Inv.id,
        Inv.invoice_code,
        Inv.total,
        Inv.actual_shipping_cost,
        Inv.created_at,
        Inv.payment_method,
        Inv.customer_name,
        Inv.customer_phone,
        Inv.shipping_company,
    ).filter(Inv.type == "S", *inv_filters).order_by(Inv.id.desc()).all()

    sale_ids = [int(r.id) for r in sales_rows]

    # ✅ allocations per invoice مرة واحدة
    alloc_map: Dict[int, float] = {}
    if sale_ids:
        rows = (
            db.query(models.ShippingAllocation.invoice_id, func.coalesce(func.sum(models.ShippingAllocation.amount), 0.0))
            .filter(models.ShippingAllocation.invoice_id.in_(sale_ids))
            .group_by(models.ShippingAllocation.invoice_id)
            .all()
        )
        alloc_map = {int(i): float(s or 0.0) for (i, s) in rows}

    # ✅ returns linked per sale_id مرة واحدة
    ret_link_map: Dict[int, Tuple[float, float]] = {}
    if sale_ids:
        rows = (
            db.query(
                Inv.original_sale_id,
                func.coalesce(func.sum(Inv.total), 0.0),
                func.coalesce(func.sum(Inv.return_shipping_fee), 0.0),
            )
            .filter(Inv.type == "R", Inv.original_sale_id.in_(sale_ids))
            .group_by(Inv.original_sale_id)
            .all()
        )
        ret_link_map = {int(sid): (float(t or 0.0), float(f or 0.0)) for (sid, t, f) in rows}

    # ✅ covers مرة واحدة
    covered_set = set()
    if sale_ids and hasattr(models, "ShippingAdminCover"):
        rows = db.query(models.ShippingAdminCover.invoice_id).filter(models.ShippingAdminCover.invoice_id.in_(sale_ids)).all()
        covered_set = {int(x[0]) for x in rows}

    # ✅ returns داخل الفترة لنفس الشركة (للكروت)
    rets = db.query(Inv).filter(Inv.type == "R", *inv_filters).all()
    returns_sum = sum(float(r.total or 0) for r in rets)
    returns_ship_fees = sum(float(r.return_shipping_fee or 0) for r in rets)

    # ✅ تجهيز القوائم + حساب الكروت بنفس منطقك
    invoices_unpaid_all, invoices_paid_all = [], []
    sales_sum_non_direct = 0.0
    actual_ship_sum_all = 0.0
    paid_sum_alloc = 0.0
    net_due = 0.0

    for r in sales_rows:
        method = (r.payment_method or "").strip()
        is_direct = method in DIRECT_METHODS

        actual_ship_sum_all += float(r.actual_shipping_cost or 0.0)

        inv_id = int(r.id)
        paid_on_inv = float(alloc_map.get(inv_id, 0.0))

        ret_total, ret_ship = ret_link_map.get(inv_id, (0.0, 0.0))
        base_due = float(r.total or 0) - float(r.actual_shipping_cost or 0)
        due_after_returns = base_due - ret_total - ret_ship

        if is_direct:
            rec = {
                "id": inv_id,
                "invoice_code": r.invoice_code,
                "type": "S",
                "total": float(r.total or 0),
                "actual_shipping_cost": float(r.actual_shipping_cost or 0),
                "created_at": r.created_at,
                "due": 0.0, "paid": 0.0, "outstanding": 0.0,
                "linked_returns_total": round(ret_total, 2),
                "linked_returns_ship": round(ret_ship, 2),
                "direct_paid": True,
                "payment_method": method,
                "admin_covered": False,
                "customer_name": r.customer_name or "-",
                "customer_phone": r.customer_phone or "",
            }
            invoices_paid_all.append(rec)
            continue

        sales_sum_non_direct += float(r.total or 0.0)
        paid_sum_alloc += paid_on_inv

        covered = inv_id in covered_set
        outstanding = round(due_after_returns - paid_on_inv, 2)

        rec = {
            "id": inv_id,
            "invoice_code": r.invoice_code,
            "type": "S",
            "total": float(r.total or 0),
            "actual_shipping_cost": float(r.actual_shipping_cost or 0),
            "created_at": r.created_at,
            "due": round(due_after_returns, 2),
            "paid": round(paid_on_inv, 2),
            "outstanding": outstanding,
            "linked_returns_total": round(ret_total, 2),
            "linked_returns_ship": round(ret_ship, 2),
            "direct_paid": False,
            "payment_method": method,
            "admin_covered": covered,
            "customer_name": r.customer_name or "-",
            "customer_phone": r.customer_phone or "",
        }

        if covered or abs(outstanding) < 0.01:
            invoices_paid_all.append(rec)
        else:
            invoices_unpaid_all.append(rec)

        if (not covered) and outstanding > 0.009:
            net_due += outstanding

    t = {
        "sales_sum": float(sales_sum_non_direct),
        "actual_ship_sum": float(actual_ship_sum_all),
        "returns_sum": float(returns_sum),
        "returns_ship_fees": float(returns_ship_fees),
        "paid_sum": float(paid_sum_alloc),
        "net_due": float(round(net_due, 2)),
        "sales": [],   # مش محتاجينهم هنا
        "rets": rets,
        "payments": [],
    }
    

    def _matches_invoice(rec, q):
        if not q:
            return True
        _q = q.strip().lower()
        return (_q in (rec.get("invoice_code","") or "").lower()) or (_q in (rec.get("customer_phone","") or "").lower())

    if unpaid_q:
        invoices_unpaid_all = [r for r in invoices_unpaid_all if _matches_invoice(r, unpaid_q)]

    if paid_q:
        invoices_paid_all = [r for r in invoices_paid_all if _matches_invoice(r, paid_q)]

    def _paginate(lst, page, size):
        total = len(lst)
        pages = (total + size - 1) // size if size > 0 else 1
        page = max(1, min(page, max(1, pages)))
        start = (page - 1) * size
        end = start + size
        return lst[start:end], page, pages

    unpaid_page_size = int(unpaid_page_size or 0) or int(page_size or 10)
    paid_page_size   = int(paid_page_size or 0)   or int(page_size or 10)
    unpaid_page_size = max(1, unpaid_page_size)
    paid_page_size   = max(1, paid_page_size)

    unpaid_slice, unpaid_page, unpaid_pages = _paginate(invoices_unpaid_all, int(unpaid_page or 1), unpaid_page_size)
    paid_slice,   paid_page,   paid_pages   = _paginate(invoices_paid_all,   int(paid_page or 1),   paid_page_size)

    pay_filters = [models.ShippingPayment.company == company]
    if dfrom: pay_filters.append(models.ShippingPayment.date >= dfrom)
    if dto:   pay_filters.append(models.ShippingPayment.date <= dto)
    payments = db.query(models.ShippingPayment).filter(*pay_filters).order_by(models.ShippingPayment.id.desc()).all()

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

    def _paginate2(lst, page, size):
        total = len(lst)
        pages = (total + size - 1) // size if size > 0 else 1
        page = max(1, min(int(page or 1), max(1, pages)))
        start = (page - 1) * size
        end = start + size
        return lst[start:end], page, pages

    payments_page_size = int(payments_page_size or 5)
    payments_page_size = max(1, payments_page_size)
    payments_page = max(1, int(payments_page or 1))

    payments_slice, payments_page, payments_pages = _paginate2(payments_with_avail, payments_page, payments_page_size)

    payments_select = [p for p in payments_with_avail if (p.get("available", 0) or 0) > 0.0009]

    ctx = {
        "request": request,
        "companies": companies,
        "company": company,
        "dfrom": dfrom or "",
        "dto": dto or "",
        "all_companies": distinct_companies(db),
        "has_selection": True,

        "sales_sum": t["sales_sum"],
        "actual_ship_sum": t["actual_ship_sum"],
        "returns_sum": t["returns_sum"],
        "returns_ship_fees": t["returns_ship_fees"],
        "paid_sum": t["paid_sum"],
        "net_due": t["net_due"],

        "sales": t["sales"],
        "rets": t["rets"],

        "payments": payments,
        "payments_with_avail": payments_with_avail,
        "payments_table": payments_slice,
        "payments_page": payments_page,
        "payments_pages": payments_pages,
        "payments_page_size": payments_page_size,
        "payments_select": payments_select,

        "invoices_unpaid": unpaid_slice,
        "invoices_paid": paid_slice,

        "unpaid_page": unpaid_page,
        "unpaid_pages": unpaid_pages,
        "paid_page": paid_page,
        "paid_pages": paid_pages,
        "unpaid_page_size": unpaid_page_size,
        "paid_page_size": paid_page_size,
        "unpaid_q": unpaid_q,
        "paid_q": paid_q,

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

    if not inv or not pay or inv.shipping_company != company or pay.company != company:
        url = f"/shipping?company={company}&unpaid_page={unpaid_page}&paid_page={paid_page}&page_size={page_size}"
        if q_dfrom: url += f"&dfrom={q_dfrom}"
        if q_dto:   url += f"&dto={q_dto}"
        return RedirectResponse(url=url + "&msg=⚠️ بيانات غير صحيحة", status_code=303)

    if inv.type == "R":
        url = f"/shipping?company={company}&unpaid_page={unpaid_page}&paid_page={paid_page}&page_size={page_size}"
        if q_dfrom: url += f"&dfrom={q_dfrom}"
        if q_dto:   url += f"&dto={q_dto}"
        return RedirectResponse(url=url + "&msg=⚠️ لا يمكن تخصيص دفعة لفاتورة مرتجع", status_code=303)

    method = (inv.payment_method or "").strip()
    if method in DIRECT_METHODS:
        url = f"/shipping?company={company}&unpaid_page={unpaid_page}&paid_page={paid_page}&page_size={page_size}"
        if q_dfrom: url += f"&dfrom={q_dfrom}"
        if q_dto:   url += f"&dto={q_dto}"
        return RedirectResponse(url=url + "&msg=⚠️ الفاتورة مدفوعة مباشرة، لا تحتاج تخصيص", status_code=303)

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

    p.date = date_.strip()
    p.amount = amount
    p.note = note.strip() or None
    db.commit()

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

    db.query(models.ShippingAllocation).filter(models.ShippingAllocation.payment_id == p.id).delete()

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

    method = (inv.payment_method or "").strip()
    if method in DIRECT_METHODS or is_admin_covered(db, inv.id, company):
        url = f"/shipping?company={company}&unpaid_page={unpaid_page}&paid_page={paid_page}&page_size={page_size}"
        if q_dfrom: url += f"&dfrom={q_dfrom}"
        if q_dto:   url += f"&dto={q_dto}"
        return RedirectResponse(url=url + "&msg=ℹ️ هذه الفاتورة ليست قابلة للتغطية", status_code=303)

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

@router.get("/invoice/{invoice_id}", response_class=HTMLResponse)
def invoice_popup(request: Request, invoice_id: int, db: Session = Depends(get_db)):
    inv = db.query(models.Invoice).get(int(invoice_id))
    if not inv:
        raise HTTPException(status_code=404, detail="Invoice not found")

    # ✅ Fallback values (عشان مايحصلش NameError)
    customer_name = getattr(inv, "customer_name", None) or "-"
    customer_phone = getattr(inv, "customer_phone", None) or "-"
    address = getattr(inv, "address", None) or getattr(inv, "customer_address", None) or "-"
    governorate = getattr(inv, "governorate", None) or getattr(inv, "city", None) or "-"

    shipping_company = getattr(inv, "shipping_company", None) or "-"
    actual_shipping_cost = float(getattr(inv, "actual_shipping_cost", 0) or 0)
    customer_shipping_fee = float(getattr(inv, "customer_shipping_fee", 0) or 0)
    payment_method = getattr(inv, "payment_method", None) or "-"

    total_invoice = float(getattr(inv, "total", 0) or 0)
    discount_value = float(getattr(inv, "discount_value", 0) or getattr(inv, "discount", 0) or 0)
    total_after_discount = float(getattr(inv, "total_after_discount", None) or (total_invoice - discount_value))

    # ✅ Items (اختياري) — لو عندك موديل items هنجيبه، لو مش موجود هنعرض بدون منتجات
    items = []
    if hasattr(models, "InvoiceItem"):
        items = db.query(models.InvoiceItem).filter(models.InvoiceItem.invoice_id == inv.id).all()
    elif hasattr(models, "InvoiceLine"):
        items = db.query(models.InvoiceLine).filter(models.InvoiceLine.invoice_id == inv.id).all()

    ctx = {
        "request": request,
        "inv": inv,
        "customer_name": customer_name,
        "customer_phone": customer_phone,
        "address": address,
        "governorate": governorate,

        "items_rows": items,
        "total_invoice": total_invoice,
        "discount_value": discount_value,
        "total_after_discount": total_after_discount,

        "shipping_company": shipping_company,
        "actual_shipping_cost": actual_shipping_cost,
        "customer_shipping_fee": customer_shipping_fee,
        "payment_method": payment_method,
    }

    return templates.TemplateResponse("invoice_popup.html", ctx)


# ═══════════════════════════════════════════════
# نقل فاتورة من شركة شحن لأخرى
# ═══════════════════════════════════════════════
@router.post("/transfer-company")
def transfer_company(
    invoice_id: int = Form(...),
    from_company: str = Form(...),
    to_company: str = Form(...),
    q_dfrom: str = Form(""),
    q_dto: str = Form(""),
    unpaid_page: int = Form(1),
    paid_page: int = Form(1),
    page_size: int = Form(10),
    db: Session = Depends(get_db),
):
    inv = db.query(models.Invoice).get(invoice_id)
    if not inv:
        return RedirectResponse(
            url=f"/shipping?company={from_company}&dfrom={q_dfrom}&dto={q_dto}&msg=❌+الفاتورة+غير+موجودة",
            status_code=303
        )

    # تحقق إن مفيش تخصيصات
    alloc_sum = allocations_sum_for_invoice(db, invoice_id)
    if alloc_sum > 0:
        return RedirectResponse(
            url=f"/shipping?company={from_company}&dfrom={q_dfrom}&dto={q_dto}&unpaid_page={unpaid_page}&paid_page={paid_page}&page_size={page_size}&msg=❌+مش+ممكن+النقل،+الفاتورة+عندها+تخصيص+بقيمة+{int(alloc_sum)}+ج،+الغ+التخصيص+الأول",
            status_code=303
        )

    # نقل الفاتورة
    inv.shipping_company = to_company.strip()
    db.commit()

    # نرجع لنفس الشركة اللي كنا فيها
    return RedirectResponse(
        url=f"/shipping?company={from_company}&dfrom={q_dfrom}&dto={q_dto}&unpaid_page={unpaid_page}&paid_page={paid_page}&page_size={page_size}&msg=✅+تم+نقل+الفاتورة+{inv.invoice_code}+لـ+{to_company}",
        status_code=303
    )
