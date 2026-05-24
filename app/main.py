from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from sqlalchemy import func, desc, or_  # ← أضفت or_
from datetime import datetime, date, time, timedelta
import asyncio

from app.database import Base, engine, SessionLocal
from app import models

# إنشاء الجداول (إن لم تكن موجودة)
Base.metadata.create_all(bind=engine)

# Routers الأساسية
from app.routers import (
    products,
    sales,
    returns,
    invoices,
    shipping,
    customers,
    reports,
    settlements,
    products_import,
    shipping_cashlink,
    pl_settings,
    cash_compat,
    alerts,
    invoices_browser,
    product_card,
    finance_snapshot,
    purchases,   # ✅ مشتريات
    materials,
    wastage,
    users,       # ✅ نظام المستخدمين
)

from app.auth import User, ensure_admin_user, get_current_user, has_permission, get_session
from app.database import Base

# =========================
# [NEW] اختياري: استيراد روترات الخزنة وP&L والمسوّقين إن وُجدت
try:
    from app.routers import finance  # /cash , /cash/close-day ...
except Exception:
    finance = None

try:
    from app.routers import pl  # /pl
except Exception:
    pl = None

try:
    from app.routers import marketers  # /marketers , /marketers/commission ...
except Exception:
    marketers = None
# =========================

app = FastAPI(title="MumEase POS")

# ── Middleware للتحقق من الـ Login ──
from fastapi import HTTPException
from fastapi.responses import RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

PUBLIC_PATHS = ["/login", "/static", "/health", "/favicon.ico"]

class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        # صفحات عامة مش محتاجة login
        if any(path.startswith(p) for p in PUBLIC_PATHS):
            return await call_next(request)
        # تحقق من الـ session
        token = request.cookies.get("session_token")
        from app.auth import get_session
        if not token or not get_session(token):
            return RedirectResponse(url="/login", status_code=302)
        return await call_next(request)

app.add_middleware(AuthMiddleware)

# Static & Templates
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# === [FACTORY ACCOUNTS START] ===
app.state.templates = templates
# === [FACTORY ACCOUNTS END] ===


def _auto_close_previous_day(db):
    """
    يقفل يوم أمس تلقائيًا عند أول زيارة للـ / (الهوم) لو مفيش DailySettlement لليوم ده.
    """
    try:
        y_date = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")

        exists = (
            db.query(models.DailySettlement)
              .filter(models.DailySettlement.date == y_date)
              .first()
        )
        if exists:
            return

        y = date.today() - timedelta(days=1)
        start_dt = datetime.combine(y, time.min)
        end_dt   = datetime.combine(y, time.max)

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

        ex_instapay_wallet = ~or_(
            models.FinanceEntry.category.ilike("%instapay%"),
            models.FinanceEntry.category.ilike("%wallet%"),
            models.FinanceEntry.category.ilike("%انستا%"),
            models.FinanceEntry.category.ilike("%محفظة%"),
            models.FinanceEntry.note.ilike("%instapay%"),
            models.FinanceEntry.note.ilike("%wallet%"),
            models.FinanceEntry.note.ilike("%انستا%"),
            models.FinanceEntry.note.ilike("%محفظة%"),
        )
        operating_expenses = float(
            db.query(func.coalesce(func.sum(models.FinanceEntry.amount), 0.0))
              .filter(
                  models.FinanceEntry.type == "OUT",
                  models.FinanceEntry.date == y_date,
                  ex_instapay_wallet
              )
              .scalar() or 0.0
        )

        expenses_total = shipping_expense_actual + return_shipping_fees + operating_expenses
        net_total = sales_total - returns_total - expenses_total

        row = models.DailySettlement(
            date=y_date,
            sales_total=sales_total,
            returns_total=returns_total,
            expenses_total=expenses_total,
            net_total=net_total,
            created_at=datetime.now()
        )
        db.add(row)
        db.commit()
    except Exception:
        db.rollback()
        return


