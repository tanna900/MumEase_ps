# app/routers/users.py
"""
روتر المصادقة وإدارة المستخدمين
"""
import json
from fastapi import APIRouter, Request, Depends, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from datetime import datetime

from app.database import SessionLocal
from app.auth import (
    User, hash_password, verify_password,
    create_session, delete_session,
    get_current_user, require_login, require_permission,
    get_user_permissions, ALL_PERMISSIONS, PERMISSION_GROUPS,
    ensure_admin_user,
)

router = APIRouter(tags=["Auth"])
templates = Jinja2Templates(directory="app/templates")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ═══════════════════════════════
# Login / Logout
# ═══════════════════════════════

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, msg: str = Query("")):
    # لو مسجل دخول خليه يروح الهوم
    if get_current_user(request):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "msg": msg})


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    ensure_admin_user(db)
    user = db.query(User).filter(
        User.username == username.strip(),
        User.is_active == True
    ).first()

    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse("login.html", {
            "request": request,
            "msg": "❌ اسم المستخدم أو الباسورد غلط"
        })

    # تحديث آخر دخول
    user.last_login = datetime.now()
    db.commit()

    perms = get_user_permissions(user)
    token = create_session(user.id, user.username, user.is_admin, perms)

    resp = RedirectResponse(url="/", status_code=302)
    resp.set_cookie("session_token", token, httponly=True, max_age=7200)
    return resp


@router.get("/logout")
def logout(request: Request):
    token = request.cookies.get("session_token")
    if token:
        delete_session(token)
    resp = RedirectResponse(url="/login", status_code=302)
    resp.delete_cookie("session_token")
    return resp

# ═══════════════════════════════
# حسابي
# ═══════════════════════════════

@router.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request):
    user = require_login(request)

    return templates.TemplateResponse("profile.html", {
        "request": request,
        "user": user,
    })

# ═══════════════════════════════
# تغيير الباسورد
# ═══════════════════════════════

@router.get("/change-password", response_class=HTMLResponse)
def change_password_page(request: Request):
    user = require_login(request)
    return templates.TemplateResponse("change_password.html", {
        "request": request, "msg": "", "user": user
    })


