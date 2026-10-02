import re
import time
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from ..db import get_db
from ..models import User
from ..security import current_user, hash_password, make_token, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_fails: dict[str, list[float]] = {}  # login throttle; move to Redis if you run several workers
MAX_FAILS, WINDOW = 5, 300


class Creds(BaseModel):
    email: str = Field(max_length=255)
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(default="", max_length=255)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip().lower()
        if not _EMAIL.match(v):
            raise ValueError("not a valid email address")
        return v


class LoginIn(BaseModel):
    email: str
    password: str


def _user(u: User) -> dict:
    return {"id": u.id, "email": u.email, "full_name": u.full_name, "role": u.role}


def _session(u: User) -> dict:
    return {"access_token": make_token(u), "token_type": "bearer", "user": _user(u)}


@router.post("/register", status_code=201)
def register(body: Creds, db: Session = Depends(get_db)):
    if db.scalars(select(User.id).where(User.email == body.email)).first():
        raise HTTPException(409, "Email already registered")
    u = User(email=body.email, password_hash=hash_password(body.password), full_name=body.full_name.strip())
    db.add(u)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Email already registered")
    return _session(u)


@router.post("/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    key, t = body.email.strip().lower(), time.time()
    recent = [x for x in _fails.get(key, []) if x > t - WINDOW]
    if len(recent) >= MAX_FAILS:
        raise HTTPException(429, "Too many failed attempts. Try again in a few minutes.")
    u = db.scalars(select(User).where(User.email == key)).first()
    if not u or not verify_password(body.password, u.password_hash):
        _fails[key] = recent + [t]
        raise HTTPException(401, "Wrong email or password")
    _fails.pop(key, None)
    return _session(u)


@router.get("/me")
def me(user: User = Depends(current_user)):
    return _user(user)
