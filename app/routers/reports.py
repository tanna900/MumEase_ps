# app/routers/reports.py
import csv, io
from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, and_, or_
from datetime import datetime, timedelta, date

from app.database import SessionLocal
from app import models
from fastapi.templating import Jinja2Templates

router = APIRouter(prefix="/reports", tags=["Reports"])
templates = Jinja2Templates(directory="app/templates")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.get("", response_class=HTMLResponse)
def reports(request: Request, date_from: str = "", date_to: str = "", t: str = "all", export: str = "", db: Session = Depends(get_db)):
    q = db.query(models.Invoice)
    if date_from: q = q.filter(models.Invoice.created_at >= date_from)
    if date_to:   q = q.filter(models.Invoice.created_at <= date_to + " 23:59:59")
    if t in ("S","R"): q = q.filter(models.Invoice.type == t)
    rows = q.order_by(models.Invoice.id.desc()).all()

    if export == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Code","Type","Customer","Phone","Subtotal","Discount","Shipping","Total","Date"])
        for r in rows:
            w.writerow([r.invoice_code, r.type, r.customer_name, r.customer_phone, r.subtotal, r.discount, r.shipping_cost, r.total, r.created_at])
        buf.seek(0)
        return StreamingResponse(
            iter([buf.getvalue().encode("utf-8")]),
            media_type="text/csv",
            headers={"Content-Disposition":"attachment; filename=report.csv"}
        )
    return templates.TemplateResponse("reports.html", {"request": request, "rows": rows, "date_from": date_from, "date_to": date_to, "t": t})

# ===== Helpers (تاريخ) =====
def _parse_date(s: str):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except:
        return None

def _apply_range(q, col, date_from: str, date_to: str):
    df = _parse_date(date_from)
    dt = _parse_date(date_to)
    if df:
        q = q.filter(col >= df)
    if dt:
        q = q.filter(col <= (dt + timedelta(days=1) - timedelta(seconds=1)))
    return q

def _apply_range_on_string_date(q, col, date_from: str, date_to: str):
    # col = models.FinanceEntry.date (string YYYY-MM-DD)
    if date_from:
        q = q.filter(col >= date_from)
    if date_to:
        q = q.filter(col <= date_to)
    return q

# ===== Helpers (حسابات) =====
def _allocations_sum_for_invoice(db: Session, invoice_id: int) -> float:
    return float(
        db.query(func.coalesce(func.sum(models.ShippingAllocation.amount), 0.0))
          .filter(models.ShippingAllocation.invoice_id == invoice_id)
          .scalar() or 0.0
    )

def _linked_returns_totals(db: Session, sale_id: int):
    rets = db.query(models.Invoice).filter(
        models.Invoice.type == "R",
        models.Invoice.original_sale_id == sale_id
    ).all()
    total_ret = sum(float(r.total or 0) for r in rets)
    total_ret_ship = sum(float(r.return_shipping_fee or 0) for r in rets)
    return float(total_ret), float(total_ret_ship)

def _get_admin_covered_invoice_ids(db: Session, date_from: str, date_to: str):
    admin_ids = set()
    AdminCover = getattr(models, "ShippingAdminCover", None)
    if AdminCover is None:
        return admin_ids

    q = db.query(AdminCover.invoice_id)
    has_created_at = hasattr(AdminCover, "created_at")

    if date_from or date_to:
        if has_created_at:
            df = _parse_date(date_from)
            dt = _parse_date(date_to)
            if df:
                q = q.filter(AdminCover.created_at >= df)
            if dt:
                q = q.filter(AdminCover.created_at <= (dt + timedelta(days=1) - timedelta(seconds=1)))

    rows = q.all()
    for (iid,) in rows:
        if iid is not None:
            admin_ids.add(int(iid))
    return admin_ids

# ====== Payment method normalization (FIX للدونات) ======
_DIRECT_PAID_ALIASES = {
    # English
    "instapay", "insta pay", "insta-pay",
    "wallet", "e-wallet", "ewallet",
    "vodafone cash", "vf cash", "vodafone",
    "cash", "cod", "cash on delivery",
    "card", "visa", "mastercard", "debit", "credit",
    # Arabic
    "محفظة إلكترونية", "محفظه الكترونيه", "محفظة", "محفظه",
    "فودافون كاش", "كاش", "نقدي",
    "فيزا", "ماستر", "كارت",
}

