# app/routers/finance_snapshot.py
from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, or_, text, inspect
from datetime import datetime, date, time, timedelta
from typing import Optional, List, Tuple, Dict

from app.database import SessionLocal
from app import models
from fastapi.templating import Jinja2Templates

router = APIRouter(prefix="/finance", tags=["Finance"])
templates = Jinja2Templates(directory="app/templates")

# ─────────────── Helpers ───────────────

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def _parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d").date()

def _start_of_day(d: date) -> datetime:
    return datetime.combine(d, time.min)

def _end_of_day(d: date) -> datetime:
    return datetime.combine(d, time.max)

def _month_end(dt: date) -> date:
    nxt = (dt.replace(day=28) + timedelta(days=4)).replace(day=1)
    return nxt - timedelta(days=1)

def _iter_month_ends(n: int = 12) -> List[date]:
    today = date.today()
    ends = []
    y, m = today.year, today.month
    for _ in range(n):
        ends.append(_month_end(date(y, m, 1)))
        if m == 1:
            y -= 1; m = 12
        else:
            m -= 1
    return list(reversed(ends))

# طرق الدفع المباشرة (لا تُحسب كمدين عند شركات الشحن)
DIRECT_METHODS = {"InstaPay", "محفظة إلكترونية"}

# مفاتيح تحليلية للفترة (مش للرصد الجاري)
KEYS_EWALLET  = []
KEYS_INSTAPAY = ["SubCash:InstaPay", "تحويل من InstaPay"]

# ─────────────── Finance helpers ───────────────

def _settings_get(db: Session, key: str, default: float = 0.0) -> float:
    try:
        row = db.query(models.Setting).filter(models.Setting.key == key).first()
        if not row:
            return float(default)
        return float(row.value or 0)
    except Exception:
        try:
            row = db.execute(text("SELECT value FROM settings WHERE key=:k"), {"k": key}).fetchone()
            if not row:
                return float(default)
            return float(row[0] or 0)
        except Exception:
            return float(default)

def _cashbox_net_flow(db: Session, up_to: Optional[date] = None) -> float:
    """
    صافي حركة الخزنة (IN-OUT) باستبعاد SubCash:% .
    """
    if not hasattr(models, "FinanceEntry"):
        return 0.0

    q_in = db.query(func.coalesce(func.sum(models.FinanceEntry.amount), 0.0))\
             .filter(models.FinanceEntry.type == "IN")\
             .filter(~models.FinanceEntry.category.ilike("SubCash:%"))
    q_out = db.query(func.coalesce(func.sum(models.FinanceEntry.amount), 0.0))\
              .filter(models.FinanceEntry.type == "OUT")\
              .filter(~models.FinanceEntry.category.ilike("SubCash:%"))

    if up_to:
        d = up_to.strftime("%Y-%m-%d")
        q_in  = q_in.filter(models.FinanceEntry.date <= d)
        q_out = q_out.filter(models.FinanceEntry.date <= d)

    ins  = q_in.scalar()  or 0.0
    outs = q_out.scalar() or 0.0
    return round(float(ins) - float(outs), 2)

def _cashbox_balance(db: Session, up_to: Optional[date] = None) -> float:
    opening = _settings_get(db, "cash_opening_balance", 0.0)
    net_flow = _cashbox_net_flow(db, up_to=up_to)
    return round(float(opening) + float(net_flow), 2)

