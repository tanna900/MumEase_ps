import os
from datetime import datetime

import requests
from fastapi import APIRouter

router = APIRouter()

DEFAULT_API_VERSION = "2026-01"
VARIANT_CACHE = {}


def _load_dotenv():
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = os.path.join(BASE_DIR, ".env")

    if not os.path.exists(env_path):
        return

    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(
                    key.strip(),
                    value.strip().strip('"').strip("'")
                )
    except Exception:
        pass


_load_dotenv():
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = os.path.join(BASE_DIR, ".env")

    if not os.path.exists(env_path):
        return

    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except Exception:
        pass


_load_dotenv()


def _settings():
    store = (os.getenv("SHOPIFY_STORE") or "").strip()
    token = (os.getenv("SHOPIFY_TOKEN") or os.getenv("SHOPIFY_ACCESS_TOKEN") or "").strip()
    api_version = (os.getenv("SHOPIFY_API_VERSION") or DEFAULT_API_VERSION).strip()
    if store.endswith(".myshopify.com"):
        store_domain = store
    else:
        store_domain = f"{store}.myshopify.com" if store else ""
    return store_domain, token, api_version


def _admin_url(path: str) -> str:
    store_domain, _, api_version = _settings()
    path = path.lstrip("/")
    if path == "graphql.json":
        return f"https://{store_domain}/admin/api/{api_version}/graphql.json"
    return f"https://{store_domain}/admin/api/{api_version}/{path}"


