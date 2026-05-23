from typing import List, Dict, Optional
from datetime import datetime
from fastapi import APIRouter, Request, Depends, Form, Query
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse, Response
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app import models
from app.utils import get_next_invoice_code
from fastapi.templating import Jinja2Templates

router = APIRouter(prefix="/returns", tags=["Returns"])
templates = Jinja2Templates(directory="app/templates")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ================= Helpers =================

def sale_by_code_or_id(db: Session, sale_code: Optional[str], sale_id: Optional[int]) -> Optional[models.Invoice]:
    q = db.query(models.Invoice).filter(models.Invoice.type == "S")
    if sale_id:
        return q.filter(models.Invoice.id == sale_id).first()
    if sale_code:
        return q.filter(models.Invoice.invoice_code == sale_code).first()
    return None

def returned_qty_for_sale_item(db: Session, sale_id: int, product_id: int) -> int:
    """إجمالي الكمية المرتجعة لهذا الـ product ضمن كل مرتجعات الفاتورة الأصلية."""
    rets = db.query(models.Invoice).filter(
        models.Invoice.type == "R",
        models.Invoice.original_sale_id == sale_id
    ).all()
    total = 0
    for r in rets:
        for it in r.items:
            if it.product_id == product_id:
                total += int(it.qty or 0)
    return total

def build_sale_items_with_remaining(db: Session, sale: models.Invoice) -> List[Dict]:
    rows: List[Dict] = []
    for it in sale.items:
        sold_qty = int(it.qty or 0)
        returned_qty = returned_qty_for_sale_item(db, sale.id, it.product_id)
        remaining = max(0, sold_qty - returned_qty)
        rows.append({
            "product_id": it.product_id,
            "product_name": it.product_name,
            "sold_qty": sold_qty,
            "returned_qty": returned_qty,
            "remaining_qty": remaining,
            "unit_price": float(it.unit_price or 0),
        })
    return rows

# ================= Routes =================

@router.get("", response_class=HTMLResponse)
def returns_home(
    request: Request,
    page: int = Query(1, ge=1, description="رقم الصفحة"),
    page_size: int = Query(5, ge=1, le=100, description="عدد العناصر في الصفحة"),
    db: Session = Depends(get_db)
):
    """قائمة المرتجعات مع تقسيم صفحات."""
    base_q = db.query(models.Invoice).filter(models.Invoice.type == "R")

    # إجمالي السجلات
    total_count = base_q.count()
    pages = (total_count + page_size - 1) // page_size if total_count else 1
    if page > pages:
        page = pages

    # بيانات الصفحة الحالية
    offset = (page - 1) * page_size
    rets = (
        base_q
        .order_by(models.Invoice.id.desc())
        .offset(offset)
        .limit(page_size)
        .all()
    )

    return templates.TemplateResponse("returns_list.html", {
        "request": request,
        "title": "قائمة المرتجعات",
        "invoices": rets,
        # بيانات التقسيم
        "page": page,
        "page_size": page_size,
        "total_count": total_count,
        "pages": pages,
        "has_prev": page > 1,
        "has_next": page < pages,
    })

