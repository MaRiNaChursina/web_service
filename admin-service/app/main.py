import json
import os
from calendar import monthrange
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any, Optional

import bcrypt
import httpx
import jwt
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .database import Base, SessionLocal, engine
from .models import AdminUser, AuditLog, OrderStatusHistory

PRODUCT_URL = os.getenv("PRODUCT_SERVICE_URL", "http://127.0.0.1:3001").rstrip("/")
ORDER_URL = os.getenv("ORDER_SERVICE_URL", "http://127.0.0.1:3002").rstrip("/")

JWT_SECRET = os.getenv("JWT_SECRET", "change-me-in-prod")
JWT_ALGORITHM = "HS256"
ACCESS_MINUTES = int(os.getenv("JWT_ACCESS_EXPIRES_MINUTES", "60"))
REFRESH_DAYS = int(os.getenv("JWT_REFRESH_EXPIRES_DAYS", "30"))

BOOTSTRAP_EMAIL = os.getenv("ADMIN_BOOTSTRAP_EMAIL", "admin@lampshop.local")
BOOTSTRAP_PASSWORD = os.getenv("ADMIN_BOOTSTRAP_PASSWORD", "Admin123!")
BOOTSTRAP_NAME = os.getenv("ADMIN_BOOTSTRAP_FULL_NAME", "Администратор")

bearer_auth = HTTPBearer(auto_error=False)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if not db.query(AdminUser).first():
            h = bcrypt.hashpw(BOOTSTRAP_PASSWORD.encode("utf-8"), bcrypt.gensalt(rounds=12))
            db.add(
                AdminUser(
                    email=BOOTSTRAP_EMAIL.lower(),
                    password_hash=h.decode("utf-8"),
                    full_name=BOOTSTRAP_NAME,
                    permissions="[]",
                    is_active=True,
                )
            )
            db.commit()
    finally:
        db.close()
    app.state.http = httpx.Client(timeout=60.0)
    yield
    app.state.http.close()


app = FastAPI(
    title="LampShop — Admin Service",
    description="Панель управления (ТЗ: порт **3003**). Выдаёт JWT и проксирует вызовы к Product/Order.",
    version="1.0.0",
    lifespan=lifespan,
    servers=[{"url": "http://127.0.0.1:3003", "description": "Локально"}],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ALLOW_ORIGINS", "http://127.0.0.1:5173,http://localhost:5173").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


Db = Annotated[Session, Depends(get_db)]


def err(status_code: int, code: str, message: str, details: Any = None) -> JSONResponse:
    body: dict = {"error": {"code": code, "message": message, "status": status_code}}
    if details is not None:
        body["error"]["details"] = details
    return JSONResponse(status_code=status_code, content=body)


def issue_tokens(admin: AdminUser) -> dict:
    now = datetime.now(timezone.utc)
    access_exp = now + timedelta(minutes=ACCESS_MINUTES)
    refresh_exp = now + timedelta(days=REFRESH_DAYS)
    access = jwt.encode(
        {
            "sub": admin.id,
            "email": admin.email,
            "role": "admin",
            "typ": "access",
            "exp": access_exp,
        },
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )
    refresh = jwt.encode(
        {
            "sub": admin.id,
            "typ": "refresh",
            "exp": refresh_exp,
        },
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "bearer",
        "expires_in": ACCESS_MINUTES * 60,
    }


def decode_token(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])


def require_access(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_auth),
) -> dict:
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    try:
        payload = decode_token(credentials.credentials)
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc
    if payload.get("typ") == "refresh":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Access token required")
    if payload.get("role") != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")
    return {**payload, "token": credentials.credentials}


AdminCtx = Annotated[dict, Depends(require_access)]


def get_http(request: Request) -> httpx.Client:
    return request.app.state.http


