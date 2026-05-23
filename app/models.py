from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from datetime import datetime
from app.database import Base

class Setting(Base):
    __tablename__ = "settings"
    id = Column(Integer, primary_key=True)
    key = Column(String, unique=True, index=True)
    value = Column(String)

class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    color = Column(String)
    size = Column(String)
    barcode = Column(String, unique=True, index=True)
    cost_price = Column(Float, default=0)   # ✅ متوسط تكلفة المخزون
    price = Column(Float, default=0)        # ✅ آخر سعر بيع
    stock = Column(Integer, default=0)

# 🆕 جدول المسوّقين
class Marketer(Base):
    __tablename__ = "marketers"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)
    commission_rate = Column(Float, default=0)  # نسبة العمولة % (0..100)

    invoices = relationship("Invoice", back_populates="marketer")

class Invoice(Base):
    __tablename__ = "invoices"
    id = Column(Integer, primary_key=True, index=True)
    type = Column(String, default="S")               # S=بيع, R=مرتجع
    invoice_code = Column(String, index=True)        # INV S-xxxx / INV R-xxxx
    customer_name = Column(String, nullable=False)
    customer_phone = Column(String, nullable=False)
    customer_address = Column(String)
    order_number = Column(String)
    payment_method = Column(String)
    shipping_company = Column(String)
    shipping_cost = Column(Float, default=0)         # اللي للعميل
    actual_shipping_cost = Column(Float, default=0)  # اللي الشركة بتحسبه فعليًا (داخلي)
    discount = Column(Float, default=0)
    subtotal = Column(Float, default=0)
    total = Column(Float, default=0)
    return_reason = Column(Text)                     # للمرتجع
    return_shipping_fee = Column(Float, default=0)   # تكلفة شحن المرتجع تخصم من رصيد الشركة
    note = Column(Text)                              # ملاحظات الفاتورة (اختياري)
    created_at = Column(DateTime, default=datetime.now)

    # جديد: ربط المرتجع بفاتورة البيع الأصلية
    original_sale_id = Column(Integer, ForeignKey("invoices.id"), nullable=True)
    original_sale_code = Column(String, nullable=True)

    # 🆕 ربط الفاتورة بمسوّق
    marketer_id = Column(Integer, ForeignKey("marketers.id"), nullable=True)

    items = relationship("InvoiceItem", back_populates="invoice", cascade="all, delete-orphan")
    marketer = relationship("Marketer", back_populates="invoices")

    # 🆕 جديد: المحافظة
    governorate = Column(String, nullable=True)  # مثال: "القاهرة"

class InvoiceItem(Base):
    __tablename__ = "invoice_items"
    id = Column(Integer, primary_key=True, index=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id"))
    product_id = Column(Integer, ForeignKey("products.id"))
    product_name = Column(String)
    qty = Column(Integer)
    unit_price = Column(Float)
    line_total = Column(Float)

    invoice = relationship("Invoice", back_populates="items")

# مصروفات نثرية
class Expense(Base):
    __tablename__ = "expenses"
    id = Column(Integer, primary_key=True, index=True)
    date = Column(String, index=True)   # "YYYY-MM-DD"
    amount = Column(Float, default=0)
    note = Column(Text)
    created_at = Column(DateTime, default=datetime.now)

# تسوية يومية
class DailySettlement(Base):
    __tablename__ = "daily_settlements"
    id = Column(Integer, primary_key=True, index=True)
    date = Column(String, index=True)   # "YYYY-MM-DD"
    sales_total = Column(Float, default=0)
    returns_total = Column(Float, default=0)
    expenses_total = Column(Float, default=0)
    net_total = Column(Float, default=0)
    created_at = Column(DateTime, default=datetime.now)

# دفعات شركات الشحن
class ShippingPayment(Base):
    __tablename__ = "shipping_payments"
    id = Column(Integer, primary_key=True, index=True)
    company = Column(String, index=True)
    date = Column(String, index=True)   # "YYYY-MM-DD"
    amount = Column(Float, default=0)
    note = Column(Text)
    created_at = Column(DateTime, default=datetime.now)

# تخصيص دفعة لفاتورة شحن (تسديد جزئي/كامل)
class ShippingAllocation(Base):
    __tablename__ = "shipping_allocations"
    id = Column(Integer, primary_key=True, index=True)
    payment_id = Column(Integer, ForeignKey("shipping_payments.id"), nullable=False)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=False)
    amount = Column(Float, default=0)
    created_at = Column(DateTime, default=datetime.now)

# =========================
# Finance / Cashbook
from sqlalchemy import Boolean

