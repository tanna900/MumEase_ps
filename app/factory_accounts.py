from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import func
from decimal import Decimal
from app.database import SessionLocal
from app import models

import io, csv, re
from urllib.parse import quote

router = APIRouter(prefix="/factory", tags=["factory"])

PER_PAGE = 10  # ✅ كل جدول 10 سطور


# -------------------- Helpers --------------------

def _sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", name or "export").strip()
    return name or "export"


def _range(d1: str | None, d2: str | None):
    """
    حالة 1: لو الاتنين فاضيين → رجّع (None, None) = بدون فلترة تاريخ.
    غير كده: استخدم ما تم تمريره، ولو طرف ناقص حط حد واسع معقول.
    """
    if not d1 and not d2:
        return None, None
    return d1 or "1900-01-01", d2 or "2999-12-31"


def _apply_date_filters(q, model_date_col, d1: str | None, d2: str | None):
    """إضافة فلاتر التاريخ لو موجودة فقط."""
    if d1:
        q = q.filter(model_date_col >= d1)
    if d2:
        q = q.filter(model_date_col <= d2)
    return q


def _safe_int(x, default=1):
    try:
        v = int(x)
        return v if v > 0 else default
    except Exception:
        return default


def _pages(total_rows: int, per_page: int) -> int:
    if total_rows <= 0:
        return 1
    return (total_rows + per_page - 1) // per_page


def _back_to_referer(request: Request, fallback: str):
    ref = request.headers.get("referer")
    if ref:
        return RedirectResponse(ref, status_code=303)
    return RedirectResponse(fallback, status_code=303)


# -------------------- Index & Detail --------------------

@router.get("", response_class=HTMLResponse)
def factory_index(request: Request, d1: str | None = None, d2: str | None = None):
    db = SessionLocal()
    try:
        rd1, rd2 = _range(d1, d2)
        factories = db.query(models.Factory).order_by(models.Factory.name.asc()).all()

        summaries = []
        for f in factories:
            # إجمالي تكلفة التصنيع
            qb = db.query(
                func.coalesce(func.sum(models.ManufacturingBatch.qty * models.ManufacturingBatch.unit_cost), 0.0)
            ).filter(models.ManufacturingBatch.factory_id == f.id)
            qb = _apply_date_filters(qb, models.ManufacturingBatch.date, rd1, rd2)
            total_cost = qb.scalar() or 0.0

            # إجمالي المدفوع
            qp = db.query(
                func.coalesce(func.sum(models.FactoryPayment.amount), 0.0)
            ).filter(models.FactoryPayment.factory_id == f.id)
            qp = _apply_date_filters(qp, models.FactoryPayment.date, rd1, rd2)
            total_paid = qp.scalar() or 0.0

            balance = Decimal(str(total_cost)) - Decimal(str(total_paid))
            summaries.append({
                "factory": f,
                "total_cost": float(total_cost),
                "total_paid": float(total_paid),
                "balance": float(balance),
            })

        templates = request.app.state.templates
        return templates.TemplateResponse("factory/index.html", {
            "request": request,
            "summaries": summaries,
            "d1": d1 or "",
            "d2": d2 or "",
        })
    finally:
        db.close()


