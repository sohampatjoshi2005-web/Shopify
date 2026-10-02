import time
from collections import defaultdict
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from ..db import get_db
from ..models import Address, User
from ..schemas import AddressIn, LoginIn, RegisterIn
from ..security import create_token, current_user, hash_password, verify_password

router = APIRouter(tags=["auth"])
_fails: dict[str, list[float]] = defaultdict(list)  # per-process throttle; use Redis behind multiple workers


def _user(u: User):
    return {"id": u.id, "email": u.email, "full_name": u.full_name, "role": u.role}


@router.post("/auth/register", status_code=201)
def register(body: RegisterIn, db: Session = Depends(get_db)):
    email = body.email.lower()
    if db.scalars(select(User).where(User.email == email)).first():
        raise HTTPException(409, "Email already registered")
    u = User(email=email, password_hash=hash_password(body.password), full_name=body.full_name)
    db.add(u)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Email already registered")
    return {"access_token": create_token(u), "user": _user(u)}


@router.post("/auth/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    email = body.email.lower()
    recent = [t for t in _fails[email] if t > time.time() - 900]
    _fails[email] = recent
    if len(recent) >= 5:
        raise HTTPException(429, "Too many attempts, try again in 15 minutes")
    u = db.scalars(select(User).where(User.email == email)).first()
    if not u or not u.is_active or not verify_password(body.password, u.password_hash):
        _fails[email].append(time.time())
        raise HTTPException(401, "Invalid credentials")
    _fails.pop(email, None)
    return {"access_token": create_token(u), "user": _user(u)}


@router.get("/auth/me")
def me(user: User = Depends(current_user)):
    return _user(user)


def _addr(a: Address):
    return {c.name: getattr(a, c.name) for c in Address.__table__.columns if c.name != "user_id"}


@router.get("/addresses")
def list_addresses(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return [_addr(a) for a in db.scalars(select(Address).where(Address.user_id == user.id).order_by(Address.id))]


@router.post("/addresses", status_code=201)
def add_address(body: AddressIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    first = not db.scalars(select(Address.id).where(Address.user_id == user.id)).first()
    if body.is_default or first:
        db.execute(update(Address).where(Address.user_id == user.id).values(is_default=False))
    a = Address(user_id=user.id, **{**body.model_dump(), "is_default": body.is_default or first})
    db.add(a)
    db.commit()
    return _addr(a)


@router.delete("/addresses/{aid}", status_code=204)
def delete_address(aid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    a = db.get(Address, aid)
    if not a or a.user_id != user.id:
        raise HTTPException(404, "Not found")
    db.delete(a)
    db.commit()
