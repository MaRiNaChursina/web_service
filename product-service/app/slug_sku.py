"""Генерация URL-безопасного slug и уникального SKU из названия (кириллица → латиница)."""
from __future__ import annotations

import re
import uuid

from sqlalchemy.orm import Session

from .models import Product

_CYR = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
_LAT = (
    "a",
    "b",
    "v",
    "g",
    "d",
    "e",
    "e",
    "zh",
    "z",
    "i",
    "y",
    "k",
    "l",
    "m",
    "n",
    "o",
    "p",
    "r",
    "s",
    "t",
    "u",
    "f",
    "h",
    "ts",
    "ch",
    "sh",
    "sch",
    "",
    "y",
    "",
    "e",
    "yu",
    "ya",
)
_TRANSLIT = str.maketrans(dict(zip(_CYR, _LAT)))


def slug_base_from_name(name: str) -> str:
    s = name.lower().strip().translate(_TRANSLIT)
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return (s[:180] if s else "item") or "item"


def ensure_unique_slug(db: Session, name: str) -> str:
    base = slug_base_from_name(name)
    candidate = base
    for _ in range(50):
        exists = db.query(Product.id).filter(Product.slug == candidate).first()
        if not exists:
            return candidate[:200]
        candidate = f"{base}-{uuid.uuid4().hex[:8]}"[:200]
    return f"{base}-{uuid.uuid4().hex}"[:200]


def ensure_unique_sku(db: Session) -> str:
    for _ in range(50):
        cand = f"SKU-{uuid.uuid4().hex[:10].upper()}"
        if not db.query(Product.id).filter(Product.sku == cand).first():
            return cand[:50]
    return f"SKU-{uuid.uuid4().hex}"[:50]