def _norm_pm(val: str | None) -> str:
    return (val or "").strip().lower()

def _is_invoice_paid(db: Session, row, admin_cover_ids: set, tol: float = 0.01) -> bool:
    """
    يحدد هل الفاتورة مدفوعة أم لا:
    - دفع مباشر (InstaPay/محفظة/كاش/كارت... بعد تطبيع)
    - أو مغطّاة إداريًا
    - أو التخصيصات >= صافي المستحق بعد خصم الشحن الفعلي/المرتجعات
    """
    inv_id = int(row.id)

    pm = _norm_pm(getattr(row, "payment_method", None))
    direct_paid = pm in _DIRECT_PAID_ALIASES

    # عمود admin_covered إن وُجد
    admin_flag = False
    try:
        admin_flag = bool(getattr(row, "admin_covered"))
    except Exception:
        admin_flag = False
    admin_covered = admin_flag or (inv_id in admin_cover_ids)

    if direct_paid or admin_covered:
        return True

    gross = float(row.total or 0.0)

    ship_actual = row.actual_shipping_cost if row.actual_shipping_cost is not None else row.shipping_cost
    ship_actual = float(ship_actual or 0.0)

    ret_total, ret_ship = _linked_returns_totals(db, inv_id)
    due_after_returns = max(gross - ship_actual - ret_total - ret_ship, 0.0)

    paid_on_inv = _allocations_sum_for_invoice(db, inv_id)

    return (paid_on_inv + tol) >= due_after_returns

# ====== API: حالة الطلبات للدونات في الهوم ======
@router.get("/api/order-stats")
def order_stats(
    date_from: str = Query("", description="YYYY-MM-DD"),
    date_to: str = Query("", description="YYYY-MM-DD"),
    db: Session = Depends(get_db)
):
    # Returns count
    q_ret = db.query(func.count(models.Invoice.id)).filter(models.Invoice.type == "R")
    q_ret = _apply_range(q_ret, models.Invoice.created_at, date_from, date_to)
    return_count = int(q_ret.scalar() or 0)

    # Sales rows (only needed columns)
    admin_col = getattr(models.Invoice, "admin_covered", None)
    base_cols = [
        models.Invoice.id,
        models.Invoice.total,
        models.Invoice.shipping_cost,
        models.Invoice.actual_shipping_cost,
        models.Invoice.payment_method,
        models.Invoice.created_at,
    ]
    if admin_col is not None:
        base_cols.append(admin_col.label("admin_covered"))

    q_sales = db.query(*base_cols).filter(models.Invoice.type == "S")
    q_sales = _apply_range(q_sales, models.Invoice.created_at, date_from, date_to)
    sales_rows = q_sales.all()

    admin_cover_ids = _get_admin_covered_invoice_ids(db, date_from, date_to)

    paid_count = 0
    unpaid_count = 0
    for r in sales_rows:
        if _is_invoice_paid(db, r, admin_cover_ids):
            paid_count += 1
        else:
            unpaid_count += 1

    total_orders = len(sales_rows) + return_count

    if date_from and date_to:
        range_label = f"من {date_from} إلى {date_to}"
    elif date_from:
        range_label = f"من {date_from} إلى الآن"
    elif date_to:
        range_label = f"حتى {date_to}"
    else:
        range_label = "كل الفواتير"

    return JSONResponse({
        "total_orders": total_orders,
        "paid_count": paid_count,
        "unpaid_count": unpaid_count,
        "return_count": return_count,
        "range_label": range_label
    })

