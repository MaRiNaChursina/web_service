import json
import uuid

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, func

from .database import Base


def new_id() -> str:
    return str(uuid.uuid4())


class AdminUser(Base):
    __tablename__ = "admin_users"

    id = Column(String, primary_key=True, default=new_id)
    email = Column(String(255), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    full_name = Column(String(200), nullable=False)
    permissions = Column(Text, nullable=False, default="[]")
    last_login_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, server_default=func.now())

    def permissions_list(self) -> list:
        try:
            v = json.loads(self.permissions or "[]")
            return v if isinstance(v, list) else []
        except json.JSONDecodeError:
            return []


class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    admin_id = Column(String, nullable=False)
    action = Column(String(50), nullable=False)
    entity_type = Column(String(50), nullable=False)
    entity_id = Column(String, nullable=False)
    changes = Column(Text, nullable=True)
    ip_address = Column(String(100), nullable=True)
    created_at = Column(DateTime, server_default=func.now())


class OrderStatusHistory(Base):
    __tablename__ = "order_status_history"

    id = Column(String, primary_key=True, default=new_id)
    order_id = Column(String(100), nullable=False, index=True)
    old_status = Column(String(30), nullable=True)
    new_status = Column(String(30), nullable=False)
    changed_by = Column(String, nullable=True)
    comment = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