class FinanceEntry(Base):
    __tablename__ = "finance_entries"
    id = Column(Integer, primary_key=True, index=True)
    date = Column(String, index=True)          # "YYYY-MM-DD"
    type = Column(String, index=True)          # "IN" = إيراد, "OUT" = مصروف
    category = Column(String, index=True)      # بند (ينفع أي نص)
    amount = Column(Float, default=0)
    note = Column(Text)
    created_at = Column(DateTime, default=datetime.now)

# =========================
# ربط دفعات الشحن بقيود الخزنة (mapping table)
class ShippingPaymentCashMap(Base):
    __tablename__ = "shipping_payment_cash_map"
    id = Column(Integer, primary_key=True, index=True)
    payment_id = Column(Integer, index=True)         # يشير إلى ShippingPayment.id
    finance_entry_id = Column(Integer, index=True)   # يشير إلى FinanceEntry.id
    created_at = Column(DateTime, default=datetime.now)

# ====== تنبيهات متجاهَلة ======
class DismissedAlert(Base):
    __tablename__ = "dismissed_alerts"
    id = Column(Integer, primary_key=True, index=True)
    kind = Column(String, index=True)      # 'low_stock' أو 'overdue'
    ref_id = Column(Integer, index=True)   # Product.id أو Invoice.id
    created_at = Column(DateTime, default=datetime.now)

# === [FACTORY ACCOUNTS START] ===
# موديلات حساب المصنع (تصنيع + مدفوعات)
class Factory(Base):
    __tablename__ = "factories"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False, index=True)
    phone = Column(String)
    address = Column(Text)
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.now)

class ManufacturingBatch(Base):
    __tablename__ = "manufacturing_batches"
    id = Column(Integer, primary_key=True, index=True)
    date = Column(String, index=True)  # "YYYY-MM-DD"
    factory_id = Column(Integer, ForeignKey("factories.id"), index=True, nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=True)
    qty = Column(Integer, nullable=False)
    unit_cost = Column(Float, default=0)   # تكلفة القطعة وقت التصنيع
    note = Column(Text)
    created_at = Column(DateTime, default=datetime.now)

class FactoryPayment(Base):
    __tablename__ = "factory_payments"
    id = Column(Integer, primary_key=True, index=True)
    date = Column(String, index=True)  # "YYYY-MM-DD"
    factory_id = Column(Integer, ForeignKey("factories.id"), index=True, nullable=False)
    amount = Column(Float, default=0)  # المبلغ المدفوع للمصنع
    method = Column(String)            # cash/bank/vcash...
    note = Column(Text)
    created_at = Column(DateTime, default=datetime.now)
# === [FACTORY ACCOUNTS END] ===

# =========================
# 🆕 موديلات المشتريات (Purchase Invoices)
# =========================

class PurchaseInvoice(Base):
    __tablename__ = "purchase_invoices"
    id = Column(Integer, primary_key=True, index=True)

    code = Column(String, index=True)          # رقم فاتورة الشراء
    date = Column(String, index=True)          # YYYY-MM-DD
    factory_id = Column(Integer, ForeignKey("factories.id"), nullable=True)

    total_qty = Column(Float, default=0)       # إجمالي عدد القطع
    total_cost = Column(Float, default=0)      # ✅ إجمالي تكلفة المخزون (qty * unit_total_cost)
    file_name = Column(String)                 # اسم ملف الإكسيل
    note = Column(Text)                        # ملاحظات اختيارية
    history = Column(Text)                     # سجل التعديلات

    created_at = Column(DateTime, default=datetime.now)

    items = relationship("PurchaseItem", back_populates="invoice", cascade="all, delete-orphan")


