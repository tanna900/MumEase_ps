# app/routers/products.py
from fastapi import APIRouter, Request, Depends, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import or_, asc, desc, func, and_
from app.database import SessionLocal
from app import models
from fastapi.templating import Jinja2Templates
import csv, io
from datetime import datetime, timedelta
from urllib.parse import urlencode

router = APIRouter(prefix="/products", tags=["Products"])
templates = Jinja2Templates(directory="app/templates")

LOW_STOCK_THRESHOLD = 4

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ===== Helpers =====
_SORT_MAP = {
    "name": models.Product.name,
    "barcode": models.Product.barcode,
    "color": models.Product.color,
    "size": models.Product.size,
    "cost": models.Product.cost_price,   # ✅ فرز بالتكلفة
    "price": models.Product.price,
    "stock": models.Product.stock,
}

def _parse_date(s: str):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except:
        return None

def _range_inclusive_datetime(col, dfrom: str, dto: str):
    df = _parse_date(dfrom)
    dt = _parse_date(dto)
    clauses = []
    if df:
        clauses.append(col >= df)
    if dt:
        clauses.append(col <= (dt + timedelta(days=1) - timedelta(seconds=1)))
    return and_(*clauses) if clauses else True

def _apply_filters_and_sort(
    db: Session,
    q: str,
    color: str,
    size: str,
    low_only: int,
    in_stock: int,
    sort: str,
    direction: str
):
    query = db.query(models.Product)

    q = (q or "").strip()
    if q:
        query = query.filter(
            or_(
                models.Product.name.contains(q),
                models.Product.barcode.contains(q)
            )
        )

    color = (color or "").strip()
    if color:
        query = query.filter(models.Product.color.contains(color))

    size = (size or "").strip()
    if size:
        query = query.filter(models.Product.size.contains(size))

    if int(low_only or 0) == 1:
        query = query.filter(func.coalesce(models.Product.stock, 0) < LOW_STOCK_THRESHOLD)

    if int(in_stock or 0) == 1:
        query = query.filter(func.coalesce(models.Product.stock, 0) > 0)

    sort_key = (sort or "name").lower()
    col = _SORT_MAP.get(sort_key, models.Product.name)
    dir_key = (direction or "asc").lower()
    order_clause = asc(col) if dir_key != "desc" else desc(col)

    return query.order_by(order_clause, asc(models.Product.id))

def _back_url(
    msg="",
    q="",
    color="",
    size="",
    low_only=0,
    in_stock=0,
    sort="name",
    dir="asc",
    page=1,
    page_size=15
):
    params = [
        ("q", q or ""),
        ("color", color or ""),
        ("size", size or ""),
        ("low_only", str(int(low_only or 0))),
        ("in_stock", str(int(in_stock or 0))),
        ("sort", sort or "name"),
        ("dir", dir or "asc"),
        ("page", str(page or 1)),
        ("page_size", str(page_size or 15)),
    ]
    if msg:
        params.insert(0, ("msg", msg))
    return "/products?" + urlencode(params)

def _build_page_items(page: int, total_pages: int, window: int = 2):
    if total_pages <= 1:
        return [1]
    page = max(1, min(page, total_pages))
    pages = set([1, total_pages, page])
    for p in range(page - window, page + window + 1):
        if 1 <= p <= total_pages:
            pages.add(p)
    pages = sorted(pages)
    out = []
    prev = None
    for p in pages:
        if prev is not None and p - prev > 1:
            out.append(None)
        out.append(p)
        prev = p
    return out

def _distinct_options(db: Session):
    colors = [
        r[0] for r in db.query(models.Product.color)
        .filter(models.Product.color.isnot(None))
        .distinct()
        .order_by(models.Product.color.asc())
        .all()
        if (r[0] or "").strip()
    ]
    sizes = [
        r[0] for r in db.query(models.Product.size)
        .filter(models.Product.size.isnot(None))
        .distinct()
        .order_by(models.Product.size.asc())
        .all()
        if (r[0] or "").strip()
    ]
    return colors, sizes