@router.get("/new", response_class=HTMLResponse)
def returns_new(
    request: Request,
    sale_code: Optional[str] = Query(None, description="كود فاتورة البيع الأصلية"),
    sale_id: Optional[int] = Query(None, description="ID فاتورة البيع الأصلية"),
    msg: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    sale = sale_by_code_or_id(db, sale_code, sale_id)

    ctx = {
        "request": request,
        "sale": sale,
        "sale_items": [],
        "msg": msg or "",
    }

    if sale:
        items = build_sale_items_with_remaining(db, sale)
        if all(r["remaining_qty"] == 0 for r in items):
            ctx["msg"] = "⚠️ تم عمل مرتجع كامل لهذه الفاتورة بالفعل."
        ctx["sale_items"] = items

    return templates.TemplateResponse("return_form.html", ctx)

@router.get("/search", response_class=JSONResponse)
def search_sales(q: str = Query(..., min_length=1), limit: int = 10, db: Session = Depends(get_db)):
    """
    بحث سريع عن فواتير البيع حسب جزء من كود الفاتورة.
    يرجّع آخر النتائج المطابقة بحد أقصى 'limit'.
    """
    q_str = f"%{q}%"
    rows: List[models.Invoice] = (
        db.query(models.Invoice)
        .filter(
            models.Invoice.type == "S",
            models.Invoice.invoice_code.like(q_str)
        )
        .order_by(models.Invoice.id.desc())
        .limit(limit)
        .all()
    )
    data = []
    for r in rows:
        data.append({
            "id": r.id,
            "invoice_code": r.invoice_code,
            "created_at": r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "",
            "customer_name": r.customer_name,
            "total": float(r.total or 0),
            "shipping_company": r.shipping_company or "",
        })
    return JSONResponse(content={"results": data})

@router.post("/full")
def return_full_invoice(
    invoice_id: int = Form(...),
    company: str = Form(...),
    note: str = Form("مرتجع كامل من حسابات الشحن"),
    q_dfrom: str = Form(""),
    q_dto: str = Form(""),
    unpaid_page: int = Form(1),
    paid_page: int = Form(1),
    page_size: int = Form(20),
    db: Session = Depends(get_db),
):
    # فاتورة البيع الأصلية
    sale = db.query(models.Invoice).filter(
        models.Invoice.id == invoice_id,
        models.Invoice.type == "S"
    ).first()

    if not sale:
        return RedirectResponse(
            url=f"/shipping?company={company}&msg=⚠️ فاتورة غير موجودة",
            status_code=303
        )

    # منع تكرار المرتجع الكامل
    already_returned = db.query(models.Invoice).filter(
        models.Invoice.type == "R",
        models.Invoice.original_sale_id == sale.id
    ).first()

    if already_returned:
        return RedirectResponse(
            url=f"/shipping?company={company}&msg=ℹ️ الفاتورة مرتجعة بالفعل",
            status_code=303
        )

    # إنشاء فاتورة مرتجع كامل
    inv_r = models.Invoice(
        type="R",
        invoice_code=get_next_invoice_code(db, "R"),
        customer_name=sale.customer_name,
        customer_phone=sale.customer_phone,
        customer_address=sale.customer_address,
        order_number=sale.order_number,
        payment_method=sale.payment_method,
        shipping_company=sale.shipping_company,
        subtotal=sale.total,
        discount=0,
        total=float(sale.total or 0),
        return_reason="مرتجع كامل",
        return_shipping_fee=0,
        note=note,
        created_at=datetime.now(),
        original_sale_id=sale.id,
        original_sale_code=sale.invoice_code,
        marketer_id=sale.marketer_id
    )
    db.add(inv_r)
    db.commit()
    db.refresh(inv_r)

    # نسخ كل البنود
    for it in sale.items:
        db.add(models.InvoiceItem(
            invoice_id=inv_r.id,
            product_id=it.product_id,
            product_name=it.product_name,
            qty=it.qty,
            unit_price=it.unit_price,
            line_total=it.line_total
        ))

        # رجوع للمخزون
        prod = db.query(models.Product).get(it.product_id)
        if prod:
            prod.stock = int(prod.stock or 0) + int(it.qty or 0)

    db.commit()

    return RedirectResponse(
        url=f"/shipping?company={company}"
            f"&unpaid_page={unpaid_page}"
            f"&paid_page={paid_page}"
            f"&page_size={page_size}"
            f"&msg=✅ تم عمل مرتجع كامل",
        status_code=303
    )

@router.post("/create")
def create_return(
    sale_id: int = Form(...),
    reason: str = Form(""),
    return_shipping_fee: float = Form(0),
    note: str = Form(""),
    # Arrays aligned with sale items
    product_id: List[int] = Form([]),
    return_qty: List[int] = Form([]),
    unit_price: List[float] = Form([]),
    # ===== [ADD ONLY] خصم المرتجع اليدوي =====
    return_discount: float = Form(0),
    # =========================================
    db: Session = Depends(get_db),
):
    sale = db.query(models.Invoice).filter(models.Invoice.id == sale_id, models.Invoice.type == "S").first()
    if not sale:
        return RedirectResponse(url=f"/returns/new?msg=⚠️ فاتورة البيع غير موجودة", status_code=303)

    # المتبقي لكل بند
    sale_items_map = {it.product_id: it for it in sale.items}
    remaining_map: Dict[int, int] = {}
    for it in sale.items:
        sold_qty = int(it.qty or 0)
        ret_qty = returned_qty_for_sale_item(db, sale.id, it.product_id)
        remaining_map[it.product_id] = max(0, sold_qty - ret_qty)

    # تكوين بنود المرتجع
    lines = []
    for pid, rq, up in zip(product_id or [], return_qty or [], unit_price or []):
        if not pid:
            continue
        pid = int(pid)
        qty = int(rq or 0)
        price = float(up or 0)
        if qty <= 0:
            continue
        allowed = int(remaining_map.get(pid, 0))
        if allowed <= 0:
            continue
        if qty > allowed:
            return RedirectResponse(
                url=f"/returns/new?sale_id={sale.id}&msg=⚠️ كمية المرتجع أكبر من المتبقي للصنف",
                status_code=303
            )
        lines.append((pid, qty, price))

    if not lines:
        return RedirectResponse(url=f"/returns/new?sale_id={sale.id}&msg=⚠️ لا توجد بنود مرتجع صحيحة", status_code=303)

    # لو متبقي صفر لجميع البنود → مرتجع كامل بالفعل
    if all(remaining_map.get(it.product_id, 0) == 0 for it in sale.items):
        return RedirectResponse(url=f"/returns/new?sale_id={sale.id}&msg=⚠️ تم عمل مرتجع كامل لهذه الفاتورة بالفعل", status_code=303)

    # ===== [ADD ONLY] حساب الإجماليات مع خصم المرتجع اليدوي =====
    ret_subtotal = sum(q * p for _, q, p in lines)
    ret_disc = float(return_discount or 0.0)
    if ret_disc < 0:
        ret_disc = 0.0
    if ret_disc > ret_subtotal:
        ret_disc = ret_subtotal  # منع ناتج سالب
    ret_total = round(ret_subtotal - ret_disc, 2)
    # ===========================================

    # إنشاء فاتورة مرتجع R
    inv_r = models.Invoice(
        type="R",
        invoice_code=get_next_invoice_code(db, "R"),
        customer_name=sale.customer_name,
        customer_phone=sale.customer_phone,
        customer_address=sale.customer_address,
        order_number=sale.order_number,
        payment_method=sale.payment_method,
        shipping_company=sale.shipping_company,
        shipping_cost=0.0,
        actual_shipping_cost=0.0,

        # ===== [ADD ONLY] تخزين الخصم والإجمالي بعد الخصم =====
        subtotal=ret_subtotal,
        discount=ret_disc,
        total=ret_total,   # قيمة المرتجع موجبة بعد طرح الخصم
        # ======================================================

        return_reason=(reason.strip() or None),
        return_shipping_fee=float(return_shipping_fee or 0),
        note=(note.strip() or None),
        created_at=datetime.now(),
        original_sale_id=sale.id,
        original_sale_code=sale.invoice_code,

        # ===== NEW: اربط المرتجع بنفس المسوّق بتاع فاتورة البيع =====
        marketer_id=sale.marketer_id
        # ============================================
    )
    db.add(inv_r)
    db.commit()
    db.refresh(inv_r)

    # إضافة البنود + تعديل المخزون
    for pid, qty, unit in lines:
        prod = db.query(models.Product).get(pid)
        # اسم البند: نحافظ على نفس صياغة البيع الأصلي إن أمكن
        if pid in sale_items_map:
            name = sale_items_map[pid].product_name
        else:
            name = f"{prod.name}{f' ({getattr(prod, 'size', None)})' if getattr(prod, 'size', None) else ''}{f' {getattr(prod, 'color', None)}' if getattr(prod, 'color', None) else ''}"

        db.add(models.InvoiceItem(
            invoice_id=inv_r.id,
            product_id=pid,
            product_name=name,
            qty=qty,
            unit_price=unit,
            line_total=qty * unit
        ))

        if prod:
            prod.stock = int(prod.stock or 0) + qty  # المرتجع يزيد المخزون

    db.commit()
    return RedirectResponse(url=f"/invoices/{inv_r.id}", status_code=303)

# ===== NEW: تصدير CSV لقائمة المرتجعات (كلها) =====
@router.get("/export")
def returns_export(db: Session = Depends(get_db)):
    rows = (
        db.query(models.Invoice)
        .filter(models.Invoice.type == "R")
        .order_by(models.Invoice.id.desc())
        .all()
    )
    lines = []
    lines.append("كود_المرتجع,كود_البيع_الأصلي,العميل,التاريخ,إجمالي_المرتجع,شركة_الشحن\n")
    for inv in rows:
        lines.append(
            f"{inv.invoice_code or ''},{inv.original_sale_code or ''},"
            f"\"{(inv.customer_name or '').replace('\"','')}\","
            f"{(inv.created_at.strftime('%Y-%m-%d %H:%M') if inv.created_at else '')},"
            f"{(inv.total or 0):.2f},"
            f"{inv.shipping_company or ''}\n"
        )
    data = ("\ufeff" + "".join(lines)).encode("utf-8")
    headers = {
        "Content-Disposition": 'attachment; filename="returns.csv"',
        "Cache-Control": "no-store",
    }
    return Response(content=data, media_type="text/csv; charset=utf-8", headers=headers)