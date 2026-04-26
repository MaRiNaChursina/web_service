from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class CategoryCreate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "Для растений",
                "slug": "plant",
                "description": "Лампы для выращивания растений",
            }
        }
    )

    name: str = Field(..., description="Название категории")
    slug: str = Field(..., description="URL-слаг")
    description: Optional[str] = Field(None, description="Описание")


class CategoryUpdate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "Для растений",
                "slug": "plant",
                "description": "Обновлённое описание",
            }
        }
    )

    name: str
    slug: str
    description: Optional[str] = None


class ProductCreate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "category_id": "uuid-категории",
                "name": "Лампочка LED GU10 7W дневной свет",
                "slug": "led-gu10-7w-dl",
                "description": "Светодиодная лампочка GU10 7Вт",
                "price": 189.0,
                "sku": "LED-GU10-7W-DL",
                "stock_quantity": 100,
                "power_watts": 7,
                "base_type": "GU10",
                "color_temp_k": 4000,
            }
        }
    )

    category_id: str
    name: str
    slug: str
    description: Optional[str] = None
    price: float = Field(..., gt=0)
    sku: str
    stock_quantity: int
    power_watts: Optional[int] = None
    base_type: Optional[str] = None
    color_temp_k: Optional[int] = None


class ProductUpdate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "category_id": "uuid-категории",
                "name": "Лампочка LED GU10 7W дневной свет",
                "slug": "led-gu10-7w-dl",
                "description": "Описание",
                "price": 189.0,
                "sku": "LED-GU10-7W-DL",
                "stock_quantity": 100,
                "power_watts": 7,
                "base_type": "GU10",
                "color_temp_k": 4000,
                "is_active": True,
            }
        }
    )

    category_id: str
    name: str
    slug: str
    description: Optional[str] = None
    price: float = Field(..., gt=0)
    sku: str
    stock_quantity: int
    power_watts: Optional[int] = None
    base_type: Optional[str] = None
    color_temp_k: Optional[int] = None
    is_active: Optional[bool] = None


class StockPatch(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {"stock_quantity": 500}})

    stock_quantity: int = Field(..., description="Новый остаток на складе")
