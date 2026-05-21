"""Запуск из каталога product-service: python -m app.seed"""

from sqlalchemy.orm import Session

from .database import SessionLocal, engine, Base
from .models import Category, Product, ProductImage


CATALOG = [
    {"cat": "led", "sku": "LED-E27-10W-WW", "name": "Лампочка LED E27 10W тёплый белый", "slug": "led-e27-10w-ww", "price": 149.9, "stock": 250, "power": 10, "base": "E27", "colorK": 2700},
    {"cat": "led", "sku": "LED-E27-10W-CW", "name": "Лампочка LED E27 10W холодный белый", "slug": "led-e27-10w-cw", "price": 149.9, "stock": 220, "power": 10, "base": "E27", "colorK": 6500},
    {"cat": "led", "sku": "LED-E27-15W-WW", "name": "Лампочка LED E27 15W тёплый белый", "slug": "led-e27-15w-ww", "price": 189.0, "stock": 180, "power": 15, "base": "E27", "colorK": 2700},
    {"cat": "led", "sku": "LED-E27-15W-DL", "name": "Лампочка LED E27 15W дневной свет", "slug": "led-e27-15w-dl", "price": 189.0, "stock": 175, "power": 15, "base": "E27", "colorK": 4000},
    {"cat": "led", "sku": "LED-E14-7W-WW", "name": "Лампочка LED E14 7W тёплый белый", "slug": "led-e14-7w-ww", "price": 129.0, "stock": 200, "power": 7, "base": "E14", "colorK": 2700},
    {"cat": "led", "sku": "LED-E14-7W-CW", "name": "Лампочка LED E14 7W холодный белый", "slug": "led-e14-7w-cw", "price": 129.0, "stock": 195, "power": 7, "base": "E14", "colorK": 6500},
    {"cat": "led", "sku": "LED-GU10-7W-DL", "name": "Лампочка LED GU10 7W дневной свет", "slug": "led-gu10-7w-dl", "price": 189.0, "stock": 160, "power": 7, "base": "GU10", "colorK": 4000},
    {"cat": "led", "sku": "LED-GU10-5W-WW", "name": "Лампочка LED GU10 5W тёплый белый", "slug": "led-gu10-5w-ww", "price": 159.0, "stock": 170, "power": 5, "base": "GU10", "colorK": 2700},
    {"cat": "led", "sku": "LED-B22-12W-WW", "name": "Лампочка LED B22 12W тёплый белый", "slug": "led-b22-12w-ww", "price": 199.0, "stock": 140, "power": 12, "base": "B22", "colorK": 2700},
    {"cat": "led", "sku": "LED-FIL-E27-8W", "name": "Лампочка LED филамент E27 8W", "slug": "led-fil-e27-8w", "price": 249.0, "stock": 130, "power": 8, "base": "E27", "colorK": 2200},
    {"cat": "smart", "sku": "LED-RGB-E27-10W", "name": "Лампочка LED RGB E27 10W с пультом", "slug": "led-rgb-e27-10w", "price": 599.0, "stock": 90, "power": 10, "base": "E27", "colorK": None},
    {"cat": "smart", "sku": "LED-SMART-E27-9W", "name": "Лампочка LED умная E27 Wi-Fi 9W", "slug": "led-smart-e27-9w", "price": 890.0, "stock": 75, "power": 9, "base": "E27", "colorK": 4000},
    {"cat": "incandescent", "sku": "INCAND-E27-40W", "name": "Лампочка накаливания E27 40W", "slug": "incand-e27-40w", "price": 45.0, "stock": 300, "power": 40, "base": "E27", "colorK": 2700},
    {"cat": "incandescent", "sku": "INCAND-E27-60W", "name": "Лампочка накаливания E27 60W", "slug": "incand-e27-60w", "price": 52.0, "stock": 280, "power": 60, "base": "E27", "colorK": 2700},
    {"cat": "halogen", "sku": "HALOG-GU10-50W", "name": "Лампочка галогенная GU10 50W", "slug": "halog-gu10-50w", "price": 119.0, "stock": 150, "power": 50, "base": "GU10", "colorK": 3000},
    {"cat": "halogen", "sku": "HALOG-E14-42W", "name": "Лампочка галогенная E14 42W", "slug": "halog-e14-42w", "price": 109.0, "stock": 145, "power": 42, "base": "E14", "colorK": 2800},
    {"cat": "fluorescent", "sku": "FL-E27-20W", "name": "Лампочка люминесцентная E27 20W", "slug": "fl-e27-20w", "price": 159.0, "stock": 110, "power": 20, "base": "E27", "colorK": 4100},
    {"cat": "special", "sku": "LED-FRIDGE-E14-2W", "name": "Лампочка светодиодная для холодильника E14 2W", "slug": "led-fridge-e14-2w", "price": 99.0, "stock": 200, "power": 2, "base": "E14", "colorK": 4000},
    {"cat": "special", "sku": "LED-PLANT-E27-20W", "name": "Лампочка LED для растений E27 20W", "slug": "led-plant-e27-20w", "price": 349.0, "stock": 85, "power": 20, "base": "E27", "colorK": None},
    {"cat": "special", "sku": "LED-FLOOD-30W", "name": "Прожектор LED уличный 30W", "slug": "led-flood-30w", "price": 1290.0, "stock": 60, "power": 30, "base": None, "colorK": 6500},
]