Http = Annotated[httpx.Client, Depends(get_http)]


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def iso_utc_z(dt: Optional[datetime]) -> Optional[str]:
    """Сериализация времени в ISO UTC с суффиксом Z (наивное значение трактуем как UTC)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _fetch_admin_list_all_pages(
    http: httpx.Client,
    url: str,
    headers: dict[str, str],
    *,
    page_limit: int,
    extra_params: Optional[dict[str, Any]] = None,
) -> list[dict]:
    """Собирает все страницы списка (лимит страницы не больше, чем разрешено downstream API)."""
    extra = dict(extra_params or {})
    out: list[dict] = []
    page = 1
    for _ in range(500):
        params: dict[str, Any] = {"page": page, "limit": page_limit, **extra}
        try:
            r = http.get(url, params=params, headers=headers)
        except httpx.HTTPError:
            break
        if not r.is_success:
            break
        try:
            payload = r.json()
        except Exception:
            break
        if not isinstance(payload, dict):
            break
        chunk = payload.get("data")
        if not isinstance(chunk, list) or not chunk:
            break
        out.extend(chunk)
        pagination = payload.get("pagination") or {}
        total_pages = int(pagination.get("total_pages") or 1)
        if page >= total_pages:
            break
        page += 1
    return out


def _parse_order_datetime_utc(value: Any) -> Optional[datetime]:
    """Парсит created_at заказа в aware UTC (naive из БД трактуем как UTC)."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.fromisoformat(s.replace(" ", "T"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def write_audit(
    db: Session,
    admin_id: str,
    action: str,
    entity_type: str,
    entity_id: str,
    changes: Any = None,
    ip: Optional[str] = None,
) -> None:
    row = AuditLog(
        admin_id=admin_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        changes=json.dumps(changes, ensure_ascii=False) if changes is not None else None,
        ip_address=ip,
    )
    db.add(row)
    db.commit()


def history_for_order(db: Session, order_id: str) -> list[dict]:
    rows = (
        db.query(OrderStatusHistory)
        .filter(OrderStatusHistory.order_id == order_id)
        .order_by(OrderStatusHistory.created_at.asc())
        .all()
    )
    return [
        {
            "old_status": r.old_status,
            "new_status": r.new_status,
            "changed_by": r.changed_by,
            "comment": r.comment,
            "created_at": iso_utc_z(r.created_at),
        }
        for r in rows
    ]


@app.get("/", tags=["Meta"])
def root():
    return {
        "service": "lampshop-admin-service",
        "api": "/api/v1/admin",
        "product_service": PRODUCT_URL,
        "order_service": ORDER_URL,
        "docs": "/docs",
    }


@app.post("/api/v1/admin/auth/login", tags=["Auth"])
def admin_login(body: dict, db: Db, request: Request):
    raw_email = str(body.get("email") or body.get("login") or "").strip().lower()
    password = str(body.get("password") or "")
    if not raw_email or not password:
        return err(400, "VALIDATION_ERROR", "Укажите email и пароль")
    user = db.query(AdminUser).filter(AdminUser.email == raw_email).first()
    if not user or not user.is_active:
        return err(401, "UNAUTHORIZED", "Неверный email или пароль")
    try:
        ok = bcrypt.checkpw(password.encode("utf-8"), user.password_hash.encode("utf-8"))
    except ValueError:
        ok = False
    if not ok:
        return err(401, "UNAUTHORIZED", "Неверный email или пароль")
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    write_audit(db, user.id, "LOGIN", "admin_user", user.id, None, request.client.host if request.client else None)
    return issue_tokens(user)


@app.post("/api/v1/admin/auth/refresh", tags=["Auth"])
def admin_refresh(body: dict, db: Db):
    token = str(body.get("refresh_token") or "").strip()
    if not token:
        return err(400, "VALIDATION_ERROR", "Нужен refresh_token")
    try:
        payload = decode_token(token)
    except jwt.PyJWTError:
        return err(401, "UNAUTHORIZED", "Неверный refresh_token")
    if payload.get("typ") != "refresh":
        return err(400, "VALIDATION_ERROR", "Ожидался refresh_token")
    admin_id = payload.get("sub")
    user = db.query(AdminUser).filter(AdminUser.id == admin_id).first()
    if not user or not user.is_active:
        return err(401, "UNAUTHORIZED", "Пользователь не найден")
    return issue_tokens(user)


