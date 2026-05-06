from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from db.session import Base


class SystemConfig(Base):
    """运行时配置存储（替代 .env 文件动态写入）"""

    __tablename__ = "system_configs"

    key: Mapped[str] = mapped_column(String(128), primary_key=True, comment="配置键名（环境变量名）")
    value: Mapped[str] = mapped_column(Text, nullable=False, comment="配置值（统一字符串存储）")
    value_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="str", comment="值类型: str/int/float/bool"
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True, comment="配置说明")
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    updated_by: Mapped[str] = mapped_column(
        String(64), server_default="system", comment="修改来源: api/migration/system"
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "value": self.value,
            "value_type": self.value_type,
            "description": self.description,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "updated_by": self.updated_by,
        }
