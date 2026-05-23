# app/routers/pl.py
from datetime import datetime, date, timedelta
from typing import Dict
from fastapi import APIRouter, Request, Depends, Query, Form
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, and_, text
from fastapi.templating import Jinja2Templates
import io, csv

from app.database import SessionLocal
from app import models

router = APIRouter(prefix="/pl", tags=["Profit & Loss"])
templates = Jinja2Templates(directory="app/templates")

def get_db():
    db = SessionLocal()
    try:
        db.execute(text("""
        CREATE TABLE IF NOT EXISTS pl_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,         -- FIXED | VARIABLE
            category TEXT NOT NULL,
            note TEXT
        );
        """))
        db.commit()
        yield db
    finally:
        db.close()

def _month_start_today():
    today = date.today()
    start = today.replace(day=1)
    return start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")

def _cash_filters(date_from: str, date_to: str):
    flt = []
    if date_from: flt.append(models.FinanceEntry.date >= date_from)  # string YYYY-MM-DD
    if date_to:   flt.append(models.FinanceEntry.date <= date_to)
    return flt

# ✅ فلتر تاريخ الفواتير بشكل قوي (يشتغل لو created_at DateTime أو Text)
def _inv_date_range(date_from: str, date_to: str):
    # func.date يحوّل datetime/text لتاريخ YYYY-MM-DD داخل DB
    col = func.date(models.Invoice.created_at)
    clauses = []
    if date_from:
        clauses.append(col >= date_from)
    if date_to:
        clauses.append(col <= date_to)
    return and_(*clauses) if clauses else True

# ✅ تطبيع النوع (S / R) حتى لو فيه مسافات أو حروف صغيرة
def _type_is(letter: str):
    return func.upper(func.trim(models.Invoice.type)) == letter

_OUT_LOWER = {"out", "مصروف", "مصروفات"}

def get_fixed_categories(db: Session):
    rows = db.execute(text(
        "SELECT id, category, coalesce(note,'') FROM pl_categories WHERE kind='FIXED' ORDER BY category"
    )).fetchall()
    return [{"id": r[0], "category": r[1], "note": r[2]} for r in rows]

def get_variable_categories(db: Session):
    rows = db.execute(text(
        "SELECT id, category, coalesce(note,'') FROM pl_categories WHERE kind='VARIABLE' ORDER BY category"
    )).fetchall()
    return [{"id": r[0], "category": r[1], "note": r[2]} for r in rows]

def get_out_cash_categories(db: Session):
    try:
        q = (
            db.query(models.FinanceEntry.category)
              .filter(func.lower(models.FinanceEntry.type).in_(_OUT_LOWER))
              .filter(models.FinanceEntry.category.isnot(None))
              .distinct()
              .order_by(models.FinanceEntry.category.asc())
        )
        rows = q.all()
        cats = [r[0] for r in rows if (r[0] or "").strip()]
        if cats:
            return cats

        rows2 = (
            db.query(models.FinanceEntry.category)
              .filter(models.FinanceEntry.category.isnot(None))
              .distinct()
              .order_by(models.FinanceEntry.category.asc())
              .all()
        )
        return [r[0] for r in rows2 if (r[0] or "").strip()]
    except:
        return []

@router.get("/settings", response_class=HTMLResponse)
def pl_settings_page(request: Request, db: Session = Depends(get_db)):
    fixed = get_fixed_categories(db)
    variable = get_variable_categories(db)
    out_choices = get_out_cash_categories(db)
    return templates.TemplateResponse("pl_settings.html", {
        "request": request,
        "fixed": fixed,
        "variable": variable,
        "out_choices": out_choices,
    })

@router.post("/settings/add")
def pl_settings_add(
    kind: str = Form(...),
    name: str = Form(""),
    links: list[str] = Form([]),
    note: str = Form(""),
    db: Session = Depends(get_db)
):
    picked = [s.strip() for s in (links or []) if s and s.strip()]
    if name.strip():
        picked.append(name.strip())
    if not picked:
        return RedirectResponse(url="/pl/settings", status_code=303)

    for cat in picked:
        db.execute(
            text("INSERT INTO pl_categories(kind, category, note) VALUES (:k,:c,:n)"),
            {"k": kind.upper(), "c": cat, "n": note}
        )
    db.commit()
    return RedirectResponse(url="/pl/settings", status_code=303)