# ============================================================
# 🆕 API جديد للهوم: Dashboard (UI + محاسبة + جداول)
# ============================================================
@router.get("/api/dashboard")
def dashboard_api(
    date_from: str = Query("", description="YYYY-MM-DD"),
    date_to: str = Query("", description="YYYY-MM-DD"),
    db: Session = Depends(get_db)
):
    # =========================
    # Standard defaults:
    # - KPIs + tables default to TODAY if no range provided
    # =========================
    today_str = date.today().strftime("%Y-%m-%d")
    if not date_from and not date_to:
        date_from = today_str
        date_to = today_str

    # ---- Range label ----
    if date_from and date_to:
        range_label = f"من {date_from} إلى {date_to}"
    elif date_from:
        range_label = f"من {date_from} إلى الآن"
    elif date_to:
        range_label = f"حتى {date_to}"
    else:
        range_label = "كل الفواتير"

    # ---- Base queries ----
    q_sales = db.query(models.Invoice).filter(models.Invoice.type == "S")
    q_ret   = db.query(models.Invoice).filter(models.Invoice.type == "R")
    q_sales = _apply_range(q_sales, models.Invoice.created_at, date_from, date_to)
    q_ret   = _apply_range(q_ret,   models.Invoice.created_at, date_from, date_to)

    sales_rows = q_sales.order_by(models.Invoice.id.desc()).all()
    ret_rows   = q_ret.order_by(models.Invoice.id.desc()).all()

    sales_sum = float(sum(float(x.total or 0) for x in sales_rows))
    returns_sum = float(sum(float(x.total or 0) for x in ret_rows))
    sales_count = int(len(sales_rows))
    returns_count = int(len(ret_rows))
    invoices_count = int(sales_count + returns_count)

    # ---- Shipping expense (range) ----
    ship_sales_q = db.query(func.coalesce(func.sum(models.Invoice.actual_shipping_cost), 0.0)).filter(models.Invoice.type == "S")
    ship_sales_q = _apply_range(ship_sales_q, models.Invoice.created_at, date_from, date_to)
    ship_sales_sum = float(ship_sales_q.scalar() or 0.0)

    ship_ret_q = db.query(func.coalesce(func.sum(models.Invoice.return_shipping_fee), 0.0)).filter(models.Invoice.type == "R")
    ship_ret_q = _apply_range(ship_ret_q, models.Invoice.created_at, date_from, date_to)
    ship_ret_sum = float(ship_ret_q.scalar() or 0.0)

    shipping_expense = float(ship_sales_sum + ship_ret_sum)

    # ✅ Net sales = Sales - (Actual Shipping Cost for Sales)
    net_sales = float(sales_sum - ship_sales_sum)

    # ---- Quality KPIs ----
    avg_order_value = float(sales_sum / max(sales_count, 1)) if sales_count else 0.0
    avg_discount = float(sum(float(x.discount or 0.0) for x in sales_rows) / max(sales_count, 1)) if sales_count else 0.0
    return_rate_pct = float((returns_sum / sales_sum) * 100.0) if sales_sum > 0 else 0.0

    # ---- Finance KPIs (same range) ----
    q_in = db.query(func.coalesce(func.sum(models.FinanceEntry.amount), 0.0)).filter(models.FinanceEntry.type == "IN")
    q_out = db.query(func.coalesce(func.sum(models.FinanceEntry.amount), 0.0)).filter(models.FinanceEntry.type == "OUT")
    q_in = _apply_range_on_string_date(q_in, models.FinanceEntry.date, date_from, date_to)
    q_out = _apply_range_on_string_date(q_out, models.FinanceEntry.date, date_from, date_to)
    cash_in = float(q_in.scalar() or 0.0)
    cash_out = float(q_out.scalar() or 0.0)
    cash_net = float(cash_in - cash_out)

    # ---- Inventory value (global) ----
    inv_val_q = db.query(func.coalesce(func.sum((models.Product.stock * models.Product.cost_price)), 0.0))
    inventory_value = float(inv_val_q.scalar() or 0.0)

    # ---- Ads expense (OUT) within same range ----
    ads_q = db.query(func.coalesce(func.sum(models.FinanceEntry.amount), 0.0)).filter(models.FinanceEntry.type == "OUT").filter(
        or_(
            models.FinanceEntry.category.ilike("%ads%"),
            models.FinanceEntry.category.ilike("%ad%"),
            models.FinanceEntry.category.ilike("%marketing%"),
            models.FinanceEntry.category.ilike("%اعلان%"),
            models.FinanceEntry.category.ilike("%إعلان%"),
            models.FinanceEntry.note.ilike("%ads%"),
            models.FinanceEntry.note.ilike("%ad%"),
            models.FinanceEntry.note.ilike("%marketing%"),
            models.FinanceEntry.note.ilike("%اعلان%"),
            models.FinanceEntry.note.ilike("%إعلان%"),
        )
    )
    ads_q = _apply_range_on_string_date(ads_q, models.FinanceEntry.date, date_from, date_to)
    ads_expense = float(ads_q.scalar() or 0.0)

    # ---- COGS (approx using current Product.cost_price) ----
    cogs_q = (
        db.query(func.coalesce(func.sum(models.InvoiceItem.qty * models.Product.cost_price), 0.0))
          .join(models.Invoice, models.InvoiceItem.invoice_id == models.Invoice.id)
          .join(models.Product, models.InvoiceItem.product_id == models.Product.id)
          .filter(models.Invoice.type == "S")
    )
    cogs_q = _apply_range(cogs_q, models.Invoice.created_at, date_from, date_to)
    cogs = float(cogs_q.scalar() or 0.0)

    # ✅ Gross Profit = (Sales - SalesShipping) - Returns - Ads - COGS
    gross_profit = float((sales_sum - ship_sales_sum) - returns_sum - ads_expense - cogs)
    gross_margin_pct = float((gross_profit / sales_sum) * 100.0) if sales_sum > 0 else 0.0

    # ---- Low stock count (global) ----
    low_stock_count = int(
        db.query(models.Product)
          .filter((models.Product.stock == None) | (models.Product.stock < 4))
          .count()
    )

    # ---- Latest invoices (smaller) ----
    q_latest = db.query(models.Invoice)
    q_latest = _apply_range(q_latest, models.Invoice.created_at, date_from, date_to)
    latest = q_latest.order_by(models.Invoice.id.desc()).limit(6).all()

    latest_rows = []
    for r in latest:
        try:
            ca = r.created_at.strftime("%H:%M")
        except Exception:
            ca = str(r.created_at or "")
        latest_rows.append({
            "id": int(r.id),
            "invoice_code": r.invoice_code,
            "customer_name": r.customer_name,
            "type": (r.type or "").strip(),
            "total": float(r.total or 0.0),
            "created_at": ca,
        })

    # ---- Top products (smaller) ----
    top_rows = (
        db.query(
            models.InvoiceItem.product_name.label("name"),
            func.sum(models.InvoiceItem.qty).label("qty"),
            func.sum(models.InvoiceItem.line_total).label("rev"),
        )
        .join(models.Invoice, models.InvoiceItem.invoice_id == models.Invoice.id)
        .filter(models.Invoice.type == "S")
    )
    top_rows = _apply_range(top_rows, models.Invoice.created_at, date_from, date_to)
    top_rows = (
        top_rows.group_by(models.InvoiceItem.product_name)
        .order_by(func.sum(models.InvoiceItem.qty).desc(), func.sum(models.InvoiceItem.line_total).desc())
        .limit(6)
        .all()
    )
    top_products = [{"name": r.name, "qty": int(r.qty or 0), "rev": float(r.rev or 0.0)} for r in top_rows]

    return JSONResponse({
        "date_from": date_from,
        "date_to": date_to,
        "range_label": range_label,

        "kpis": {
            "sales_sum": round(sales_sum, 2),
            "returns_sum": round(returns_sum, 2),
            "net_sales": round(net_sales, 2),

            "sales_count": sales_count,
            "returns_count": returns_count,
            "invoices_count": invoices_count,

            "avg_order_value": round(avg_order_value, 2),
            "avg_discount": round(avg_discount, 2),
            "return_rate_pct": round(return_rate_pct, 2),

            "gross_profit": round(gross_profit, 2),
            "gross_margin_pct": round(gross_margin_pct, 2),

            "low_stock_count": low_stock_count,

            # breakdown extras (optional)
            "ads_expense": round(ads_expense, 2),
            "cogs": round(cogs, 2),
            "sales_shipping": round(ship_sales_sum, 2),
        },

        "finance": {
            "cash_net": round(cash_net, 2),
            "shipping_expense": round(shipping_expense, 2),
            "inventory_value": round(inventory_value, 2),
        },

        "tables": {
            "latest_invoices": latest_rows,
            "top_products": top_products,
        }
    })

