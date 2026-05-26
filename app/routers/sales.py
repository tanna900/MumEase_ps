from typing import List
from datetime import datetime
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse  # ← أضفت JSONResponse
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app import models
from app.utils import get_next_invoice_code
from fastapi.templating import Jinja2Templates
from app.auth import require_permission

router = APIRouter(prefix="/sales", tags=["Sales"])
templates = Jinja2Templates(directory="app/templates")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.get("/new", response_class=HTMLResponse)
def sales_new(request: Request, db: Session = Depends(get_db)):
    require_permission(request, "view_sales")
    products = db.query(models.Product).order_by(models.Product.name.asc()).all()
    products_js = []
    for p in products:
        disp = p.name or ""
        if p.size: disp += f" ({p.size})"
        if p.color: disp += f" {p.color}"
        stock_val = int(p.stock or 0)
        products_js.append({
            "id": p.id,
            "name": disp,
            "name_raw": p.name or "",
            "color": p.color or "",
            "size": p.size or "",
            "price": float(p.price or 0),
            "barcode": p.barcode or "",
            "stock": stock_val,
        })

    marketers = db.query(models.Marketer).order_by(models.Marketer.name.asc()).all()

    # 🆕 قائمة المحافظات (ثابتة)
    governorates = [
        "القاهرة","الجيزة","القليوبية","الإسكندرية","البحيرة","كفر الشيخ","دمياط","بورسعيد","الإسماعيلية",
        "السويس","الدقهلية","الشرقية","الغربية","المنوفية","البحر الأحمر","الفيوم","بني سويف","المنيا",
        "أسيوط","الوادي الجديد","سوهاج","قنا","الأقصر","أسوان","مطروح","شمال سيناء","جنوب سيناء"
    ]

    return templates.TemplateResponse("sale_form.html", {
        "request": request,
        "products_js": products_js,
        "marketers": marketers,
        "governorates": governorates,  # ← NEW
    })

def collect_items(db: Session, product_id: List[int], qty: List[int], unit_price: List[float]):
    items = []
    for pid, q, up in zip(product_id or [], qty or [], unit_price or []):
        if not pid: continue
        product = db.query(models.Product).get(int(pid))
        if not product: continue
        quantity = int(q or 0)
        if quantity <= 0: continue
        unit = float(up or product.price or 0)
        items.append((product, quantity, unit, unit * quantity))
    return items

# ===== NEW: فحص المخزون قبل إنشاء الفاتورة (منع السالب) =====
def _validate_no_negative_stock(items):
    """
    يرجّع None لو كل الكميات متاحة.
    ولو في صنف كميته المطلوبة أكبر من المتاح، يرجّع Tuple(product, available, requested).
    """
    for product, quantity, unit, line_total in items:
        available = int(product.stock or 0)
        if quantity > available:
            return (product, available, quantity)
    return None
# ============================================================

# ===== NEW: إيداع تلقائي في الخزن الفرعية عبر FinanceEntry =====
def _deposit_sub_wallet(db: Session, payment_method: str, amount: float, inv_code: str, inv_dt: datetime):
    """
    لو طريقة الدفع InstaPay أو محفظة إلكترونية:
      نسجل قيد IN في FinanceEntry ببند:
        - SubCash:InstaPay
        - SubCash:EWallet
    التاريخ = تاريخ الفاتورة (YYYY-MM-DD)
    """
    if amount is None: return
    amt = float(amount or 0.0)
    if amt <= 0: return

    method = (payment_method or "").strip()
    if method == "InstaPay":
        cat = "SubCash:InstaPay"
    elif method == "محفظة إلكترونية":
        cat = "SubCash:EWallet"
    else:
        return  # غير ذلك: لا شيء

    db.add(models.FinanceEntry(
        type="IN",
        category=cat,
        amount=amt,
        date=(inv_dt.strftime("%Y-%m-%d") if inv_dt else datetime.now().strftime("%Y-%m-%d")),
        note=f"تحصيل فاتورة {inv_code}"
    ))
    db.commit()
# =======================================

def _create_invoice(db: Session, payload: dict, items):
    subtotal = sum(line for *_, line in items)
    discount = float(payload["discount"] or 0)
    shipping_cost = float(payload["shipping_cost"] or 0)
    total = subtotal - discount + shipping_cost
    actual_shipping_cost = float(payload["actual_shipping_cost"] or payload["shipping_cost"] or 0)

    inv = models.Invoice(
        type="S",
        invoice_code=get_next_invoice_code(db, "S"),
        customer_name=payload["customer_name"].strip(),
        customer_phone=payload["customer_phone"].strip(),
        customer_address=(payload["customer_address"] or "").strip() or None,
        order_number=(payload["order_number"] or "").strip() or None,
        payment_method=payload["payment_method"],
        shipping_company=(payload["shipping_company"] or "").strip() or None,
        shipping_cost=shipping_cost,
        actual_shipping_cost=actual_shipping_cost,
        discount=discount,
        subtotal=subtotal,
        total=total,
        note=(payload["note"] or "").strip() or None,
        created_at=datetime.now(),
        marketer_id=int(payload["marketer_id"]),
        # 🆕 حفظ المحافظة
        governorate=(payload.get("governorate") or "").strip() or None,
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)

    for product, quantity, unit, line_total in items:
        db.add(models.InvoiceItem(
            invoice_id=inv.id,
            product_id=product.id,
            product_name=f"{product.name}{f' ({product.size})' if product.size else ''}{f' {product.color}' if product.color else ''}",
            qty=quantity, unit_price=unit, line_total=line_total
        ))
        product.stock = int(product.stock or 0) - quantity

    db.commit()

    # NEW: إيداع تلقائي في خزنة InstaPay/محفظة
    try:
        _deposit_sub_wallet(db, inv.payment_method, inv.total or 0.0, inv.invoice_code or f"INV-{inv.id}", inv.created_at)
    except Exception:
        pass

    return inv