def _sum_finance_period_net(db: Session, keywords: List[str], s_dt: Optional[datetime], e_dt: Optional[datetime]) -> float:
    if not hasattr(models, "FinanceEntry") or not keywords or (not s_dt and not e_dt):
        return 0.0
    like_filters = [models.FinanceEntry.category.ilike(f"%{k}%") for k in keywords]
    q_in = db.query(func.coalesce(func.sum(models.FinanceEntry.amount), 0.0))\
             .filter(models.FinanceEntry.type == "IN")\
             .filter(or_(*like_filters))
    q_out = db.query(func.coalesce(func.sum(models.FinanceEntry.amount), 0.0))\
              .filter(models.FinanceEntry.type == "OUT")\
              .filter(or_(*like_filters))
    if s_dt:
        d = s_dt.strftime("%Y-%m-%d")
        q_in  = q_in.filter(models.FinanceEntry.date >= d)
        q_out = q_out.filter(models.FinanceEntry.date >= d)
    if e_dt:
        d = e_dt.strftime("%Y-%m-%d")
        q_in  = q_in.filter(models.FinanceEntry.date <= d)
        q_out = q_out.filter(models.FinanceEntry.date <= d)
    ins  = float(q_in.scalar()  or 0.0)
    outs = float(q_out.scalar() or 0.0)
    return round(ins - outs, 2)

# ─────────────── Materials ───────────────

_MAT_MIGRATED = False

def _ensure_materials_schema(db: Session):
    global _MAT_MIGRATED
    if _MAT_MIGRATED:
        return
    try:
        engine = db.get_bind()
        inspector_ = inspect(engine)
        tables = set(inspector_.get_table_names())
        if "production_material_entries" not in tables:
            db.execute(text("""
                CREATE TABLE IF NOT EXISTS production_material_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT,
                    type TEXT,
                    amount REAL DEFAULT 0,
                    note TEXT,
                    finance_entry_id INTEGER,
                    ref_type TEXT,
                    ref_id INTEGER,
                    created_at TEXT
                )
            """))
            db.execute(text("CREATE INDEX IF NOT EXISTS ix_pme_date ON production_material_entries(date)"))
            db.execute(text("CREATE INDEX IF NOT EXISTS ix_pme_type ON production_material_entries(type)"))
            db.execute(text("CREATE INDEX IF NOT EXISTS ix_pme_ref ON production_material_entries(ref_type, ref_id)"))
            db.execute(text("CREATE INDEX IF NOT EXISTS ix_pme_fin ON production_material_entries(finance_entry_id)"))
            db.commit()
        _MAT_MIGRATED = True
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        _MAT_MIGRATED = True

def _materials_balance(db: Session, up_to: Optional[date] = None) -> float:
    _ensure_materials_schema(db)
    try:
        if up_to:
            d = up_to.strftime("%Y-%m-%d")
            row = db.execute(text("""
                SELECT
                  COALESCE(SUM(CASE WHEN type='IN'  THEN amount ELSE 0 END),0) -
                  COALESCE(SUM(CASE WHEN type='OUT' THEN amount ELSE 0 END),0)
                FROM production_material_entries
                WHERE date <= :d
            """), {"d": d}).fetchone()
        else:
            row = db.execute(text("""
                SELECT
                  COALESCE(SUM(CASE WHEN type='IN'  THEN amount ELSE 0 END),0) -
                  COALESCE(SUM(CASE WHEN type='OUT' THEN amount ELSE 0 END),0)
                FROM production_material_entries
            """)).fetchone()

        return round(float((row[0] if row else 0.0) or 0.0), 2)
    except Exception:
        return 0.0

# ─────────────── Shipping receivables (include_admin + up_to) ───────────────

def _linked_returns_totals(db: Session, sale_id: int) -> Tuple[float, float]:
    rets = db.query(models.Invoice).filter(
        getattr(models.Invoice, "type") == "R",
        getattr(models.Invoice, "original_sale_id") == sale_id
    ).all()
    total_ret = sum(float(getattr(r, "total", 0) or 0) for r in rets)
    total_ret_ship = sum(float(getattr(r, "return_shipping_fee", 0) or 0) for r in rets)
    return total_ret, total_ret_ship

def _allocations_sum_for_invoice(db: Session, invoice_id: int) -> float:
    s = db.query(func.coalesce(func.sum(models.ShippingAllocation.amount), 0.0))\
          .filter(models.ShippingAllocation.invoice_id == invoice_id)\
          .scalar()
    return float(s or 0.0)