@router.get("/{fid}", response_class=HTMLResponse)
def factory_detail(
    fid: int,
    request: Request,
    d1: str | None = None,
    d2: str | None = None,
    bp: int | None = 1,   # ✅ batch page
    pp: int | None = 1,   # ✅ payments page
):
    db = SessionLocal()
    try:
        f = db.query(models.Factory).get(fid)
        if not f:
            return HTMLResponse("<div class='container py-5'>المصنع غير موجود</div>", status_code=404)

        rd1, rd2 = _range(d1, d2)

        # -------- Batches (دفعات التصنيع) Pagination --------
        bp = _safe_int(bp, 1)
        qb_base = db.query(models.ManufacturingBatch).filter(models.ManufacturingBatch.factory_id == fid)
        qb_base = _apply_date_filters(qb_base, models.ManufacturingBatch.date, rd1, rd2)

        batches_total = (
            db.query(func.count(models.ManufacturingBatch.id))
              .filter(models.ManufacturingBatch.factory_id == fid)
        )
        batches_total = _apply_date_filters(batches_total, models.ManufacturingBatch.date, rd1, rd2).scalar() or 0

        batch_pages = _pages(int(batches_total), PER_PAGE)
        if bp > batch_pages:
            bp = batch_pages

        batches = (
            qb_base.order_by(models.ManufacturingBatch.date.desc(), models.ManufacturingBatch.id.desc())
                  .offset((bp - 1) * PER_PAGE)
                  .limit(PER_PAGE)
                  .all()
        )

        # -------- Payments (المدفوعات) Pagination --------
        pp = _safe_int(pp, 1)
        qp_base = db.query(models.FactoryPayment).filter(models.FactoryPayment.factory_id == fid)
        qp_base = _apply_date_filters(qp_base, models.FactoryPayment.date, rd1, rd2)

        payments_total = (
            db.query(func.count(models.FactoryPayment.id))
              .filter(models.FactoryPayment.factory_id == fid)
        )
        payments_total = _apply_date_filters(payments_total, models.FactoryPayment.date, rd1, rd2).scalar() or 0

        pay_pages = _pages(int(payments_total), PER_PAGE)
        if pp > pay_pages:
            pp = pay_pages

        payments = (
            qp_base.order_by(models.FactoryPayment.date.desc(), models.FactoryPayment.id.desc())
                  .offset((pp - 1) * PER_PAGE)
                  .limit(PER_PAGE)
                  .all()
        )

        # -------- Totals for header cards (لازم تبقى على كل الفترة مش الصفحة) --------
        # إجمالي تكلفة التصنيع (كل الفترة)
        total_cost = (
            db.query(func.coalesce(func.sum(models.ManufacturingBatch.qty * models.ManufacturingBatch.unit_cost), 0.0))
              .filter(models.ManufacturingBatch.factory_id == fid)
        )
        total_cost = _apply_date_filters(total_cost, models.ManufacturingBatch.date, rd1, rd2).scalar() or 0.0

        # إجمالي المدفوع (كل الفترة)
        total_paid = (
            db.query(func.coalesce(func.sum(models.FactoryPayment.amount), 0.0))
              .filter(models.FactoryPayment.factory_id == fid)
        )
        total_paid = _apply_date_filters(total_paid, models.FactoryPayment.date, rd1, rd2).scalar() or 0.0

        balance = Decimal(str(total_cost)) - Decimal(str(total_paid))

        templates = request.app.state.templates
        return templates.TemplateResponse("factory/detail.html", {
            "request": request,
            "f": f,

            # ✅ paginated lists
            "batches": batches,
            "payments": payments,

            # ✅ header totals (full period)
            "total_cost": float(total_cost),
            "total_paid": float(total_paid),
            "balance": float(balance),

            # ✅ filters
            "d1": d1 or "",
            "d2": d2 or "",

            # ✅ pagination vars (علشان الترقيم يظهر)
            "batch_page": bp,
            "batch_pages": batch_pages,
            "pay_page": pp,
            "pay_pages": pay_pages,
        })
    finally:
        db.close()


# -------------------- Create --------------------

@router.post("/{fid}/batches")
def add_batch(
    fid: int,
    request: Request,
    date_: str = Form(...),
    qty: int = Form(...),
    unit_cost: float = Form(...),
    note: str | None = Form(None),
):
    db = SessionLocal()
    try:
        b = models.ManufacturingBatch(
            factory_id=fid, date=date_, qty=qty,
            unit_cost=unit_cost, note=note
        )
        db.add(b)
        db.commit()
        return _back_to_referer(request, f"/factory/{fid}")
    finally:
        db.close()


@router.post("/{fid}/payments")
def add_payment(
    fid: int,
    request: Request,
    date_: str = Form(...),
    amount: float = Form(...),
    method: str | None = Form(None),
    note: str | None = Form(None),
):
    db = SessionLocal()
    try:
        p = models.FactoryPayment(
            factory_id=fid, date=date_, amount=amount,
            method=method, note=note
        )
        db.add(p)
        db.commit()
        return _back_to_referer(request, f"/factory/{fid}")
    finally:
        db.close()