@app.get("/api/v1/admin/dashboard", tags=["Dashboard"])
def admin_dashboard(ctx: AdminCtx, http: Http):
    token = ctx["token"]
    hdrs = auth_header(token)
    products = _fetch_admin_list_all_pages(
        http,
        f"{PRODUCT_URL}/api/v1/admin/products",
        hdrs,
        page_limit=200,
        extra_params={"include_inactive": True},
    )
    orders = _fetch_admin_list_all_pages(
        http,
        f"{ORDER_URL}/api/v1/admin/orders",
        hdrs,
        page_limit=100,
    )

    total_products = len(products)
    active_products = sum(1 for p in products if p.get("is_active", True))
    low_stock = [p for p in products if isinstance(p.get("stock_quantity"), (int, float)) and p["stock_quantity"] < 10][
        :20
    ]
    low_stock_out = [
        {"id": p.get("id"), "name": p.get("name"), "stock_quantity": p.get("stock_quantity")} for p in low_stock
    ]

    now = datetime.now(timezone.utc)
    today = now.date()
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    last_day = monthrange(now.year, now.month)[1]
    month_end = datetime(now.year, now.month, last_day, 23, 59, 59, tzinfo=timezone.utc)

    orders_today = 0
    revenue_month = 0.0
    by_status: dict[str, int] = {}
    for o in orders:
        st = str(o.get("status") or "")
        by_status[st] = by_status.get(st, 0) + 1
        created = o.get("created_at")
        dt = _parse_order_datetime_utc(created)
        if dt:
            try:
                if dt.date() == today:
                    orders_today += 1
                if month_start <= dt <= month_end:
                    revenue_month += float(o.get("total_amount") or 0)
            except (TypeError, ValueError):
                continue

    return {
        "total_products": total_products,
        "active_products": active_products,
        "total_orders": len(orders),
        "orders_today": orders_today,
        "revenue_month": round(revenue_month, 2),
        "orders_by_status": by_status,
        "low_stock_products": low_stock_out,
    }


@app.get("/api/v1/admin/users", tags=["Users"])
def proxy_users(
    ctx: AdminCtx,
    http: Http,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    email: str = Query(""),
):
    params: dict[str, Any] = {"page": page, "limit": limit}
    if email:
        params["email"] = email
    r = http.get(f"{ORDER_URL}/api/v1/admin/users", params=params, headers=auth_header(ctx["token"]))
    return JSONResponse(status_code=r.status_code, content=r.json() if r.content else {})


@app.get("/api/v1/admin/users/{user_id}", tags=["Users"])
def proxy_user_detail(user_id: str, ctx: AdminCtx, http: Http):
    r = http.get(f"{ORDER_URL}/api/v1/admin/users/{user_id}", headers=auth_header(ctx["token"]))
    return JSONResponse(status_code=r.status_code, content=r.json() if r.content else {})


@app.patch("/api/v1/admin/users/{user_id}/block", tags=["Users"])
def proxy_user_block(user_id: str, body: dict, ctx: AdminCtx, http: Http, db: Db, request: Request):
    r = http.patch(
        f"{ORDER_URL}/api/v1/admin/users/{user_id}/block",
        headers={**auth_header(ctx["token"]), "Content-Type": "application/json"},
        json=body,
    )
    if r.is_success:
        write_audit(
            db,
            ctx["sub"],
            "UPDATE",
            "user",
            user_id,
            {"blocked": body.get("blocked")},
            request.client.host if request.client else None,
        )
    return JSONResponse(status_code=r.status_code, content=r.json() if r.content else {})


@app.get("/api/v1/admin/order-export", tags=["Orders"])
def proxy_order_export_csv(
    ctx: AdminCtx,
    http: Http,
    status_filter: Optional[str] = Query(None, alias="status"),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    order_number: str = Query("", max_length=40),
    customer_q: str = Query("", max_length=120),
):
    params: dict[str, Any] = {}
    if status_filter:
        params["status"] = status_filter
    if date_from:
        params["date_from"] = date_from.isoformat()
    if date_to:
        params["date_to"] = date_to.isoformat()
    if order_number.strip():
        params["order_number"] = order_number.strip()
    if customer_q.strip():
        params["customer_q"] = customer_q.strip()
    r = http.get(f"{ORDER_URL}/api/v1/admin/order-export", params=params, headers=auth_header(ctx["token"]))
    cd = r.headers.get("content-disposition")
    headers = {"Content-Disposition": cd} if cd else {}
    return Response(
        content=r.content,
        status_code=r.status_code,
        media_type=r.headers.get("content-type", "text/csv; charset=utf-8"),
        headers=headers,
    )