# ============================================================
# 🆕 API: Profit Breakdown لنفس الفترة (للداشبورد)
# ============================================================
@router.get("/api/profit")
def profit_api(
    date_from: str = Query("", description="YYYY-MM-DD"),
    date_to: str = Query("", description="YYYY-MM-DD"),
    db: Session = Depends(get_db)
):
    # ---- Revenue pieces ----
    # Sales core = subtotal - discount (بدون شحن)
    q_sales_core = db.query(func.coalesce(func.sum(models.Invoice.subtotal - models.Invoice.discount), 0.0))\
                     .filter(models.Invoice.type == "S")
    q_sales_core = _apply_range(q_sales_core, models.Invoice.created_at, date_from, date_to)
    sales_core = float(q_sales_core.scalar() or 0.0)

    # Shipping income collected from customers
    q_ship_income = db.query(func.coalesce(func.sum(models.Invoice.shipping_cost), 0.0))\
                      .filter(models.Invoice.type == "S")
    q_ship_income = _apply_range(q_ship_income, models.Invoice.created_at, date_from, date_to)
    shipping_income = float(q_ship_income.scalar() or 0.0)

    # Sales total (includes shipping)
    q_sales_total = db.query(func.coalesce(func.sum(models.Invoice.total), 0.0))\
                      .filter(models.Invoice.type == "S")
    q_sales_total = _apply_range(q_sales_total, models.Invoice.created_at, date_from, date_to)
    sales_total = float(q_sales_total.scalar() or 0.0)

    # Returns total (as invoices total)
    q_returns_total = db.query(func.coalesce(func.sum(models.Invoice.total), 0.0))\
                        .filter(models.Invoice.type == "R")
    q_returns_total = _apply_range(q_returns_total, models.Invoice.created_at, date_from, date_to)
    returns_total = float(q_returns_total.scalar() or 0.0)

    # Net revenue (after returns)
    net_revenue_after_returns = float((sales_core + shipping_income) - returns_total)

    # ---- Actual shipping expense (sales + return fees) ----
    q_ship_actual_sales = db.query(func.coalesce(func.sum(models.Invoice.actual_shipping_cost), 0.0))\
                            .filter(models.Invoice.type == "S")
    q_ship_actual_sales = _apply_range(q_ship_actual_sales, models.Invoice.created_at, date_from, date_to)
    ship_actual_sales = float(q_ship_actual_sales.scalar() or 0.0)

    q_return_ship_fees = db.query(func.coalesce(func.sum(models.Invoice.return_shipping_fee), 0.0))\
                           .filter(models.Invoice.type == "R")
    q_return_ship_fees = _apply_range(q_return_ship_fees, models.Invoice.created_at, date_from, date_to)
    return_ship_fees = float(q_return_ship_fees.scalar() or 0.0)

    shipping_actual_total = float(ship_actual_sales + return_ship_fees)

    net_after_shipping = float(net_revenue_after_returns - shipping_actual_total)

    # ---- Ads expense (from FinanceEntry OUT/مصروف...) ----
    _OUT_LOWER = {"out", "مصروف", "مصروفات"}
    ads_q = db.query(func.coalesce(func.sum(models.FinanceEntry.amount), 0.0))\
              .filter(func.lower(models.FinanceEntry.type).in_(_OUT_LOWER))\
              .filter(or_(
                  models.FinanceEntry.category == "اعلانات",
                  models.FinanceEntry.category.ilike("%اعلان%"),
                  models.FinanceEntry.category.ilike("%ads%"),
                  models.FinanceEntry.category.ilike("%marketing%"),
                  models.FinanceEntry.note.ilike("%اعلان%"),
                  models.FinanceEntry.note.ilike("%ads%"),
                  models.FinanceEntry.note.ilike("%marketing%"),
              ))
    ads_q = _apply_range_on_string_date(ads_q, models.FinanceEntry.date, date_from, date_to)
    ads_expense = float(ads_q.scalar() or 0.0)

    # ---- COGS NET (sales - returns) ----
    cogs_sales_q = db.query(func.coalesce(func.sum(models.InvoiceItem.qty * models.Product.cost_price), 0.0))\
        .join(models.Invoice, models.InvoiceItem.invoice_id == models.Invoice.id)\
        .join(models.Product, models.InvoiceItem.product_id == models.Product.id)\
        .filter(models.Invoice.type == "S")
    cogs_sales_q = _apply_range(cogs_sales_q, models.Invoice.created_at, date_from, date_to)
    cogs_sales = float(cogs_sales_q.scalar() or 0.0)

    cogs_returns_q = db.query(func.coalesce(func.sum(models.InvoiceItem.qty * models.Product.cost_price), 0.0))\
        .join(models.Invoice, models.InvoiceItem.invoice_id == models.Invoice.id)\
        .join(models.Product, models.InvoiceItem.product_id == models.Product.id)\
        .filter(models.Invoice.type == "R")
    cogs_returns_q = _apply_range(cogs_returns_q, models.Invoice.created_at, date_from, date_to)
    cogs_returns = float(cogs_returns_q.scalar() or 0.0)

    cogs_net = float(cogs_sales - cogs_returns)

    # ---- Marketing commission (your rule) ----
    # commission = (Net Sales after returns and after actual shipping) * 5%
    base_for_commission = max(net_after_shipping, 0.0)
    marketer_commission = float(base_for_commission * 0.05)

    # ---- Net Profit (your definition) ----
    # profit = net_after_shipping - cogs_net - ads - commission
    net_profit = float(net_after_shipping - cogs_net - ads_expense - marketer_commission)

    margin_pct = float((net_profit / sales_total) * 100.0) if sales_total > 0 else 0.0

    return JSONResponse({
        "date_from": date_from,
        "date_to": date_to,

        "sales_total": sales_total,
        "sales_core": sales_core,
        "shipping_income": shipping_income,

        "returns_total": returns_total,
        "net_revenue_after_returns": net_revenue_after_returns,

        "ship_actual_sales": ship_actual_sales,
        "return_ship_fees": return_ship_fees,
        "shipping_actual_total": shipping_actual_total,

        "net_after_shipping": net_after_shipping,

        "cogs_sales": cogs_sales,
        "cogs_returns": cogs_returns,
        "cogs_net": cogs_net,

        "ads_expense": ads_expense,
        "marketer_commission": marketer_commission,

        "net_profit": net_profit,
        "margin_pct": margin_pct,
    })