@router.post("/create")
def sales_create(
    request: Request,
    customer_name: str = Form(...),
    customer_phone: str = Form(...),
    customer_address: str = Form(""),
    order_number: str = Form(""),
    shipping_company: str = Form(""),
    payment_method: str = Form("عند الاستلام"),
    shipping_cost: float = Form(0),
    actual_shipping_cost: float = Form(0),
    discount: float = Form(0),
    note: str = Form(""),
    marketer_id: int = Form(...),
    product_id: List[int] = Form([]),
    qty: List[int] = Form([]),
    unit_price: List[float] = Form([]),
    # 🆕 استلام المحافظة من النموذج
    governorate: str = Form(""),
    db: Session = Depends(get_db)
):
    require_permission(request, "create_invoice")

    items = collect_items(db, product_id, qty, unit_price)
    if not items:
        return RedirectResponse(url="/sales/new?msg=⚠️ أضِف بندًا واحدًا واختر الصنف", status_code=303)

    if not (shipping_company or "").strip():
        return RedirectResponse(url="/sales/new?msg=⚠️ لازم تختار شركة الشحن", status_code=303)

    mk = db.query(models.Marketer).get(int(marketer_id))
    if not mk:
        return RedirectResponse(url="/sales/new?msg=⚠️ اختر مسوّقًا صحيحًا", status_code=303)

    # NEW: منع البيع لو الكمية أكبر من المتاح
    stock_err = _validate_no_negative_stock(items)
    if stock_err:
        prod, available, requested = stock_err
        msg = f"⚠️ المخزون غير كافٍ للصنف: {prod.name} — المتاح {available} القطعة، المطلوب {requested}."
        return RedirectResponse(url=f"/sales/new?msg={msg}", status_code=303)

    inv = _create_invoice(db, locals(), items)
    return RedirectResponse(url=f"/invoices/{inv.id}", status_code=303)

@router.post("/create-print")
def sales_create_and_print(
    request: Request,
    customer_name: str = Form(...),
    customer_phone: str = Form(...),
    customer_address: str = Form(""),
    order_number: str = Form(""),
    shipping_company: str = Form(""),
    payment_method: str = Form("عند الاستلام"),
    shipping_cost: float = Form(0),
    actual_shipping_cost: float = Form(0),
    discount: float = Form(0),
    note: str = Form(""),
    marketer_id: int = Form(...),
    product_id: List[int] = Form([]),
    qty: List[int] = Form([]),
    unit_price: List[float] = Form([]),
    # 🆕 استلام المحافظة من النموذج
    governorate: str = Form(""),
    db: Session = Depends(get_db)
):
    require_permission(request, "print_invoice")
    items = collect_items(db, product_id, qty, unit_price)
    if not items:
        return RedirectResponse(url="/sales/new?msg=⚠️ أضِف بندًا واحدًا واختر الصنف", status_code=303)

    if not (shipping_company or "").strip():
        return RedirectResponse(url="/sales/new?msg=⚠️ لازم تختار شركة الشحن", status_code=303)

    mk = db.query(models.Marketer).get(int(marketer_id))
    if not mk:
        return RedirectResponse(url="/sales/new?msg=⚠️ اختر مسوّقًا صحيحًا", status_code=303)

    # NEW: منع البيع لو الكمية أكبر من المتاح (نفس الفحص قبل الطباعة)
    stock_err = _validate_no_negative_stock(items)
    if stock_err:
        prod, available, requested = stock_err
        msg = f"⚠️ المخزون غير كافٍ للصنف: {prod.name} — المتاح {available} القطعة، المطلوب {requested}."
        return RedirectResponse(url=f"/sales/new?msg={msg}", status_code=303)

    inv = _create_invoice(db, locals(), items)
    return RedirectResponse(url=f"/invoices/{inv.id}/print-4x6", status_code=303)

# ================================
# 🆕 API: جلب بيانات عميل بالموبايل
# ================================
@router.get("/api/customer-by-phone")
def customer_by_phone(request: Request, phone: str, db: Session = Depends(get_db)):
    require_permission(request, "view_sales")
    """
    يرجّع أحدث بيانات عميل (الاسم/العنوان/المحافظة) بناءً على رقم الموبايل من جدول الفواتير.
    """
    p = (phone or "").strip()
    if not p:
        return JSONResponse({"found": False})

    inv = (
        db.query(models.Invoice)
          .filter(models.Invoice.customer_phone == p)
          .order_by(models.Invoice.created_at.desc(), models.Invoice.id.desc())
          .first()
    )
    if not inv:
        return JSONResponse({"found": False})

    return JSONResponse({
        "found": True,
        "name": inv.customer_name or "",
        "address": inv.customer_address or "",
        "phone": inv.customer_phone or "",
        "governorate": inv.governorate or ""
    })