@app.get("/api/v1/admin/orders", tags=["Orders"])
def proxy_orders(
    ctx: AdminCtx,
    http: Http,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    status_filter: Optional[str] = Query(None, alias="status"),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    order_number: str = Query("", max_length=40),
    customer_q: str = Query("", max_length=120),
):
    params: dict[str, Any] = {"page": page, "limit": limit}
    if status_filter:
        params["status"] = status_filter
    if date_from:
        params["date_from"] = date_from.isoformat()
    if date_to:
        params["date_to"] = date_to.isoformat()
    if order_number.strip():
        params["order_number"] = order_number.strip()
    if customer_q.strip():
        params["customer_q"] = customer_q.strip()
    r = http.get(f"{ORDER_URL}/api/v1/admin/orders", params=params, headers=auth_header(ctx["token"]))
    return JSONResponse(status_code=r.status_code, content=r.json() if r.content else {})


@app.get("/api/v1/admin/orders/export", tags=["Orders"])
@app.get("/api/v1/admin/orders/export.csv", tags=["Orders"])
def proxy_orders_export_csv(
    ctx: AdminCtx,
    http: Http,
    status_filter: Optional[str] = Query(None, alias="status"),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    order_number: str = Query("", max_length=40),
    customer_q: str = Query("", max_length=120),
):
    params: dict[str, Any] = {}
    if status_filter:
        params["status"] = status_filter
    if date_from:
        params["date_from"] = date_from.isoformat()
    if date_to:
        params["date_to"] = date_to.isoformat()
    if order_number.strip():
        params["order_number"] = order_number.strip()
    if customer_q.strip():
        params["customer_q"] = customer_q.strip()
    r = http.get(f"{ORDER_URL}/api/v1/admin/order-export", params=params, headers=auth_header(ctx["token"]))
    cd = r.headers.get("content-disposition")
    headers = {"Content-Disposition": cd} if cd else {}
    return Response(
        content=r.content,
        status_code=r.status_code,
        media_type=r.headers.get("content-type", "text/csv; charset=utf-8"),
        headers=headers,
    )


@app.get("/api/v1/admin/orders/{order_id}", tags=["Orders"])
def admin_order_detail(order_id: str, ctx: AdminCtx, http: Http, db: Db):
    r = http.get(f"{ORDER_URL}/api/v1/admin/orders/{order_id}", headers=auth_header(ctx["token"]))
    if not r.is_success:
        return JSONResponse(status_code=r.status_code, content=r.json() if r.content else {})
    data = r.json()
    data["status_history"] = history_for_order(db, order_id)
    return data


@app.patch("/api/v1/admin/orders/{order_id}/status", tags=["Orders"])
def admin_order_status(
    order_id: str,
    body: dict,
    ctx: AdminCtx,
    http: Http,
    db: Db,
    request: Request,
):
    gr = http.get(f"{ORDER_URL}/api/v1/admin/orders/{order_id}", headers=auth_header(ctx["token"]))
    old_status = gr.json().get("status") if gr.is_success else None

    r = http.patch(
        f"{ORDER_URL}/api/v1/admin/orders/{order_id}/status",
        headers={**auth_header(ctx["token"]), "Content-Type": "application/json"},
        json=body,
    )
    if not r.is_success:
        return JSONResponse(status_code=r.status_code, content=r.json() if r.content else {})

    data = r.json()
    new_status = data.get("status")
    if old_status != new_status:
        row = OrderStatusHistory(
            order_id=order_id,
            old_status=old_status,
            new_status=str(new_status),
            changed_by=ctx["sub"],
            comment=body.get("comment"),
        )
        db.add(row)
        db.commit()
    write_audit(
        db,
        ctx["sub"],
        "UPDATE",
        "order",
        order_id,
        {"status": new_status, "comment": body.get("comment")},
        request.client.host if request.client else None,
    )
    data["status_history"] = history_for_order(db, order_id)
    return data


