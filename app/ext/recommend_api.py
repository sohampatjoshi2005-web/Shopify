from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from ..db import get_db
from ..models import User
from ..security import admin_user, current_user
from . import recommend as svc

router = APIRouter(prefix="/recommend", tags=["recommendations"])


@router.get("/me")
def for_me(limit: int = Query(8, ge=1, le=24), user: User = Depends(current_user), db: Session = Depends(get_db)):
    return {"items": svc.for_user(db, user.id, limit)}


@router.get("/product/{pid}")
def also_liked(pid: int, limit: int = Query(6, ge=1, le=24), db: Session = Depends(get_db)):
    return {"items": svc.for_product(db, pid, limit)}


@router.get("/popular")
def popular(limit: int = Query(8, ge=1, le=24), db: Session = Depends(get_db)):
    return {"items": svc.popular(db, svc.model(db), set(), limit)}


@router.post("/refresh", dependencies=[Depends(admin_user)])
def refresh():
    svc.invalidate()
    return {"ok": True}
