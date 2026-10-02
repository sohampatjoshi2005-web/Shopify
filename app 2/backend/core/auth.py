import hmac, time, jwt
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from backend.core.config import settings

router = APIRouter(prefix="/auth", tags=["auth"])

class Login(BaseModel):
    email: str
    password: str

def _eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())

@router.post("/login")
def login(body: Login):
    if not settings.admin_email or not settings.admin_password:
        raise HTTPException(503, "Admin credentials are not configured")
    if not (_eq(body.email, settings.admin_email) and _eq(body.password, settings.admin_password)):
        raise HTTPException(401, "Invalid credentials")
    token = jwt.encode({"sub": body.email, "exp": int(time.time()) + 8 * 3600}, settings.jwt_secret, algorithm="HS256")
    return {"access_token": token}

def require_user(authorization: str = Header(None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing token")
    try:
        return jwt.decode(authorization.split(" ", 1)[1], settings.jwt_secret, algorithms=["HS256"])["sub"]
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid or expired token")

def widget_key(x_api_key: str = Header(None)) -> None:
    """Protects public endpoints used by the Shopify chat widget / tracking snippet."""
    if settings.widget_api_key:
        if not x_api_key or not _eq(x_api_key, settings.widget_api_key):
            raise HTTPException(401, "Invalid API key")
    elif settings.env == "production":
        raise HTTPException(503, "WIDGET_API_KEY not configured")