def today_range():
    """بترجع بداية ونهاية اليوم بالتوقيت المحلي للسيرفر."""
    today = date.today()
    return datetime.combine(today, time.min), datetime.combine(today, time.max)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    """
    الهوم داشبورد:
    - KPIs كلها اليوم فقط
    - حالة الطلبات: ستاندرد الشهر الحالي (من الـ JS)
    """
    db = SessionLocal()
    try:
        _auto_close_previous_day(db)

        start_dt, end_dt = today_range()
        today_str = date.today().strftime("%Y-%m-%d")

        token = request.cookies.get("session_token")
        session_user = get_session(token) if token else None

        username = "User"

        if session_user:
            username = session_user.get("username", "User")

        # هل يومية اليوم مقفولة؟
        today_settlement = (
            db.query(models.DailySettlement)
              .filter(models.DailySettlement.date == today_str)
              .first()
        )
        settlement_closed = today_settlement is not None

        # فواتير اليوم
        sales_today = db.query(models.Invoice).filter(
            models.Invoice.type == "S",
            models.Invoice.created_at >= start_dt,
            models.Invoice.created_at <= end_dt
        ).all()

        returns_today = db.query(models.Invoice).filter(
            models.Invoice.type == "R",
            models.Invoice.created_at >= start_dt,
            models.Invoice.created_at <= end_dt
        ).all()

        # ====== أرقام أساسية اليوم ======
        if settlement_closed:
            sales_count = 0
            sales_sum = 0.0
            returns_count = 0
            returns_sum = 0.0
            top_products = []
        else:
            sales_count = len(sales_today)
            sales_sum = float(sum(float(inv.total or 0) for inv in sales_today))
            returns_count = len(returns_today)
            returns_sum = float(sum(float(inv.total or 0) for inv in returns_today))

            # Top products اليوم (بيع فقط)
            top_rows = (
                db.query(
                    models.InvoiceItem.product_name.label("name"),
                    func.sum(models.InvoiceItem.qty).label("qty"),
                    func.sum(models.InvoiceItem.line_total).label("rev"),
                )
                .join(models.Invoice, models.InvoiceItem.invoice_id == models.Invoice.id)
                .filter(
                    models.Invoice.type == "S",
                    models.Invoice.created_at >= start_dt,
                    models.Invoice.created_at <= end_dt
                )
                .group_by(models.InvoiceItem.product_name)
                .order_by(desc("qty"), desc("rev"))
                .limit(4)
                .all()
            )
            top_products = [
                {"name": r.name, "qty": int(r.qty or 0), "rev": float(r.rev or 0)}
                for r in top_rows
            ]

        # Low stock
        low_count = db.query(models.Product).filter(
            (models.Product.stock == None) | (models.Product.stock < 4)
        ).count()

        # ========= KPIs إضافية (TODAY فقط) =========
        if settlement_closed:
            ship_sales_sum = 0.0
            ship_ret_sum = 0.0
            shipping_expense_today = 0.0
            net_sales_today = 0.0
            invoices_count_today = 0
            avg_order_value_today = 0.0
            avg_discount_today = 0.0
            return_rate_pct_today = 0.0
            cash_net_today = 0.0
            inventory_value = float(
                db.query(func.coalesce(func.sum(
                    func.coalesce(models.Product.stock, 0) * func.coalesce(models.Product.cost_price, 0)
                ), 0.0)).scalar() or 0.0
            )
            ads_expense_today = 0.0
            cogs_today = 0.0
            gross_profit_today = 0.0
            gross_margin_pct_today = 0.0
            recent_invoices = []
        else:
            ship_sales_sum = float(
                db.query(func.coalesce(func.sum(models.Invoice.actual_shipping_cost), 0.0))
                  .filter(models.Invoice.type == "S",
                          models.Invoice.created_at >= start_dt,
                          models.Invoice.created_at <= end_dt)
                  .scalar() or 0.0
            )
            ship_ret_sum = float(
                db.query(func.coalesce(func.sum(models.Invoice.return_shipping_fee), 0.0))
                  .filter(models.Invoice.type == "R",
                          models.Invoice.created_at >= start_dt,
                          models.Invoice.created_at <= end_dt)
                  .scalar() or 0.0
            )
            shipping_expense_today = ship_sales_sum + ship_ret_sum

            # صافي المبيعات = مبيعات - شحن المبيعات (بس)
            net_sales_today = float(sales_sum or 0.0) - float(ship_sales_sum or 0.0)

            invoices_count_today = int((sales_count or 0) + (returns_count or 0))
            avg_order_value_today = float(sales_sum / sales_count) if sales_count else 0.0
            avg_discount_today = float(sum(float(x.discount or 0.0) for x in sales_today) / sales_count) if sales_count else 0.0
            return_rate_pct_today = float((returns_sum / sales_sum) * 100.0) if (sales_sum and sales_sum > 0) else 0.0

            # صافي الخزنة (اليوم فقط) من FinanceEntry
            cash_in_today = float(
                db.query(func.coalesce(func.sum(models.FinanceEntry.amount), 0.0))
                  .filter(models.FinanceEntry.type == "IN", models.FinanceEntry.date == today_str)
                  .scalar() or 0.0
            )
            cash_out_today = float(
                db.query(func.coalesce(func.sum(models.FinanceEntry.amount), 0.0))
                  .filter(models.FinanceEntry.type == "OUT", models.FinanceEntry.date == today_str)
                  .scalar() or 0.0
            )
            cash_net_today = cash_in_today - cash_out_today

            # قيمة المخزون (Global)
            inventory_value = float(
                db.query(func.coalesce(func.sum(
                    func.coalesce(models.Product.stock, 0) * func.coalesce(models.Product.cost_price, 0)
                ), 0.0)).scalar() or 0.0
            )

            # Ads expense (اليوم) — اختياري حسب البنود
            ads_expense_today = float(
                db.query(func.coalesce(func.sum(models.FinanceEntry.amount), 0.0))
                  .filter(models.FinanceEntry.type == "OUT",
                          models.FinanceEntry.date == today_str)
                  .filter(or_(
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
                  ))
                  .scalar() or 0.0
            )

            # COGS (اليوم) = qty * cost_price الحالي للمنتج (تقريب ممتاز للداشبورد)
            cogs_today = float(
                db.query(func.coalesce(func.sum(
                    func.coalesce(models.InvoiceItem.qty, 0) * func.coalesce(models.Product.cost_price, 0)
                ), 0.0))
                  .join(models.Invoice, models.InvoiceItem.invoice_id == models.Invoice.id)
                  .join(models.Product, models.InvoiceItem.product_id == models.Product.id)
                  .filter(models.Invoice.type == "S",
                          models.Invoice.created_at >= start_dt,
                          models.Invoice.created_at <= end_dt)
                  .scalar() or 0.0
            )

            # مجمل الربح حسب تعريفك
            gross_profit_today = (sales_sum - ship_sales_sum) - returns_sum - ads_expense_today - cogs_today
            gross_margin_pct_today = (gross_profit_today / sales_sum * 100.0) if (sales_sum and sales_sum > 0) else 0.0

            # جدول متابعة سريع أصغر: آخر 5 فواتير اليوم (بيع+مرتجع)
            recent_invoices = (
                db.query(models.Invoice)
                  .filter(models.Invoice.created_at >= start_dt, models.Invoice.created_at <= end_dt)
                  .order_by(models.Invoice.id.desc())
                  .limit(5)
                  .all()
            )

        ctx = {
            "request": request,
            "username": username,
            "today_date": date.today().strftime("%d/%m/%Y"),

            # الموجودين عندك أصلاً
            "today_str": today_str,
            "sales_count": sales_count,
            "sales_sum": sales_sum,
            "returns_count": returns_count,
            "returns_sum": returns_sum,
            "top_products": top_products,
            "low_stock_count": low_count,
            "settlement_closed": settlement_closed,
            "today_settlement": today_settlement,

            # KPIs الجديدة (TODAY فقط)
            "net_sales_today": net_sales_today,
            "invoices_count_today": invoices_count_today,
            "avg_order_value_today": avg_order_value_today,
            "avg_discount_today": avg_discount_today,
            "return_rate_pct_today": return_rate_pct_today,
            "cash_net_today": cash_net_today,
            "shipping_expense_today": shipping_expense_today,
            "inventory_value": inventory_value,
            "gross_profit_today": gross_profit_today,
            "gross_margin_pct_today": gross_margin_pct_today,
            "recent_invoices": recent_invoices,
        }
        return templates.TemplateResponse("home.html", ctx)
    finally:
        db.close()


