from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from .config import settings


class Base(DeclarativeBase):
    pass


_kw: dict = {"pool_pre_ping": True}  # pre-ping: Neon/Supabase close idle connections
if settings.database_url.startswith("sqlite"):
    _kw["connect_args"] = {"check_same_thread": False}  # FastAPI runs sync routes in worker threads
else:
    _kw.update(pool_size=5, max_overflow=5, pool_recycle=300)

engine = create_engine(settings.database_url, **_kw)

if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

SessionLocal = sessionmaker(engine)


def get_db():
    with SessionLocal() as db:
        yield db
