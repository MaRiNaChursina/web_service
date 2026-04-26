from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


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
                "payment_method": "card",
                "notes": "Позвонить за час до доставки",
            }
        }
    )

    delivery_address: DeliveryAddress
    payment_method: str
    notes: Optional[str] = None
