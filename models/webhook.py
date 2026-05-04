import hashlib
import logging
from datetime import datetime
from typing import Any

import orjson
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB

from adapters.summary_extractors import extract_summary_fields
from core.compression import compress_payload, decompress_payload
from db.session import Base

_logger = logging.getLogger(__name__)


# ====== 告警哈希字段配置 ======
PROMETHEUS_ROOT_FIELDS = ["alertingRuleName"]
PROMETHEUS_LABEL_FIELDS = ["alertname", "internal_label_alert_level", "host", "instance", "pod", "namespace", "service", "path", "method"]
PROMETHEUS_ALERT_FIELDS = ["fingerprint"]
GENERIC_FIELDS = ["Type", "RuleName", "event", "event_type", "MetricName", "Level", "alert_id", "alert_name", "resource_id", "service"]


class WebhookEvent(Base):
    """Webhook 事件模型"""

    __tablename__ = "webhook_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(100), nullable=False, index=True)
    client_ip = Column(String(50))
    timestamp = Column(DateTime, nullable=False, default=datetime.now, index=True)

    raw_payload = Column(LargeBinary)
    headers = Column(JSONB)
    parsed_data = Column(JSONB)

    alert_hash = Column(String(64), index=True)

    ai_analysis = Column(JSONB)
    importance = Column(String(20), index=True)

    processing_status = Column(String(20), default="received", nullable=False, index=True)
    retry_count = Column(Integer, default=0, nullable=False)
    failure_reason = Column(String(500), nullable=True)
    error_message = Column(Text, nullable=True)

    forward_status = Column(String(20))

    prev_alert_id = Column(BigInteger, nullable=True)

    is_duplicate = Column(Integer, default=0)
    duplicate_of = Column(Integer)
    duplicate_count = Column(Integer, default=1)
    beyond_window = Column(Integer, default=0)
    last_notified_at = Column(DateTime)

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("idx_unique_alert_hash_original", "alert_hash", unique=True, postgresql_where=(is_duplicate == 0)),
        Index("idx_hash_timestamp", "alert_hash", "timestamp"),
        Index("idx_importance_timestamp", "importance", "timestamp"),
        Index("idx_duplicate_lookup", "alert_hash", "is_duplicate", "timestamp"),
        Index("idx_status_created", "processing_status", "created_at"),
        Index("idx_source_timestamp_id", "source", "timestamp", "id"),
    )

    @staticmethod
    def generate_hash(data: dict[str, Any], source: str) -> str:
        """生成告警哈希"""
        key_fields = {"source": source}
        if isinstance(data, dict):
            if "alerts" in data and isinstance(data.get("alerts"), list) and data["alerts"]:
                # Prometheus
                key_fields.update({f: data[f] for f in PROMETHEUS_ROOT_FIELDS if f in data})
                labels = data["alerts"][0].get("labels", {})
                if isinstance(labels, dict):
                    key_fields.update({f: labels[f] for f in PROMETHEUS_LABEL_FIELDS if f in labels})
                key_fields.update({f: data["alerts"][0][f] for f in PROMETHEUS_ALERT_FIELDS if f in data["alerts"][0]})
            else:
                # Generic
                key_fields.update({f: data[f] for f in GENERIC_FIELDS if f in data})
                resources = data.get("Resources", [])
                if isinstance(resources, list) and resources and isinstance(resources[0], dict):
                    rid = resources[0].get("InstanceId") or resources[0].get("Id") or resources[0].get("id")
                    if rid: key_fields["resource_id"] = rid
                    dims = resources[0].get("Dimensions", [])
                    if isinstance(dims, list):
                        important = {"Node", "ResourceID", "Instance", "InstanceId", "Host", "Pod", "Container"}
                        for d in dims:
                            if isinstance(d, dict) and d.get("Name") in important and d.get("Value"):
                                key_fields[f"dim_{d['Name'].lower()}"] = d["Value"]
        
        return hashlib.sha256(orjson.dumps(key_fields, option=orjson.OPT_SORT_KEYS)).hexdigest()

    def fill_fields(self, **kwargs):
        """统一填充字段"""
        for k, v in kwargs.items():
            if k == "raw_payload" and isinstance(v, bytes):
                try: v = compress_payload(v.decode("utf-8"))
                except Exception: pass
            if k == "headers" and v is not None: v = dict(v)
            if hasattr(self, k): setattr(self, k, v)
        if not self.timestamp: self.timestamp = datetime.now()
        if not self.created_at: self.created_at = datetime.now()
        self.updated_at = datetime.now()

    def to_summary_dict(self):
        summary = None
        ai_analysis = self.ai_analysis
        if ai_analysis:
            summary = ai_analysis.get("summary", "")

        alert_info = extract_summary_fields(self.source, self.parsed_data)

        return {
            "id": self.id,
            "source": self.source,
            "client_ip": self.client_ip,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "importance": self.importance,
            "is_duplicate": self.is_duplicate,
            "duplicate_of": self.duplicate_of,
            "duplicate_count": self.duplicate_count,
            "beyond_window": self.beyond_window,
            "forward_status": self.forward_status,
            "failure_reason": self.failure_reason,
            "error_message": self.error_message,
            "summary": summary,
            "alert_info": alert_info,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "prev_alert_id": self.prev_alert_id,
        }

    def to_dict(self, *, include_raw_payload: bool = True):
        raw_payload = None
        if include_raw_payload:
            raw_payload = decompress_payload(self.raw_payload)
        return {
            "id": self.id,
            "source": self.source,
            "client_ip": self.client_ip,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "raw_payload": raw_payload,
            "headers": self.headers,
            "parsed_data": self.parsed_data,
            "alert_hash": self.alert_hash,
            "ai_analysis": self.ai_analysis,
            "importance": self.importance,
            "processing_status": self.processing_status,
            "forward_status": self.forward_status,
            "failure_reason": self.failure_reason,
            "error_message": self.error_message,
            "is_duplicate": self.is_duplicate,
            "duplicate_of": self.duplicate_of,
            "duplicate_count": self.duplicate_count,
            "beyond_window": self.beyond_window,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ArchivedWebhookEvent(Base):
    """已归档的 Webhook 事件模型（历史备份）"""

    __tablename__ = "archived_webhook_events"

    id = Column(Integer, primary_key=True)
    source = Column(String(100), nullable=False, index=True)
    client_ip = Column(String(50))
    timestamp = Column(DateTime, nullable=False, index=True)

    raw_payload = Column(LargeBinary)
    headers = Column(JSONB)
    parsed_data = Column(JSONB)
    alert_hash = Column(String(64), index=True)
    ai_analysis = Column(JSONB)
    importance = Column(String(20), index=True)
    forward_status = Column(String(20))
    is_duplicate = Column(Integer)
    duplicate_of = Column(Integer)
    duplicate_count = Column(Integer)
    beyond_window = Column(Integer)
    last_notified_at = Column(DateTime)
    created_at = Column(DateTime)
    updated_at = Column(DateTime)
    archived_at = Column(DateTime, default=datetime.now, index=True)

    __table_args__ = (Index("idx_archived_hash_timestamp", "alert_hash", "timestamp"),)