# ============================================================
# 🆕 تقرير المحافظات (كما هو)
# ============================================================
@router.get("/api/by-governorate")
def by_governorate(
    date_from: str = Query("", description="YYYY-MM-DD"),
    date_to: str = Query("", description="YYYY-MM-DD"),
    db: Session = Depends(get_db),
):
    t_norm = func.upper(func.trim(models.Invoice.type)).label("t_norm")

    q_base = db.query(
        models.Invoice.governorate.label("gov"),
        t_norm,
        func.count(models.Invoice.id).label("cnt"),
        func.coalesce(func.sum(models.Invoice.total), 0.0).label("sum"),
    )

    if date_from:
        q_base = q_base.filter(models.Invoice.created_at >= date_from)
    if date_to:
        q_base = q_base.filter(models.Invoice.created_at <= date_to + " 23:59:59")

    q_base = q_base.filter(t_norm.in_(["S", "R"]))
    q_base = q_base.group_by(models.Invoice.governorate, t_norm)

    raw = q_base.all()

    agg = {}
    for row in raw:
        gov = row.gov or "غير محدد"
        rec = agg.setdefault(gov, {
            "governorate": gov,
            "sales_count": 0,
            "sales_total": 0.0,
            "returns_count": 0,
            "returns_total": 0.0,
        })
        if row.t_norm == "S":
            rec["sales_count"] += int(row.cnt or 0)
            rec["sales_total"] += float(row.sum or 0.0)
        elif row.t_norm == "R":
            rec["returns_count"] += int(row.cnt or 0)
            rec["returns_total"] += float(row.sum or 0.0)

    out = sorted(agg.values(), key=lambda x: (-x["sales_total"], -x["sales_count"], x["governorate"]))
    return JSONResponse({"rows": out, "date_from": date_from, "date_to": date_to})

@router.get("/governorates", response_class=HTMLResponse)
def governorates_report_page(request: Request):
    return templates.TemplateResponse("reports_governorates.html", {"request": request})