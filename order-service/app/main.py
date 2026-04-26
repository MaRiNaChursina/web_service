import json
import os
from contextlib import asynccontextmanager
from typing import Annotated, Any, Optional

import httpx
from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response
from fastapi.security import APIKeyHeader
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

from .database import Base, SessionLocal, engine
from .models import Cart, CartItem, Order, OrderItem
from .schemas import CartItemAdd, CartItemQuantity, OrderCreate

PRODUCT_URL = os.getenv("PRODUCT_SERVICE_URL", "http://localhost:3001").rstrip("/")

session_api = APIKeyHeader(
    name="X-Session-Id",
    description="Один и тот же UUID для корзины и заказов. В Swagger: кнопка **Authorize** и вставьте значение.",
    auto_error=False,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = httpx.Client(timeout=30.0)
    Base.metadata.create_all(bind=engine)
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
        {"name": "Cart", "description": "Корзина: просмотр, позиции, очистка"},
        {"name": "Orders", "description": "Оформление и просмотр заказов"},
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
    r = http.patch(
        f"{PRODUCT_URL}/api/v1/products/{product_id}/stock",
        json={"stock_quantity": stock},
    )
    if not r.is_success:
        raise RuntimeError(f"Stock update failed: {r.status_code} {r.text}")


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


def format_order(order: Order) -> dict:
    return {
        "id": order.id,
        "order_number": order.order_number,
        "status": order.status,
        "total_amount": order.total_amount,
        "payment_method": order.payment_method,
        "payment_status": order.payment_status,
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


@app.get("/", tags=["Meta"])
def root():
    return {
        "service": "lampshop-order-service",
        "api": "/api/v1",
        "stack": "Python FastAPI",
        "swagger_ui": "/docs",
        "redoc": "/redoc",
        "product_service": PRODUCT_URL,
        "note": "В Swagger используйте Authorize → X-Session-Id (UUID).",
    }


@app.get("/favicon.ico", tags=["Meta"])
def favicon():
    return Response(status_code=204)


@app.get("/api/v1/cart", tags=["Cart"])
def cart_get(
    db: Db,
    sid: Sid,
):
    session_id = _norm_session(sid)
    if not session_id:
        return err(400, "SESSION_REQUIRED", "Укажите заголовок X-Session-Id")
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
def cart_clear(db: Db, sid: Sid):
    session_id = _norm_session(sid)
    if not session_id:
        return err(400, "SESSION_REQUIRED", "Укажите заголовок X-Session-Id")
    cart = get_or_create_cart(db, session_id)
    db.query(CartItem).filter(CartItem.cart_id == cart.id).delete()
    db.commit()
    return Response(status_code=204)


@app.post("/api/v1/cart/items", tags=["Cart"])
def cart_add(
    db: Db,
    http: Http,
    body: CartItemAdd,
    sid: Sid,
):
    session_id = _norm_session(sid)
    if not session_id:
        return err(400, "SESSION_REQUIRED", "Укажите заголовок X-Session-Id")
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
    sid: Sid,
):
    session_id = _norm_session(sid)
    if not session_id:
        return err(400, "SESSION_REQUIRED", "Укажите заголовок X-Session-Id")
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
    sid: Sid,
):
    session_id = _norm_session(sid)
    if not session_id:
        return err(400, "SESSION_REQUIRED", "Укажите заголовок X-Session-Id")
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


@app.post("/api/v1/orders", tags=["Orders"])
def orders_create(
    db: Db,
    http: Http,
    body: OrderCreate,
    sid: Sid,
):
    session_id = _norm_session(sid)
    if not session_id:
        return err(400, "SESSION_REQUIRED", "Укажите заголовок X-Session-Id")
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
    from datetime import datetime

    year = datetime.utcnow().year
    prefix = f"ORD-{year}-"
    cnt = db.query(func.count(Order.id)).filter(Order.order_number.like(f"{prefix}%")).scalar() or 0
    order_number = prefix + str(cnt + 1).zfill(4)

    order = Order(
        session_id=session_id,
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

    return JSONResponse(status_code=201, content=format_order(order))


@app.get("/api/v1/orders", tags=["Orders"])
def orders_list(
    db: Db,
    sid: Sid,
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
):
    session_id = _norm_session(sid)
    if not session_id:
        return err(400, "SESSION_REQUIRED", "Укажите заголовок X-Session-Id")
    q = db.query(Order).filter(Order.session_id == session_id)
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
                "total_amount": o.total_amount,
                "payment_status": o.payment_status,
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
    sid: Sid,
):
    session_id = _norm_session(sid)
    if not session_id:
        return err(400, "SESSION_REQUIRED", "Укажите заголовок X-Session-Id")
    order = (
        db.query(Order)
        .options(selectinload(Order.items))
        .filter(Order.id == order_id, Order.session_id == session_id)
        .first()
    )
    if not order:
        return err(404, "NOT_FOUND", "Заказ не найден")
    return format_order(order)


@app.patch("/api/v1/orders/{order_id}/cancel", tags=["Orders"])
def order_cancel(
    order_id: str,
    db: Db,
    http: Http,
    sid: Sid,
):
    session_id = _norm_session(sid)
    if not session_id:
        return err(400, "SESSION_REQUIRED", "Укажите заголовок X-Session-Id")
    order = (
        db.query(Order)
        .options(selectinload(Order.items))
        .filter(Order.id == order_id, Order.session_id == session_id)
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
    return format_order(order)
