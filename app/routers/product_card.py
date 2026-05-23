# app/routers/product_card.py
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, List, Dict

from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session
from sqlalchemy import func, and_, or_

from app.database import SessionLocal
from app import models
from fastapi.templating import Jinja2Templates

router = APIRouter(prefix="/products", tags=["Products"])
templates = Jinja2Templates(directory="app/templates")


# ---------- DB ----------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------- helpers ----------
def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except:  # noqa: E722
        return None


def _inclusive_range(col, dfrom: Optional[str], dto: Optional[str]):
    q = []
    df = _parse_dt(dfrom)
    dt = _parse_dt(dto)
    if df:
        q.append(col >= df)
    if dt:
        q.append(col <= (dt + timedelta(days=1) - timedelta(seconds=1)))
    return and_(*q) if q else True


@dataclass
class Move:
    dt: datetime
    kind: str           # 'إضافة' / 'بيع' / 'مرتجع' / 'تسوية'
    ref: str            # كود الفاتورة / رقم التشغيلة
    qty: int            # موجبة/سالبة
    note: str = ""
    unit_price: float = 0.0
    extra: Dict = None


def _extract_cost(note: str) -> float:
    """
    يطلع cost=123.45 من Note لو موجودة (لحشو عمود سعر وحدة في الكارت).
    """
    if not note:
        return 0.0
    try:
        import re
        m = re.search(r"cost\s*=\s*([0-9]+(\.[0-9]+)?)", note)
        if m:
            return float(m.group(1))
    except Exception:
        return 0.0
    return 0.0


# ---------- core ----------
def _movements_for_product(
    db: Session,
    product_id: int,
    dfrom: Optional[str],
    dto: Optional[str],
) -> List[Move]:
    """يبني كل الحركات (بيع - مرتجع - إضافة/تسوية تصنيع - مشتريات) داخل الفترة المطلوبة."""
    pid = int(product_id)
    rng_inv = _inclusive_range(models.Invoice.created_at, dfrom, dto)
    rng_batch = _inclusive_range(models.ManufacturingBatch.created_at, dfrom, dto)

    # بيع (S): سالب
    sales_rows = (
        db.query(models.InvoiceItem, models.Invoice)
          .join(models.Invoice, models.Invoice.id == models.InvoiceItem.invoice_id)
          .filter(models.InvoiceItem.product_id == pid, models.Invoice.type == "S")
          .filter(rng_inv)
          .all()
    )

    # مرتجع (R): موجب
    ret_rows = (
        db.query(models.InvoiceItem, models.Invoice)
          .join(models.Invoice, models.Invoice.id == models.InvoiceItem.invoice_id)
          .filter(models.InvoiceItem.product_id == pid, models.Invoice.type == "R")
          .filter(rng_inv)
          .all()
    )

    # إضافة/تسوية تصنيع (manual adjustments): ManufacturingBatch مربوط بمنتج
    batch_rows = (
        db.query(models.ManufacturingBatch)
          .filter(models.ManufacturingBatch.product_id == pid)
          .filter(rng_batch)
          .all()
    )

    # ✅ مشتريات/تعديلات/حذف من ProductMovement
    pm_rows = []
    if hasattr(models, "ProductMovement"):
        rng_pm = _inclusive_range(models.ProductMovement.created_at, dfrom, dto)
        pm_rows = (
            db.query(models.ProductMovement)
              .filter(models.ProductMovement.product_id == pid)
              .filter(rng_pm)
              .all()
        )

    moves: List[Move] = []

    for it, inv in sales_rows:
        moves.append(Move(
            dt=inv.created_at or datetime.now(),
            kind="بيع",
            ref=inv.invoice_code or f"S#{inv.id}",
            qty=-(int(it.qty or 0)),
            note=(inv.customer_name or ""),
            unit_price=float(it.unit_price or 0),
            extra={"invoice_id": inv.id}
        ))

    for it, inv in ret_rows:
        moves.append(Move(
            dt=inv.created_at or datetime.now(),
            kind="مرتجع",
            ref=inv.invoice_code or f"R#{inv.id}",
            qty=int(it.qty or 0),
            note=(inv.return_reason or inv.customer_name or ""),
            unit_price=float(it.unit_price or 0),
            extra={"invoice_id": inv.id}
        ))

    for b in batch_rows:
        note_txt = (b.note or "").strip()
        kind_lbl = "تسوية" if note_txt.startswith("[ADJ]") else "إضافة"
        moves.append(Move(
            dt=b.created_at or _parse_dt(b.date) or datetime.now(),
            kind=kind_lbl,
            ref=f"BATCH#{b.id}",
            qty=int(b.qty or 0),
            note=note_txt,
            unit_price=float(getattr(b, "unit_cost", 0) or 0),
            extra={"batch_id": b.id}
        ))

    # ✅ مشتريات من ProductMovement
    for m in pm_rows:
        note_txt = (m.note or "").strip()
        q = int(m.qty or 0)
        kind_lbl = "إضافة" if q > 0 else "تسوية"
        # خليه يظهر "سعر وحدة" = cost المستخرجة من note
        unit_cost = _extract_cost(note_txt)

        # ref لطيف: لو الحركة مرتبطة بفاتورة مشتريات
        ref_txt = "مشتريات"
        if (m.ref_type or "") == "PurchaseInvoice" and m.ref_id:
            ref_txt = f"PO#{m.ref_id}"

        moves.append(Move(
            dt=m.created_at or datetime.now(),
            kind=kind_lbl,
            ref=ref_txt,
            qty=q,
            note=note_txt,
            unit_price=float(unit_cost or 0),
            extra={"purchase_id": m.ref_id} if (m.ref_type or "") == "PurchaseInvoice" else {}
        ))

    moves.sort(key=lambda x: (x.dt or datetime.min, x.kind))
    return moves