@router.post("/change-password")
def change_password_submit(
    request: Request,
    old_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    current = require_login(request)
    user = db.query(User).filter(User.id == current["user_id"]).first()

    if not user or not verify_password(old_password, user.password_hash):
        return templates.TemplateResponse("change_password.html", {
            "request": request, "msg": "❌ الباسورد الحالي غلط", "user": current
        })

    if new_password != confirm_password:
        return templates.TemplateResponse("change_password.html", {
            "request": request, "msg": "❌ الباسورد الجديد مش متطابق", "user": current
        })

    if len(new_password) < 6:
        return templates.TemplateResponse("change_password.html", {
            "request": request, "msg": "❌ الباسورد لازم يكون 6 حروف على الأقل", "user": current
        })

    user.password_hash = hash_password(new_password)
    db.commit()

    return templates.TemplateResponse("change_password.html", {
        "request": request, "msg": "✅ تم تغيير الباسورد بنجاح", "user": current
    })


# ═══════════════════════════════
# إدارة المستخدمين (Admin فقط)
# ═══════════════════════════════

@router.get("/admin/users", response_class=HTMLResponse)
def users_list(request: Request, db: Session = Depends(get_db)):
    require_permission(request, "admin_users")
    users = db.query(User).order_by(User.id).all()
    return templates.TemplateResponse("admin_users.html", {
        "request": request,
        "users": users,
        "ALL_PERMISSIONS": ALL_PERMISSIONS,
        "PERMISSION_GROUPS": PERMISSION_GROUPS,
        "msg": request.query_params.get("msg", ""),
    })


@router.get("/admin/users/new", response_class=HTMLResponse)
def new_user_page(request: Request):
    require_permission(request, "admin_users")
    return templates.TemplateResponse("admin_user_form.html", {
        "request": request,
        "user": None,
        "ALL_PERMISSIONS": ALL_PERMISSIONS,
        "PERMISSION_GROUPS": PERMISSION_GROUPS,
        "msg": "",
    })


@router.post("/admin/users/new")
async def new_user_submit(
    request: Request,
    db: Session = Depends(get_db),
):
    require_permission(request, "admin_users")
    form = await request.form()

    username     = (form.get("username") or "").strip()
    display_name = (form.get("display_name") or "").strip()
    password     = (form.get("password") or "").strip()
    is_admin     = form.get("is_admin") == "1"

    if not username or not display_name or not password:
        return templates.TemplateResponse("admin_user_form.html", {
            "request": request, "user": None,
            "ALL_PERMISSIONS": ALL_PERMISSIONS,
            "PERMISSION_GROUPS": PERMISSION_GROUPS,
            "msg": "❌ كل الحقول مطلوبة",
        })

    if db.query(User).filter(User.username == username).first():
        return templates.TemplateResponse("admin_user_form.html", {
            "request": request, "user": None,
            "ALL_PERMISSIONS": ALL_PERMISSIONS,
            "PERMISSION_GROUPS": PERMISSION_GROUPS,
            "msg": "❌ اسم المستخدم موجود بالفعل",
        })

    perms = [k for k in ALL_PERMISSIONS if form.get(f"perm_{k}") == "1"]

    user = User(
        username=username,
        display_name=display_name,
        password_hash=hash_password(password),
        is_admin=is_admin,
        is_active=True,
        permissions=json.dumps(perms),
    )
    db.add(user)
    db.commit()

    return RedirectResponse(url="/admin/users?msg=✅+تم+إضافة+المستخدم", status_code=303)


@router.get("/admin/users/{user_id}/edit", response_class=HTMLResponse)
def edit_user_page(user_id: int, request: Request, db: Session = Depends(get_db)):
    require_permission(request, "admin_users")
    user = db.query(User).get(user_id)
    if not user:
        return RedirectResponse(url="/admin/users", status_code=302)

    user_perms = get_user_permissions(user)
    return templates.TemplateResponse("admin_user_form.html", {
        "request": request,
        "user": user,
        "user_perms": user_perms,
        "ALL_PERMISSIONS": ALL_PERMISSIONS,
        "PERMISSION_GROUPS": PERMISSION_GROUPS,
        "msg": "",
    })


@router.post("/admin/users/{user_id}/edit")
async def edit_user_submit(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    require_permission(request, "admin_users")
    user = db.query(User).get(user_id)
    if not user:
        return RedirectResponse(url="/admin/users", status_code=302)

    form = await request.form()

    user.display_name = (form.get("display_name") or user.display_name).strip()
    user.is_admin     = form.get("is_admin") == "1"
    user.is_active    = form.get("is_active") == "1"

    new_pass = (form.get("password") or "").strip()
    if new_pass:
        if len(new_pass) < 6:
            user_perms = get_user_permissions(user)
            return templates.TemplateResponse("admin_user_form.html", {
                "request": request, "user": user, "user_perms": user_perms,
                "ALL_PERMISSIONS": ALL_PERMISSIONS,
                "PERMISSION_GROUPS": PERMISSION_GROUPS,
                "msg": "❌ الباسورد لازم يكون 6 حروف على الأقل",
            })
        user.password_hash = hash_password(new_pass)

    perms = [k for k in ALL_PERMISSIONS if form.get(f"perm_{k}") == "1"]
    user.permissions = json.dumps(perms)

    db.commit()
    return RedirectResponse(url="/admin/users?msg=✅+تم+تعديل+المستخدم", status_code=303)


@router.post("/admin/users/{user_id}/delete")
def delete_user(user_id: int, request: Request, db: Session = Depends(get_db)):
    require_permission(request, "admin_users")
    current = get_current_user(request)

    if current and current["user_id"] == user_id:
        return RedirectResponse(url="/admin/users?msg=❌+مش+تقدر+تمسح+نفسك", status_code=303)

    user = db.query(User).get(user_id)
    if user:
        db.delete(user)
        db.commit()

    return RedirectResponse(url="/admin/users?msg=✅+تم+حذف+المستخدم", status_code=303)