class PurchaseItem(Base):
    __tablename__ = "purchase_items"
    id = Column(Integer, primary_key=True, index=True)

    purchase_id = Column(Integer, ForeignKey("purchase_invoices.id"))
    product_id = Column(Integer, ForeignKey("products.id"))

    barcode = Column(String, index=True)
    name = Column(String)
    color = Column(String)
    size = Column(String)

    qty = Column(Integer, default=0)

    # ✅ الجديد المطلوب
    unit_mfg_cost = Column(Float, default=0)      # تكلفة تصنيع القطعة
    unit_total_cost = Column(Float, default=0)    # إجمالي تكلفة القطعة (اللي تدخل المخزون)

    # ✅ 🆕 إضافات جديدة لتكلفة القطعة (تفصيل)
    unit_fabric_cost = Column(Float, default=0)      # ✅ تكلفة القماش/الخامات فقط للقطعة (اللي يخصم من خامات انتاج)
    unit_overhead_cost = Column(Float, default=0)    # ✅ مصروفات نثرية/وحدة (تغليف/مواصلات/هوادر)

    # ✅ Legacy (عشان ما نكسرش أي عرض قديم): هنخليه دايمًا = unit_total_cost
    unit_cost = Column(Float, default=0)

    unit_price = Column(Float, default=0)

    # قبل التعديل (بنفس المنطق القديم بس هنستخدمهم كـ prev values)
    old_cost_price = Column(Float, default=0)   # متوسط التكلفة قبل الفاتورة
    old_price = Column(Float, default=0)        # سعر البيع قبل الفاتورة

    # ✅ إضافات داخلية عشان edit/delete يبقوا "آمنين" مع المتوسط
    old_stock = Column(Integer, default=0)          # المخزون قبل الفاتورة
    applied_cost_price = Column(Float, default=0)   # متوسط التكلفة بعد تطبيق الفاتورة
    applied_price = Column(Float, default=0)        # سعر البيع بعد تطبيق الفاتورة
    applied_stock = Column(Integer, default=0)      # المخزون بعد تطبيق الفاتورة

    created_at = Column(DateTime, default=datetime.now)

    invoice = relationship("PurchaseInvoice", back_populates="items")


class ProductMovement(Base):
    __tablename__ = "product_movements"

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)

    movement_type = Column(String, nullable=False)
    # purchase / purchase_edit / purchase_delete
    # sale / return / manufacturing

    qty = Column(Integer, default=0)
    balance_after = Column(Integer, default=0)

    ref_type = Column(String)         # PurchaseInvoice / Invoice / Batch
    ref_id = Column(Integer)          # ID of invoice or batch

    note = Column(Text)
    created_at = Column(DateTime, default=datetime.now)

# =========================
# 🆕 NEW: تغطية إدارية لفواتير الشحن (Display-only)
class ShippingAdminCover(Base):
    __tablename__ = "shipping_admin_covers"
    id = Column(Integer, primary_key=True, index=True)
    invoice_id = Column(Integer, index=True, nullable=False)  # يشير إلى invoices.id
    company = Column(String, index=True, nullable=False)      # لحماية سياق الشركة
    note = Column(Text)                                       # اختياري
    created_at = Column(DateTime, default=datetime.now)

# =========================
# 🆕 NEW: خامات إنتاج (تحت التشغيل) - Vouchers + Allocations
# =========================
class RawMaterialVoucher(Base):
    __tablename__ = "raw_material_vouchers"
    id = Column(Integer, primary_key=True, index=True)

    code = Column(String, index=True)          # RM-YYYYMMDD-0001
    date = Column(String, index=True)          # "YYYY-MM-DD"
    amount = Column(Float, default=0)          # قيمة الخامات المدفوعة
    finance_entry_id = Column(Integer, nullable=True)  # ربطها بقيد الخزنة (اختياري)
    note = Column(Text)
    created_at = Column(DateTime, default=datetime.now)

class RawMaterialAllocation(Base):
    __tablename__ = "raw_material_allocations"
    id = Column(Integer, primary_key=True, index=True)

    voucher_id = Column(Integer, ForeignKey("raw_material_vouchers.id"), index=True, nullable=False)
    purchase_id = Column(Integer, ForeignKey("purchase_invoices.id"), index=True, nullable=False)
    amount = Column(Float, default=0)          # جزء من السند اتصرف على الفاتورة
    created_at = Column(DateTime, default=datetime.now)
# =========================
# 🧵 Production Materials Ledger (Raw Materials)
# =========================
from sqlalchemy import Column, Integer, String, Float, DateTime, Text
from datetime import datetime
from app.database import Base

class ProductionMaterialEntry(Base):
    __tablename__ = "production_material_entries"

    id = Column(Integer, primary_key=True, index=True)
    date = Column(String, index=True)          # "YYYY-MM-DD"
    type = Column(String, index=True)          # "IN" / "OUT"
    amount = Column(Float, default=0)

    note = Column(Text)

    # ربط اختياري بالخزنة (لو الحركة جاية من سحب خزنة)
    finance_entry_id = Column(Integer, index=True, nullable=True)

    # ربط اختياري بفواتير مشتريات (علشان نقدر نعمل revert في edit/delete)
    ref_type = Column(String, index=True, nullable=True)   # "PurchaseInvoice"
    ref_id = Column(Integer, index=True, nullable=True)    # purchase.id

    created_at = Column(DateTime, default=datetime.now)