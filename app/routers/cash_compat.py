# app/routers/cash_compat.py
from datetime import date
from fastapi import APIRouter, Request, Query
from fastapi.responses import RedirectResponse

router = APIRouter(tags=["Compat"])

@router.get("/cash/close-day")
def compat_close_day(date_str: str = Query("", alias="date")):
    """
    مسار توافق قديم:
    /cash/close-day[?date=YYYY-MM-DD]  -->  /settlements/close?date=YYYY-MM-DD
    لو التاريخ مش متبعت، نستخدم تاريخ اليوم.
    """
    d = (date_str or date.today().strftime("%Y-%m-%d"))
    return RedirectResponse(url=f"/settlements/close?date={d}", status_code=303)