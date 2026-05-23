# app/routers/finance.py
from datetime import datetime
from typing import Optional, List
from fastapi import APIRouter, Request, Depends, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, case
from fastapi.templating import Jinja2Templates
import csv, io

from app.database import SessionLocal
from app import models

router = APIRouter(prefix="/cash", tags=["Cash"])
templates = Jinja2Templates(directory="app/templates")

# ---------- DB ----------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ---------- Sub-wallets (NEW) ----------
SUB_WALLETS = [
    {"key": "instapay", "label": "InstaPay",           "cat": "SubCash:InstaPay"},
    {"key": "ewallet",  "label": "محفظة إلكترونية",    "cat": "SubCash:EWallet"},
]

def _sub_wallet_by_key(key: str):
    for w in SUB_WALLETS:
        if w["key"] == key:
            return w
    return None

def _sub_balance(db: Session, cat: str) -> float:
    """رصيد الخزنة الفرعية = مجموع IN - مجموع OUT لنفس البند"""
    sum_in = float(
        db.query(func.coalesce(func.sum(models.FinanceEntry.amount), 0.0))
          .filter(models.FinanceEntry.type == "IN", models.FinanceEntry.category == cat)
          .scalar() or 0.0
    )
    sum_out = float(
        db.query(func.coalesce(func.sum(models.FinanceEntry.amount), 0.0))
          .filter(models.FinanceEntry.type == "OUT", models.FinanceEntry.category == cat)
          .scalar() or 0.0
    )
    return sum_in - sum_out

# =========================
# 🆕 Raw Materials (خامات إنتاج) helpers
# =========================
RAW_MAT_CATS = {
    "خامات انتاج",
    "خامات إنتاج",
    "raw materials",
    "raw_materials",
}

def _is_raw_material_cat(cat: str) -> bool:
    s = (cat or "").strip().lower()
    return s in RAW_MAT_CATS

def _ensure_raw_material_tables(db: Session):
    """
    Auto-create tables on SQLite فقط (زي أسلوبك).
    لو DB تانية، سيبه للمهاجرات.
    """
    try:
        engine = db.get_bind()
        if engine.dialect.name != "sqlite":
            return

        from sqlalchemy import inspect, text
        insp = inspect(engine)
        tables = set(insp.get_table_names())

        if "raw_material_vouchers" not in tables:
            db.execute(text("""
            CREATE TABLE IF NOT EXISTS raw_material_vouchers (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              code TEXT,
              date TEXT,
              amount REAL DEFAULT 0,
              finance_entry_id INTEGER,
              note TEXT,
              created_at DATETIME
            );
            """))
            db.execute(text("CREATE INDEX IF NOT EXISTS ix_raw_material_vouchers_date ON raw_material_vouchers(date);"))
            db.execute(text("CREATE INDEX IF NOT EXISTS ix_raw_material_vouchers_code ON raw_material_vouchers(code);"))

        if "raw_material_allocations" not in tables:
            db.execute(text("""
            CREATE TABLE IF NOT EXISTS raw_material_allocations (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              voucher_id INTEGER NOT NULL,
              purchase_id INTEGER NOT NULL,
              amount REAL DEFAULT 0,
              created_at DATETIME
            );
            """))
            db.execute(text("CREATE INDEX IF NOT EXISTS ix_raw_material_allocations_voucher_id ON raw_material_allocations(voucher_id);"))
            db.execute(text("CREATE INDEX IF NOT EXISTS ix_raw_material_allocations_purchase_id ON raw_material_allocations(purchase_id);"))

        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass

def _gen_raw_voucher_code(db: Session, d: str) -> str:
    # d = "YYYY-MM-DD"
    base = "RM-" + (d or datetime.now().strftime("%Y-%m-%d")).replace("-", "")
    try:
        cnt = (
            db.query(func.count(models.RawMaterialVoucher.id))
            .filter(models.RawMaterialVoucher.code.like(f"{base}-%"))
            .scalar() or 0
        )
        return f"{base}-{cnt+1:04d}"
    except Exception:
        return f"{base}-0001"

def _raw_balance(db: Session) -> float:
    try:
        _ensure_raw_material_tables(db)
        total_in = float(
            db.query(func.coalesce(func.sum(models.RawMaterialVoucher.amount), 0.0)).scalar() or 0.0
        )
        total_out = float(
            db.query(func.coalesce(func.sum(models.RawMaterialAllocation.amount), 0.0)).scalar() or 0.0
        )
        return round(total_in - total_out, 2)
    except Exception:
        return 0.0