# -------------------- Edit --------------------

@router.post("/{fid}/batches/{bid}/edit")
def edit_batch(
    fid: int,
    bid: int,
    request: Request,
    date_: str = Form(...),
    qty: int = Form(...),
    unit_cost: float = Form(...),
    note: str | None = Form(None),
):
    db = SessionLocal()
    try:
        b = db.query(models.ManufacturingBatch).get(bid)
        if not b or b.factory_id != fid:
            return HTMLResponse("Batch not found", status_code=404)
        b.date = date_
        b.qty = qty
        b.unit_cost = unit_cost
        b.note = note
        db.commit()
        return _back_to_referer(request, f"/factory/{fid}")
    finally:
        db.close()


@router.post("/{fid}/payments/{pid}/edit")
def edit_payment(
    fid: int,
    pid: int,
    request: Request,
    date_: str = Form(...),
    amount: float = Form(...),
    method: str | None = Form(None),
    note: str | None = Form(None),
):
    db = SessionLocal()
    try:
        p = db.query(models.FactoryPayment).get(pid)
        if not p or p.factory_id != fid:
            return HTMLResponse("Payment not found", status_code=404)
        p.date = date_
        p.amount = amount
        p.method = method
        p.note = note
        db.commit()
        return _back_to_referer(request, f"/factory/{fid}")
    finally:
        db.close()


# -------------------- Delete --------------------

@router.post("/{fid}/batches/{bid}/delete")
def delete_batch(fid: int, bid: int, request: Request):
    db = SessionLocal()
    try:
        b = db.query(models.ManufacturingBatch).get(bid)
        if not b or b.factory_id != fid:
            return HTMLResponse("Batch not found", status_code=404)
        db.delete(b)
        db.commit()
        return _back_to_referer(request, f"/factory/{fid}")
    finally:
        db.close()


@router.post("/{fid}/payments/{pid}/delete")
def delete_payment(fid: int, pid: int, request: Request):
    db = SessionLocal()
    try:
        p = db.query(models.FactoryPayment).get(pid)
        if not p or p.factory_id != fid:
            return HTMLResponse("Payment not found", status_code=404)
        db.delete(p)
        db.commit()
        return _back_to_referer(request, f"/factory/{fid}")
    finally:
        db.close()


# -------------------- Add Factory --------------------

@router.post("/new")
def add_factory(
    request: Request,
    name: str = Form(...),
    phone: str | None = Form(None),
    address: str | None = Form(None),
    notes: str | None = Form(None),
):
    db = SessionLocal()
    try:
        f = models.Factory(name=name, phone=phone, address=address, notes=notes)
        db.add(f)
        db.commit()
        return _back_to_referer(request, "/factory")
    finally:
        db.close()


# -------------------- Export CSV (كما هو) --------------------

@router.get("/export.csv")
def export_factories_csv(d1: str | None = None, d2: str | None = None):
    """تصدير ملخص كل المصانع في الفترة كـ CSV. لو التواريخ فاضية → كل السجلات."""
    db = SessionLocal()
    try:
        rd1, rd2 = _range(d1, d2)
        factories = db.query(models.Factory).order_by(models.Factory.name.asc()).all()

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["المصنع", "إجمالي التصنيع", "إجمالي المدفوع", "الرصيد", "من", "إلى"])

        for f in factories:
            qb = db.query(
                func.coalesce(func.sum(models.ManufacturingBatch.qty * models.ManufacturingBatch.unit_cost), 0.0)
            ).filter(models.ManufacturingBatch.factory_id == f.id)
            qb = _apply_date_filters(qb, models.ManufacturingBatch.date, rd1, rd2)
            total_cost = qb.scalar() or 0.0

            qp = db.query(
                func.coalesce(func.sum(models.FactoryPayment.amount), 0.0)
            ).filter(models.FactoryPayment.factory_id == f.id)
            qp = _apply_date_filters(qp, models.FactoryPayment.date, rd1, rd2)
            total_paid = qp.scalar() or 0.0

            balance = float(total_cost) - float(total_paid)
            writer.writerow([f.name, f"{total_cost:.2f}", f"{total_paid:.2f}", f"{balance:.2f}", d1 or "", d2 or ""])

        data = buf.getvalue().encode("utf-8-sig")

        utf8_name = _sanitize_filename(f"factories_{(d1 or 'ALL')}_{(d2 or 'ALL')}.csv")
        ascii_fallback = "factories_export.csv"
        disp = f"attachment; filename={ascii_fallback}; filename*=UTF-8''{quote(utf8_name)}"
        headers = {"Content-Disposition": disp}
        return StreamingResponse(iter([data]), media_type="text/csv; charset=utf-8", headers=headers)
    finally:
        db.close()


