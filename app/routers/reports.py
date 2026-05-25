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
    # ── مبيعات (S) ──
    q_sales = db.query(
        models.Invoice.governorate.label("gov"),
        func.count(models.Invoice.id).label("cnt"),
        func.coalesce(func.sum(models.Invoice.total), 0.0).label("sum"),
    ).filter(func.upper(func.trim(models.Invoice.type)) == "S")

    if date_from:
        q_sales = q_sales.filter(models.Invoice.created_at >= date_from)
    if date_to:
        q_sales = q_sales.filter(models.Invoice.created_at <= date_to + " 23:59:59")

    q_sales = q_sales.group_by(models.Invoice.governorate)
    sales_raw = q_sales.all()

    # ── مرتجعات (R) — نجيب المحافظة من فاتورة البيع الأصلية ──
    from sqlalchemy.orm import aliased
    RetInv    = models.Invoice
    SaleAlias = aliased(models.Invoice)

    q_ret = (
        db.query(
            func.coalesce(SaleAlias.governorate, RetInv.governorate).label("gov"),
            func.count(RetInv.id).label("cnt"),
            func.coalesce(func.sum(RetInv.total), 0.0).label("sum"),
        )
        .select_from(RetInv)
        .outerjoin(SaleAlias, SaleAlias.id == RetInv.original_sale_id)
        .filter(func.upper(func.trim(RetInv.type)) == "R")
    )

    if date_from:
        q_ret = q_ret.filter(RetInv.created_at >= date_from)
    if date_to:
        q_ret = q_ret.filter(RetInv.created_at <= date_to + " 23:59:59")

    q_ret = q_ret.group_by(func.coalesce(SaleAlias.governorate, RetInv.governorate))
    ret_raw = q_ret.all()

    # ── تجميع ──
    agg = {}
    for row in sales_raw:
        gov = (row.gov or "").strip() or "غير محدد"
        rec = agg.setdefault(gov, {"governorate": gov, "sales_count": 0, "sales_total": 0.0, "returns_count": 0, "returns_total": 0.0})
        rec["sales_count"] += int(row.cnt or 0)
        rec["sales_total"] += float(row.sum or 0.0)

    for row in ret_raw:
        gov = (row.gov or "").strip() or "غير محدد"
        rec = agg.setdefault(gov, {"governorate": gov, "sales_count": 0, "sales_total": 0.0, "returns_count": 0, "returns_total": 0.0})
        rec["returns_count"] += int(row.cnt or 0)
        rec["returns_total"] += float(row.sum or 0.0)

    out = sorted(agg.values(), key=lambda x: (-x["sales_total"], -x["sales_count"], x["governorate"]))
    return JSONResponse({"rows": out, "date_from": date_from, "date_to": date_to})

@router.get("/governorates", response_class=HTMLResponse)
def governorates_report_page(request: Request):
    return templates.TemplateResponse("reports_governorates.html", {"request": request})


# ============================================================
# تقرير مبيعات المنتجات (صافي بعد المرتجعات) مع الستوك
# ============================================================
from datetime import date as date_cls

