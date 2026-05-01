from sqlalchemy import (
    Column,
    DateTime,
    String,
    Text,
    func,
)

from db.session import Base


class SystemConfig(Base):
    """运行时配置存储（替代 .env 文件动态写入）"""

    __tablename__ = "system_configs"

    key = Column(String(128), primary_key=True, comment="配置键名（环境变量名）")
    value = Column(Text, nullable=False, comment="配置值（统一字符串存储）")
    value_type = Column(String(16), nullable=False, default="str", comment="值类型: str/int/float/bool")
    description = Column(Text, nullable=True, comment="配置说明")
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    updated_by = Column(String(64), server_default="system", comment="修改来源: api/migration/system")

    def to_dict(self):
        return {
            "key": self.key,
            "value": self.value,
            "value_type": self.value_type,
            "description": self.description,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "updated_by": self.updated_by,
        }