@router.get("/{fid}/export.csv")
def export_factory_detail_csv(fid: int, d1: str | None = None, d2: str | None = None):
    """تصدير تفاصيل مصنع واحد (دفعات تصنيع + مدفوعات) إلى CSV. لو التواريخ فاضية → كل السجلات."""
    db = SessionLocal()
    try:
        f = db.query(models.Factory).get(fid)
        if not f:
            return HTMLResponse("Factory not found", status_code=404)

        rd1, rd2 = _range(d1, d2)

        qb = db.query(models.ManufacturingBatch).filter(models.ManufacturingBatch.factory_id == fid)
        qb = _apply_date_filters(qb, models.ManufacturingBatch.date, rd1, rd2)
        batches = qb.order_by(models.ManufacturingBatch.date.asc(), models.ManufacturingBatch.id.asc()).all()

        qp = db.query(models.FactoryPayment).filter(models.FactoryPayment.factory_id == fid)
        qp = _apply_date_filters(qp, models.FactoryPayment.date, rd1, rd2)
        payments = qp.order_by(models.FactoryPayment.date.asc(), models.FactoryPayment.id.asc()).all()

        total_cost = sum((b.qty or 0) * (b.unit_cost or 0) for b in batches)
        total_paid = sum((p.amount or 0) for p in payments)
        balance = float(total_cost) - float(total_paid)

        buf = io.StringIO()
        writer = csv.writer(buf)

        writer.writerow([f"تفاصيل مصنع: {f.name}"])
        writer.writerow([f"الفترة: {(d1 or 'ALL')} إلى {(d2 or 'ALL')}"])
        writer.writerow([])

        writer.writerow(["دفعات التصنيع"])
        writer.writerow(["التاريخ", "العدد", "تكلفة القطعة", "الإجمالي", "ملاحظة"])
        for b in batches:
            row_total = (b.qty or 0) * (b.unit_cost or 0)
            writer.writerow([b.date, b.qty, f"{b.unit_cost:.2f}", f"{row_total:.2f}", (b.note or "")])
        writer.writerow(["الإجمالي", "", "", f"{total_cost:.2f}", ""])
        writer.writerow([])

        writer.writerow(["المدفوعات"])
        writer.writerow(["التاريخ", "المبلغ", "الطريقة", "ملاحظة"])
        for p in payments:
            writer.writerow([p.date, f"{(p.amount or 0):.2f}", (p.method or ""), (p.note or "")])
        writer.writerow(["الإجمالي", f"{total_paid:.2f}", "", ""])
        writer.writerow([])

        writer.writerow(["الرصيد", f"{balance:.2f}"])
        writer.writerow([])

        data = buf.getvalue().encode("utf-8-sig")

        utf8_name = _sanitize_filename(f"factory_{f.id}_{(d1 or 'ALL')}_{(d2 or 'ALL')}.csv")
        ascii_fallback = f"factory_{f.id}.csv"
        disp = f"attachment; filename={ascii_fallback}; filename*=UTF-8''{quote(utf8_name)}"
        headers = {"Content-Disposition": disp}
        return StreamingResponse(iter([data]), media_type="text/csv; charset=utf-8", headers=headers)
    finally:
        db.close()