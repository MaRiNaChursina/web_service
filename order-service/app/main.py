import csv
import io
import json
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any, Optional

import bcrypt
import httpx
import jwt
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.security import APIKeyHeader
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import exists, func, or_
from sqlalchemy.orm import Session, selectinload

from .database import Base, SessionLocal, engine
from .models import Cart, CartItem, Order, OrderItem, ProductFavorite, ProductReview, User
from .schemas import (
    CartItemAdd,
    CartItemQuantity,
    CartMergeRequest,
    CustomerLogin,
    CustomerRegister,
    FavoriteAdd,
    OrderCreate,
    ProductReviewCreate,
    ProductReviewUpdate,
)

PRODUCT_URL = os.getenv("PRODUCT_SERVICE_URL", "http://localhost:3001").rstrip("/")
JWT_SECRET = os.getenv("JWT_SECRET", "change-me-in-prod")
JWT_ALGORITHM = "HS256"
ACCESS_MINUTES = int(os.getenv("JWT_ACCESS_EXPIRES_MINUTES", "60"))
REFRESH_DAYS = int(os.getenv("JWT_REFRESH_EXPIRES_DAYS", "30"))

ALLOWED_STATUS_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"confirmed", "cancelled"},
    "confirmed": {"processing", "cancelled"},
    "processing": {"shipped", "cancelled"},
    "shipped": {"delivered"},
    "delivered": set(),
    "cancelled": set(),
}

ORDER_STATUS_RU: dict[str, str] = {
    "pending": "Новый",
    "confirmed": "Подтверждён",
    "processing": "В обработке",
    "shipped": "Отправлен",
    "delivered": "Доставлен",
    "cancelled": "Отменён",
}

PAYMENT_METHOD_RU: dict[str, str] = {
    "sbp": "СБП",
    "card": "Карта",
    "cash": "Наличные",
}

PAYMENT_STATUS_RU: dict[str, str] = {
    "unpaid": "Не оплачен",
    "paid": "Оплачен",
    "refunded": "Возврат средств",
}


def _ru_order_status(code: Optional[str]) -> str:
    if not code:
        return "—"
    c = str(code).strip().lower()
    return ORDER_STATUS_RU.get(c, str(code))


def _ru_payment_method(code: Optional[str]) -> str:
    if not code:
        return "—"
    c = str(code).strip().lower()
    return PAYMENT_METHOD_RU.get(c, str(code))


def _ru_payment_status(code: Optional[str]) -> str:
    if not code:
        return "—"
    c = str(code).strip().lower()
    return PAYMENT_STATUS_RU.get(c, str(code))

session_api = APIKeyHeader(
    name="X-Session-Id",
    description="Один и тот же UUID для корзины и заказов. В Swagger: кнопка **Authorize** и вставьте значение.",
    auto_error=False,
)


def _ensure_demo_customer(
    db: Session,
    *,
    email: str,
    password: str,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    phone: Optional[str] = None,
) -> None:
    """Создаёт тестового покупателя, если такого email ещё нет (удобно для локальной демо)."""
    normalized = email.strip().lower()
    if db.query(User).filter(User.email == normalized).first():
        return
    pwd_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")
    db.add(
        User(
            email=normalized,
            password_hash=pwd_hash,
            first_name=first_name,
            last_name=last_name,
            phone=phone,
            role="customer",
            is_blocked=False,
        )
    )
    db.commit()