def _headers():
    _, token, _ = _settings()
    return {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def is_configured() -> bool:
    store_domain, token, _ = _settings()
    return bool(store_domain and token)


def shopify_request(method: str, path: str, **kwargs):
    if not is_configured():
        return None, {"success": False, "message": "Shopify settings are missing"}

    try:
        response = requests.request(
            method,
            _admin_url(path),
            headers=_headers(),
            timeout=30,
            **kwargs,
        )
    except Exception as exc:
        return None, {"success": False, "message": str(exc)}

    if response.status_code < 200 or response.status_code >= 300:
        return response, {"success": False, "message": response.text}

    if not response.content:
        return response, {"success": True}

    try:
        data = response.json()
    except Exception:
        data = {}
    if path.lstrip("/") == "graphql.json" and data.get("errors"):
        messages = []
        for err in data.get("errors") or []:
            msg = err.get("message") or ""
            required = (err.get("extensions") or {}).get("requiredAccess") or ""
            if required:
                msg = f"{msg} Required access: {required}"
            if msg:
                messages.append(msg)
        return response, {"success": False, "message": "; ".join(messages) or "Shopify GraphQL error", "data": data}
    return response, {"success": True, "data": data}


def get_variant_data(variant_id):
    if not variant_id:
        return {}

    variant_id = str(variant_id)
    if variant_id in VARIANT_CACHE:
        return VARIANT_CACHE[variant_id]

    _, payload = shopify_request("GET", f"variants/{variant_id}.json")
    if not payload.get("success"):
        return {}

    variant = (payload.get("data") or {}).get("variant", {}) or {}
    data = {
        "barcode": variant.get("barcode") or "",
        "sku": variant.get("sku") or "",
    }
    VARIANT_CACHE[variant_id] = data
    return data


def get_variants_data(variant_ids):
    ids = []
    for variant_id in variant_ids or []:
        if variant_id:
            ids.append(str(variant_id))

    missing = [variant_id for variant_id in sorted(set(ids)) if variant_id not in VARIANT_CACHE]
    last_error = ""
    for i in range(0, len(missing), 80):
        chunk = missing[i:i + 80]
        if not chunk:
            continue

        gids = [f"gid://shopify/ProductVariant/{variant_id}" for variant_id in chunk]
        query = """
        query variantBarcodes($ids: [ID!]!) {
          nodes(ids: $ids) {
            ... on ProductVariant {
              id
              barcode
              sku
            }
          }
        }
        """
        _, payload = shopify_request(
            "POST",
            "graphql.json",
            json={"query": query, "variables": {"ids": gids}},
        )
        if not payload.get("success"):
            last_error = payload.get("message") or ""
            continue

        data = (payload.get("data") or {}).get("data") or {}
        nodes = data.get("nodes") or []
        for node in nodes:
            if not node or not node.get("id"):
                continue
            variant_id = str(node["id"]).split("/")[-1]
            VARIANT_CACHE[variant_id] = {
                "barcode": node.get("barcode") or "",
                "sku": node.get("sku") or "",
            }

    result = {}
    for variant_id in ids:
        result[variant_id] = VARIANT_CACHE.get(str(variant_id), {})
        if not result[variant_id] and last_error:
            result[variant_id] = {"error": last_error}
    return result


def _money(value):
    try:
        return round(float(value or 0), 2)
    except Exception:
        return 0.0


def _address_text(address):
    if not address:
        return ""

    parts = [
        address.get("company"),
        address.get("address1"),
        address.get("address2"),
        address.get("city"),
        address.get("province"),
        address.get("zip"),
        address.get("country"),
    ]

    return " - ".join(
        [str(p).strip() for p in parts if str(p or "").strip()]
    )


def _note_address(order):
    keys = ("address", "addr", "street", "shipping", "العنوان", "عنوان")
    values = []
    for attr in order.get("note_attributes", []) or []:
        name = str(attr.get("name") or "").strip().lower()
        value = str(attr.get("value") or "").strip()
        if value and any(key in name for key in keys):
            values.append(value)
    return " - ".join(values)


def _format_order(order, variant_lookup=None):
    variant_lookup = variant_lookup or {}
    customer = order.get("customer", {}) or {}
    shipping = order.get("shipping_address", {}) or {}
    billing = order.get("billing_address", {}) or {}
    default_address = customer.get("default_address", {}) or {}
    chosen_address = shipping or billing or default_address or {}

    raw_name = str(chosen_address.get("name") or "").strip()

    invalid_names = [
        f"shopify #{order.get('order_number')}",
        f"عميل #{order.get('order_number')}",
    ]

    if raw_name.lower() in [x.lower() for x in invalid_names]:
        raw_name = ""

    customer_first = (
        customer.get("first_name")
        or shipping.get("first_name")
        or billing.get("first_name")
        or ""
    )

    customer_last = (
        customer.get("last_name")
        or shipping.get("last_name")
        or billing.get("last_name")
        or ""
    )

    customer_full = f"{customer_first} {customer_last}".strip()

    full_name = (
        customer_full
        or raw_name
        or order.get("contact_email")
        or order.get("email")
        or "عميل Shopify"
    )
    phone = (
        shipping.get("phone")
        or billing.get("phone")
        or chosen_address.get("phone")
        or customer.get("phone")
        or order.get("phone")
        or ""
    )

    items = []
    for item in order.get("line_items", []) or []:
        variant_id = item.get("variant_id")
        variant_data = variant_lookup.get(str(variant_id)) or {}
        variant_title = item.get("variant_title") or ""
        items.append({
            "line_item_id": item.get("id"),
            "product_name": item.get("title") or item.get("name") or "",
            "variant_title": variant_title,
            "quantity": int(item.get("quantity") or 0),
            "fulfillable_quantity": int(item.get("fulfillable_quantity") or 0),
            "price": _money(item.get("price")),
            "variant_id": variant_id,
            "barcode": variant_data.get("barcode") or "",
            "sku": variant_data.get("sku") or item.get("sku") or "",
            "barcode_error": variant_data.get("error") or "",
        })

    shipping_total = 0.0
    for line in order.get("shipping_lines", []) or []:
        shipping_total += _money(line.get("price"))

    return {
        "id": order.get("id"),
        "name": order.get("name") or f"#{order.get('order_number')}",
        "order_number": order.get("order_number"),
        "customer_name": full_name,
        "province": chosen_address.get("province") or "",
        "phone": phone,
        "address": _address_text(chosen_address) or _note_address(order) or (order.get("note") or ""),
        "total_price": _money(order.get("total_price")),
        "total_discounts": _money(order.get("total_discounts")),
        "shipping_cost": round(shipping_total, 2),
        "financial_status": order.get("financial_status"),
        "fulfillment_status": order.get("fulfillment_status"),
        "created_at": order.get("created_at"),
        "items": items,
    }


@router.get("/api/shopify/orders")
def get_orders():
    if not is_configured():
        return {"success": False, "message": "Shopify settings are missing", "orders": []}

    params = {
        "status": "open",
        "fulfillment_status": "any",
        "limit": 50,
        "order": "created_at desc",
    }
    _, payload = shopify_request("GET", "orders.json", params=params)
    if not payload.get("success"):
        return {"success": False, "message": payload.get("message", ""), "orders": []}

    raw_orders = (payload.get("data") or {}).get("orders", []) or []
    variant_ids = []
    for order in raw_orders:
        for item in order.get("line_items", []) or []:
            if item.get("variant_id"):
                variant_ids.append(item.get("variant_id"))
    variant_lookup = get_variants_data(variant_ids)

    orders = []
    for order in raw_orders:
        if order.get("cancelled_at"):
            continue
        fulfillment_status = (order.get("fulfillment_status") or "").strip().lower()
        if fulfillment_status in ("fulfilled", "restocked"):
            continue
        formatted = _format_order(order, variant_lookup)
        if any((item.get("fulfillable_quantity") or item.get("quantity") or 0) > 0 for item in formatted["items"]):
            orders.append(formatted)

    return {"success": True, "orders": orders}


def fulfill_order(order_id):
    _, payload = shopify_request("GET", f"orders/{order_id}/fulfillment_orders.json")
    if not payload.get("success"):
        return payload

    fulfillment_orders = (payload.get("data") or {}).get("fulfillment_orders", []) or []
    lines = []
    for fo in fulfillment_orders:
        status = (fo.get("status") or "").lower()
        if status not in ("open", "in_progress", "scheduled"):
            continue
        supported = fo.get("supported_actions") or []
        if supported and "create_fulfillment" not in supported:
            continue
        lines.append({"fulfillment_order_id": fo.get("id")})

    if not lines:
        return {"success": True, "message": "No open fulfillment orders"}

    body = {
        "fulfillment": {
            "line_items_by_fulfillment_order": lines,
            "notify_customer": False,
        }
    }
    _, payload = shopify_request("POST", "fulfillments.json", json=body)
    return payload


def mark_order_paid(order_id):
    gid = f"gid://shopify/Order/{order_id}"
    query = """
    mutation orderMarkAsPaid($input: OrderMarkAsPaidInput!) {
      orderMarkAsPaid(input: $input) {
        userErrors { field message }
        order { id displayFinancialStatus canMarkAsPaid }
      }
    }
    """
    _, payload = shopify_request(
        "POST",
        "graphql.json",
        json={"query": query, "variables": {"input": {"id": gid}}},
    )
    if not payload.get("success"):
        return payload

    data = (payload.get("data") or {}).get("data") or {}
    result = data.get("orderMarkAsPaid") or {}
    errors = result.get("userErrors") or []
    if errors:
        message = "; ".join(err.get("message", "") for err in errors if err.get("message"))
        return {"success": False, "message": message or "Shopify rejected mark as paid"}

    return {"success": True, "data": result}


def sync_fulfilled_for_invoice(inv):
    order_id = getattr(inv, "shopify_order_id", None)
    if not order_id:
        return False
    result = fulfill_order(order_id)
    if result.get("success"):
        inv.shopify_fulfilled_at = datetime.now()
        inv.shopify_sync_note = None
        return True
    inv.shopify_sync_note = result.get("message") or "Shopify fulfillment failed"
    return False


def sync_paid_for_invoice(inv):
    order_id = getattr(inv, "shopify_order_id", None)
    if not order_id or getattr(inv, "shopify_paid_at", None):
        return False
    result = mark_order_paid(order_id)
    if result.get("success"):
        inv.shopify_paid_at = datetime.now()
        inv.shopify_sync_note = None
        return True
    inv.shopify_sync_note = result.get("message") or "Shopify mark as paid failed"
    return False
