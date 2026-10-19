"""Database setup and SQLAlchemy session utilities."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def create_database(database_url: str) -> tuple[Engine, sessionmaker[Session]]:
    connect_args = {}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        if database_url.startswith("sqlite:///") and not database_url.startswith("sqlite:////"):
            relative_path = database_url.removeprefix("sqlite:///").split("?", 1)[0]
            if relative_path and relative_path != ":memory:":
                Path(relative_path).parent.mkdir(parents=True, exist_ok=True)
        if database_url.endswith(":memory:"):
            from sqlalchemy.pool import StaticPool

            engine = create_engine(
                database_url,
                connect_args=connect_args,
                poolclass=StaticPool,
                pool_pre_ping=True,
            )
        else:
            engine = create_engine(database_url, connect_args=connect_args, pool_pre_ping=True)
    else:
        engine = create_engine(database_url, pool_pre_ping=True)

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    return engine, factory


def initialize_database(engine: Engine) -> None:
    from scenepilot import models  # noqa: F401

    Base.metadata.create_all(engine)
    existing_columns = {column["name"] for column in inspect(engine).get_columns("jobs")}
    additions = {
        "transcript": "JSON",
        "scene_plan": "JSON",
        "narration_duration_seconds": "FLOAT",
    }
    with engine.begin() as connection:
        for column_name, column_type in additions.items():
            if column_name not in existing_columns:
                connection.execute(
                    text(f"ALTER TABLE jobs ADD COLUMN {column_name} {column_type}")
                )
