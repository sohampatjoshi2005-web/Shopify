import re
from sqlalchemy import select
from sqlalchemy.orm import Session


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:120] or "item"


def unique_slug(db: Session, model, name: str) -> str:
    base = slugify(name)
    slug, n = base, 1
    while db.scalars(select(model.id).where(model.slug == slug)).first():
        n += 1
        slug = f"{base}-{n}"
    return slug
