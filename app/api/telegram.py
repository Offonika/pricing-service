from fastapi import APIRouter

router = APIRouter()


@router.get("/today")
def today_summary():
    return {"message": "Telegram bot stub for /today"}


@router.get("/alerts")
def alerts():
    return {"message": "Telegram bot stub for /alerts"}