@router.post("/settings/delete/{row_id}")
def pl_settings_delete(row_id: int, db: Session = Depends(get_db)):
    db.execute(text("DELETE FROM pl_categories WHERE id=:i"), {"i": row_id})
    db.commit()
    return RedirectResponse(url="/pl/settings", status_code=303)

@router.get("", response_class=HTMLResponse)
def pl_page(
    request: Request,
    date_from: str = Query("", description="YYYY-MM-DD"),
    date_to: str = Query("", description="YYYY-MM-DD"),
    db: Session = Depends(get_db)
):
    if not date_from or not date_to:
        df, dt = _month_start_today()
        date_from = date_from or df
        date_to   = date_to or dt

    inv_rng = _inv_date_range(date_from, date_to)
    cash_flt = _cash_filters(date_from, date_to)

    # ---- Sales breakdown ----
    sales_core = float(
        db.query(func.coalesce(func.sum(models.Invoice.subtotal - models.Invoice.discount), 0.0))
          .filter(_type_is("S")).filter(inv_rng).scalar() or 0.0
    )
    shipping_income = float(
        db.query(func.coalesce(func.sum(models.Invoice.shipping_cost), 0.0))
          .filter(_type_is("S")).filter(inv_rng).scalar() or 0.0
    )
    sales_total = float(
        db.query(func.coalesce(func.sum(models.Invoice.total), 0.0))
          .filter(_type_is("S")).filter(inv_rng).scalar() or 0.0
    )

    returns_total = float(
        db.query(func.coalesce(func.sum(models.Invoice.total), 0.0))
          .filter(_type_is("R")).filter(inv_rng).scalar() or 0.0
    )

    # صافي الإيراد (عرض)
    net_revenue = (sales_core + shipping_income) - returns_total

    # ---- COGS net ----
    cogs_sales = float(
        db.query(func.coalesce(func.sum(models.InvoiceItem.qty * models.Product.cost_price), 0.0))
          .join(models.Invoice, models.Invoice.id == models.InvoiceItem.invoice_id)
          .join(models.Product, models.Product.id == models.InvoiceItem.product_id)
          .filter(_type_is("S")).filter(inv_rng).scalar() or 0.0
    )
    cogs_returns = float(
        db.query(func.coalesce(func.sum(models.InvoiceItem.qty * models.Product.cost_price), 0.0))
          .join(models.Invoice, models.Invoice.id == models.InvoiceItem.invoice_id)
          .join(models.Product, models.Product.id == models.InvoiceItem.product_id)
          .filter(_type_is("R")).filter(inv_rng).scalar() or 0.0
    )
    cogs = cogs_sales - cogs_returns

    # ---- Qty net ----
    sold_qty = float(
        db.query(func.coalesce(func.sum(models.InvoiceItem.qty), 0.0))
          .join(models.Invoice, models.Invoice.id == models.InvoiceItem.invoice_id)
          .filter(_type_is("S")).filter(inv_rng).scalar() or 0.0
    )
    returned_qty = float(
        db.query(func.coalesce(func.sum(models.InvoiceItem.qty), 0.0))
          .join(models.Invoice, models.Invoice.id == models.InvoiceItem.invoice_id)
          .filter(_type_is("R")).filter(inv_rng).scalar() or 0.0
    )
    net_sold_qty = max(0.0, sold_qty - returned_qty)
    avg_cogs_per_unit = (cogs / net_sold_qty) if net_sold_qty > 0 else 0.0

    # ---- Shipping actual expense ----
    shipping_expense = float(
        db.query(func.coalesce(func.sum(models.Invoice.actual_shipping_cost), 0.0))
          .filter(_type_is("S")).filter(inv_rng).scalar() or 0.0
    )
    return_ship_fees = float(
        db.query(func.coalesce(func.sum(models.Invoice.return_shipping_fee), 0.0))
          .filter(_type_is("R")).filter(inv_rng).scalar() or 0.0
    )
    shipping_total_expense = shipping_expense + return_ship_fees

    # ---- Fixed categories from settings ----
    fixed_rows = get_fixed_categories(db)
    get_variable_categories(db)  # future usage
    fixed_cats = [r["category"] for r in fixed_rows]

    # ---- Ads (من الخزنة) ----
    ads_expense = float(
        db.query(func.coalesce(func.sum(models.FinanceEntry.amount), 0.0))
          .filter(func.lower(models.FinanceEntry.type).in_(_OUT_LOWER))
          .filter(models.FinanceEntry.category == "اعلانات")
          .filter(*cash_flt).scalar() or 0.0
    )

    # ✅ Commission = (صافي المبيعات بعد المرتجعات وبعد خصم الشحن الفعلي) * 5%
    # ملاحظة: sales_total هنا غالبًا شامل إيراد الشحن لأن total بيشمله
    commission_base = float(sales_total - returns_total - shipping_total_expense)
    if commission_base < 0:
        commission_base = 0.0
    marketer_commission = round(commission_base * 0.05, 2)

    # variable total (ads + commission)
    variable_total = float(ads_expense + marketer_commission)

    # fixed total
    fixed_total = 0.0
    if fixed_cats:
        fixed_total = float(
            db.query(func.coalesce(func.sum(models.FinanceEntry.amount), 0.0))
              .filter(func.lower(models.FinanceEntry.type).in_(_OUT_LOWER))
              .filter(models.FinanceEntry.category.in_(fixed_cats))
              .filter(*cash_flt).scalar() or 0.0
        )

    operating_total_out = float(
        db.query(func.coalesce(func.sum(models.FinanceEntry.amount), 0.0))
          .filter(func.lower(models.FinanceEntry.type).in_(_OUT_LOWER))
          .filter(*cash_flt).scalar() or 0.0
    )

    # ---- Profits ----
    gross_profit = net_revenue - cogs
    net_profit = gross_profit - shipping_total_expense - variable_total - fixed_total

    # ---- Break-even ----
    asp_per_unit = (sales_core / net_sold_qty) if net_sold_qty > 0 else 0.0
    variable_per_unit = (
        avg_cogs_per_unit +
        (shipping_total_expense / net_sold_qty if net_sold_qty > 0 else 0.0) +
        (variable_total / net_sold_qty if net_sold_qty > 0 else 0.0)
    )
    cm_per_unit = max(0.0, asp_per_unit - variable_per_unit)
    be_units = (fixed_total / cm_per_unit) if cm_per_unit > 0 else 0.0
    be_revenue = be_units * asp_per_unit
    be_progress_units = (net_sold_qty / be_units * 100.0) if be_units > 0 else 0.0
    be_progress_rev   = (sales_core / be_revenue * 100.0) if be_revenue > 0 else 0.0

    # ---- Product stats net ----
    product_stats: Dict[str, Dict[str, float]] = {}

    rows_s = (
        db.query(
            models.Product.name.label("name"),
            func.coalesce(func.sum(models.InvoiceItem.qty), 0.0).label("qty"),
            func.coalesce(func.sum(models.InvoiceItem.qty * models.InvoiceItem.unit_price), 0.0).label("sales"),
            func.coalesce(func.sum(models.InvoiceItem.qty * models.Product.cost_price), 0.0).label("cogs"),
        )
        .join(models.Invoice, models.Invoice.id == models.InvoiceItem.invoice_id)
        .join(models.Product, models.Product.id == models.InvoiceItem.product_id)
        .filter(_type_is("S"))
        .filter(inv_rng)
        .group_by(models.Product.name)
        .all()
    )
    for r in rows_s:
        name = r.name or "بدون اسم"
        product_stats[name] = {
            "name": name,
            "qty": float(r.qty or 0.0),
            "sales": float(r.sales or 0.0),
            "cogs": float(r.cogs or 0.0),
        }

    rows_r = (
        db.query(
            models.Product.name.label("name"),
            func.coalesce(func.sum(models.InvoiceItem.qty), 0.0).label("qty"),
            func.coalesce(func.sum(models.InvoiceItem.qty * models.InvoiceItem.unit_price), 0.0).label("sales"),
            func.coalesce(func.sum(models.InvoiceItem.qty * models.Product.cost_price), 0.0).label("cogs"),
        )
        .join(models.Invoice, models.Invoice.id == models.InvoiceItem.invoice_id)
        .join(models.Product, models.Product.id == models.InvoiceItem.product_id)
        .filter(_type_is("R"))
        .filter(inv_rng)
        .group_by(models.Product.name)
        .all()
    )
    for r in rows_r:
        name = r.name or "بدون اسم"
        st = product_stats.setdefault(name, {"name": name, "qty": 0.0, "sales": 0.0, "cogs": 0.0})
        st["qty"]   -= float(r.qty or 0.0)
        st["sales"] -= float(r.sales or 0.0)
        st["cogs"]  -= float(r.cogs or 0.0)

    products_stats = []
    for st in product_stats.values():
        profit_p = st["sales"] - st["cogs"]
        if abs(st["qty"]) < 1e-6 and abs(st["sales"]) < 0.01 and abs(st["cogs"]) < 0.01:
            continue
        st["profit"] = profit_p
        products_stats.append(st)
    products_stats.sort(key=lambda x: x["sales"], reverse=True)

    return templates.TemplateResponse("pl.html", {
        "request": request,
        "date_from": date_from, "date_to": date_to,

        "sales_core": sales_core,
        "shipping_income": shipping_income,
        "returns_total": returns_total,
        "net_revenue": net_revenue,

        "cogs_sales": cogs_sales, "cogs_returns": cogs_returns, "cogs": cogs,

        "sold_qty": sold_qty, "returned_qty": returned_qty,
        "net_sold_qty": net_sold_qty, "avg_cogs_per_unit": avg_cogs_per_unit,

        "shipping_expense": shipping_expense,
        "return_ship_fees": return_ship_fees,
        "shipping_total_expense": shipping_total_expense,

        "operating_total_out": operating_total_out,
        "fixed_total": fixed_total,
        "variable_total": variable_total,
        "ads_expense": ads_expense,
        "marketer_commission": marketer_commission,

        "gross_profit": gross_profit,
        "net_profit": net_profit,

        "asp_per_unit": asp_per_unit,
        "variable_per_unit": variable_per_unit,
        "cm_per_unit": cm_per_unit,
        "be_units": be_units,
        "be_revenue": be_revenue,
        "be_progress_units": be_progress_units,
        "be_progress_rev": be_progress_rev,

        "products_stats": products_stats,
        "pl_settings_url": "/pl/settings",
    })

