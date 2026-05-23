from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app import models
from fastapi.templating import Jinja2Templates

router = APIRouter(prefix="/invoices", tags=["Invoices"])
templates = Jinja2Templates(directory="app/templates")

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

# ========== Helpers: status-in-note (لا نعدّل الـ DB Schema) ==========
import re, json
from datetime import datetime
from typing import Optional

_STATUS_RE = re.compile(r"<!--STATUS:({.*})-->$", re.DOTALL)

def _extract_status(note: str | None):
    """يرجع (dict, clean_note). نخزن الحالة كـ JSON داخل تعليق HTML في آخر الملاحظة."""
    note = note or ""
    m = _STATUS_RE.search(note)
    if not m:
        return {}, note
    try:
        data = json.loads(m.group(1))
    except Exception:
        data = {}
    clean = note[:m.start()].rstrip()
    return data, clean

def _save_status_into_note(clean_note: str, data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return (clean_note or "").rstrip() + "\n\n" + f"<!--STATUS:{payload}-->"

def _now_iso():
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"

def _update_status(inv: models.Invoice, updates: dict, db: Session, force: bool=False):
    """
    يدمج التحديثات داخل note. لو force=False مش هنكتب تايم ستامب موجودة بالفعل.
    """
    data, clean = _extract_status(inv.note)
    for k, v in updates.items():
        if v is None:
            continue
        if force or not data.get(k):
            data[k] = v
    inv.note = _save_status_into_note(clean, data)
    db.add(inv); db.commit(); db.refresh(inv)
    return data

def _apply_status_attrs(inv: models.Invoice):
    """يضيف خصائص مشتقة على الكائن للعرض في القوالب، ويعدّ ملاحظة نظيفة للعرض."""
    data, clean = _extract_status(getattr(inv, "note", None))
    setattr(inv, "_prepared_at",  data.get("prepared_at"))
    setattr(inv, "_shipped_at",   data.get("shipped_at"))
    setattr(inv, "_delivered_at", data.get("delivered_at"))
    setattr(inv, "_settled_at",   data.get("settled_at"))
    setattr(inv, "_tracking_no",  data.get("tracking_no"))
    setattr(inv, "_clean_note",   clean)

# شحن / مرتجع وحساب المتبقي (لصفحة unpaid)
from sqlalchemy import func
def _allocations_sum_for_invoice(db: Session, invoice_id: int) -> float:
    s = db.query(func.coalesce(func.sum(models.ShippingAllocation.amount), 0.0))\
          .filter(models.ShippingAllocation.invoice_id == invoice_id)\
          .scalar()
    return float(s or 0.0)

def _linked_returns_totals(db: Session, sale_id: int) -> tuple[float, float]:
    rets = db.query(models.Invoice).filter(
        models.Invoice.type == "R",
        models.Invoice.original_sale_id == sale_id
    ).all()
    total_ret = sum(float(r.total or 0) for r in rets)
    total_ret_ship = sum(float(r.return_shipping_fee or 0) for r in rets)
    return total_ret, total_ret_ship

def _admin_covered_ids(db: Session) -> set:
    try:
        rows = db.query(models.ShippingAdminCover.invoice_id).all()
        return {r[0] for r in rows}
    except Exception:
        return set()

# =================== End helpers ===================

@router.get("/{iid}", response_class=HTMLResponse)
def invoice_detail(iid: int, request: Request, db: Session = Depends(get_db), popup: int = 0):
    inv = db.query(models.Invoice).get(iid)
    if not inv:
        return HTMLResponse("<h3>الفاتورة غير موجودة</h3>", status_code=404)
    items = db.query(models.InvoiceItem).filter(models.InvoiceItem.invoice_id == iid).all()

    # ✅ نضّف الحالة من الملاحظة ونوفّر inv._clean_note لعرضها
    _apply_status_attrs(inv)

    # لما نطلبها كـ popup = 1 نرجّع قالب خفيف للـ Modal
    if popup:
        return templates.TemplateResponse(
            "_invoice_popup.html",
            {"request": request, "inv": inv, "items": items}
        )

    # العرض العادي (صفحة كاملة)
    return templates.TemplateResponse("invoice_detail.html", {"request": request, "inv": inv, "items": items})

@router.get("/{iid}/print-4x6", response_class=HTMLResponse)
def invoice_print_4x6(iid: int, request: Request, db: Session = Depends(get_db)):
    inv = db.query(models.Invoice).get(iid)
    if not inv: return HTMLResponse("Not found", status_code=404)
    # أوتوماتيك "تم التجهيز" أول مرة تتم الطباعة لفاتورة بيع
    data, _ = _extract_status(inv.note)
    if inv.type == "S" and not data.get("prepared_at"):
        _update_status(inv, {"prepared_at": _now_iso()}, db)
    items = db.query(models.InvoiceItem).filter(models.InvoiceItem.invoice_id == iid).all()
    _apply_status_attrs(inv)
    return templates.TemplateResponse("invoice_print_4x6.html", {"request": request, "inv": inv, "items": items})

# ====== /invoices?status=unpaid ======
from fastapi import Query

def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.get("", response_class=HTMLResponse)
def invoices_index(
    request: Request,
    status: Optional[str] = Query(None, description="unpaid لعرض غير المدفوعة"),
    db: Session = Depends(_get_db),
):
    if (status or "").lower() == "unpaid":
        invs_all = db.query(models.Invoice)\
            .filter(models.Invoice.type == "S")\
            .order_by(models.Invoice.id.desc())\
            .all()

        covered = _admin_covered_ids(db)
        unpaid = []
        for inv in invs_all:
            _apply_status_attrs(inv)
            if inv.id in covered:
                setattr(inv, "_status", "مغطّى إداريًا")
                continue

            base_due = float(inv.total or 0) - float(inv.actual_shipping_cost or 0)
            ret_total, ret_ship = _linked_returns_totals(db, inv.id)
            due_after_returns = base_due - ret_total - ret_ship
            paid_on_inv = _allocations_sum_for_invoice(db, inv.id)
            outstanding = round(due_after_returns - paid_on_inv, 2)

            direct_paid = (inv.payment_method in ("InstaPay", "محفظة إلكترونية"))

            if inv._settled_at:
                setattr(inv, "_status", "تم التوريد")
            elif inv._delivered_at:
                setattr(inv, "_status", "تم الاستلام")
            elif inv._shipped_at:
                setattr(inv, "_status", "تم الشحن")
            elif inv._prepared_at:
                setattr(inv, "_status", "تم التجهيز")

            if direct_paid:
                setattr(inv, "_status", "مدفوع مباشرة")
            elif outstanding > 0.009:
                setattr(inv, "_status", getattr(inv, "_status", "غير مدفوع"))
                unpaid.append(inv)
            else:
                setattr(inv, "_status", "مدفوع")

        return templates.TemplateResponse(
            "invoices_today.html",
            {"request": request, "title": "الفواتير غير المدفوعة", "invoices": unpaid}
        )

    # عرض افتراضي (آخر 50)
    default_list = db.query(models.Invoice).order_by(models.Invoice.id.desc()).limit(50).all()
    covered = _admin_covered_ids(db)
    for inv in default_list:
        _apply_status_attrs(inv)
        if inv.id in covered:
            setattr(inv, "_status", "مغطّى إداريًا")
            continue
        base_due = float(inv.total or 0) - float(inv.actual_shipping_cost or 0)
        ret_total, ret_ship = _linked_returns_totals(db, inv.id)
        due_after_returns = base_due - ret_total - ret_ship
        paid_on_inv = _allocations_sum_for_invoice(db, inv.id)
        outstanding = round(due_after_returns - paid_on_inv, 2)
        direct_paid = (inv.payment_method in ("InstaPay", "محفظة إلكترونية"))
        if direct_paid:
            st = "مدفوع مباشرة"
        elif inv._settled_at:
            st = "تم التوريد"
        elif outstanding > 0.009:
            st = "غير مدفوع"
        else:
            st = "مدفوع"
        setattr(inv, "_status", st)

    return templates.TemplateResponse(
        "invoices_today.html",
        {"request": request, "title": "آخر الفواتير", "invoices": default_list}
    )

# ====== Mark status endpoints (أزرار التفاصيل) ======

@router.post("/{iid}/mark-shipped")
def mark_shipped(iid: int, request: Request, db: Session = Depends(get_db), tracking_no: str | None = None):
    inv = db.query(models.Invoice).get(iid)
    if not inv: return JSONResponse({"ok": False, "msg": "Not found"}, status_code=404)
    data, _ = _extract_status(inv.note)
    if not data.get("shipped_at"):
        updates = {"shipped_at": _now_iso()}
        if tracking_no:
            updates["tracking_no"] = tracking_no.strip()
        _update_status(inv, updates, db)
    return JSONResponse({"ok": True})

@router.post("/{iid}/mark-delivered")
def mark_delivered(iid: int, request: Request, db: Session = Depends(get_db)):
    inv = db.query(models.Invoice).get(iid)
    if not inv: return JSONResponse({"ok": False, "msg": "Not found"}, status_code=404)
    data, _ = _extract_status(inv.note)
    if not data.get("delivered_at"):
        _update_status(inv, {"delivered_at": _now_iso()}, db)
    return JSONResponse({"ok": True})

# ====== NEW: Bulk Status (من صفحة القائمة) ======

@router.post("/bulk-status")
def bulk_status_change(
    action: str = Form(...),                     # prepared / shipped / delivered / settled / clear
    ids: list[int] = Form(...),                  # مصفوفة IDs من الشيك بوكس
    tracking_no: Optional[str] = Form(None),     # اختيارية لو action=shipped
    redirect_to: Optional[str] = Form(None),     # يرجّع لنفس الصفحة
    force: Optional[bool] = Form(False),         # لو True هنكتب التايم ستامب حتى لو موجودة
    db: Session = Depends(get_db),
):
    action = (action or "").strip().lower()
    allowed = {"prepared", "shipped", "delivered", "settled", "clear"}
    if action not in allowed or not ids:
        return RedirectResponse(url=redirect_to or "/invoices", status_code=303)

    now = _now_iso()
    for iid in ids:
        inv = db.query(models.Invoice).get(int(iid))
        if not inv or inv.type != "S":
            continue

        if action == "clear":
            # امسح كل حالاتنا فقط واترك باقي الملاحظة كما هي
            data, clean = _extract_status(inv.note)
            for k in ("prepared_at", "shipped_at", "delivered_at", "settled_at", "tracking_no"):
                data.pop(k, None)
            inv.note = _save_status_into_note(clean, data) if data else clean
            db.add(inv); db.commit(); db.refresh(inv)
            continue

        updates = {}
        if action == "prepared":
            updates["prepared_at"] = now
        elif action == "shipped":
            updates["shipped_at"] = now
            if tracking_no:
                updates["tracking_no"] = tracking_no.strip()
        elif action == "delivered":
            updates["delivered_at"] = now
        elif action == "settled":
            updates["settled_at"] = now

        _update_status(inv, updates, db, force=bool(force))

    return RedirectResponse(url=redirect_to or "/invoices", status_code=303)

# ====== Draft & Items JSON (بدون تغيير سوى إزالة status) ======
from fastapi import HTTPException
from pydantic import BaseModel, Field
from contextlib import contextmanager

class DraftInvoiceIn(BaseModel):
    customer_name: str | None = None
    customer_phone: str | None = None
    notes: str | None = None
    type: str = Field(default="S")  # S بيع / R مرتجع

class AddItemIn(BaseModel):
    product_id: int
    qty: float = Field(gt=0)
    unit_price: float = Field(ge=0)

@contextmanager
def _tx(db: Session):
    try:
        yield
        db.commit()
    except Exception:
        db.rollback()
        raise

@router.post("/draft", response_model=dict)
def create_draft_invoice(body: DraftInvoiceIn, db: Session = Depends(get_db)):
    inv = models.Invoice(
        type=body.type or "S",
        customer_name=body.customer_name,
        customer_phone=body.customer_phone,
        note=body.notes,
    )
    db.add(inv); db.flush()
    return {"invoice_id": inv.id}

@router.post("/{iid}/items-json", response_model=dict)
def add_item_json(iid: int, body: AddItemIn, db: Session = Depends(get_db)):
    inv = db.query(models.Invoice).get(iid)
    if not inv: raise HTTPException(404, "الفاتورة غير موجودة")

    prod = db.query(models.Product).get(body.product_id)
    if not prod: raise HTTPException(404, "الصنف غير موجود")

    available = float(getattr(prod, "stock", 0) or 0)
    if available < body.qty:
        raise HTTPException(status_code=409, detail={"code":"OUT_OF_STOCK","available":available,"message":"الكمية غير متوفرة"})

    with _tx(db):
        item = models.InvoiceItem(
            invoice_id=inv.id, product_id=prod.id,
            qty=body.qty, unit_price=body.unit_price,
        )
        if hasattr(item, "line_total"):
            item.line_total = float(body.qty) * float(body.unit_price)

        prod.stock = available - float(body.qty)
        db.add(item)

        if hasattr(inv, "subtotal"):
            items = db.query(models.InvoiceItem).filter(models.InvoiceItem.invoice_id == inv.id).all()
            inv.subtotal = sum(float(getattr(x, "line_total", (x.qty or 0)*(x.unit_price or 0)) or 0) for x in items)
        if hasattr(inv, "total"):
            ship = float(getattr(inv, "shipping_cost", 0) or 0)
            disc = float(getattr(inv, "discount", 0) or 0)
            inv.total = float(getattr(inv, "subtotal", 0) or 0) - disc + ship
        db.flush()

        return {"item_id": item.id, "invoice_id": inv.id, "new_stock": prod.stock,
                "subtotal": float(getattr(inv, "subtotal", 0) or 0),
                "total": float(getattr(inv, "total", 0) or 0)}

@router.get("/{iid}/items-json", response_model=dict)
def list_items_json(iid: int, db: Session = Depends(get_db)):
    inv = db.query(models.Invoice).get(iid)
    if not inv: raise HTTPException(404, "الفاتورة غير موجودة")
    items = db.query(models.InvoiceItem).filter(models.InvoiceItem.invoice_id == iid).all()
    out=[]
    for it in items:
        out.append({
            "id": it.id, "product_id": it.product_id,
            "product_name": getattr(it, "product_name", None),
            "qty": float(it.qty or 0),
            "unit_price": float(it.unit_price or 0),
            "line_total": float(getattr(it, "line_total", (it.qty or 0)*(it.unit_price or 0)) or 0),
        })
    return {"invoice_id": inv.id, "items": out}