def _opening_balance(db: Session, product_id: int, dfrom: Optional[str]) -> int:
    """رصيد أول المدة = مجموع كل الحركات قبل بداية الفترة."""
    if not dfrom:
        return 0
    pid = int(product_id)
    df = _parse_dt(dfrom)
    if not df:
        return 0

    # بيع قبل المدة (سالب)
    sales_before = (
        db.query(func.coalesce(func.sum(models.InvoiceItem.qty), 0))
          .join(models.Invoice, models.Invoice.id == models.InvoiceItem.invoice_id)
          .filter(models.InvoiceItem.product_id == pid, models.Invoice.type == "S")
          .filter(models.Invoice.created_at < df)
          .scalar() or 0
    )

    # مرتجع قبل المدة (موجب)
    ret_before = (
        db.query(func.coalesce(func.sum(models.InvoiceItem.qty), 0))
          .join(models.Invoice, models.Invoice.id == models.InvoiceItem.invoice_id)
          .filter(models.InvoiceItem.product_id == pid, models.Invoice.type == "R")
          .filter(models.Invoice.created_at < df)
          .scalar() or 0
    )

    # تصنيع قبل المدة (موجب/سالب حسب qty) — فقط المرتبط بمنتج
    batch_before = (
        db.query(func.coalesce(func.sum(models.ManufacturingBatch.qty), 0))
          .filter(models.ManufacturingBatch.product_id == pid)
          .filter(models.ManufacturingBatch.created_at < df)
          .scalar() or 0
    )

    # ✅ مشتريات قبل المدة من ProductMovement
    pm_before = 0
    if hasattr(models, "ProductMovement"):
        pm_before = (
            db.query(func.coalesce(func.sum(models.ProductMovement.qty), 0))
              .filter(models.ProductMovement.product_id == pid)
              .filter(models.ProductMovement.created_at < df)
              .scalar() or 0
        )

    opening = int(batch_before) + int(pm_before) + int(ret_before) - int(sales_before)
    return opening