@router.get("/products-sales", response_class=HTMLResponse)
def products_sales_report(
    request: Request,
    date_from: str = Query(""),
    date_to: str = Query(""),
    export: str = Query(""),
    db: Session = Depends(get_db),
):
    today = date_cls.today()
    if not date_from:
        date_from = today.replace(day=1).strftime("%Y-%m-%d")
    if not date_to:
        date_to = today.strftime("%Y-%m-%d")

    sales_rows = (
        db.query(
            models.InvoiceItem.product_id,
            models.InvoiceItem.product_name,
            func.sum(models.InvoiceItem.qty).label("sold_qty"),
            func.sum(models.InvoiceItem.line_total).label("sold_total"),
        )
        .join(models.Invoice, models.Invoice.id == models.InvoiceItem.invoice_id)
        .filter(models.Invoice.type == "S")
        .filter(models.Invoice.created_at >= date_from)
        .filter(models.Invoice.created_at <= date_to + " 23:59:59")
        .group_by(models.InvoiceItem.product_id, models.InvoiceItem.product_name)
        .all()
    )

    return_rows = (
        db.query(
            models.InvoiceItem.product_id,
            func.sum(models.InvoiceItem.qty).label("ret_qty"),
            func.sum(models.InvoiceItem.line_total).label("ret_total"),
        )
        .join(models.Invoice, models.Invoice.id == models.InvoiceItem.invoice_id)
        .filter(models.Invoice.type == "R")
        .filter(models.Invoice.created_at >= date_from)
        .filter(models.Invoice.created_at <= date_to + " 23:59:59")
        .group_by(models.InvoiceItem.product_id)
        .all()
    )

    ret_map = {r.product_id: {"qty": int(r.ret_qty or 0), "total": float(r.ret_total or 0)} for r in return_rows}
    products_map = {p.id: p for p in db.query(models.Product).all()}

    rows = []
    for s in sales_rows:
        pid  = s.product_id
        prod = products_map.get(pid)

        sold_qty   = int(s.sold_qty or 0)
        sold_total = float(s.sold_total or 0)
        ret        = ret_map.get(pid, {"qty": 0, "total": 0.0})
        ret_qty    = ret["qty"]
        ret_total  = ret["total"]
        net_qty    = sold_qty - ret_qty
        net_sales  = sold_total - ret_total

        cost_price = float(getattr(prod, "cost_price", 0) or 0) if prod else 0
        net_cost   = net_qty * cost_price
        stock      = int(getattr(prod, "stock", 0) or 0) if prod else 0
        color      = getattr(prod, "color", "") or ""
        size       = getattr(prod, "size", "") or ""
        name       = (prod.name if prod else None) or s.product_name or f"#{pid}"

        rows.append({
            "pid":        pid,
            "name":       name,
            "color":      color,
            "size":       size,
            "sold_qty":   sold_qty,
            "ret_qty":    ret_qty,
            "net_qty":    net_qty,
            "sold_total": round(sold_total, 2),
            "ret_total":  round(ret_total, 2),
            "net_sales":  round(net_sales, 2),
            "cost_price": round(cost_price, 2),
            "net_cost":   round(net_cost, 2),
            "net_profit": round(net_sales - net_cost, 2),
            "stock":      stock,
        })

    rows.sort(key=lambda x: -x["net_qty"])

    total_net_qty    = sum(r["net_qty"]    for r in rows)
    total_net_sales  = sum(r["net_sales"]  for r in rows)
    total_net_cost   = sum(r["net_cost"]   for r in rows)
    total_net_profit = sum(r["net_profit"] for r in rows)

    if export == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["المنتج","اللون","المقاس","مباع","مرتجع","صافي قطع",
                    "صافي مبيعات","إجمالي تكلفة","صافي ربح","ستوك متبقي"])
        for r in rows:
            w.writerow([r["name"], r["color"], r["size"],
                        r["sold_qty"], r["ret_qty"], r["net_qty"],
                        r["net_sales"], r["net_cost"], r["net_profit"], r["stock"]])
        w.writerow([])
        w.writerow(["الإجمالي","","","","",total_net_qty,
                    total_net_sales, total_net_cost, total_net_profit,""])
        buf.seek(0)
        fname = f"products_sales_{date_from}_{date_to}.csv"
        return StreamingResponse(
            iter([("\ufeff" + buf.getvalue()).encode("utf-8")]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    ctx = {
        "request":          request,
        "date_from":        date_from,
        "date_to":          date_to,
        "rows":             rows,
        "total_net_qty":    total_net_qty,
        "total_net_sales":  round(total_net_sales, 2),
        "total_net_cost":   round(total_net_cost, 2),
        "total_net_profit": round(total_net_profit, 2),
    }
    return templates.TemplateResponse("report_products_sales.html", ctx)


# ============================================================
# تقرير 1: الألوان والمقاسات الأكتر مبيعاً
# ============================================================
@router.get("/colors-sizes", response_class=HTMLResponse)
def colors_sizes_report(
    request: Request,
    date_from: str = Query(""),
    date_to: str = Query(""),
    export: str = Query(""),
    db: Session = Depends(get_db),
):
    today = date_cls.today()
    if not date_from:
        date_from = today.replace(day=1).strftime("%Y-%m-%d")
    if not date_to:
        date_to = today.strftime("%Y-%m-%d")

    sales_rows = (
        db.query(
            models.InvoiceItem.product_id,
            func.sum(models.InvoiceItem.qty).label("sold_qty"),
            func.sum(models.InvoiceItem.line_total).label("sold_total"),
        )
        .join(models.Invoice, models.Invoice.id == models.InvoiceItem.invoice_id)
        .filter(models.Invoice.type == "S")
        .filter(models.Invoice.created_at >= date_from)
        .filter(models.Invoice.created_at <= date_to + " 23:59:59")
        .group_by(models.InvoiceItem.product_id)
        .all()
    )

    ret_rows = (
        db.query(
            models.InvoiceItem.product_id,
            func.sum(models.InvoiceItem.qty).label("ret_qty"),
            func.sum(models.InvoiceItem.line_total).label("ret_total"),
        )
        .join(models.Invoice, models.Invoice.id == models.InvoiceItem.invoice_id)
        .filter(models.Invoice.type == "R")
        .filter(models.Invoice.created_at >= date_from)
        .filter(models.Invoice.created_at <= date_to + " 23:59:59")
        .group_by(models.InvoiceItem.product_id)
        .all()
    )

    ret_map       = {r.product_id: int(r.ret_qty or 0)   for r in ret_rows}
    ret_map_total = {r.product_id: float(r.ret_total or 0) for r in ret_rows}
    products_map  = {p.id: p for p in db.query(models.Product).all()}

    color_agg   = {}
    size_agg    = {}
    product_agg = {}

    for s in sales_rows:
        pid  = s.product_id
        prod = products_map.get(pid)
        if not prod:
            continue
        net_qty   = int(s.sold_qty or 0) - ret_map.get(pid, 0)
        net_sales = float(s.sold_total or 0) - ret_map_total.get(pid, 0.0)
        color = (prod.color or "غير محدد").strip()
        size  = (prod.size  or "غير محدد").strip()
        name  = (prod.name  or "غير محدد").strip()

        c = color_agg.setdefault(color, {"color": color, "net_qty": 0, "net_sales": 0.0})
        c["net_qty"] += net_qty; c["net_sales"] += net_sales

        z = size_agg.setdefault(size, {"size": size, "net_qty": 0, "net_sales": 0.0})
        z["net_qty"] += net_qty; z["net_sales"] += net_sales

        p = product_agg.setdefault(name, {"name": name, "net_qty": 0, "net_sales": 0.0})
        p["net_qty"] += net_qty; p["net_sales"] += net_sales

    colors   = sorted(color_agg.values(),   key=lambda x: -x["net_qty"])
    sizes    = sorted(size_agg.values(),    key=lambda x: -x["net_qty"])
    products = sorted(product_agg.values(), key=lambda x: -x["net_qty"])

    for r in colors:   r["net_sales"] = round(r["net_sales"], 2)
    for r in sizes:    r["net_sales"] = round(r["net_sales"], 2)
    for r in products: r["net_sales"] = round(r["net_sales"], 2)

    if export == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["── المنتجات ──"])
        w.writerow(["المنتج", "صافي قطع", "صافي مبيعات"])
        for r in products:
            w.writerow([r["name"], r["net_qty"], r["net_sales"]])
        w.writerow([])
        w.writerow(["── الألوان ──"])
        w.writerow(["اللون", "صافي قطع", "صافي مبيعات"])
        for r in colors:
            w.writerow([r["color"], r["net_qty"], r["net_sales"]])
        w.writerow([])
        w.writerow(["── المقاسات ──"])
        w.writerow(["المقاس", "صافي قطع", "صافي مبيعات"])
        for r in sizes:
            w.writerow([r["size"], r["net_qty"], r["net_sales"]])
        buf.seek(0)
        fname = f"colors_sizes_{date_from}_{date_to}.csv"
        return StreamingResponse(
            iter([("\ufeff" + buf.getvalue()).encode("utf-8")]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    return templates.TemplateResponse("report_colors_sizes.html", {
        "request":   request,
        "date_from": date_from,
        "date_to":   date_to,
        "colors":    colors,
        "sizes":     sizes,
        "products":  products,
    })


@router.get("/repeat-customers", response_class=HTMLResponse)
def repeat_customers_report(
    request: Request,
    date_from: str = Query(""),
    date_to: str = Query(""),
    export: str = Query(""),
    db: Session = Depends(get_db),
):
    today = date_cls.today()
    if not date_from:
        date_from = today.replace(day=1).strftime("%Y-%m-%d")
    if not date_to:
        date_to = today.strftime("%Y-%m-%d")

    sales_rows = (
        db.query(
            models.Invoice.customer_name,
            models.Invoice.customer_phone,
            func.count(models.Invoice.id).label("orders"),
            func.sum(models.Invoice.total).label("total_spent"),
            func.min(models.Invoice.created_at).label("first_order"),
            func.max(models.Invoice.created_at).label("last_order"),
        )
        .filter(models.Invoice.type == "S")
        .filter(models.Invoice.created_at >= date_from)
        .filter(models.Invoice.created_at <= date_to + " 23:59:59")
        .group_by(models.Invoice.customer_phone, models.Invoice.customer_name)
        .all()
    )

    rows = []
    for r in sales_rows:
        rows.append({
            "name":        r.customer_name or "",
            "phone":       r.customer_phone or "",
            "orders":      int(r.orders or 0),
            "total_spent": round(float(r.total_spent or 0), 2),
            "first_order": str(r.first_order)[:10] if r.first_order else "",
            "last_order":  str(r.last_order)[:10]  if r.last_order  else "",
        })

    # ترتيب: الأكتر طلبات أولاً
    rows.sort(key=lambda x: (-x["orders"], -x["total_spent"]))

    repeat  = [r for r in rows if r["orders"] > 1]
    singles = [r for r in rows if r["orders"] == 1]

    total_customers = len(rows)
    repeat_count    = len(repeat)
    repeat_pct      = round((repeat_count / max(total_customers, 1)) * 100, 1)

    if export == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["الاسم", "الهاتف", "عدد الطلبات", "إجمالي الإنفاق", "أول طلب", "آخر طلب"])
        for r in rows:
            w.writerow([r["name"], r["phone"], r["orders"],
                        r["total_spent"], r["first_order"], r["last_order"]])
        buf.seek(0)
        fname = f"repeat_customers_{date_from}_{date_to}.csv"
        return StreamingResponse(
            iter([("\ufeff" + buf.getvalue()).encode("utf-8")]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    return templates.TemplateResponse("report_repeat_customers.html", {
        "request":        request,
        "date_from":      date_from,
        "date_to":        date_to,
        "repeat":         repeat,
        "singles":        singles,
        "total_customers":total_customers,
        "repeat_count":   repeat_count,
        "repeat_pct":     repeat_pct,
    })


# ============================================================
# تقرير 3: تقرير الهالك
# ============================================================
@router.get("/wastage-report", response_class=HTMLResponse)
def wastage_report(
    request: Request,
    export: str = Query(""),
    db: Session = Depends(get_db),
):
    products = db.query(models.Product).all()

    rows = []
    for p in products:
        w = int(getattr(p, "wastage_stock", 0) or 0)
        if w <= 0:
            continue
        cost  = float(p.cost_price or 0)
        price = float(p.price or 0)
        rows.append({
            "name":          p.name or "",
            "color":         p.color or "",
            "size":          p.size  or "",
            "barcode":       p.barcode or "",
            "wastage_qty":   w,
            "cost_price":    round(cost, 2),
            "total_cost":    round(w * cost, 2),
            "sale_price":    round(price, 2),
            "lost_revenue":  round(w * price, 2),
            "stock":         int(p.stock or 0),
        })

    rows.sort(key=lambda x: -x["total_cost"])

    total_wastage_qty  = sum(r["wastage_qty"]  for r in rows)
    total_cost_value   = round(sum(r["total_cost"]   for r in rows), 2)
    total_lost_revenue = round(sum(r["lost_revenue"] for r in rows), 2)

    if export == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["المنتج","اللون","المقاس","كمية الهالك",
                    "تكلفة/قطعة","إجمالي التكلفة","سعر البيع","إيراد ضائع","ستوك متبقي"])
        for r in rows:
            w.writerow([r["name"],r["color"],r["size"],r["wastage_qty"],
                        r["cost_price"],r["total_cost"],r["sale_price"],
                        r["lost_revenue"],r["stock"]])
        w.writerow([])
        w.writerow(["الإجمالي","","",total_wastage_qty,"",
                    total_cost_value,"",total_lost_revenue,""])
        buf.seek(0)
        fname = "wastage_report.csv"
        return StreamingResponse(
            iter([("\ufeff" + buf.getvalue()).encode("utf-8")]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    return templates.TemplateResponse("report_wastage.html", {
        "request":            request,
        "rows":               rows,
        "total_wastage_qty":  total_wastage_qty,
        "total_cost_value":   total_cost_value,
        "total_lost_revenue": total_lost_revenue,
    })

# ============================================================
# تقرير الأداء
# ============================================================
@router.get("/performance", response_class=HTMLResponse)
def performance_report(
    request: Request,
    date_from: str = Query(""),
    date_to: str = Query(""),
    gov_sort: str = Query("orders"),
    db: Session = Depends(get_db),
):
    from sqlalchemy import text
    today = date_cls.today()
    if not date_from:
        date_from = today.replace(day=1).strftime("%Y-%m-%d")
    if not date_to:
        date_to = today.strftime("%Y-%m-%d")

    dt_to = date_to + " 23:59:59"

    # ── مبيعات ومرتجعات ──
    sales_rows = db.query(models.Invoice).filter(
        models.Invoice.type == "S",
        models.Invoice.created_at >= date_from,
        models.Invoice.created_at <= dt_to
    ).all()

    ret_rows = db.query(models.Invoice).filter(
        models.Invoice.type == "R",
        models.Invoice.created_at >= date_from,
        models.Invoice.created_at <= dt_to
    ).all()

    sales_count  = len(sales_rows)
    sales_products_total = sum(float((r.subtotal or 0) - (r.discount or 0)) for r in sales_rows)
    shipping_income = sum(float(r.shipping_cost or 0) for r in sales_rows)
    sales_total  = sales_products_total + shipping_income
    ret_count    = len(ret_rows)
    returns_invoice_total = sum(float(r.total or 0) for r in ret_rows)
    ret_total    = returns_invoice_total
    net_sales    = sales_total
    avg_order    = 0.0
    avg_discount = round(sum(float(r.discount or 0) for r in sales_rows) / max(sales_count, 1), 2)
    ret_rate_pct = round((ret_count / max(sales_count, 1)) * 100, 1)
    ret_val_pct  = 0

    # ── COGS ──
    cogs_sales = float(
        db.query(func.coalesce(func.sum(models.InvoiceItem.qty * models.Product.cost_price), 0.0))
        .join(models.Invoice, models.InvoiceItem.invoice_id == models.Invoice.id)
        .join(models.Product, models.InvoiceItem.product_id == models.Product.id)
        .filter(models.Invoice.type == "S", models.Invoice.created_at >= date_from, models.Invoice.created_at <= dt_to)
        .scalar() or 0.0
    )
    cogs_returns = float(
        db.query(func.coalesce(func.sum(models.InvoiceItem.qty * models.Product.cost_price), 0.0))
        .join(models.Invoice, models.InvoiceItem.invoice_id == models.Invoice.id)
        .join(models.Product, models.InvoiceItem.product_id == models.Product.id)
        .filter(models.Invoice.type == "R", models.Invoice.created_at >= date_from, models.Invoice.created_at <= dt_to)
        .scalar() or 0.0
    )
    returns_goods_total = float(
        db.query(func.coalesce(func.sum(models.InvoiceItem.line_total), 0.0))
        .join(models.Invoice, models.InvoiceItem.invoice_id == models.Invoice.id)
        .filter(models.Invoice.type == "R", models.Invoice.created_at >= date_from, models.Invoice.created_at <= dt_to)
        .scalar() or 0.0
    )
    # Net sales should use the return invoice total so the top KPI matches the
    # financial impact of returns, while returns_goods_total remains a product
    # breakdown line in the table.
    ret_total = returns_invoice_total
    net_sales = sales_total - returns_invoice_total
    avg_order = round(net_sales / max(sales_count, 1), 2)
    ret_val_pct = round((returns_invoice_total / max(sales_total, 1)) * 100, 1)
    cogs_net     = cogs_sales - cogs_returns
    gross_profit = net_sales - cogs_net
    gross_margin = round((gross_profit / max(net_sales, 1)) * 100, 1) if net_sales > 0 else 0

    # ── مصاريف من pl_categories ──
    def _clean_categories(cats):
        seen = set()
        cleaned = []
        for cat in cats:
            cat = (cat or "").strip()
            if not cat or cat in seen:
                continue
            seen.add(cat)
            cleaned.append(cat)
        return cleaned

    def _get_expense(cats):
        total = 0.0
        for cat in _clean_categories(cats):
            q = db.query(func.coalesce(func.sum(models.FinanceEntry.amount), 0.0))\
                .filter(func.lower(models.FinanceEntry.type).in_(["out","مصروف","مصروفات"]))\
                .filter(models.FinanceEntry.category == cat)\
                .filter(models.FinanceEntry.date >= date_from)\
                .filter(models.FinanceEntry.date <= date_to)
            total += float(q.scalar() or 0.0)
        return total

    def _get_expense_breakdown(cats):
        cats = _clean_categories(cats)
        product_load_keywords = [
            "%اعلان%", "%إعلان%", "%اعلانات%", "%إعلانات%",
            "%marketing%", "%ads%", "%ad%",
            "%ايجار%", "%إيجار%", "%rent%",
            "%اشتراك%", "%اشتراكات%", "%subscription%",
        ]

        q = (
            db.query(
                models.FinanceEntry.category.label("category"),
                func.coalesce(func.sum(models.FinanceEntry.amount), 0.0).label("amount"),
            )
            .filter(func.lower(models.FinanceEntry.type).in_(["out","مصروف","مصروفات"]))
            .filter(models.FinanceEntry.date >= date_from)
            .filter(models.FinanceEntry.date <= date_to)
        )

        filters = []
        if cats:
            filters.append(models.FinanceEntry.category.in_(cats))
        for word in product_load_keywords:
            filters.append(models.FinanceEntry.category.ilike(word))
            filters.append(models.FinanceEntry.note.ilike(word))

        if filters:
            q = q.filter(or_(*filters))
        else:
            return []

        for word in ["%شحن%", "%shipping%", "%ship%"]:
            q = q.filter(or_(models.FinanceEntry.category == None, ~models.FinanceEntry.category.ilike(word)))

        # منع تكرار عمولة التسويق لأنها محسوبة تلقائي 5%
        q = q.filter(
            ~models.FinanceEntry.category.ilike("%عمولة تسويق%"),
            ~models.FinanceEntry.category.ilike("%تسويق%"),
            ~models.FinanceEntry.category.ilike("%marketing commission%"),
        )

        rows = (
            q.group_by(models.FinanceEntry.category)
            .order_by(func.coalesce(func.sum(models.FinanceEntry.amount), 0.0).desc())
            .all()
        )

        return [
            {
                "category": (r.category or "غير محدد").strip(),
                "amount": round(float(r.amount or 0.0), 2),
            }
            for r in rows
            if float(r.amount or 0.0) != 0.0
        ]

    try:
        all_cats = [r[0] for r in db.execute(text(
            "SELECT category FROM pl_categories"
        )).fetchall()]
    except Exception:
        all_cats = []

    ship_actual_sales = 0.0
    for r in sales_rows:
        actual = r.actual_shipping_cost if r.actual_shipping_cost is not None else r.shipping_cost
        actual = float(actual or 0.0)
        ship_actual_sales += actual

    return_ship_fees = sum(float(r.return_shipping_fee or 0.0) for r in ret_rows)
    ship_actual_expense = float(ship_actual_sales + return_ship_fees)
    ship_expense = ship_actual_expense

    expense_breakdown = []
    if ship_expense:
        expense_breakdown.append({
            "category": "تكلفة الشحن الفعلية",
            "amount": round(ship_expense, 2),
        })

    net_sales_after_shipping = max(net_sales - ship_actual_expense, 0.0)
    marketer_commission = round(net_sales_after_shipping * 0.05, 2)
    if marketer_commission:
        expense_breakdown.append({
            "category": "عمولة تسويق 5%",
            "amount": marketer_commission,
        })

    expense_breakdown.extend(_get_expense_breakdown(all_cats))
    other_expenses  = sum(float(item["amount"] or 0.0) for item in expense_breakdown)
    total_expenses  = other_expenses
    net_profit      = gross_profit - total_expenses
    net_margin      = round((net_profit / max(net_sales, 1)) * 100, 1) if net_sales > 0 else 0

    # ── منتجات ──
    prod_sales = (
        db.query(
            models.InvoiceItem.product_id,
            func.sum(models.InvoiceItem.qty).label("sold_qty"),
            func.sum(models.InvoiceItem.line_total).label("sold_total"),
        )
        .join(models.Invoice, models.Invoice.id == models.InvoiceItem.invoice_id)
        .filter(models.Invoice.type == "S", models.Invoice.created_at >= date_from, models.Invoice.created_at <= dt_to)
        .group_by(models.InvoiceItem.product_id).all()
    )
    prod_rets = (
        db.query(
            models.InvoiceItem.product_id,
            func.sum(models.InvoiceItem.qty).label("ret_qty"),
            func.sum(models.InvoiceItem.line_total).label("ret_total"),
        )
        .join(models.Invoice, models.Invoice.id == models.InvoiceItem.invoice_id)
        .filter(models.Invoice.type == "R", models.Invoice.created_at >= date_from, models.Invoice.created_at <= dt_to)
        .group_by(models.InvoiceItem.product_id).all()
    )
    ret_map_qty   = {r.product_id: int(r.ret_qty or 0)    for r in prod_rets}
    ret_map_total = {r.product_id: float(r.ret_total or 0) for r in prod_rets}
    products_map  = {p.id: p for p in db.query(models.Product).all()}

    sales_map = {
        s.product_id: {
            "qty": int(s.sold_qty or 0),
            "total": float(s.sold_total or 0),
        }
        for s in prod_sales
    }
    product_ids = set(sales_map.keys()) | set(ret_map_qty.keys()) | set(ret_map_total.keys())

    prod_rows = []
    for pid in product_ids:
        prod    = products_map.get(pid)
        sold    = sales_map.get(pid, {"qty": 0, "total": 0.0})
        net_qty = int(sold["qty"] or 0) - ret_map_qty.get(pid, 0)
        net_rev = float(sold["total"] or 0) - ret_map_total.get(pid, 0)
        cost_p  = float(getattr(prod, "cost_price", 0) or 0) if prod else 0
        net_cost= net_qty * cost_p
        profit  = net_rev - net_cost
        margin  = round((profit / max(net_rev, 1)) * 100, 1) if net_rev > 0 else 0
        name    = (prod.name if prod else None) or f"#{pid}"
        color   = getattr(prod, "color", "") or ""
        size    = getattr(prod, "size", "") or ""
        prod_rows.append({
            "pid": pid, "name": name, "color": color, "size": size,
            "net_qty": net_qty, "net_sales": round(net_rev, 2),
            "net_cost": round(net_cost, 2), "profit": round(profit, 2),
            "margin": margin,
        })

    prod_rows.sort(key=lambda x: -x["net_qty"])
    top10  = prod_rows[:10]
    worst5 = sorted(prod_rows, key=lambda x: x["net_qty"])[:5]
    total_net_qty = sum(int(r["net_qty"] or 0) for r in prod_rows)
    expense_per_unit = round(total_expenses / max(total_net_qty, 1), 2) if total_net_qty > 0 else 0.0

    # ── أكتر محافظات ──
    sales_gov_rows = (
        db.query(
            models.Invoice.governorate,
            func.count(models.Invoice.id).label("cnt"),
            func.sum(models.Invoice.total).label("total"),
        )
        .filter(models.Invoice.type == "S", models.Invoice.created_at >= date_from, models.Invoice.created_at <= dt_to)
        .filter(models.Invoice.governorate != None, models.Invoice.governorate != "")
        .group_by(models.Invoice.governorate)
        .all()
    )

    from sqlalchemy.orm import aliased
    SaleAlias = aliased(models.Invoice)
    ret_gov_rows = (
        db.query(
            func.coalesce(SaleAlias.governorate, models.Invoice.governorate).label("gov"),
            func.count(models.Invoice.id).label("cnt"),
            func.coalesce(func.sum(models.Invoice.total), 0.0).label("total"),
        )
        .outerjoin(SaleAlias, SaleAlias.id == models.Invoice.original_sale_id)
        .filter(models.Invoice.type == "R", models.Invoice.created_at >= date_from, models.Invoice.created_at <= dt_to)
        .group_by(func.coalesce(SaleAlias.governorate, models.Invoice.governorate))
        .all()
    )
    ret_gov_map = {(r.gov or "").strip(): int(r.cnt or 0) for r in ret_gov_rows if (r.gov or "").strip()}
    ret_gov_total_map = {(r.gov or "").strip(): float(r.total or 0) for r in ret_gov_rows if (r.gov or "").strip()}

    sales_gov_map = {
        ((r.governorate or "").strip() or "غير محدد"): {
            "cnt": int(r.cnt or 0),
            "total": float(r.total or 0),
        }
        for r in sales_gov_rows
    }

    all_govs = []
    for gov in sorted(set(sales_gov_map.keys()) | set(ret_gov_map.keys()) | set(ret_gov_total_map.keys())):
        sales_gov = sales_gov_map.get(gov, {"cnt": 0, "total": 0.0})
        cnt = int(sales_gov["cnt"] or 0)
        sales_total_gov = float(sales_gov["total"] or 0)
        ret_cnt = ret_gov_map.get(gov, 0)
        ret_total_gov = ret_gov_total_map.get(gov, 0.0)
        total = sales_total_gov - ret_total_gov
        all_govs.append({
            "gov": gov,
            "cnt": cnt,
            "order_pct": round((cnt / max(sales_count, 1)) * 100, 1),
            "avg_order": round(total / max(cnt, 1), 2),
            "returns_count": ret_cnt,
            "return_pct": round((ret_cnt / max(cnt, 1)) * 100, 1) if cnt > 0 else 0,
            "total": round(total, 2),
        })

    gov_sort_keys = {
        "orders": lambda x: (-x["cnt"], -x["total"], x["gov"]),
        "total": lambda x: (-x["total"], -x["cnt"], x["gov"]),
        "pct": lambda x: (-x["order_pct"], -x["cnt"], x["gov"]),
        "returns": lambda x: (-x["return_pct"], -x["returns_count"], x["gov"]),
        "avg": lambda x: (-x["avg_order"], -x["cnt"], x["gov"]),
    }
    all_govs = sorted(all_govs, key=gov_sort_keys.get(gov_sort, gov_sort_keys["orders"]))
    top_govs = all_govs[:5]
    top_govs_summary = {
        "cnt": sum(g["cnt"] for g in top_govs),
        "pct": round((sum(g["cnt"] for g in top_govs) / max(sales_count, 1)) * 100, 1),
        "total": round(sum(float(g["total"] or 0) for g in top_govs), 2),
    }

    return templates.TemplateResponse("report_performance.html", {
        "request": request, "date_from": date_from, "date_to": date_to, "gov_sort": gov_sort,
        "sales_count": sales_count, "sales_total": round(sales_total, 2),
        "sales_products_total": round(sales_products_total, 2),
        "shipping_income": round(shipping_income, 2),
        "returns_goods_total": round(returns_goods_total, 2),
        "ret_count": ret_count, "ret_total": round(ret_total, 2),
        "net_sales": round(net_sales, 2), "avg_order": avg_order,
        "avg_discount": avg_discount, "ret_rate_pct": ret_rate_pct,
        "ret_val_pct": ret_val_pct, "cogs_net": round(cogs_net, 2),
        "gross_profit": round(gross_profit, 2), "gross_margin": gross_margin,
        "ship_expense": round(ship_expense, 2), "ship_actual_expense": round(ship_actual_expense, 2),
        "other_expenses": round(other_expenses, 2),
        "total_expenses": round(total_expenses, 2), "net_profit": round(net_profit, 2),
        "net_margin": net_margin, "expense_breakdown": expense_breakdown,
        "net_sales_after_shipping": round(net_sales_after_shipping, 2),
        "marketer_commission": round(marketer_commission, 2),
        "expense_per_unit": expense_per_unit, "total_net_qty": total_net_qty,
        "top10": top10, "worst5": worst5, "top_govs": top_govs,
        "top_govs_summary": top_govs_summary,
    })
