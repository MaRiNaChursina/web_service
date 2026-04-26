import uuid

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import relationship

from .database import Base


def new_id() -> str:
    return str(uuid.uuid4())


class Cart(Base):
    __tablename__ = "carts"

    id = Column(String, primary_key=True, default=new_id)
    session_id = Column(String(100), unique=True, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    items = relationship("CartItem", back_populates="cart", cascade="all, delete-orphan")


class CartItem(Base):
    __tablename__ = "cart_items"
    __table_args__ = (UniqueConstraint("cart_id", "product_id", name="uq_cart_product"),)

    id = Column(String, primary_key=True, default=new_id)
    cart_id = Column(String, ForeignKey("carts.id", ondelete="CASCADE"), nullable=False)
    cart = relationship("Cart", back_populates="items")
    product_id = Column(String, nullable=False)
    product_name = Column(String(200), nullable=False)
    quantity = Column(Integer, nullable=False)
    unit_price = Column(Float, nullable=False)


class Order(Base):
    __tablename__ = "orders"

    id = Column(String, primary_key=True, default=new_id)
    session_id = Column(String(100), nullable=False)
    order_number = Column(String(20), unique=True, nullable=False)
    status = Column(String(30), nullable=False, default="pending")
    total_amount = Column(Float, nullable=False)
    payment_method = Column(String(30), nullable=True)
    payment_status = Column(String(20), nullable=False, default="unpaid")
    notes = Column(Text, nullable=True)
    delivery_address = Column(Text, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")


class OrderItem(Base):
    __tablename__ = "order_items"

    id = Column(String, primary_key=True, default=new_id)
    order_id = Column(String, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    order = relationship("Order", back_populates="items")
    product_id = Column(String, nullable=False)
    product_name = Column(String(200), nullable=False)
    product_sku = Column(String(50), nullable=False)
    quantity = Column(Integer, nullable=False)
    unit_price = Column(Float, nullable=False)
    total_price = Column(Float, nullable=False)