# ---------- Pages ----------
@router.get("/{product_id}/card", response_class=HTMLResponse)
def product_card_page(
    product_id: int,
    request: Request,
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    prod = db.query(models.Product).get(int(product_id))
    if not prod:
        return HTMLResponse("<h3>الصنف غير موجود</h3>", status_code=404)

    opening = _opening_balance(db, product_id, date_from)
    moves = _movements_for_product(db, product_id, date_from, date_to)

    running = opening
    table_rows = []
    total_in = 0
    total_out = 0

    for m in moves:
        running += m.qty
        if m.qty > 0:
            total_in += m.qty
        else:
            total_out += (-m.qty)

        table_rows.append({
            "dt": m.dt.strftime("%Y-%m-%d %H:%M") if m.dt else "",
            "kind": m.kind,
            "ref": m.ref,
            "qty": m.qty,
            "unit_price": m.unit_price,
            "note": m.note or "",
            "running": running,
            "extra": m.extra or {},
        })

    ctx = {
        "request": request,
        "product": prod,
        "date_from": date_from or "",
        "date_to": date_to or "",
        "opening": opening,
        "total_in": total_in,
        "total_out": total_out,
        "closing": running,
        "rows": table_rows,
    }
    return templates.TemplateResponse("product_card.html", ctx)


@router.get("/{product_id}/card/export")
def product_card_export(
    product_id: int,
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    prod = db.query(models.Product).get(int(product_id))
    if not prod:
        return HTMLResponse("Not found", status_code=404)

    opening = _opening_balance(db, product_id, date_from)
    moves = _movements_for_product(db, product_id, date_from, date_to)

    running = opening
    lines = []
    lines.append("التاريخ,النوع,مرجع,كمية,سعر_وحدة,ملاحظة,رصيد_جارٍ\n")
    for m in moves:
        running += m.qty
        lines.append(
            f"{(m.dt.strftime('%Y-%m-%d %H:%M') if m.dt else '')},{m.kind},{m.ref},{m.qty},{(m.unit_price or 0):.2f},\"{(m.note or '').replace('\"','')}\",{running}\n"
        )

    csv_data = ("\ufeff" + "".join(lines)).encode("utf-8")
    fname = f"product_card_{product_id}.csv"
    headers = {
        "Content-Disposition": f'attachment; filename="{fname}"',
        "Cache-Control": "no-store",
    }
    return Response(content=csv_data, media_type="text/csv; charset=utf-8", headers=headers)


# ===== Search page & helpers =====
@router.get("/card/search", response_class=HTMLResponse)
def product_card_search(
    request: Request,
    q: str = Query("", description="ابحث بجزء من الاسم أو الباركود"),
    db: Session = Depends(get_db),
):
    results = []
    q_str = (q or "").strip()
    if q_str:
        results = (
            db.query(models.Product)
              .filter(
                  or_(
                      models.Product.name.ilike(f"%{q_str}%"),
                      models.Product.barcode.ilike(f"%{q_str}%")
                  )
              )
              .order_by(models.Product.name.asc(), models.Product.id.asc())
              .limit(100)
              .all()
        )
        if len(results) == 1:
            return Response(
                status_code=307,
                headers={"Location": f"/products/{results[0].id}/card"}
            )

    ctx = {
        "request": request,
        "q": q_str,
        "results": results,
    }
    return templates.TemplateResponse("product_card_search.html", ctx)


@router.get("/card/by-barcode")
def product_card_by_barcode(
    barcode: str = Query(...),
    db: Session = Depends(get_db),
):
    p = (
        db.query(models.Product)
          .filter(models.Product.barcode == (barcode or "").strip())
          .first()
    )
    if not p:
        return Response(
            status_code=307,
            headers={"Location": f"/products/card/search?q={barcode or ''}"}
        )
    return Response(
        status_code=307,
        headers={"Location": f"/products/{p.id}/card"}
    )