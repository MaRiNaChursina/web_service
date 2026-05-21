import os
from typing import Annotated, Any, Optional

import jwt
from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .database import Base, SessionLocal, engine
from .models import Category, Product, ProductImage
from .slug_sku import ensure_unique_sku, ensure_unique_slug
from .schemas import (
    CategoryCreate,
    CategoryUpdate,
    ProductCreate,
    ProductImageCreate,
    ProductUpdate,
    StockPatch,
)

app = FastAPI(
    title="LampShop — Product Service",
    description=(
        "Каталог товаров и категорий (**порт 3001** по ТЗ).\n\n"
        "- **Swagger UI:** [/docs](/docs) — здесь можно вызвать все методы.\n"
        "- **ReDoc:** [/redoc](/redoc)\n"
        "- **OpenAPI JSON:** [/openapi.json](/openapi.json)\n\n"
        "**Order Service** (корзина и заказы): запустите на [http://127.0.0.1:3002/docs](http://127.0.0.1:3002/docs).\n\n"
        "Перед тестами каталога выполните сид: `python -m app.seed` из каталога `product-service`."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    servers=[
        {"url": "http://127.0.0.1:3001", "description": "Локально (ТЗ: Product Service → 3001)"},
    ],
    openapi_tags=[
        {"name": "Meta", "description": "Служебные endpoint'ы"},
        {"name": "Categories", "description": "Категории: список, товары в категории, CRUD"},
        {
            "name": "Products",
            "description": "Товары: публичный каталог (GET) и админ-операции с Bearer JWT (POST/PUT/PATCH/DELETE, /api/v1/admin/products)",
        },
    ],
    swagger_ui_parameters={
        "tryItOutEnabled": True,
        "persistAuthorization": True,
        "displayRequestDuration": True,
    },
)


@app.on_event("startup")
def startup() -> None:
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


Db = Annotated[Session, Depends(get_db)]
bearer_auth = HTTPBearer(auto_error=False)

JWT_SECRET = os.getenv("JWT_SECRET", "change-me-in-prod")
JWT_ALGORITHM = "HS256"
def err(status: int, code: str, message: str, details: Any = None) -> JSONResponse:
    body: dict = {"error": {"code": code, "message": message, "status": status}}
    if details is not None:
        body["error"]["details"] = details
    return JSONResponse(status_code=status, content=body)


def require_admin(credentials: HTTPAuthorizationCredentials = Depends(bearer_auth)) -> dict:
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc
    if payload.get("typ") == "refresh":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Access token required")
    if payload.get("role") != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")
    return payload


def sort_images(product: Product) -> list[ProductImage]:
    return sorted(
        product.images,
        key=lambda i: (i.sort_order, 0 if i.is_primary else 1, i.id or ""),
    )


def map_list_row(p: Product) -> dict:
    imgs = sort_images(p)
    primary = next((i.url for i in imgs if i.is_primary), None) or (imgs[0].url if imgs else None)
    return {
        "id": p.id,
        "name": p.name,
        "slug": p.slug,
        "price": p.price,
        "sku": p.sku,
        "stock_quantity": p.stock_quantity,
        "power_watts": p.power_watts,
        "base_type": p.base_type,
        "color_temp_k": p.color_temp_k,
        "category": {"id": p.category.id, "name": p.category.name},
        "primary_image": primary,
    }


def map_admin_list_row(p: Product) -> dict:
    row = map_list_row(p)
    row["is_active"] = p.is_active
    row["description"] = p.description
    return row


def map_detail(p: Product) -> dict:
    imgs = sort_images(p)
    return {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "price": p.price,
        "sku": p.sku,
        "stock_quantity": p.stock_quantity,
        "power_watts": p.power_watts,
        "base_type": p.base_type,
        "color_temp_k": p.color_temp_k,
        "is_active": p.is_active,
        "category": {"id": p.category.id, "name": p.category.name, "slug": p.category.slug},
        "images": [
            {"url": i.url, "alt_text": i.alt_text, "is_primary": i.is_primary, "sort_order": i.sort_order}
            for i in imgs
        ],
    }


def list_active_products(
    db: Session,
    page: int,
    limit: int,
    category_id: Optional[str],
    sort: str,
    q: str,
) -> JSONResponse:
    q_obj = db.query(Product).options(selectinload(Product.category), selectinload(Product.images)).filter(
        Product.is_active.is_(True)
    )
    if category_id:
        q_obj = q_obj.filter(Product.category_id == category_id)
    if q:
        q_obj = q_obj.filter(Product.name.contains(q))
    total = q_obj.count()
    order_by = [Product.created_at.desc()]
    if sort == "price_asc":
        order_by = [Product.price.asc()]
    elif sort == "price_desc":
        order_by = [Product.price.desc()]
    elif sort == "name_asc":
        order_by = [Product.name.asc()]
    rows = (
        q_obj.order_by(*order_by)
        .offset((page - 1) * limit)
        .limit(limit)
        .all()
    )
    return JSONResponse(
        {
            "data": [map_list_row(p) for p in rows],
            "pagination": {
                "page": page,
                "limit": limit,
                "total": total,
                "total_pages": max(1, (total + limit - 1) // limit),
            },
        }
    )


@app.get("/", tags=["Meta"])
def root():
    return {
        "service": "lampshop-product-service",
        "api": "/api/v1",
        "stack": "Python FastAPI",
        "swagger_ui": "/docs",
        "redoc": "/redoc",
        "openapi": "/openapi.json",
        "examples": ["GET /api/v1/categories", "GET /api/v1/products?page=1&limit=20"],
    }


@app.get("/favicon.ico", tags=["Meta"])
def favicon():
    return Response(status_code=204    )


def list_admin_products(
    db: Session,
    page: int,
    limit: int,
    category_id: Optional[str],
    sort: str,
    q: str,
    include_inactive: bool = False,
) -> JSONResponse:
    q_obj = db.query(Product).options(selectinload(Product.category), selectinload(Product.images))
    if not include_inactive:
        q_obj = q_obj.filter(Product.is_active.is_(True))
    if category_id:
        q_obj = q_obj.filter(Product.category_id == category_id)
    if q:
        q_obj = q_obj.filter(Product.name.contains(q))
    total = q_obj.count()
    order_by = [Product.created_at.desc()]
    if sort == "price_asc":
        order_by = [Product.price.asc()]
    elif sort == "price_desc":
        order_by = [Product.price.desc()]
    elif sort == "name_asc":
        order_by = [Product.name.asc()]
    rows = (
        q_obj.order_by(*order_by)
        .offset((page - 1) * limit)
        .limit(limit)
        .all()
    )
    return JSONResponse(
        {
            "data": [map_admin_list_row(p) for p in rows],
            "pagination": {
                "page": page,
                "limit": limit,
                "total": total,
                "total_pages": max(1, (total + limit - 1) // limit),
            },
        }
    )


@app.get("/api/v1/categories", tags=["Categories"])
def categories_list(db: Db):
    cats = db.query(Category).order_by(Category.name).all()
    out = []
    for c in cats:
        cnt = (
            db.query(func.count(Product.id))
            .filter(Product.category_id == c.id, Product.is_active.is_(True))
            .scalar()
        )
        out.append({"id": c.id, "name": c.name, "slug": c.slug, "product_count": cnt or 0})
    return out


@app.get("/api/v1/categories/{category_id}/products", tags=["Categories"])
def categories_products(
    category_id: str,
    db: Db,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    sort: str = Query(""),
    q: str = Query(""),
):
    if not db.query(Category.id).filter(Category.id == category_id).first():
        return err(404, "NOT_FOUND", "Категория не найдена")
    return list_active_products(db, page, limit, category_id, sort, q)


@app.post("/api/v1/categories", tags=["Categories"])
def categories_create(db: Db, body: CategoryCreate, _: dict = Depends(require_admin)):
    try:
        c = Category(name=body.name, slug=body.slug, description=body.description)
        db.add(c)
        db.commit()
        db.refresh(c)
        return JSONResponse(
            status_code=201,
            content={
                "id": c.id,
                "name": c.name,
                "slug": c.slug,
                "description": c.description,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            },
        )
    except IntegrityError:
        db.rollback()
        return err(409, "CONFLICT", "Категория с таким name или slug уже существует")


@app.put("/api/v1/categories/{category_id}", tags=["Categories"])
def categories_update(category_id: str, db: Db, body: CategoryUpdate, _: dict = Depends(require_admin)):
    c = db.query(Category).filter(Category.id == category_id).first()
    if not c:
        return err(404, "NOT_FOUND", "Категория не найдена")
    try:
        c.name = body.name
        c.slug = body.slug
        c.description = body.description
        db.commit()
        return {"id": c.id, "name": c.name, "slug": c.slug, "description": c.description}
    except IntegrityError:
        db.rollback()
        return err(409, "CONFLICT", "Категория с таким name или slug уже существует")


@app.delete("/api/v1/categories/{category_id}", tags=["Categories"])
def categories_delete(category_id: str, db: Db, _: dict = Depends(require_admin)):
    cnt = db.query(Product).filter(Product.category_id == category_id).count()
    if cnt > 0:
        return err(409, "CONFLICT", "Нельзя удалить категорию с привязанными товарами")
    c = db.query(Category).filter(Category.id == category_id).first()
    if not c:
        return err(404, "NOT_FOUND", "Категория не найдена")
    db.delete(c)
    db.commit()
    return Response(status_code=204)


@app.get("/api/v1/admin/products", tags=["Products"])
def admin_products_list(
    db: Db,
    _: dict = Depends(require_admin),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    category_id: Optional[str] = Query(None),
    sort: str = Query(""),
    q: str = Query(""),
    include_inactive: bool = Query(
        False,
        description="Показать товары, снятые с продажи (is_active=false)",
    ),
):
    return list_admin_products(db, page, limit, category_id, sort, q, include_inactive)


@app.get("/api/v1/products", tags=["Products"])
def products_list(
    db: Db,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    category_id: Optional[str] = Query(None),
    sort: str = Query(""),
    q: str = Query(""),
):
    return list_active_products(db, page, limit, category_id, sort, q)


@app.get("/api/v1/products/slug/{slug}", tags=["Products"])
def product_by_slug(slug: str, db: Db):
    p = (
        db.query(Product)
        .options(selectinload(Product.category), selectinload(Product.images))
        .filter(Product.slug == slug, Product.is_active.is_(True))
        .first()
    )
    if not p:
        return err(404, "PRODUCT_NOT_FOUND", "Товар с указанным slug не найден")
    return map_detail(p)


@app.get("/api/v1/products/{product_id}", tags=["Products"])
def product_by_id(product_id: str, db: Db):
    p = (
        db.query(Product)
        .options(selectinload(Product.category), selectinload(Product.images))
        .filter(Product.id == product_id, Product.is_active.is_(True))
        .first()
    )
    if not p:
        return err(404, "PRODUCT_NOT_FOUND", "Товар с указанным ID не найден")
    return map_detail(p)


@app.post("/api/v1/products", tags=["Products"])
def products_create(db: Db, body: ProductCreate, _: dict = Depends(require_admin)):
    if not db.query(Category.id).filter(Category.id == body.category_id).first():
        return err(400, "CATEGORY_NOT_FOUND", "Категория не найдена")
    try:
        slug = body.slug or ensure_unique_slug(db, body.name)
        sku = body.sku or ensure_unique_sku(db)
        p = Product(
            category_id=body.category_id,
            name=body.name,
            slug=slug,
            description=body.description,
            price=body.price,
            sku=sku,
            stock_quantity=body.stock_quantity,
            power_watts=body.power_watts,
            base_type=body.base_type,
            color_temp_k=body.color_temp_k,
            is_active=True,
        )
        db.add(p)
        db.commit()
        db.refresh(p)
        return JSONResponse(
            status_code=201,
            content={
                "id": p.id,
                "name": p.name,
                "price": p.price,
                "sku": p.sku,
                "is_active": p.is_active,
                "created_at": p.created_at.isoformat() if p.created_at else None,
            },
        )
    except IntegrityError:
        db.rollback()
        return err(409, "CONFLICT", "SKU или slug уже заняты")


@app.put("/api/v1/products/{product_id}", tags=["Products"])
def products_update(product_id: str, db: Db, body: ProductUpdate, _: dict = Depends(require_admin)):
    if not db.query(Category.id).filter(Category.id == body.category_id).first():
        return err(400, "CATEGORY_NOT_FOUND", "Категория не найдена")
    p = (
        db.query(Product)
        .options(selectinload(Product.category), selectinload(Product.images))
        .filter(Product.id == product_id)
        .first()
    )
    if not p:
        return err(404, "PRODUCT_NOT_FOUND", "Товар с указанным ID не найден")
    try:
        p.category_id = body.category_id
        p.name = body.name
        p.slug = body.slug
        p.description = body.description
        p.price = body.price
        p.sku = body.sku
        p.stock_quantity = body.stock_quantity
        p.power_watts = body.power_watts
        p.base_type = body.base_type
        p.color_temp_k = body.color_temp_k
        if body.is_active is not None:
            p.is_active = body.is_active
        db.commit()
        db.refresh(p)
        return map_detail(p)
    except IntegrityError:
        db.rollback()
        return err(409, "CONFLICT", "SKU или slug уже заняты")


@app.patch("/api/v1/products/{product_id}/stock", tags=["Products"])
def products_stock(product_id: str, db: Db, body: StockPatch, _: dict = Depends(require_admin)):
    p = db.query(Product).filter(Product.id == product_id).first()
    if not p:
        return err(404, "PRODUCT_NOT_FOUND", "Товар с указанным ID не найден")
    p.stock_quantity = body.stock_quantity
    db.commit()
    return {"id": p.id, "stock_quantity": p.stock_quantity}


@app.delete("/api/v1/products/{product_id}", tags=["Products"])
def products_delete(product_id: str, db: Db, _: dict = Depends(require_admin)):
    p = db.query(Product).filter(Product.id == product_id).first()
    if not p:
        return err(404, "PRODUCT_NOT_FOUND", "Товар с указанным ID не найден")
    db.query(ProductImage).filter(ProductImage.product_id == product_id).delete(synchronize_session=False)
    db.delete(p)
    db.commit()
    return Response(status_code=204)


@app.post("/api/v1/products/{product_id}/images", tags=["Products"])
def products_add_image(product_id: str, db: Db, body: ProductImageCreate, _: dict = Depends(require_admin)):
    p = db.query(Product).filter(Product.id == product_id).first()
    if not p:
        return err(404, "PRODUCT_NOT_FOUND", "Товар с указанным ID не найден")
    if body.is_primary:
        db.query(ProductImage).filter(ProductImage.product_id == product_id).delete(
            synchronize_session=False
        )
        db.flush()
    img = ProductImage(
        product_id=p.id,
        url=body.url,
        alt_text=body.alt_text,
        is_primary=body.is_primary,
        sort_order=body.sort_order,
    )
    db.add(img)
    db.commit()
    db.refresh(img)
    return JSONResponse(
        status_code=201,
        content={
            "id": img.id,
            "product_id": p.id,
            "url": img.url,
            "alt_text": img.alt_text,
            "is_primary": img.is_primary,
            "sort_order": img.sort_order,
        },
    )