def upsert_category(db: Session, name: str, slug: str, desc: str) -> Category:
    c = db.query(Category).filter(Category.slug == slug).first()
    if c:
        c.name = name
        c.description = desc
        return c
    c = Category(name=name, slug=slug, description=desc)
    db.add(c)
    db.flush()
    return c


def run() -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        cats = {
            "led": upsert_category(db, "LED лампы", "led", "Светодиодные лампы общего назначения"),
            "smart": upsert_category(db, "Умные и RGB", "smart", "Умные лампы и RGB"),
            "incandescent": upsert_category(db, "Накаливания", "incandescent", "Лампы накаливания"),
            "halogen": upsert_category(db, "Галогенные", "halogen", "Галогенные лампы"),
            "fluorescent": upsert_category(db, "Люминесцентные", "fluorescent", "Люминесцентные лампы"),
            "special": upsert_category(db, "Специальные", "special", "Холодильник, растения, улица"),
        }
        db.commit()

        for row in CATALOG:
            cat = cats[row["cat"]]
            p = db.query(Product).filter(Product.sku == row["sku"]).first()
            if p:
                p.category_id = cat.id
                p.name = row["name"]
                p.slug = row["slug"]
                p.description = row["name"]
                p.price = row["price"]
                p.stock_quantity = row["stock"]
                p.power_watts = row["power"]
                p.base_type = row["base"]
                p.color_temp_k = row["colorK"]
                p.is_active = True
            else:
                p = Product(
                    category_id=cat.id,
                    name=row["name"],
                    slug=row["slug"],
                    description=row["name"],
                    price=row["price"],
                    sku=row["sku"],
                    stock_quantity=row["stock"],
                    power_watts=row["power"],
                    base_type=row["base"],
                    color_temp_k=row["colorK"],
                    is_active=True,
                )
                db.add(p)
            db.flush()

        db.commit()

        first = db.query(Product).filter(Product.sku == "LED-E27-10W-WW").first()
        if first:
            db.query(ProductImage).filter(ProductImage.product_id == first.id).delete()
            db.add(
                ProductImage(
                    product_id=first.id,
                    url="https://placehold.co/400x400/e8edf2/64748b?text=LED+E27",
                    alt_text="LED E27 10W тёплый белый",
                    is_primary=True,
                    sort_order=0,
                )
            )
            db.commit()

        n = db.query(Product).count()
        print("Seed OK. Products:", n)
    finally:
        db.close()


if __name__ == "__main__":
    run()