def _ensure_orders_user_id_column() -> None:
    """SQLite: добавить колонку user_id к orders, если её ещё нет (create_all не меняет существующие таблицы)."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    try:
        cols = [c["name"] for c in insp.get_columns("orders")]
    except Exception:
        return
    if "user_id" in cols:
        return
    try:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE orders ADD COLUMN user_id VARCHAR"))
    except Exception:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = httpx.Client(timeout=30.0)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        _ensure_demo_customer(
            db,
            email="customer@example.com",
            password="User123!",
            first_name="Иван",
            last_name="Покупатель",
            phone="+79001234567",
        )
    finally:
        db.close()
    _ensure_orders_user_id_column()
    yield
    app.state.http.close()


app = FastAPI(
    title="LampShop — Order Service",
    description=(
        "Корзина и заказы (**порт 3002** по ТЗ). Должен быть запущен **Product Service** "
        f"(`{PRODUCT_URL}`).\n\n"
        "### Как тестировать в Swagger\n"
        "1. Откройте [/docs](/docs).\n"
        "2. Нажмите **Authorize** (замок).\n"
        "3. В поле **X-Session-Id** вставьте один и тот же UUID для всех запросов "
        "(например `11111111-1111-1111-1111-111111111111`) и подтвердите.\n"
        "4. Вызовите `GET /api/v1/cart`, затем `POST /api/v1/cart/items` "
        "(возьмите `product_id` из Product Service → GET `/api/v1/products`).\n\n"
        "**Каталог:** [Product Service Swagger](http://127.0.0.1:3001/docs)\n\n"
        "JWT на этом этапе не используется — идентификация гостя через заголовок сессии."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
    servers=[
        {"url": "http://127.0.0.1:3002", "description": "Локально (ТЗ: Order Service → 3002)"},
    ],
    openapi_tags=[
        {"name": "Meta", "description": "Служебные endpoint'ы"},
        {"name": "Auth", "description": "Регистрация и вход покупателя"},
        {"name": "Cart", "description": "Корзина: просмотр, позиции, очистка"},
        {"name": "Orders", "description": "Оформление и просмотр заказов"},
        {"name": "Admin", "description": "Администрирование пользователей и заказов (JWT администратора)"},
        {"name": "Reviews", "description": "Отзывы и средний рейтинг товаров"},
        {"name": "Favorites", "description": "Избранные товары покупателя (JWT)"},
    ],
    swagger_ui_parameters={
        "tryItOutEnabled": True,
        "persistAuthorization": True,
        "displayRequestDuration": True,
    },
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


Db = Annotated[Session, Depends(get_db)]


def get_http(request: Request) -> httpx.Client:
    return request.app.state.http


Http = Annotated[httpx.Client, Depends(get_http)]


Sid = Annotated[Optional[str], Depends(session_api)]
bearer_auth = HTTPBearer(auto_error=False)


def _norm_session(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    s = raw.strip()
    return s or None


def err(status: int, code: str, message: str, details: Any = None) -> JSONResponse:
    body: dict = {"error": {"code": code, "message": message, "status": status}}
    if details is not None:
        body["error"]["details"] = details
    return JSONResponse(status_code=status, content=body)


def service_admin_token() -> str:
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_MINUTES)
    payload = {
        "sub": "order-service",
        "role": "admin",
        "typ": "access",
        "exp": expires_at,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


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


def optional_customer(
    db: Db,
    credentials: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_auth)],
) -> Optional[User]:
    if not credentials or credentials.scheme.lower() != "bearer":
        return None
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None
    if payload.get("typ") == "refresh":
        return None
    if payload.get("role") != "customer":
        return None
    uid = str(payload.get("sub") or "").strip()
    if not uid:
        return None
    u = db.query(User).filter(User.id == uid).first()
    if not u or u.is_blocked:
        return None
    return u


def require_customer_dep(customer: Annotated[Optional[User], Depends(optional_customer)]) -> User:
    if not customer:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Требуется вход покупателя")
    return customer


CustomerUser = Annotated[User, Depends(require_customer_dep)]


def get_or_create_cart(db: Session, session_id: str) -> Cart:
    cart = db.query(Cart).filter(Cart.session_id == session_id).first()
    if not cart:
        cart = Cart(session_id=session_id)
        db.add(cart)
        db.commit()
        db.refresh(cart)
    return cart


def fetch_product(http: httpx.Client, product_id: str) -> Optional[dict]:
    r = http.get(f"{PRODUCT_URL}/api/v1/products/{product_id}")
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def patch_stock(http: httpx.Client, product_id: str, stock: int) -> None:
    token = service_admin_token()
    r = http.patch(
        f"{PRODUCT_URL}/api/v1/products/{product_id}/stock",
        json={"stock_quantity": stock},
        headers={"Authorization": f"Bearer {token}"},
    )
    if not r.is_success:
        raise RuntimeError(f"Stock update failed: {r.status_code} {r.text}")


def restore_order_stock(http: httpx.Client, order: Order) -> None:
    for i in order.items:
        p = fetch_product(http, i.product_id)
        cur = p["stock_quantity"] if p else 0
        patch_stock(http, i.product_id, cur + i.quantity)


def delivery_from_order(order: Order) -> Optional[dict]:
    try:
        a = json.loads(order.delivery_address)
        if isinstance(a, dict):
            return {
                "city": a.get("city"),
                "street": a.get("street"),
                "building": a.get("building"),
                "apartment": a.get("apartment"),
                "postal_code": a.get("postal_code"),
            }
    except json.JSONDecodeError:
        pass
    return None


def format_order(db: Session, order: Order, *, admin: bool = False) -> dict:
    cust: Optional[User] = None
    if order.user_id:
        cust = db.query(User).filter(User.id == order.user_id).first()
    out: dict[str, Any] = {
        "id": order.id,
        "order_number": order.order_number,
        "status": order.status,
        "status_label": _ru_order_status(order.status),
        "total_amount": order.total_amount,
        "payment_method": order.payment_method,
        "payment_method_label": _ru_payment_method(order.payment_method),
        "payment_status": order.payment_status,
        "payment_status_label": _ru_payment_status(order.payment_status),
        "notes": order.notes,
        "delivery_address": delivery_from_order(order),
        "items": [
            {
                "product_id": i.product_id,
                "product_name": i.product_name,
                "product_sku": i.product_sku,
                "quantity": i.quantity,
                "unit_price": i.unit_price,
                "total_price": i.total_price,
            }
            for i in order.items
        ],
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "updated_at": order.updated_at.isoformat() if order.updated_at else None,
    }
    if cust:
        out["customer"] = {
            "id": cust.id,
            "email": cust.email,
            "full_name": customer_display_name(cust),
            "phone": cust.phone,
        }
    if admin:
        out["session_id"] = order.session_id
        if cust:
            parts = [cust.email, customer_display_name(cust)]
            out["buyer_summary"] = " · ".join(p for p in parts if p)
        else:
            out["buyer_summary"] = "Гость (без аккаунта)"
    return out


def format_user_row(u: User) -> dict:
    return {
        "id": u.id,
        "email": u.email,
        "first_name": u.first_name,
        "last_name": u.last_name,
        "phone": u.phone,
        "role": u.role,
        "is_blocked": u.is_blocked,
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }


def customer_display_name(u: User) -> str:
    fn = (u.first_name or "").strip()
    ln = (u.last_name or "").strip()
    label = f"{fn} {ln}".strip()
    return label or (u.email.split("@")[0] if u.email else "Покупатель")


def customer_access_token(u: User) -> str:
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_MINUTES)
    payload = {
        "sub": u.id,
        "email": u.email,
        "role": "customer",
        "typ": "access",
        "exp": expires_at,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def customer_refresh_token(u: User) -> str:
    expires_at = datetime.now(timezone.utc) + timedelta(days=REFRESH_DAYS)
    payload = {
        "sub": u.id,
        "role": "customer",
        "typ": "refresh",
        "exp": expires_at,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def customer_auth_response(u: User) -> dict:
    return {
        "access_token": customer_access_token(u),
        "refresh_token": customer_refresh_token(u),
        "token_type": "bearer",
        "expires_in": ACCESS_MINUTES * 60,
        "email": u.email,
        "display_name": customer_display_name(u),
        "user_id": u.id,
    }


@app.post("/api/v1/auth/register", tags=["Auth"])
def auth_register(body: CustomerRegister, db: Db):
    email = body.email.strip().lower()
    if db.query(User).filter(User.email == email).first():
        return err(409, "EMAIL_EXISTS", "Этот email уже зарегистрирован")
    fn = body.first_name.strip() if body.first_name and body.first_name.strip() else None
    ln = body.last_name.strip() if body.last_name and body.last_name.strip() else None
    pwd_hash = bcrypt.hashpw(body.password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")
    u = User(
        email=email,
        password_hash=pwd_hash,
        first_name=fn,
        last_name=ln,
        role="customer",
        is_blocked=False,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return customer_auth_response(u)


@app.post("/api/v1/auth/login", tags=["Auth"])
def auth_login(body: CustomerLogin, db: Db):
    email = body.email.strip().lower()
    u = db.query(User).filter(User.email == email).first()
    if not u or u.role != "customer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Неверный email или пароль")
    if u.is_blocked:
        return err(403, "USER_BLOCKED", "Учётная запись заблокирована")
    try:
        ok = bcrypt.checkpw(body.password.encode("utf-8"), u.password_hash.encode("utf-8"))
    except ValueError:
        ok = False
    if not ok:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Неверный email или пароль")
    return customer_auth_response(u)


@app.post("/api/v1/auth/refresh", tags=["Auth"])
def auth_refresh(body: dict, db: Db):
    token = str(body.get("refresh_token") or "").strip()
    if not token:
        return err(400, "VALIDATION_ERROR", "Нужен refresh_token")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        return err(401, "UNAUTHORIZED", "Неверный refresh_token")
    if payload.get("typ") != "refresh" or payload.get("role") != "customer":
        return err(400, "VALIDATION_ERROR", "Ожидался refresh_token покупателя")
    uid = str(payload.get("sub") or "").strip()
    u = db.query(User).filter(User.id == uid).first()
    if not u or u.is_blocked:
        return err(401, "UNAUTHORIZED", "Пользователь не найден")
    return customer_auth_response(u)


def review_author_name(u: User) -> str:
    name = customer_display_name(u)
    return name if name else "Покупатель"


def map_review_public(r: ProductReview, u: User) -> dict:
    return {
        "id": r.id,
        "product_id": r.product_id,
        "rating": r.rating,
        "text": r.text or "",
        "author_name": review_author_name(u),
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


def map_review_mine(r: ProductReview) -> dict:
    return {
        "id": r.id,
        "product_id": r.product_id,
        "rating": r.rating,
        "text": r.text or "",
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


def rating_stats_for_products(db: Session, product_ids: list[str]) -> dict[str, dict]:
    if not product_ids:
        return {}
    rows = (
        db.query(
            ProductReview.product_id,
            func.avg(ProductReview.rating),
            func.count(ProductReview.id),
        )
        .filter(ProductReview.product_id.in_(product_ids))
        .group_by(ProductReview.product_id)
        .all()
    )
    out: dict[str, dict] = {}
    for pid, avg, cnt in rows:
        out[str(pid)] = {
            "average_rating": round(float(avg or 0), 1),
            "review_count": int(cnt or 0),
        }
    for pid in product_ids:
        out.setdefault(str(pid), {"average_rating": 0.0, "review_count": 0})
    return out


@app.get("/api/v1/products/ratings", tags=["Reviews"])
def products_ratings_batch(
    db: Db,
    ids: str = Query("", description="ID товаров через запятую"),
):
    product_ids = [x.strip() for x in ids.split(",") if x.strip()]
    if len(product_ids) > 200:
        return err(400, "VALIDATION_ERROR", "Не более 200 ID за запрос")
    return {"data": rating_stats_for_products(db, product_ids)}


@app.get("/api/v1/products/{product_id}/reviews/summary", tags=["Reviews"])
def product_review_summary(product_id: str, db: Db):
    stats = rating_stats_for_products(db, [product_id])
    return stats.get(product_id, {"average_rating": 0.0, "review_count": 0})


@app.get("/api/v1/products/{product_id}/reviews", tags=["Reviews"])
def product_reviews_list(
    product_id: str,
    db: Db,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=50),
):
    q = (
        db.query(ProductReview, User)
        .join(User, User.id == ProductReview.user_id)
        .filter(ProductReview.product_id == product_id)
        .order_by(ProductReview.created_at.desc())
    )
    total = q.count()
    rows = q.offset((page - 1) * limit).limit(limit).all()
    return {
        "data": [map_review_public(r, u) for r, u in rows],
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "total_pages": max(1, (total + limit - 1) // limit),
        },
    }


@app.get("/api/v1/products/{product_id}/reviews/me", tags=["Reviews"])
def product_review_mine(
    product_id: str,
    db: Db,
    customer: Annotated[Optional[User], Depends(optional_customer)],
):
    if not customer:
        return {"review": None}
    r = (
        db.query(ProductReview)
        .filter(ProductReview.product_id == product_id, ProductReview.user_id == customer.id)
        .first()
    )
    return {"review": map_review_mine(r) if r else None}


@app.post("/api/v1/products/{product_id}/reviews", tags=["Reviews"])
def product_review_create(
    product_id: str,
    db: Db,
    http: Http,
    body: ProductReviewCreate,
    customer: CustomerUser,
):
    existing = (
        db.query(ProductReview)
        .filter(ProductReview.product_id == product_id, ProductReview.user_id == customer.id)
        .first()
    )
    if existing:
        return err(
            409,
            "REVIEW_EXISTS",
            "Вы уже оставляли отзыв на этот товар. Измените или удалите существующий.",
        )
    product = fetch_product(http, product_id)
    if not product or product.get("is_active") is False:
        return err(404, "PRODUCT_NOT_FOUND", "Товар не найден в каталоге")
    r = ProductReview(
        user_id=customer.id,
        product_id=product_id,
        rating=body.rating,
        text=(body.text or "").strip() or None,
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return JSONResponse(status_code=201, content={"review": map_review_mine(r)})


@app.put("/api/v1/products/{product_id}/reviews/me", tags=["Reviews"])
def product_review_update(
    product_id: str,
    db: Db,
    body: ProductReviewUpdate,
    customer: CustomerUser,
):
    r = (
        db.query(ProductReview)
        .filter(ProductReview.product_id == product_id, ProductReview.user_id == customer.id)
        .first()
    )
    if not r:
        return err(404, "REVIEW_NOT_FOUND", "Отзыв не найден. Сначала оставьте отзыв.")
    r.rating = body.rating
    r.text = (body.text or "").strip() or None
    db.commit()
    db.refresh(r)
    return {"review": map_review_mine(r)}


@app.delete("/api/v1/products/{product_id}/reviews/me", tags=["Reviews"])
def product_review_delete(product_id: str, db: Db, customer: CustomerUser):
    r = (
        db.query(ProductReview)
        .filter(ProductReview.product_id == product_id, ProductReview.user_id == customer.id)
        .first()
    )
    if not r:
        return err(404, "REVIEW_NOT_FOUND", "Отзыв не найден")
    db.delete(r)
    db.commit()
    return Response(status_code=204)


@app.get("/api/v1/favorites", tags=["Favorites"])
def favorites_list(db: Db, customer: CustomerUser):
    rows = (
        db.query(ProductFavorite.product_id)
        .filter(ProductFavorite.user_id == customer.id)
        .order_by(ProductFavorite.created_at.desc())
        .all()
    )
    return {"product_ids": [r[0] for r in rows]}


@app.post("/api/v1/favorites", tags=["Favorites"])
def favorites_add(db: Db, http: Http, body: FavoriteAdd, customer: CustomerUser):
    product_id = body.product_id.strip()
    product = fetch_product(http, product_id)
    if not product or product.get("is_active") is False:
        return err(404, "PRODUCT_NOT_FOUND", "Товар не найден в каталоге")
    existing = (
        db.query(ProductFavorite)
        .filter(ProductFavorite.user_id == customer.id, ProductFavorite.product_id == product_id)
        .first()
    )
    if existing:
        return {"product_id": product_id, "is_favorite": True}
    db.add(ProductFavorite(user_id=customer.id, product_id=product_id))
    db.commit()
    return JSONResponse(status_code=201, content={"product_id": product_id, "is_favorite": True})


@app.delete("/api/v1/favorites/{product_id}", tags=["Favorites"])
def favorites_remove(product_id: str, db: Db, customer: CustomerUser):
    db.query(ProductFavorite).filter(
        ProductFavorite.user_id == customer.id,
        ProductFavorite.product_id == product_id,
    ).delete(synchronize_session=False)
    db.commit()
    return Response(status_code=204)


@app.get("/", tags=["Meta"])
def root():
    return {
        "service": "lampshop-order-service",
        "api": "/api/v1",
        "stack": "Python FastAPI",
        "swagger_ui": "/docs",
        "redoc": "/redoc",
        "product_service": PRODUCT_URL,
        "note": "Корзина и заказы покупателя: Authorize → Bearer JWT (роль customer). Админ-заказы: admin JWT.",
    }


@app.get("/favicon.ico", tags=["Meta"])
def favicon():
    return Response(status_code=204)


@app.get("/api/v1/cart", tags=["Cart"])
def cart_get(
    db: Db,
    customer: CustomerUser,
):
    session_id = customer.id
    cart = get_or_create_cart(db, session_id)
    cart = db.query(Cart).options(selectinload(Cart.items)).filter(Cart.id == cart.id).first()
    items = []
    for i in cart.items:
        sub = round(i.quantity * i.unit_price, 2)
        items.append(
            {
                "id": i.id,
                "product_id": i.product_id,
                "product_name": i.product_name,
                "quantity": i.quantity,
                "unit_price": i.unit_price,
                "subtotal": sub,
            }
        )
    total = round(sum(x["subtotal"] for x in items), 2)
    items_count = sum(i["quantity"] for i in items)
    return {"id": cart.id, "items": items, "total": total, "items_count": items_count}


@app.delete("/api/v1/cart", tags=["Cart"])
def cart_clear(db: Db, customer: CustomerUser):
    session_id = customer.id
    cart = get_or_create_cart(db, session_id)
    db.query(CartItem).filter(CartItem.cart_id == cart.id).delete()
    db.commit()
    return Response(status_code=204)


@app.post("/api/v1/cart/items", tags=["Cart"])
def cart_add(
    db: Db,
    http: Http,
    body: CartItemAdd,
    customer: CustomerUser,
):
    session_id = customer.id
    product_id, qty = body.product_id, body.quantity
    product = fetch_product(http, product_id)
    if not product or product.get("is_active") is False:
        return err(404, "PRODUCT_NOT_FOUND", "Товар не найден в каталоге")
    cart = get_or_create_cart(db, session_id)
    existing = (
        db.query(CartItem)
        .filter(CartItem.cart_id == cart.id, CartItem.product_id == product_id)
        .first()
    )
    if existing:
        existing.quantity += qty
        existing.unit_price = product["price"]
        existing.product_name = product["name"]
        db.commit()
        db.refresh(existing)
        row = existing
        code = 200
    else:
        row = CartItem(
            cart_id=cart.id,
            product_id=product_id,
            product_name=product["name"],
            quantity=qty,
            unit_price=product["price"],
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        code = 201
    return JSONResponse(
        status_code=code,
        content={
            "id": row.id,
            "product_id": row.product_id,
            "quantity": row.quantity,
            "unit_price": row.unit_price,
        },
    )


@app.put("/api/v1/cart/items/{item_id}", tags=["Cart"])
def cart_update_item(
    item_id: str,
    db: Db,
    body: CartItemQuantity,
    customer: CustomerUser,
):
    session_id = customer.id
    qty = body.quantity
    cart = get_or_create_cart(db, session_id)
    item = (
        db.query(CartItem)
        .filter(CartItem.id == item_id, CartItem.cart_id == cart.id)
        .first()
    )
    if not item:
        return err(404, "CART_ITEM_NOT_FOUND", "Позиция не найдена в корзине")
    item.quantity = qty
    db.commit()
    db.refresh(item)
    return {
        "id": item.id,
        "product_id": item.product_id,
        "quantity": item.quantity,
        "unit_price": item.unit_price,
    }


@app.delete("/api/v1/cart/items/{item_id}", tags=["Cart"])
def cart_delete_item(
    item_id: str,
    db: Db,
    customer: CustomerUser,
):
    session_id = customer.id
    cart = get_or_create_cart(db, session_id)
    item = (
        db.query(CartItem)
        .filter(CartItem.id == item_id, CartItem.cart_id == cart.id)
        .first()
    )
    if not item:
        return err(404, "CART_ITEM_NOT_FOUND", "Позиция не найдена в корзине")
    db.delete(item)
    db.commit()
    return Response(status_code=204)


@app.post("/api/v1/cart/merge", tags=["Cart"])
def cart_merge(
    db: Db,
    http: Http,
    body: CartMergeRequest,
    customer: CustomerUser,
):
    from_sid = _norm_session(body.from_session_id)
    if not from_sid or from_sid == customer.id:
        return {"merged_items": 0}
    from_cart = db.query(Cart).options(selectinload(Cart.items)).filter(Cart.session_id == from_sid).first()
    if not from_cart or not from_cart.items:
        return {"merged_items": 0}
    to_cart = get_or_create_cart(db, customer.id)
    merged = 0
    for line in list(from_cart.items):
        product = fetch_product(http, line.product_id)
        if not product or product.get("is_active") is False:
            continue
        existing = (
            db.query(CartItem)
            .filter(CartItem.cart_id == to_cart.id, CartItem.product_id == line.product_id)
            .first()
        )
        if existing:
            existing.quantity += line.quantity
            existing.unit_price = product["price"]
            existing.product_name = product["name"]
        else:
            db.add(
                CartItem(
                    cart_id=to_cart.id,
                    product_id=line.product_id,
                    product_name=product["name"],
                    quantity=line.quantity,
                    unit_price=product["price"],
                )
            )
        merged += line.quantity
    db.query(CartItem).filter(CartItem.cart_id == from_cart.id).delete()
    db.commit()
    return {"merged_items": merged}


@app.post("/api/v1/orders", tags=["Orders"])
def orders_create(
    db: Db,
    http: Http,
    body: OrderCreate,
    customer: CustomerUser,
):
    session_id = customer.id
    addr = body.delivery_address

    cart = get_or_create_cart(db, session_id)
    cart = db.query(Cart).options(selectinload(Cart.items)).filter(Cart.id == cart.id).first()
    if not cart.items:
        return err(400, "EMPTY_CART", "Корзина пуста")

    delivery = {
        "city": addr.city,
        "street": addr.street,
        "building": addr.building,
        "apartment": addr.apartment,
        "postal_code": addr.postal_code,
    }

    items_with = []
    for line in cart.items:
        p = fetch_product(http, line.product_id)
        if not p or p.get("is_active") is False:
            return err(400, "PRODUCT_NOT_FOUND", f"Товар {line.product_id} недоступен")
        avail = p["stock_quantity"]
        if line.quantity > avail:
            return err(
                422,
                "INSUFFICIENT_STOCK",
                "Недостаточно товара на складе",
                {
                    "product_id": line.product_id,
                    "product_name": p["name"],
                    "requested": line.quantity,
                    "available": avail,
                },
            )
        unit = p["price"]
        total_price = round(unit * line.quantity, 2)
        items_with.append({"line": line, "p": p, "unit": unit, "total_price": total_price})

    total_amount = round(sum(x["total_price"] for x in items_with), 2)

    year = datetime.utcnow().year
    prefix = f"ORD-{year}-"
    cnt = db.query(func.count(Order.id)).filter(Order.order_number.like(f"{prefix}%")).scalar() or 0
    order_number = prefix + str(cnt + 1).zfill(4)

    order = Order(
        session_id=session_id,
        user_id=customer.id,
        order_number=order_number,
        status="pending",
        total_amount=total_amount,
        payment_method=body.payment_method,
        payment_status="unpaid",
        notes=body.notes,
        delivery_address=json.dumps(delivery, ensure_ascii=False),
    )
    db.add(order)
    db.flush()
    for x in items_with:
        db.add(
            OrderItem(
                order_id=order.id,
                product_id=x["line"].product_id,
                product_name=x["p"]["name"],
                product_sku=x["p"]["sku"],
                quantity=x["line"].quantity,
                unit_price=x["unit"],
                total_price=x["total_price"],
            )
        )
    db.query(CartItem).filter(CartItem.cart_id == cart.id).delete()
    db.commit()
    db.refresh(order)
    order = db.query(Order).options(selectinload(Order.items)).filter(Order.id == order.id).first()

    for x in items_with:
        new_stock = x["p"]["stock_quantity"] - x["line"].quantity
        patch_stock(http, x["line"].product_id, new_stock)

    return JSONResponse(status_code=201, content=format_order(db, order))


@app.get("/api/v1/orders", tags=["Orders"])
def orders_list(
    db: Db,
    customer: CustomerUser,
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
):
    q = db.query(Order).filter(Order.user_id == customer.id)
    total = q.count()
    rows = (
        q.options(selectinload(Order.items))
        .order_by(Order.created_at.desc())
        .offset((page - 1) * limit)
        .limit(limit)
        .all()
    )
    return {
        "data": [
            {
                "id": o.id,
                "order_number": o.order_number,
                "status": o.status,
                "status_label": _ru_order_status(o.status),
                "total_amount": o.total_amount,
                "payment_method": o.payment_method,
                "payment_method_label": _ru_payment_method(o.payment_method),
                "payment_status": o.payment_status,
                "payment_status_label": _ru_payment_status(o.payment_status),
                "items_count": sum(i.quantity for i in o.items),
                "created_at": o.created_at.isoformat() if o.created_at else None,
            }
            for o in rows
        ],
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "total_pages": max(1, (total + limit - 1) // limit),
        },
    }


@app.get("/api/v1/orders/{order_id}", tags=["Orders"])
def order_get(
    order_id: str,
    db: Db,
    customer: CustomerUser,
):
    order = (
        db.query(Order)
        .options(selectinload(Order.items))
        .filter(Order.id == order_id, Order.user_id == customer.id)
        .first()
    )
    if not order:
        return err(404, "NOT_FOUND", "Заказ не найден")
    return format_order(db, order)


@app.patch("/api/v1/orders/{order_id}/cancel", tags=["Orders"])
def order_cancel(
    order_id: str,
    db: Db,
    http: Http,
    customer: CustomerUser,
):
    order = (
        db.query(Order)
        .options(selectinload(Order.items))
        .filter(Order.id == order_id, Order.user_id == customer.id)
        .first()
    )
    if not order:
        return err(404, "NOT_FOUND", "Заказ не найден")
    if order.status != "pending":
        return err(
            400,
            "INVALID_STATUS_TRANSITION",
            f"Нельзя отменить заказ в статусе '{order.status}'",
        )
    order.status = "cancelled"
    db.commit()
    for i in order.items:
        p = fetch_product(http, i.product_id)
        cur = p["stock_quantity"] if p else 0
        patch_stock(http, i.product_id, cur + i.quantity)
    db.refresh(order)
    order = db.query(Order).options(selectinload(Order.items)).filter(Order.id == order.id).first()
    return format_order(db, order)


@app.get("/api/v1/admin/users", tags=["Admin"])
def admin_users_list(
    db: Db,
    _: dict = Depends(require_admin),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    email: str = Query(""),
):
    q = db.query(User)
    if email.strip():
        q = q.filter(User.email.contains(email.strip()))
    total = q.count()
    rows = q.order_by(User.created_at.desc()).offset((page - 1) * limit).limit(limit).all()
    return {
        "data": [format_user_row(u) for u in rows],
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "total_pages": max(1, (total + limit - 1) // limit),
        },
    }


@app.get("/api/v1/admin/users/{user_id}", tags=["Admin"])
def admin_user_detail(user_id: str, db: Db, _: dict = Depends(require_admin)):
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        return err(404, "NOT_FOUND", "Пользователь не найден")
    return format_user_row(u)


@app.patch("/api/v1/admin/users/{user_id}/block", tags=["Admin"])
def admin_user_block(user_id: str, db: Db, body: dict, _: dict = Depends(require_admin)):
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        return err(404, "NOT_FOUND", "Пользователь не найден")
    blocked = body.get("blocked")
    if not isinstance(blocked, bool):
        return err(400, "VALIDATION_ERROR", "Поле blocked должно быть boolean")
    u.is_blocked = blocked
    db.commit()
    return format_user_row(u)


def _admin_orders_filtered_query(
    db: Session,
    status_filter: Optional[str],
    date_from: Optional[date],
    date_to: Optional[date],
    order_number_q: str,
    customer_q: str = "",
):
    q = db.query(Order)
    if status_filter and status_filter.strip():
        q = q.filter(Order.status == status_filter.strip())
    if date_from is not None:
        q = q.filter(func.date(Order.created_at) >= date_from)
    if date_to is not None:
        q = q.filter(func.date(Order.created_at) <= date_to)
    on = (order_number_q or "").strip()
    if on:
        q = q.filter(Order.order_number.contains(on))
    cq = (customer_q or "").strip()
    if cq:
        pat = f"%{cq}%"
        cust_match = exists().where(
            User.id == Order.user_id,
            or_(
                User.email.ilike(pat),
                User.first_name.ilike(pat),
                User.last_name.ilike(pat),
                User.phone.ilike(pat),
            ),
        )
        q = q.filter(or_(Order.order_number.contains(cq), cust_match))
    return q


def _admin_orders_csv_response(
    db: Session,
    status_filter: Optional[str],
    date_from: Optional[date],
    date_to: Optional[date],
    order_number: str,
    customer_q: str,
) -> Response:
    q = _admin_orders_filtered_query(db, status_filter, date_from, date_to, order_number, customer_q)
    orders = q.options(selectinload(Order.items)).order_by(Order.created_at.desc()).limit(5000).all()
    user_ids = [o.user_id for o in orders if o.user_id]
    users_by_id: dict[str, User] = {}
    if user_ids:
        for u in db.query(User).filter(User.id.in_(user_ids)).all():
            users_by_id[u.id] = u
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(
        [
            "Номер",
            "Статус",
            "Создан",
            "Сумма",
            "Email клиента",
            "Клиент ФИО",
            "Телефон",
            "Позиции (кратко)",
        ]
    )
    for o in orders:
        cust = users_by_id.get(o.user_id) if o.user_id else None
        items_txt = "; ".join(f"{i.product_name} x{i.quantity}" for i in o.items[:8])
        if len(o.items) > 8:
            items_txt += " …"
        w.writerow(
            [
                o.order_number,
                _ru_order_status(o.status),
                o.created_at.isoformat() if o.created_at else "",
                str(o.total_amount).replace(".", ","),
                cust.email if cust else "",
                customer_display_name(cust) if cust else "",
                (cust.phone or "") if cust else "",
                items_txt,
            ]
        )
    payload = "\ufeff" + buf.getvalue()
    return Response(
        content=payload.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="orders_export.csv"'},
    )


@app.get("/api/v1/admin/order-export", tags=["Orders"])
def admin_order_export_csv(
    db: Db,
    _: dict = Depends(require_admin),
    status_filter: Optional[str] = Query(None, alias="status"),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    order_number: str = Query("", max_length=40),
    customer_q: str = Query("", max_length=120),
):
    """Выгрузка CSV; путь без «/orders/…», чтобы не пересекаться с /orders/{order_id} на старых деплоях."""
    return _admin_orders_csv_response(db, status_filter, date_from, date_to, order_number, customer_q)


@app.get("/api/v1/admin/orders", tags=["Orders"])
def admin_orders_list(
    db: Db,
    _: dict = Depends(require_admin),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    status_filter: Optional[str] = Query(None, alias="status"),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    order_number: str = Query("", max_length=40),
    customer_q: str = Query("", max_length=120),
):
    q = _admin_orders_filtered_query(db, status_filter, date_from, date_to, order_number, customer_q)
    total = q.count()
    rows = (
        q.options(selectinload(Order.items))
        .order_by(Order.created_at.desc())
        .offset((page - 1) * limit)
        .limit(limit)
        .all()
    )
    return {
        "data": [format_order(db, o, admin=True) for o in rows],
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "total_pages": max(1, (total + limit - 1) // limit),
        },
    }


@app.get("/api/v1/admin/orders/export", tags=["Orders"])
@app.get("/api/v1/admin/orders/export.csv", tags=["Orders"])
def admin_orders_export_csv(
    db: Db,
    _: dict = Depends(require_admin),
    status_filter: Optional[str] = Query(None, alias="status"),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    order_number: str = Query("", max_length=40),
    customer_q: str = Query("", max_length=120),
):
    return _admin_orders_csv_response(db, status_filter, date_from, date_to, order_number, customer_q)


@app.get("/api/v1/admin/orders/{order_id}", tags=["Orders"])
def admin_order_get(order_id: str, db: Db, _: dict = Depends(require_admin)):
    order = db.query(Order).options(selectinload(Order.items)).filter(Order.id == order_id).first()
    if not order:
        return err(404, "NOT_FOUND", "Заказ не найден")
    return format_order(db, order, admin=True)


@app.patch("/api/v1/admin/orders/{order_id}/status", tags=["Orders"])
def admin_order_status_update(
    order_id: str,
    body: dict,
    db: Db,
    http: Http,
    _: dict = Depends(require_admin),
):
    next_status = str(body.get("status", "")).strip().lower()
    order = db.query(Order).options(selectinload(Order.items)).filter(Order.id == order_id).first()
    if not order:
        return err(404, "NOT_FOUND", "Заказ не найден")
    old = order.status
    allowed = ALLOWED_STATUS_TRANSITIONS.get(old, set())
    if next_status not in allowed:
        return err(
            400,
            "INVALID_STATUS_TRANSITION",
            f"Нельзя перевести заказ из '{old}' в '{next_status}'",
        )
    if next_status == "cancelled" and old in {"pending", "confirmed", "processing"}:
        restore_order_stock(http, order)
    order.status = next_status
    db.commit()
    db.refresh(order)
    order = db.query(Order).options(selectinload(Order.items)).filter(Order.id == order.id).first()
    return format_order(db, order, admin=True)
