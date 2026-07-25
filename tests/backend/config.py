from typing import Any, Generic, TypeVar

from pgvector.sqlalchemy import VECTOR
from polyfactory.factories.sqlalchemy_factory import SQLAlchemyFactory
from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.sql.sqltypes import NullType

T = TypeVar("T")


class BaseFactory(SQLAlchemyFactory, Generic[T]):
    """Polyfactory SQLAlchemy base factory for Vistui test fixtures."""

    __is_base_factory__ = True

    @classmethod
    def get_sqlalchemy_types(cls) -> dict[Any, Any]:
        types = super().get_sqlalchemy_types()
        types[NullType] = lambda: None
        types[TSVECTOR] = lambda: None
        types[VECTOR] = lambda: None
        return types

    @classmethod
    def _serialize_one(
        cls,
        orm_model: DeclarativeBase,
        schema: type[BaseModel],
    ) -> dict[str, Any]:
        return schema.model_validate(orm_model, from_attributes=True).model_dump(mode="json")
