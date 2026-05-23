# app/routers/pl_settings.py
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.database import SessionLocal
from app import models

router = APIRouter(prefix="/pl/settings", tags=["P&L Settings"])
templates = Jinja2Templates(directory="app/templates")

# ---------------- DB helper ----------------
def get_db():
    db = SessionLocal()
    try:
        # نضمن وجود جدول الإعدادات (خفيف وذاتي الإنشاء)
        db.execute(text("""
        CREATE TABLE IF NOT EXISTS pl_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,         -- FIXED | VARIABLE
            category TEXT NOT NULL,     -- اسم البند (كما يظهر في الخزنة)
            note TEXT
        );
        """))
        db.commit()
        yield db
    finally:
        db.close()

# aliases للنوع OUT بالعربي/الإنجليزي
OUT_ALIASES = {"OUT", "Out", "out", "مصروف", "مصروفات"}

def _is_out(val: str) -> bool:
    return (val or "").strip() in OUT_ALIASES

# ---------------- UI ----------------
@router.get("", response_class=HTMLResponse)
def page(request: Request, db: Session = Depends(get_db)):
    fixed_rows = db.execute(text(
        "SELECT id, category, COALESCE(note,'') FROM pl_categories WHERE kind='FIXED' ORDER BY category"
    )).fetchall()
    var_rows = db.execute(text(
        "SELECT id, category, COALESCE(note,'') FROM pl_categories WHERE kind='VARIABLE' ORDER BY category"
    )).fetchall()
    return templates.TemplateResponse("pl_settings.html", {
        "request": request,
        "fixed_rows": fixed_rows,
        "var_rows": var_rows,
    })

# جلب بنود المصروف (OUT) من الخزنة لتعبئة الـ dropdown
@router.get("/out-categories.json")
def out_categories(db: Session = Depends(get_db)):
    rows = db.query(models.FinanceEntry.category, models.FinanceEntry.type).all()
    outs = sorted({c for (c, t) in rows if _is_out(t) and (c or "").strip()})
    return JSONResponse({"categories": outs})

@router.post("/add")
def add_category(kind: str = Form(...),
                 selected: str = Form(""),
                 custom_name: str = Form(""),
                 note: str = Form(""),
                 db: Session = Depends(get_db)):
    items = [s.strip() for s in (selected or "").split(",") if s.strip()]
    if custom_name.strip():
        items.append(custom_name.strip())
    for cat in items:
        db.execute(text(
            "INSERT INTO pl_categories(kind, category, note) VALUES(:k,:c,:n)"
        ), {
            "k": ("FIXED" if kind.upper().startswith("FIX") else "VARIABLE"),
            "c": cat,
            "n": note
        })
    db.commit()
    return RedirectResponse(url="/pl/settings", status_code=303)

@router.post("/delete")
def delete_category(cid: int = Form(...), db: Session = Depends(get_db)):
    db.execute(text("DELETE FROM pl_categories WHERE id=:i"), {"i": cid})
    db.commit()
    return RedirectResponse(url="/pl/settings", status_code=303)