@app.get("/api/v1/admin/products", tags=["Products"])
def proxy_products(
    ctx: AdminCtx,
    http: Http,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    category_id: Optional[str] = Query(None),
    q: str = Query(""),
    include_inactive: bool = Query(False),
):
    params: dict[str, Any] = {"page": page, "limit": limit, "q": q, "include_inactive": include_inactive}
    if category_id:
        params["category_id"] = category_id
    r = http.get(f"{PRODUCT_URL}/api/v1/admin/products", params=params, headers=auth_header(ctx["token"]))
    return JSONResponse(status_code=r.status_code, content=r.json() if r.content else {})


@app.post("/api/v1/admin/products", tags=["Products"])
def proxy_product_create(body: dict, ctx: AdminCtx, http: Http, db: Db, request: Request):
    r = http.post(
        f"{PRODUCT_URL}/api/v1/products",
        headers={**auth_header(ctx["token"]), "Content-Type": "application/json"},
        json=body,
    )
    if r.is_success:
        payload = r.json() if r.content else {}
        write_audit(
            db,
            ctx["sub"],
            "CREATE",
            "product",
            str(payload.get("id") or ""),
            payload,
            request.client.host if request.client else None,
        )
    return JSONResponse(status_code=r.status_code, content=r.json() if r.content else {})


@app.put("/api/v1/admin/products/{product_id}", tags=["Products"])
def proxy_product_update(product_id: str, body: dict, ctx: AdminCtx, http: Http, db: Db, request: Request):
    r = http.put(
        f"{PRODUCT_URL}/api/v1/products/{product_id}",
        headers={**auth_header(ctx["token"]), "Content-Type": "application/json"},
        json=body,
    )
    if r.is_success:
        write_audit(
            db,
            ctx["sub"],
            "UPDATE",
            "product",
            product_id,
            body,
            request.client.host if request.client else None,
        )
    return JSONResponse(status_code=r.status_code, content=r.json() if r.content else {})


@app.delete("/api/v1/admin/products/{product_id}", tags=["Products"])
def proxy_product_delete(product_id: str, ctx: AdminCtx, http: Http, db: Db, request: Request):
    r = http.delete(f"{PRODUCT_URL}/api/v1/products/{product_id}", headers=auth_header(ctx["token"]))
    if r.status_code in (204, 200):
        write_audit(
            db,
            ctx["sub"],
            "DELETE",
            "product",
            product_id,
            None,
            request.client.host if request.client else None,
        )
        return Response(status_code=204)
    return JSONResponse(status_code=r.status_code, content=r.json() if r.content else {})


@app.post("/api/v1/admin/products/{product_id}/images", tags=["Products"])
def proxy_product_image(product_id: str, body: dict, ctx: AdminCtx, http: Http, db: Db, request: Request):
    r = http.post(
        f"{PRODUCT_URL}/api/v1/products/{product_id}/images",
        headers={**auth_header(ctx["token"]), "Content-Type": "application/json"},
        json=body,
    )
    if r.is_success:
        write_audit(
            db,
            ctx["sub"],
            "CREATE",
            "product_image",
            product_id,
            body,
            request.client.host if request.client else None,
        )
    return JSONResponse(status_code=r.status_code, content=r.json() if r.content else {})


@app.get("/api/v1/admin/audit-log", tags=["Audit"])
def audit_log(
    db: Db,
    ctx: AdminCtx,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
):
    q = db.query(AuditLog).order_by(AuditLog.created_at.desc())
    total = q.count()
    rows = q.offset((page - 1) * limit).limit(limit).all()
    return {
        "data": [
            {
                "id": r.id,
                "admin_id": r.admin_id,
                "action": r.action,
                "entity_type": r.entity_type,
                "entity_id": r.entity_id,
                "changes": json.loads(r.changes) if r.changes else None,
                "ip_address": r.ip_address,
                "created_at": iso_utc_z(r.created_at),
            }
            for r in rows
        ],
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "total_pages": max(1, (total + limit - 1) // limit),
        },
    }