# ---------- helpers ----------
def _parse_date(s: Optional[str]):
    if not s: return None
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except:
        return None

def _filter_range(q, col, dfrom: str, dto: str):
    df = _parse_date(dfrom)
    dt = _parse_date(dto)
    if df:
        q = q.filter(col >= df.strftime("%Y-%m-%d"))
    if dt:
        q = q.filter(col <= dt.strftime("%Y-%m-%d"))
    return q

def _opening_balance(db: Session) -> float:
    s = db.query(models.Setting).filter(models.Setting.key == "cash_opening_balance").first()
    try:
        return float(s.value) if s and s.value is not None else 0.0
    except:
        return 0.0

def _set_opening_balance(db: Session, val: float):
    s = db.query(models.Setting).filter(models.Setting.key == "cash_opening_balance").first()
    if not s:
        s = models.Setting(key="cash_opening_balance", value=str(val))
        db.add(s)
    else:
        s.value = str(val)
    db.commit()

# ---------- pages ----------
@router.get("", response_class=HTMLResponse)
def cash_home(
    request: Request,
    q: str = Query("", description="بحث في الملاحظة"),
    category: str = "",
    date_from: str = "",
    date_to: str = "",
    page: int = Query(1, ge=1, description="رقم الصفحة"),          # ← NEW
    per_page: int = Query(15, ge=1, le=200, description="عدد السجلات في الصفحة"),  # ← NEW (افتراضي 15)
    db: Session = Depends(get_db)
):
    # لستة البنود (Distinct)
    cats = [c[0] for c in db.query(models.FinanceEntry.category)
                         .filter(models.FinanceEntry.category.isnot(None))
                         .group_by(models.FinanceEntry.category)
                         .order_by(models.FinanceEntry.category.asc())
                         .all()]

    # ====== NEW: base query with filters (للاستخدام في العد والنتائج) ======
    base_q = db.query(models.FinanceEntry)
    if category:
        base_q = base_q.filter(models.FinanceEntry.category == category)
    if q:
        like = f"%{q}%"
        base_q = base_q.filter(models.FinanceEntry.note.like(like))
    if date_from or date_to:
        base_q = _filter_range(base_q, models.FinanceEntry.date, date_from, date_to)

    # إجمالي السجلات المطابقة (قبل التقسيم)
    total_count = base_q.count()

    # حساب الصفحات
    per_page = max(1, int(per_page))
    pages = max(1, (total_count + per_page - 1) // per_page)
    page = min(max(1, int(page)), pages)
    offset = (page - 1) * per_page

    # الاستعلام المعروض — الأحدث أولاً + limit/offset
    qry = (
        base_q
        .order_by(models.FinanceEntry.date.desc(), models.FinanceEntry.id.desc())
        .offset(offset)
        .limit(per_page)
    )
    rows = qry.all()
    # ====== /NEW ======

    # إجماليات كلّية (مع فِلتر البند لو مستخدم)
    total_in_q = db.query(func.coalesce(func.sum(models.FinanceEntry.amount), 0.0))\
                   .filter(models.FinanceEntry.type == "IN")
    total_out_q = db.query(func.coalesce(func.sum(models.FinanceEntry.amount), 0.0))\
                    .filter(models.FinanceEntry.type == "OUT")

    if category:
        total_in_q  = total_in_q.filter(models.FinanceEntry.category == category)
        total_out_q = total_out_q.filter(models.FinanceEntry.category == category)
    if date_from or date_to:
        total_in_q  = _filter_range(total_in_q,  models.FinanceEntry.date, date_from, date_to)
        total_out_q = _filter_range(total_out_q, models.FinanceEntry.date, date_from, date_to)

    total_in  = float(total_in_q.scalar()  or 0.0)
    total_out = float(total_out_q.scalar() or 0.0)

    opening = _opening_balance(db)
    balance = opening + total_in - total_out

    # ملخّص حسب البند (لأزرار الفلاتر)
    by_cat = db.query(
        models.FinanceEntry.category,
        func.coalesce(
            func.sum(case((models.FinanceEntry.type == "IN", models.FinanceEntry.amount), else_=0.0)), 0.0
        ).label("sum_in"),
        func.coalesce(
            func.sum(case((models.FinanceEntry.type == "OUT", models.FinanceEntry.amount), else_=0.0)), 0.0
        ).label("sum_out"),
    ).group_by(models.FinanceEntry.category).order_by(models.FinanceEntry.category.asc()).all()

    # --------- NEW: عداد دفعات الشحن غير المُرحّلة للخزنة ---------
    try:
        subq = db.query(models.ShippingPaymentCashMap.payment_id)
        unlinked_q = db.query(models.ShippingPayment).filter(~models.ShippingPayment.id.in_(subq))
        unlinked_count = unlinked_q.count()
        unlinked_sum = float(
            db.query(func.coalesce(func.sum(models.ShippingPayment.amount), 0.0))
              .filter(~models.ShippingPayment.id.in_(subq))
              .scalar() or 0.0
        )
    except Exception:
        # لو جداول الربط/الدفعات مش موجودة، ما نكسرش الصفحة
        unlinked_count = 0
        unlinked_sum = 0.0
    # ---------------------------------------------------------------

    # --------- NEW: أرصدة الخزن الفرعية ---------
    sub_balances = []
    total_sub_balance = 0.0
    for w in SUB_WALLETS:
        bal = _sub_balance(db, w["cat"])
        sub_balances.append({"key": w["key"], "label": w["label"], "cat": w["cat"], "balance": bal})
        total_sub_balance += bal
    # ---------------------------------------------

    # --------- NEW: رصيد خامات إنتاج (تحت التشغيل) ---------
    raw_material_balance = _raw_balance(db)
    # -------------------------------------------------------

    return templates.TemplateResponse("finance_cash.html", {
        "request": request,
        "rows": rows,
        "categories": cats,
        "by_cat": by_cat,
        "category": category,
        "q": q,
        "date_from": date_from,
        "date_to": date_to,
        "opening": opening,
        "total_in": total_in,
        "total_out": total_out,
        "balance": balance,
        # موجودة سابقًا:
        "unlinked_count": unlinked_count,
        "unlinked_sum": unlinked_sum,
        # NEW:
        "sub_balances": sub_balances,
        "total_sub_balance": total_sub_balance,
        # Pagination NEW:
        "page": page,
        "per_page": per_page,
        "pages": pages,
        "total_count": total_count,
        # Raw materials NEW:
        "raw_material_balance": raw_material_balance,
    })

@router.post("/add")
def cash_add(
    type: str = Form(...),            # IN / OUT
    category: str = Form(...),
    amount: float = Form(...),
    date: str = Form(...),
    note: str = Form(""),
    db: Session = Depends(get_db)
):
    if type not in ("IN","OUT"):
        return RedirectResponse(url="/cash?msg=⚠️ نوع الحركة غير صحيح", status_code=303)
    category = (category or "").strip()
    if not category:
        return RedirectResponse(url="/cash?msg=⚠️ اكتب البند", status_code=303)
    try:
        amount = float(amount)
    except:
        return RedirectResponse(url="/cash?msg=⚠️ المبلغ غير صالح", status_code=303)

    # لو التاريخ فاضي خلّيه تاريخ اليوم بصيغة YYYY-MM-DD
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")

    # ✅ تأكد الجداول موجودة (SQLite auto)
    _ensure_raw_material_tables(db)

    fe = models.FinanceEntry(
        type=type,
        category=category,
        amount=amount,
        date=date,
        note=(note or "").strip() or None
    )
    db.add(fe)
    db.flush()  # عشان ناخد fe.id

    # ✅ لو مصروف بند خامات إنتاج → اعمل Voucher تلقائي
    if type == "OUT" and _is_raw_material_cat(category):
        try:
            code = _gen_raw_voucher_code(db, date)
            v = models.RawMaterialVoucher(
                code=code,
                date=date,
                amount=float(amount),
                finance_entry_id=int(fe.id),
                note=(note or "").strip() or f"سند شراء خامات من الخزنة (قيد #{fe.id})"
            )
            db.add(v)
        except Exception:
            pass

    db.commit()
    return RedirectResponse(url=f"/cash?msg=✓ تم الإضافة", status_code=303)

@router.post("/delete")
def cash_delete(eid: int = Form(...), db: Session = Depends(get_db)):
    row = db.query(models.FinanceEntry).get(int(eid))
    if row:
        db.delete(row); db.commit()
    return RedirectResponse(url="/cash?msg=✓ تم الحذف", status_code=303)

@router.post("/opening")
def cash_set_opening(opening: float = Form(0), db: Session = Depends(get_db)):
    try:
        val = float(opening or 0)
    except:
        val = 0.0
    _set_opening_balance(db, val)
    return RedirectResponse(url="/cash?msg=✓ تم ضبط رصيد افتتاحي", status_code=303)

# --------- NEW: تحويل من خزنة فرعية إلى الخزنة الرئيسية ---------
@router.post("/transfer-sub")
def transfer_sub_to_main(
    source_key: str = Form(...),              # instapay / ewallet
    amount: float = Form(...),
    date: str = Form(""),
    note: str = Form(""),
    db: Session = Depends(get_db)
):
    w = _sub_wallet_by_key(source_key)
    if not w:
        return RedirectResponse(url="/cash?msg=⚠️ خزنة فرعية غير معروفة", status_code=303)

    try:
        amt = float(amount or 0)
    except:
        return RedirectResponse(url="/cash?msg=⚠️ المبلغ غير صالح", status_code=303)

    # التاريخ الافتراضي = اليوم
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")

    # لو المستخدم اختار 0 أو أقل، نجرب نستخدم “كل الرصيد”
    if amt <= 0:
        bal = _sub_balance(db, w["cat"])
        amt = round(bal, 2)

    if amt <= 0:
        return RedirectResponse(url="/cash?msg=⚠️ لا يوجد رصيد متاح للتحويل", status_code=303)

    # قيدين: OUT من الفرعية — IN في الرئيسية
    # 1) OUT من البند الفرعي
    db.add(models.FinanceEntry(
        type="OUT",
        category=w["cat"],
        amount=amt,
        date=date,
        note=(note or f"تحويل إلى الخزنة الرئيسية")
    ))
    # 2) IN في الرئيسية (نخليه بند واضح)
    db.add(models.FinanceEntry(
        type="IN",
        category=f"تحويل من {w['label']}",
        amount=amt,
        date=date,
        note=(note or f"تحويل من {w['label']} إلى الخزنة الرئيسية")
    ))
    db.commit()
    return RedirectResponse(url="/cash?msg=✓ تم تحويل المبلغ للخزنة", status_code=303)
# ---------------------------------------------------------------

@router.get("/export")
def cash_export(
    category: str = "",
    date_from: str = "",
    date_to: str = "",
    q: str = "",
    db: Session = Depends(get_db)
):
    # في التصدير: الأقدم -> الأحدث حسب التاريخ ثم الـ id
    qry = db.query(models.FinanceEntry).order_by(
        models.FinanceEntry.date.asc(),
        models.FinanceEntry.id.asc()
    )
    if category:
        qry = qry.filter(models.FinanceEntry.category == category)
    if q:
        like = f"%{q}%"; qry = qry.filter(models.FinanceEntry.note.like(like))
    if date_from or date_to:
        qry = _filter_range(qry, models.FinanceEntry.date, date_from, date_to)

    items = qry.all()

    opening = _opening_balance(db)

    total_in_q = db.query(func.coalesce(func.sum(models.FinanceEntry.amount), 0.0))\
                   .filter(models.FinanceEntry.type=="IN")
    total_out_q = db.query(func.coalesce(func.sum(models.FinanceEntry.amount), 0.0))\
                    .filter(models.FinanceEntry.type=="OUT")
    if category:
        total_in_q  = total_in_q.filter(models.FinanceEntry.category == category)
        total_out_q = total_out_q.filter(models.FinanceEntry.category == category)
    if date_from or date_to:
        total_in_q  = _filter_range(total_in_q,  models.FinanceEntry.date, date_from, date_to)
        total_out_q = _filter_range(total_out_q, models.FinanceEntry.date, date_from, date_to)

    total_in  = float(total_in_q.scalar()  or 0.0)
    total_out = float(total_out_q.scalar() or 0.0)
    balance   = opening + total_in - total_out

    buff = io.StringIO(); w = csv.writer(buff)
    w.writerow(["التاريخ","النوع","البند","المبلغ","ملاحظة"])
    for r in items:
        w.writerow([r.date, "إيراد" if r.type=="IN" else "مصروف", r.category, f"{(r.amount or 0):.2f}", r.note or ""])
    w.writerow([])
    w.writerow(["الرصيد الافتتاحي", f"{opening:.2f}"])
    w.writerow(["إجمالي الإيرادات", f"{total_in:.2f}"])
    w.writerow(["إجمالي المصروفات", f"{total_out:.2f}"])
    w.writerow(["صافي الخزنة", f"{balance:.2f}"])

    buff.seek(0)
    filename_utf = "cashbook.csv"
    return StreamingResponse(
        iter([buff.getvalue().encode("utf-8-sig")]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=%s" % filename_utf}
    )