@app.get("/today/sales", response_class=HTMLResponse)
def today_sales(request: Request):
    db = SessionLocal()
    try:
        s, e = today_range()
        invs = (
            db.query(models.Invoice)
              .filter(
                  models.Invoice.type == "S",
                  models.Invoice.created_at >= s,
                  models.Invoice.created_at <= e
              )
              .order_by(models.Invoice.id.desc())
              .all()
        )
        return templates.TemplateResponse(
            "invoices_today.html",
            {"request": request, "title": "فواتير اليوم", "invoices": invs}
        )
    finally:
        db.close()


@app.get("/today/returns", response_class=HTMLResponse)
def today_returns(request: Request):
    db = SessionLocal()
    try:
        s, e = today_range()
        invs = (
            db.query(models.Invoice)
              .filter(
                  models.Invoice.type == "R",
                  models.Invoice.created_at >= s,
                  models.Invoice.created_at <= e
              )
              .order_by(models.Invoice.id.desc())
              .all()
        )
        return templates.TemplateResponse(
            "invoices_today.html",
            {"request": request, "title": "مرتجعات اليوم", "invoices": invs}
        )
    finally:
        db.close()


# Include Routers
app.include_router(product_card.router)
app.include_router(products.router)
app.include_router(sales.router)
app.include_router(returns.router)
app.include_router(shipping.router)
app.include_router(customers.router)
app.include_router(reports.router)
app.include_router(settlements.router)
app.include_router(products_import.router)
app.include_router(shipping_cashlink.router)
app.include_router(pl_settings.router)
app.include_router(cash_compat.router)
app.include_router(alerts.router)
app.include_router(invoices_browser.router)
app.include_router(invoices.router)
app.include_router(finance_snapshot.router)
app.include_router(purchases.router)
app.include_router(materials.router)
app.include_router(wastage.router)
app.include_router(users.router)


