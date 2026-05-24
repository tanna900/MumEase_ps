# app/auth.py
"""
نظام المصادقة والصلاحيات
"""
import hashlib
import secrets
from typing import Optional, Dict
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from fastapi import Request, HTTPException
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text
from app.database import Base

# ── Sessions في الذاكرة ──
_sessions: Dict[str, dict] = {}
SESSION_TTL_HOURS = 12

# ── الصلاحيات المتاحة ──
ALL_PERMISSIONS = {
    # المبيعات
    "sales_view":        "عرض الفواتير",
    "sales_add":         "إضافة فاتورة بيع",
    "sales_return":      "إضافة مرتجع",
    "sales_delete":      "حذف فاتورة",
    # المخزون
    "stock_view":        "عرض المخزون",
    "stock_add":         "إضافة منتج",
    "stock_edit":        "تعديل منتج",
    "stock_delete":      "حذف منتج",
    "stock_wastage":     "تحويل للهالك / استرداد",
    "stock_import":      "استيراد منتجات",
    # المشتريات
    "purchases_view":    "عرض المشتريات",
    "purchases_add":     "إضافة فاتورة مشتريات",
    "purchases_edit":    "تعديل فاتورة مشتريات",
    "purchases_delete":  "حذف فاتورة مشتريات",
    # الخزنة
    "cash_view":         "عرض الخزنة",
    "cash_add":          "إضافة قيد خزنة",
    "cash_delete":       "حذف قيد خزنة",
    "cash_transfer":     "تحويل SubCash",
    # الشحن
    "shipping_view":     "عرض حسابات الشحن",
    "shipping_add":      "إضافة دفعة شحن",
    "shipping_allocate": "تخصيص دفعات",
    # المصانع
    "factories_view":    "عرض حسابات المصانع",
    "factories_pay":     "إضافة دفعة للمصنع",
    # خامات الإنتاج
    "materials_view":    "عرض الخامات",
    "materials_add":     "إضافة قيد خامات",
    # المسوقين
    "marketers_view":    "عرض المسوقين",
    "marketers_edit":    "إضافة/تعديل مسوق",
    # التقارير
    "reports_daily":     "تقفيل اليومية",
    "reports_pl":        "أرباح وخسائر",
    "reports_snapshot":  "الوضع المالي",
    "reports_govs":      "تقرير المحافظات",
    "reports_products":  "مبيعات المنتجات",
    "reports_colors":    "الألوان والمقاسات",
    "reports_customers": "العملاء المكررين",
    "reports_wastage":   "تقرير الهالك",
    "reports_perf":      "تقرير الأداء",
    # الداشبورد
    "home_view":         "عرض الداشبورد",
    "home_financials":   "أرقام الأرباح والتكاليف في الداشبورد",
    # الإعدادات
    "settings_pl":       "إعدادات نقطة الربحية",
    "admin_users":       "إدارة المستخدمين",
}

# مجموعات الصلاحيات
PERMISSION_GROUPS = {
    "المبيعات":        ["sales_view", "sales_add", "sales_return", "sales_delete"],
    "المخزون":         ["stock_view", "stock_add", "stock_edit", "stock_delete", "stock_wastage", "stock_import"],
    "المشتريات":       ["purchases_view", "purchases_add", "purchases_edit", "purchases_delete"],
    "الخزنة":          ["cash_view", "cash_add", "cash_delete", "cash_transfer"],
    "الشحن":           ["shipping_view", "shipping_add", "shipping_allocate"],
    "المصانع":         ["factories_view", "factories_pay"],
    "خامات الإنتاج":   ["materials_view", "materials_add"],
    "المسوقين":        ["marketers_view", "marketers_edit"],
    "التقارير":        ["reports_daily", "reports_pl", "reports_snapshot", "reports_govs",
                        "reports_products", "reports_colors", "reports_customers",
                        "reports_wastage", "reports_perf"],
    "الداشبورد":       ["home_view", "home_financials"],
    "الإعدادات":       ["settings_pl", "admin_users"],
}


# ── Model ──
class User(Base):
    __tablename__ = "users"
    id           = Column(Integer, primary_key=True, index=True)
    username     = Column(String, unique=True, nullable=False, index=True)
    display_name = Column(String, nullable=False)
    password_hash= Column(String, nullable=False)
    is_admin     = Column(Boolean, default=False)
    is_active    = Column(Boolean, default=True)
    permissions  = Column(Text, default="")   # JSON string of permission keys
    created_at   = Column(DateTime, default=datetime.now)
    last_login   = Column(DateTime, nullable=True)


# ── Helpers ──
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(password: str, hashed: str) -> bool:
    return hash_password(password) == hashed

def create_session(user_id: int, username: str, is_admin: bool, permissions: list) -> str:
    token = secrets.token_hex(32)
    _sessions[token] = {
        "user_id":    user_id,
        "username":   username,
        "is_admin":   is_admin,
        "permissions": permissions,
        "expires":    datetime.now() + timedelta(hours=SESSION_TTL_HOURS),
    }
    return token

def get_session(token: str) -> Optional[dict]:
    s = _sessions.get(token)
    if not s:
        return None
    if datetime.now() > s["expires"]:
        del _sessions[token]
        return None
    return s

def delete_session(token: str):
    _sessions.pop(token, None)

def get_user_permissions(user: User) -> list:
    if user.is_admin:
        return list(ALL_PERMISSIONS.keys())
    try:
        import json
        return json.loads(user.permissions or "[]")
    except Exception:
        return []

def get_current_user(request: Request) -> Optional[dict]:
    token = request.cookies.get("session_token")
    if not token:
        return None
    return get_session(token)

def require_login(request: Request) -> dict:
    user = get_current_user(request)
    if not user:
        from fastapi.responses import RedirectResponse
        raise HTTPException(status_code=307, headers={"Location": "/login"})
    return user

def has_permission(request: Request, perm: str) -> bool:
    user = get_current_user(request)
    if not user:
        return False
    if user.get("is_admin"):
        return True
    return perm in user.get("permissions", [])

def require_permission(request: Request, perm: str):
    user = require_login(request)
    if not user.get("is_admin") and perm not in user.get("permissions", []):
        from fastapi.responses import HTMLResponse
        raise HTTPException(status_code=403, detail="ليس لديك صلاحية للوصول لهذه الصفحة")
    return user


def ensure_admin_user(db: Session):
    """ينشئ مستخدم admin افتراضي لو مفيش أي مستخدمين."""
    existing = db.query(User).first()
    if not existing:
        admin = User(
            username="admin",
            display_name="المدير",
            password_hash=hash_password("admin123"),
            is_admin=True,
            is_active=True,
            permissions="[]",
        )
        db.add(admin)
        db.commit()