@router.get("/export")
def pl_export(
    date_from: str = Query(...),
    date_to: str = Query(...),
    db: Session = Depends(get_db)
):
    inv_rng = _inv_date_range(date_from, date_to)

    sales_core = float(
        db.query(func.coalesce(func.sum(models.Invoice.subtotal - models.Invoice.discount), 0.0))
          .filter(_type_is("S")).filter(inv_rng).scalar() or 0.0
    )
    shipping_income = float(
        db.query(func.coalesce(func.sum(models.Invoice.shipping_cost), 0.0))
          .filter(_type_is("S")).filter(inv_rng).scalar() or 0.0
    )
    returns_total = float(
        db.query(func.coalesce(func.sum(models.Invoice.total), 0.0))
          .filter(_type_is("R")).filter(inv_rng).scalar() or 0.0
    )

    f = io.StringIO()
    w = csv.writer(f)
    w.writerow(["من", date_from])
    w.writerow(["إلى", date_to])
    w.writerow([])
    w.writerow(["إيرادات المبيعات (بدون شحن)", f"{sales_core:.2f}"])
    w.writerow(["إيراد الشحن المحمّل", f"{shipping_income:.2f}"])
    w.writerow(["مرتجعات", f"{returns_total:.2f}"])
    f.seek(0)

    return StreamingResponse(
        iter([f.getvalue().encode("utf-8-sig")]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="PL_{date_from}_to_{date_to}.csv"'}
    )