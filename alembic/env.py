"""Alembic environment configuration."""

import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# 将项目根目录加入 sys.path，以便导入项目模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from core.config import Config  # noqa: E402
from db.session import Base  # noqa: E402
from models import (  # noqa: E402, F401
    AIUsageLog,
    ArchivedWebhookEvent,
    DeepAnalysis,
    FailedForward,
    ForwardRule,
    RemediationExecution,
    SystemConfig,
    WebhookEvent,
)

# Alembic Config object
config = context.config

# 设置日志
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 设置 target_metadata 供 autogenerate 使用
target_metadata = Base.metadata


def get_url() -> str:
    """获取同步数据库 URL（Alembic 使用同步引擎）"""
    url = Config.DATABASE_URL
    # 将异步驱动替换为同步驱动
    url = url.replace("postgresql+asyncpg://", "postgresql://")
    return url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    Configures the context with just a URL and not an Engine.
    Calls to context.execute() here emit the given string to the script output.
    """
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    Creates an Engine and associates a connection with the context.
    """
    # 覆盖 ini 中的 sqlalchemy.url
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