def _shipping_receivables(db: Session, include_admin: bool = True, up_to: Optional[date] = None) -> float:
    if not hasattr(models, "Invoice"):
        return 0.0

    q = db.query(models.Invoice).filter(
        models.Invoice.shipping_company.isnot(None),
        models.Invoice.shipping_company != "",
    )
    if up_to:
        q = q.filter(models.Invoice.created_at <= _end_of_day(up_to))

    invoices = q.all()

    receivable = 0.0
    for inv in invoices:
        if getattr(inv, "type", "S") != "S":
            continue

        method = (getattr(inv, "payment_method", "") or "").strip()
        if method in DIRECT_METHODS:
            continue

        if (not include_admin) and hasattr(models, "ShippingAdminCover"):
            covered = db.query(models.ShippingAdminCover)\
                        .filter(models.ShippingAdminCover.invoice_id == int(inv.id),
                                models.ShippingAdminCover.company == inv.shipping_company)\
                        .first() is not None
            if covered:
                continue

        base_due = float(getattr(inv, "total", 0) or 0) - float(getattr(inv, "actual_shipping_cost", 0) or 0)
        ret_total, ret_ship = _linked_returns_totals(db, inv.id)
        due_after_returns = base_due - ret_total - ret_ship
        paid_on_inv = _allocations_sum_for_invoice(db, inv.id)
        outstanding = round(due_after_returns - paid_on_inv, 2)
        if outstanding > 0.009:
            receivable += outstanding

    return round(receivable, 2)

# ─────────────── Factories ───────────────

def _factories_net_balance(db: Session, up_to: Optional[date] = None) -> float:
    try:
        q_cost = db.query(func.coalesce(func.sum(models.ManufacturingBatch.qty * func.coalesce(models.ManufacturingBatch.unit_cost, 0)), 0.0))
        q_paid = db.query(func.coalesce(func.sum(models.FactoryPayment.amount), 0.0))
        if up_to:
            d = up_to.strftime("%Y-%m-%d")
            q_cost = q_cost.filter(models.ManufacturingBatch.date <= d)
            q_paid = q_paid.filter(models.FactoryPayment.date <= d)
        cost_total = q_cost.scalar() or 0.0
        paid_total = q_paid.scalar() or 0.0
        return round(float(cost_total) - float(paid_total), 2)
    except Exception:
        try:
            params = {}
            cost_sql = "SELECT COALESCE(SUM(qty*COALESCE(unit_cost,0)),0) FROM manufacturing_batches"
            paid_sql = "SELECT COALESCE(SUM(amount),0) FROM factory_payments"
            if up_to:
                params["d"] = up_to.strftime("%Y-%m-%d")
                cost_sql += " WHERE date <= :d"
                paid_sql += " WHERE date <= :d"
            cost_total = (db.execute(text(cost_sql), params).fetchone() or [0])[0] or 0.0
            paid_total = (db.execute(text(paid_sql), params).fetchone() or [0])[0] or 0.0
            return round(float(cost_total) - float(paid_total), 2)
        except Exception:
            return 0.0

# ─────────────── Inventory (current) ───────────────

def _inventory_cost(db: Session) -> float:
    try:
        if hasattr(models.Product, "cost_price"):
            val = db.query(func.coalesce(func.sum(models.Product.stock * models.Product.cost_price), 0.0)).scalar() or 0.0
            return float(val or 0.0)
    except Exception:
        pass
    return 0.0

# ─────────────── Monthly Series (heavy → used ONLY on demand) ───────────────

def _company_value_series(db: Session, months: int = 12, include_admin: bool = True) -> Tuple[List[str], List[float]]:
    labels, values = [], []
    inv_now = _inventory_cost(db)

    ship_now = float(_shipping_receivables(db, include_admin=include_admin, up_to=None))
    opening = _settings_get(db, "cash_opening_balance", 0.0)

    for mend in _iter_month_ends(months):
        cashNet = _cashbox_net_flow(db, up_to=mend)
        cash_at_m = round(float(opening) + float(cashNet), 2)

        fact_net_m = _factories_net_balance(db, up_to=mend)
        recv_fact_m = abs(fact_net_m) if fact_net_m < 0 else 0.0
        pay_fact_m  = fact_net_m if fact_net_m > 0 else 0.0

        mat_m = _materials_balance(db, up_to=mend)

        receivables_total = round(ship_now + recv_fact_m + mat_m, 2)
        payables_total    = round(pay_fact_m, 2)

        company_val = round(inv_now + cash_at_m + receivables_total - payables_total, 2)
        labels.append(mend.strftime("%Y-%m"))
        values.append(company_val)

    return labels, values