@router.get("", response_class=HTMLResponse)
def list_products(
    request: Request,
    q: str = "",
    color: str = "",
    size: str = "",
    low_only: int = Query(0),
    in_stock: int = Query(0),
    msg: str = "",
    sort: str = "name",
    dir: str = "asc",
    export: str = "",
    page: int = Query(1),
    page_size: int = Query(15),
    db: Session = Depends(get_db)
):
    page = max(1, int(page or 1))
    page_size = max(1, int(page_size or 15))

    base_query = _apply_filters_and_sort(db, q, color, size, low_only, in_stock, sort, dir)

    all_qty = float(db.query(func.coalesce(func.sum(models.Product.stock), 0)).scalar() or 0)

    if hasattr(models.Product, "cost_price"):
        all_cost = float(
            db.query(func.coalesce(func.sum(models.Product.stock * models.Product.cost_price), 0)).scalar() or 0
        )
    else:
        all_cost = 0.0

    all_price = float(
        db.query(func.coalesce(func.sum(models.Product.stock * models.Product.price), 0)).scalar() or 0
    )

    low_stock_global_count = int(
        db.query(models.Product)
          .filter(func.coalesce(models.Product.stock, 0) < LOW_STOCK_THRESHOLD)
          .count()
    )

    low_stock_filtered_count = int(
        base_query.with_entities(models.Product.id)
                  .filter(func.coalesce(models.Product.stock, 0) < LOW_STOCK_THRESHOLD)
                  .count()
    )

    has_search = True if (q or "").strip() else False
    if has_search:
        products_all = base_query.all()
        total_pages = 1
        page = 1
        products = products_all
    else:
        total_count = base_query.count()
        total_pages = max(1, (total_count + page_size - 1) // page_size)
        page = min(page, total_pages)
        products = (
            base_query
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        products_all = None

    low_stock_page = [p for p in products if (p.stock or 0) < LOW_STOCK_THRESHOLD]

    page_qty = float(sum(float(p.stock or 0) for p in products))
    page_cost = float(sum(float(p.stock or 0) * float(getattr(p, "cost_price", 0) or 0) for p in products))
    page_price = float(sum(float(p.stock or 0) * float(p.price or 0) for p in products))

    # ===== تصدير =====
    exp = (export or "").lower().strip()
    if exp in ("csv", "excel"):
        export_rows = products_all if has_search else base_query.all()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([
            "Name", "Barcode", "Color", "Size",
            "Cost Price", "Price", "Stock",
            "Margin/Unit", "Margin%", "Stock Profit"
        ])

        for p in export_rows:
            stock = float(p.stock or 0)
            cost_price = float(getattr(p, "cost_price", 0) or 0)
            price = float(p.price or 0)
            margin_u = price - cost_price
            margin_pct = (margin_u / price * 100.0) if price > 0 else 0.0
            stock_profit = stock * margin_u

            w.writerow([
                p.name or "",
                p.barcode or "",
                p.color or "",
                p.size or "",
                round(cost_price, 2),
                round(price, 2),
                int(stock),
                round(margin_u, 2),
                round(margin_pct, 2),
                round(stock_profit, 2),
            ])

        buf.seek(0)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")

        if exp == "csv":
            return StreamingResponse(
                iter([buf.getvalue().encode("utf-8-sig")]),
                media_type="text/csv",
                headers={"Content-Disposition": f"attachment; filename=products-{ts}.csv"}
            )
        else:
            return StreamingResponse(
                iter([buf.getvalue().encode("utf-8-sig")]),
                media_type="application/vnd.ms-excel",
                headers={"Content-Disposition": f"attachment; filename=products-{ts}.xls"}
            )

    # ✅ query strings للروابط (pagination / export / sorting arrows)
    base_params_full = {
        "q": q or "",
        "color": color or "",
        "size": size or "",
        "low_only": str(int(low_only or 0)),
        "in_stock": str(int(in_stock or 0)),
        "sort": sort or "name",
        "dir": dir or "asc",
        "page_size": str(page_size or 15),
    }
    base_qs = urlencode(base_params_full)

    base_params_filters = {
        "q": q or "",
        "color": color or "",
        "size": size or "",
        "low_only": str(int(low_only or 0)),
        "in_stock": str(int(in_stock or 0)),
        "page_size": str(page_size or 15),
    }
    base_qs_filters = urlencode(base_params_filters)

    page_items = _build_page_items(page, total_pages, window=2)
    color_options, size_options = _distinct_options(db)

    return templates.TemplateResponse("product_list.html", {
        "request": request,
        "products": products,

        "low_stock_page": low_stock_page,
        "low_stock_global_count": low_stock_global_count,
        "low_stock_filtered_count": low_stock_filtered_count,

        "q": q,
        "color": color,
        "size": size,
        "low_only": int(low_only or 0),
        "in_stock": int(in_stock or 0),

        "msg": msg,
        "sort": sort,
        "dir": dir,

        "page": page,
        "total_pages": total_pages,
        "page_size": page_size,
        "has_search": has_search,
        "page_items": page_items,

        "base_qs": base_qs,
        "base_qs_filters": base_qs_filters,

        "page_qty": page_qty,
        "page_cost": page_cost,
        "page_price": page_price,
        "all_qty": all_qty,
        "all_cost": all_cost,
        "all_price": all_price,

        "color_options": color_options,
        "size_options": size_options,

        "LOW_STOCK_THRESHOLD": LOW_STOCK_THRESHOLD,
    })


# ============================================================
# ✅ إضافة صنف يدوي (باركود اختياري)
# ============================================================
@router.post("/add")
def add_product(
    name: str = Form(...),
    color: str = Form(""),
    size: str = Form(""),
    barcode: str = Form(""),          # اختياري
    cost_price: float = Form(0),
    price: float = Form(0),
    stock: int = Form(0),

    # حفظ الفلاتر بعد الإضافة
    q: str = Form(""),
    color_filter: str = Form(""),
    size_filter: str = Form(""),
    low_only: int = Form(0),
    in_stock: int = Form(0),
    sort: str = Form("name"),
    dir: str = Form("asc"),
    page: int = Form(1),
    page_size: int = Form(15),

    db: Session = Depends(get_db),
):
    nm = (name or "").strip()
    if not nm:
        return RedirectResponse(
            url=_back_url("⚠️ اكتب اسم الصنف", q, color_filter, size_filter, low_only, in_stock, sort, dir, page, page_size),
            status_code=303
        )

    bc = (barcode or "").strip()
    bc_val = bc if bc else None  # لو فاضي نخليه NULL عشان unique

    if bc_val:
        exists = db.query(models.Product).filter(models.Product.barcode == bc_val).first()
        if exists:
            return RedirectResponse(
                url=_back_url("⚠️ الباركود موجود بالفعل", q, color_filter, size_filter, low_only, in_stock, sort, dir, page, page_size),
                status_code=303
            )

    p = models.Product(
        name=nm,
        color=(color or "").strip() or None,
        size=(size or "").strip() or None,
        barcode=bc_val,
        cost_price=float(cost_price or 0),
        price=float(price or 0),
        stock=max(0, int(stock or 0)),
    )
    db.add(p)
    db.commit()

    return RedirectResponse(
        url=_back_url("✅ تم إضافة الصنف", q, color_filter, size_filter, low_only, in_stock, sort, dir, page, page_size),
        status_code=303
    )


# ============================================================
# ✅ حذف صنف واحد (علشان زر /products/delete يشتغل)
# ============================================================
@router.post("/delete")
def delete_product(
    pid: int = Form(...),

    q: str = Form(""),
    color_filter: str = Form(""),
    size_filter: str = Form(""),
    low_only: int = Form(0),
    in_stock: int = Form(0),
    sort: str = Form("name"),
    dir: str = Form("asc"),
    page: int = Form(1),
    page_size: int = Form(15),

    db: Session = Depends(get_db),
):
    p = db.query(models.Product).get(int(pid))
    if not p:
        return RedirectResponse(
            url=_back_url("⚠️ المنتج غير موجود", q, color_filter, size_filter, low_only, in_stock, sort, dir, page, page_size),
            status_code=303
        )

    try:
        db.delete(p)
        db.commit()
        msg = "🗑️ تم حذف الصنف"
    except Exception:
        db.rollback()
        msg = "⚠️ فشل الحذف (ممكن الصنف مرتبط بفواتير/حركات)"

    return RedirectResponse(
        url=_back_url(msg, q, color_filter, size_filter, low_only, in_stock, sort, dir, page, page_size),
        status_code=303
    )


# ============================================================
# Bulk / Update / Stock Delta
# ============================================================
@router.post("/bulk")
def bulk_actions(
    action: str = Form(...),
    ids: str = Form(...),

    new_cost_price: str = Form(""),
    new_price: str = Form(""),

    new_stock: str = Form(""),
    stock_delta: str = Form(""),

    q: str = Form(""),
    color_filter: str = Form(""),
    size_filter: str = Form(""),
    low_only: int = Form(0),
    in_stock: int = Form(0),
    sort: str = Form("name"),
    dir: str = Form("asc"),
    page: int = Form(1),
    page_size: int = Form(15),
    db: Session = Depends(get_db),
):
    try:
        id_list = [int(x) for x in ids.split(",") if x.strip().isdigit()]
    except:
        return RedirectResponse(
            url=_back_url("⚠️ IDs غير صالحة", q, color_filter, size_filter, low_only, in_stock, sort, dir, page, page_size),
            status_code=303
        )

    if not id_list:
        return RedirectResponse(
            url=_back_url("⚠️ لم يتم اختيار أي صنف", q, color_filter, size_filter, low_only, in_stock, sort, dir, page, page_size),
            status_code=303
        )

    def _parse_float(val: str):
        if val is None:
            return None
        s = str(val).strip()
        if s == "":
            return None
        try:
            return float(s)
        except:
            return None

    def _parse_int(val: str):
        if val is None:
            return None
        s = str(val).strip()
        if s == "":
            return None
        try:
            return int(float(s))
        except:
            return None

    parsed_cost = _parse_float(new_cost_price)
    parsed_price = _parse_float(new_price)
    parsed_stock_set = _parse_int(new_stock)
    parsed_stock_delta = _parse_int(stock_delta)

    if action == "delete":
        deleted = 0
        failed = 0
        for pid in id_list:
            p = db.query(models.Product).get(pid)
            if not p:
                continue
            try:
                db.delete(p)
                db.flush()
                deleted += 1
            except Exception:
                db.rollback()
                failed += 1
                continue
        try:
            db.commit()
        except Exception:
            db.rollback()

        msg = f"🗑️ تم حذف {deleted} صنف"
        if failed:
            msg += f" • ⚠️ فشل حذف {failed} (مربوط بفواتير/حركات)"

        return RedirectResponse(
            url=_back_url(msg, q, color_filter, size_filter, low_only, in_stock, sort, dir, page, page_size),
            status_code=303
        )

    if action == "edit":
        updated = 0
        for pid in id_list:
            p = db.query(models.Product).get(pid)
            if not p:
                continue

            changed = False

            if parsed_cost is not None and hasattr(models.Product, "cost_price"):
                p.cost_price = float(parsed_cost)
                changed = True

            if parsed_price is not None:
                p.price = float(parsed_price)
                changed = True

            if parsed_stock_delta is not None:
                old_stock = int(p.stock or 0)
                p.stock = max(0, old_stock + int(parsed_stock_delta))
                changed = True

                delta = p.stock - old_stock
                if delta != 0 and hasattr(models, "ManufacturingBatch"):
                    try:
                        db.add(models.ManufacturingBatch(
                            date=datetime.now().strftime("%Y-%m-%d"),
                            product_id=p.id,
                            qty=delta,
                            unit_cost=float(getattr(p, "cost_price", 0) or 0),
                            note="[BULK] تعديل ستوك (Delta)"
                        ))
                    except Exception:
                        pass

            elif parsed_stock_set is not None:
                old_stock = int(p.stock or 0)
                p.stock = max(0, int(parsed_stock_set))
                changed = True

                delta = p.stock - old_stock
                if delta != 0 and hasattr(models, "ManufacturingBatch"):
                    try:
                        db.add(models.ManufacturingBatch(
                            date=datetime.now().strftime("%Y-%m-%d"),
                            product_id=p.id,
                            qty=delta,
                            unit_cost=float(getattr(p, "cost_price", 0) or 0),
                            note="[BULK] تعديل ستوك (Set)"
                        ))
                    except Exception:
                        pass

            if changed:
                updated += 1

        db.commit()
        return RedirectResponse(
            url=_back_url(f"✅ تم تعديل {updated} صنف", q, color_filter, size_filter, low_only, in_stock, sort, dir, page, page_size),
            status_code=303
        )

    return RedirectResponse(
        url=_back_url("⚠️ أكشن غير معروف", q, color_filter, size_filter, low_only, in_stock, sort, dir, page, page_size),
        status_code=303
    )


@router.post("/update")
def update_product(
    pid: int = Form(...),
    name: str = Form(...),
    color: str = Form(""),
    size: str = Form(""),
    barcode: str = Form(""),
    cost_price: float = Form(0),
    price: float = Form(0),
    stock: int = Form(0),

    q: str = Form(""),
    color_filter: str = Form(""),
    size_filter: str = Form(""),
    low_only: int = Form(0),
    in_stock: int = Form(0),
    sort: str = Form("name"),
    dir: str = Form("asc"),
    page: int = Form(1),
    page_size: int = Form(15),

    db: Session = Depends(get_db),
):
    p = db.query(models.Product).get(pid)
    if not p:
        return RedirectResponse(
            url=_back_url("⚠️ المنتج غير موجود", q, color_filter, size_filter, low_only, in_stock, sort, dir, page, page_size),
            status_code=303
        )

    p.name = name
    p.color = color
    p.size = size
    p.barcode = barcode

    if hasattr(models.Product, "cost_price"):
        p.cost_price = float(cost_price or 0)
    p.price = float(price or 0)
    p.stock = max(0, int(stock or 0))

    db.commit()

    return RedirectResponse(
        url=_back_url("✅ تم تحديث المنتج", q, color_filter, size_filter, low_only, in_stock, sort, dir, page, page_size),
        status_code=303
    )


@router.post("/stock-delta")
def stock_delta(
    pid: int = Form(...),
    delta: int = Form(...),

    q: str = Form(""),
    color_filter: str = Form(""),
    size_filter: str = Form(""),
    low_only: int = Form(0),
    in_stock: int = Form(0),
    sort: str = Form("name"),
    dir: str = Form("asc"),
    page: int = Form(1),
    page_size: int = Form(15),

    db: Session = Depends(get_db),
):
    p = db.query(models.Product).get(pid)
    if not p:
        return RedirectResponse(
            url=_back_url("⚠️ المنتج غير موجود", q, color_filter, size_filter, low_only, in_stock, sort, dir, page, page_size),
            status_code=303
        )

    old_stock = int(p.stock or 0)
    p.stock = max(0, old_stock + int(delta))
    db.commit()

    real_delta = p.stock - old_stock
    if real_delta != 0 and hasattr(models, "ManufacturingBatch"):
        try:
            db.add(models.ManufacturingBatch(
                date=datetime.now().strftime("%Y-%m-%d"),
                product_id=p.id,
                qty=real_delta,
                unit_cost=float(getattr(p, "cost_price", 0) or 0),
                note="[UI] تعديل ستوك سريع (Delta)"
            ))
            db.commit()
        except Exception:
            pass

    return RedirectResponse(
        url=_back_url("✅ تم تحديث الستوك", q, color_filter, size_filter, low_only, in_stock, sort, dir, page, page_size),
        status_code=303
    )