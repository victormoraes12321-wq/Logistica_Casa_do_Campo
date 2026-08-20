from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, PrimaryKeyConstraint, REAL, TEXT, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=True)
    file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("idx_audit_logs_created_at", "created_at"),
        Index("idx_audit_logs_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    user_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_ip: Mapped[str | None] = mapped_column(Text, nullable=True)
    action: Mapped[str | None] = mapped_column(Text, nullable=True)
    module: Mapped[str | None] = mapped_column(Text, nullable=True)
    entity: Mapped[str | None] = mapped_column(Text, nullable=True)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    document: Mapped[str | None] = mapped_column(Text, nullable=True)
    phone: Mapped[str | None] = mapped_column(Text, nullable=True)
    whatsapp: Mapped[str | None] = mapped_column(Text, nullable=True)
    city: Mapped[str | None] = mapped_column(Text, nullable=True)
    neighborhood: Mapped[str | None] = mapped_column(Text, nullable=True)
    farm_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    reference_point: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    route_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[int | None] = mapped_column(Integer, nullable=True, default=1)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int | None] = mapped_column(Integer, nullable=True, default=1)


class DeliveryProblem(Base):
    __tablename__ = "delivery_problems"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    problem_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class Driver(Base):
    __tablename__ = "drivers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    phone: Mapped[str | None] = mapped_column(Text, nullable=True)
    document: Mapped[str | None] = mapped_column(Text, nullable=True)
    vehicle_default: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[int | None] = mapped_column(Integer, nullable=True, default=1)
    updated_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int | None] = mapped_column(Integer, nullable=True, default=1)


class Holiday(Base):
    __tablename__ = "holidays"
    __table_args__ = (Index("idx_holidays_date", "date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class OrderHistory(Base):
    __tablename__ = "order_history"
    __table_args__ = (Index("idx_history_order_date", "order_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    old_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    product_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    product_name: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(Text, nullable=True)
    quantity: Mapped[float | None] = mapped_column(REAL, nullable=True, default=0)
    unit: Mapped[str | None] = mapped_column(Text, nullable=True)
    weight_kg: Mapped[float | None] = mapped_column(REAL, nullable=True, default=0)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        Index("idx_orders_status_expected", "status", "expected_delivery_date"),
        Index("idx_orders_expected_status", "expected_delivery_date", "status"),
        Index("idx_orders_sale_date", "sale_date"),
        Index("idx_orders_route_city", "route_name", "city"),
        Index("idx_orders_invoice", "invoice_number"),
        Index("idx_orders_client", "client_id"),
        Index("idx_orders_order_number", "order_number"),
        Index("idx_orders_updated_at", "updated_at"),
        Index("idx_orders_status_updated", "status", "updated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_number: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    external_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id", ondelete="SET NULL"), nullable=True)
    seller_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    seller_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    urgency: Mapped[str | None] = mapped_column(Text, nullable=True, default="Normal")
    sale_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_delivery_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    invoice_limit_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    payment_method: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_value: Mapped[float | None] = mapped_column(REAL, nullable=True, default=0)
    weight_kg: Mapped[float | None] = mapped_column(REAL, nullable=True, default=0)
    delivery_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    location_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    route_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    city: Mapped[str | None] = mapped_column(Text, nullable=True)
    uf: Mapped[str | None] = mapped_column(String(8), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    invoice_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    invoice_file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    invoiced_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    driver_id: Mapped[int | None] = mapped_column(ForeignKey("drivers.id", ondelete="SET NULL"), nullable=True)
    vehicle_id: Mapped[int | None] = mapped_column(ForeignKey("vehicles.id", ondelete="SET NULL"), nullable=True)
    delivered_to: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivered_document: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivered_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int | None] = mapped_column(Integer, nullable=True, default=1)


class RolePermission(Base):
    __tablename__ = "role_permissions"
    __table_args__ = (
        PrimaryKeyConstraint("role_name", "perm", name="pk_role_permissions"),
        Index("idx_role_permissions_role", "role_name"),
    )

    role_name: Mapped[str] = mapped_column(Text, nullable=False)
    perm: Mapped[str] = mapped_column(Text, nullable=False)
    allowed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class RouteCity(Base):
    __tablename__ = "route_cities"
    __table_args__ = (
        Index("idx_route_cities_active_city", "active", "city"),
        Index("idx_route_cities_active_route", "active", "route_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    route_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    city: Mapped[str | None] = mapped_column(Text, nullable=True)
    uf: Mapped[str | None] = mapped_column(String(8), nullable=True)
    delivery_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active: Mapped[int | None] = mapped_column(Integer, nullable=True, default=1)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int | None] = mapped_column(Integer, nullable=True, default=1)


class RouteOrder(Base):
    __tablename__ = "route_orders"
    __table_args__ = (
        CheckConstraint("delivery_order >= 1", name="ck_route_orders_delivery_order_min"),
        Index("idx_route_orders_route_seq", "route_id", "delivery_order"),
        Index("idx_route_orders_order", "order_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    route_id: Mapped[int] = mapped_column(ForeignKey("routes.id", ondelete="CASCADE"), nullable=False)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    delivery_order: Mapped[int | None] = mapped_column(Integer, nullable=True, default=1)
    status: Mapped[str | None] = mapped_column(Text, nullable=True, default="Pendente")


class Route(Base):
    __tablename__ = "routes"
    __table_args__ = (
        Index("idx_routes_status_date", "status", "date"),
        Index("idx_routes_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    date: Mapped[str | None] = mapped_column(Text, nullable=True)
    driver_id: Mapped[int | None] = mapped_column(ForeignKey("drivers.id", ondelete="SET NULL"), nullable=True)
    vehicle_id: Mapped[int | None] = mapped_column(ForeignKey("vehicles.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str | None] = mapped_column(Text, nullable=True, default="Planejada")
    route_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_weight: Mapped[float | None] = mapped_column(REAL, nullable=True, default=0)
    capacity: Mapped[float | None] = mapped_column(REAL, nullable=True, default=11000)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int | None] = mapped_column(Integer, nullable=True, default=1)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[str | None] = mapped_column(Text, nullable=True)


class UserPermission(Base):
    __tablename__ = "user_permissions"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "perm", name="pk_user_permissions"),
        Index("idx_user_permissions_user", "user_id"),
    )

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    perm: Mapped[str] = mapped_column(Text, nullable=False)
    allowed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    username: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[int | None] = mapped_column(Integer, nullable=True, default=1)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    last_login_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    must_change_password: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)


class Vehicle(Base):
    __tablename__ = "vehicles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    plate: Mapped[str | None] = mapped_column(Text, nullable=True)
    type: Mapped[str | None] = mapped_column(Text, nullable=True)
    capacity: Mapped[str | None] = mapped_column(Text, nullable=True)
    capacity_kg: Mapped[float | None] = mapped_column(REAL, nullable=True)
    active: Mapped[int | None] = mapped_column(Integer, nullable=True, default=1)
    updated_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int | None] = mapped_column(Integer, nullable=True, default=1)