# ─────────────── Routes ───────────────

@router.get("/snapshot", response_class=HTMLResponse)
def financial_snapshot(
    request: Request,
    dfrom: Optional[str] = Query(None),
    dto: Optional[str] = Query(None),
    include_admin: int = Query(0, description="Include admin-covered shipping invoices (1=yes,0=no)"),
    db: Session = Depends(get_db),
):
    """
    ✅ نسخة سريعة:
    - نعرض الكروت + الجدول التحليلي (بالأرقام القديمة كاملة)
    - الجراف لا يتحسب هنا إطلاقاً (Lazy عبر endpoint منفصل)
    """
    s_dt = _start_of_day(_parse_date(dfrom)) if dfrom else None
    e_dt = _end_of_day(_parse_date(dto)) if dto else None
    up_to = _parse_date(dto) if dto else None

    inventory_cost = _inventory_cost(db)  # حالي

    # قيمة الهالك للمعلومية
    wastage_cost = float(
        db.query(func.coalesce(func.sum(
            models.Product.wastage_stock * models.Product.cost_price
        ), 0.0)).scalar() or 0.0
    ) if hasattr(models.Product, "wastage_stock") else 0.0
    cashbox_balance = _cashbox_balance(db, up_to=up_to)

    recv_shipping = float(_shipping_receivables(db, include_admin=False, up_to=up_to))

    factories_net = _factories_net_balance(db, up_to=up_to)
    recv_factories = abs(factories_net) if factories_net < 0 else 0.0
    pay_factories  = factories_net       if factories_net > 0 else 0.0

    recv_materials = _materials_balance(db, up_to=up_to)

    pay_marketers = 0.0

    receivables_total = round(recv_shipping + recv_factories + recv_materials, 2)
    payables_total    = round(pay_marketers + pay_factories, 2)
    company_value     = round(inventory_cost + cashbox_balance + receivables_total - payables_total, 2)

    instapay_period = _sum_finance_period_net(db, KEYS_INSTAPAY, s_dt, e_dt) if (s_dt or e_dt) else None
    ewallet_period  = _sum_finance_period_net(db, KEYS_EWALLET,  s_dt, e_dt) if (s_dt or e_dt) else None

    ctx = {
        "request": request,
        "dfrom": dfrom or "",
        "dto": dto or "",
        "include_admin": int(bool(include_admin)),

        "inventory_cost": inventory_cost,
        "cashbox_balance": cashbox_balance,

        "recv_shipping": recv_shipping,
        "recv_factories": recv_factories,
        "recv_materials": recv_materials,

        "receivables_total": receivables_total,

        "pay_marketers": pay_marketers,
        "pay_factories": pay_factories,
        "payables_total": payables_total,

        "company_value": company_value,
        "wastage_cost": round(wastage_cost, 2),

        "instapay_period": instapay_period,
        "ewallet_period": ewallet_period,

        # ✅ الجراف مش بيتحسب هنا علشان السرعة
        "chart_lazy": True,
    }
    return templates.TemplateResponse("finance_snapshot.html", ctx)

@router.get("/snapshot/chart-data", response_class=JSONResponse)
def finance_snapshot_chart_data(
    dfrom: Optional[str] = Query(None),
    dto: Optional[str] = Query(None),
    include_admin: int = Query(1),
    db: Session = Depends(get_db),
):
    """
    ✅ بيانات الجراف (تقيلة) — تتحسب بس لما اليوزر يفتح الـ Collapse
    """
    labels, values = _company_value_series(db, months=12, include_admin=False)
    return JSONResponse({"labels": labels, "values": values})
