from typing import Annotated, Literal, Optional

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def _normalize_customer_email(v: object) -> str:
    if not isinstance(v, str):
        raise ValueError("Укажите email строкой")
    s = v.strip().lower()
    if len(s) < 3 or len(s) > 255:
        raise ValueError("Некорректный email")
    if s.count("@") != 1:
        raise ValueError("Некорректный email")
    local, domain = s.split("@", 1)
    if not local or not domain:
        raise ValueError("Некорректный email")
    if ".." in local or ".." in domain or " " in s:
        raise ValueError("Некорректный email")
    return s


CustomerEmail = Annotated[str, BeforeValidator(_normalize_customer_email)]


class DeliveryAddress(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "city": "Москва",
                "street": "ул. Ленина",
                "building": "1",
                "apartment": "10",
                "postal_code": "101000",
            }
        }
    )

    city: str
    street: str
    building: str
    apartment: Optional[str] = None
    postal_code: str


class CustomerRegister(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "email": "buyer@example.com",
                "password": "secret12",
                "first_name": "Анна",
                "last_name": "Иванова",
            }
        }
    )

    email: CustomerEmail
    password: str = Field(..., min_length=6, max_length=128)
    first_name: Optional[str] = Field(None, max_length=100)
    last_name: Optional[str] = Field(None, max_length=100)


class CustomerLogin(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {"email": "buyer@example.com", "password": "secret12"}})

    email: CustomerEmail
    password: str


class CartMergeRequest(BaseModel):
    """Слияние гостевой корзины в корзину авторизованного покупателя (X-Session-Id = id пользователя)."""

    model_config = ConfigDict(json_schema_extra={"example": {"from_session_id": "гостевой-uuid"}})

    from_session_id: str = Field(..., min_length=4, max_length=100)


class CartItemAdd(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "product_id": "uuid-товара-из-каталога",
                "quantity": 2,
            }
        }
    )

    product_id: str
    quantity: int = Field(..., ge=1)


class CartItemQuantity(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {"quantity": 3}})

    quantity: int = Field(..., ge=1)


class OrderCreate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "delivery_address": {
                    "city": "Москва",
                    "street": "ул. Ленина",
                    "building": "1",
                    "apartment": "10",
                    "postal_code": "101000",
                },
                "payment_method": "sbp",
                "notes": "Позвонить за час до доставки",
            }
        }
    )

    delivery_address: DeliveryAddress
    payment_method: Literal["sbp", "card", "cash"]
    notes: Optional[str] = None