# ربط الروترات الجديدة لو موجودة
if finance and hasattr(finance, "router"):
    app.include_router(finance.router)

if pl and hasattr(pl, "router"):
    app.include_router(pl.router)

if marketers and hasattr(marketers, "router"):
    app.include_router(marketers.router)

# === [FACTORY ACCOUNTS START] ===
import app.factory_accounts as factory_accounts
app.include_router(factory_accounts.router)
# === [FACTORY ACCOUNTS END] ===


@app.get("/health")
def health():
    return PlainTextResponse("OK")


# =========================
# التقفيل التلقائي لليومية (00:05 كل يوم)
# =========================

def _exclude_instapay_wallet_filter():
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


def _day_bounds(d: date):
    return datetime.combine(d, time.min), datetime.combine(d, time.max)


def _auto_close_yesterday():
    db = SessionLocal()
    try:
        y = date.today() - timedelta(days=1)
        day_str = y.strftime("%Y-%m-%d")

        exists = db.query(models.DailySettlement).filter(models.DailySettlement.date == day_str).first()
        if exists:
            return

        start_dt, end_dt = _day_bounds(y)

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
                      models.FinanceEntry.date == day_str,
                      _exclude_instapay_wallet_filter())
              .scalar() or 0.0
        )

        expenses_total = shipping_total_expense + operating_expenses
        net_total = sales_total - returns_total - expenses_total

        row = models.DailySettlement(
            date=day_str,
            sales_total=sales_total,
            returns_total=returns_total,
            expenses_total=expenses_total,
            net_total=net_total,
            created_at=datetime.now()
        )
        db.add(row)
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


async def _sleep_until_next_0005():
    now = datetime.now()
    tomorrow = now.date() + timedelta(days=1)
    target = datetime.combine(tomorrow, time(hour=0, minute=5))
    seconds = (target - now).total_seconds()
    if seconds < 1:
        seconds = 60
    await asyncio.sleep(seconds)


async def auto_close_daemon():
    await _sleep_until_next_0005()
    while True:
        try:
            _auto_close_yesterday()
        except Exception:
            pass
        await _sleep_until_next_0005()

@app.get("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("session_token")
    return response


@app.on_event("startup")
async def _start_auto_close_task():
    asyncio.create_task(auto_close_daemon())