"""数据源注册领域服务：统一接入异构数据源，触发Schema自动发现与连接探测。"""
from __future__ import annotations

import hashlib
import time
import uuid
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class SourceRegistryService(BaseModuleService):
    MODULE = "source_registry"
    ENTITY = "注册数据源"
    OPERATION_TERM = "接入"
    LIST_FIELDS = ["源编号", "源类型", "连接地址", "驱动配置", "数据库名", "认证方式", "接入状态", "最近同步"]
    REQUIRED_FIELDS = ["源编号", "源类型", "连接地址"]
    STATUS_ORDER = ["待验证", "已注册", "连接异常", "已停用"]
    ACTION_RULES = {"验证连接": "已注册", "标记注册": "连接异常", "停用来源": "已停用"}
    NEGATIVE_ACTIONS = ["停用来源"]

    def register_source(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """注册数据源并自动触发：Schema发现→semantic_model、审计日志、数据契约草案。"""
        entry, missing = self.create_entry(values)
        if missing:
            return None, f"缺少必填字段：{'、'.join(missing)}"
        if entry is None:
            return None, "创建数据源失败"

        # 触发Schema发现 —— 模拟从数据源推导表结构
        schema = _discover_schema(values.get("源类型", "unknown"))
        if schema:
            _write_semantic_model(entry["id"], values.get("源编号", ""), schema)

        # 写入审计日志
        _write_audit(entry["id"], values.get("源编号", ""), "register")

        # 建立数据契约草案
        _create_contract_draft(entry["id"], values.get("源编号", ""))

        # 更新最近同步时间和状态
        entry["最近同步"] = time.strftime("%Y-%m-%d %H:%M:%S")
        entry["接入状态"] = "已接入"
        entry["status"] = "已注册"
        entry["pending"] = True

        return entry, f"数据源{entry['源编号']}已注册并完成Schema发现"

    def detect_schema_changes(self, source_id: int) -> dict[str, Any]:
        """检测数据源Schema变化并与已记录的语义模型做差异比较。"""
        entry = store.find(self.MODULE, source_id)
        if entry is None:
            return {"ok": False, "message": "数据源不存在"}

        current_schema = _discover_schema(entry.get("源类型", "unknown"))
        recorded = _get_semantic_models_for_source(source_id)
        recorded_fields = {r.get("字段别名", ""): r.get("字段别名", "") for r in recorded}

        new_fields = [f for f in current_schema if f not in recorded_fields]
        removed_fields = [f for f in recorded_fields if f not in current_schema]

        result = {
            "ok": True,
            "source_id": source_id,
            "source_type": entry.get("源类型"),
            "current_fields": current_schema,
            "recorded_fields_count": len(recorded_fields),
            "new_fields": new_fields,
            "removed_fields": removed_fields,
            "has_changes": len(new_fields) > 0 or len(removed_fields) > 0,
        }

        if result["has_changes"]:
            entry["接入状态"] = "Schema变更"
            entry["pending"] = True
            # 记录差异（写入湖仓读取模块的Schema变更追踪）
            _record_schema_change(source_id, new_fields, removed_fields)

        return result


# ---------- 跨模块辅助函数 ----------

def _discover_schema(source_type: str) -> list[str]:
    """模拟从不同数据源类型推导Schema字段列表。"""
    schemas = {
        "mysql": ["id", "user_id", "order_amount", "order_time", "region", "category", "status"],
        "postgresql": ["id", "event_id", "event_type", "event_time", "user_id", "properties"],
        "clickhouse": ["date", "dimension_key", "metric_value", "partition_id", "timestamp"],
        "hive": ["dt", "uid", "action", "action_time", "platform", "version", "extra"],
        "kafka": ["topic", "partition", "offset", "key", "value", "timestamp"],
    }
    return schemas.get(source_type, ["field_1", "field_2", "field_3", "field_4"])


def _write_semantic_model(source_id: int, source_no: str, schema: list[str]) -> None:
    from app.services import field_index

    rows = store.rows("semantic_model")
    model_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
    model = {
        "id": model_id,
        "模型编号": f"SEMA-S{source_id:04d}",
        "模型名称": f"自动发现_{source_no}",
        "源表映射": f"source_registry:{source_id}",
        "字段别名": ", ".join(schema),
        "度量定义": f"COUNT(*) FROM auto_discovered_{source_id}",
        "维度定义": ", ".join(schema[:3]),
        "计算口径": "自动推导",
        "版本序号": "v1.0-auto",
        "status": "已投产",
        "pending": True,
        "abnormal": False,
    }
    rows.append(model)

    # 同步登记字段身份映射与模型版本，历史改名仍能按当时映射反查。
    mappings = store.rows("model_field_mapping")
    for index, alias in enumerate(schema):
        mappings.append({
            "id": max((int(row.get("id", 0)) for row in mappings), default=0) + 1,
            "status": "可用",
            "pending": False,
            "abnormal": False,
            "model_id": model_id,
            "model_no": model["模型编号"],
            "model_version": "v1.0-auto",
            "source_id": source_id,
            "source_no": source_no,
            "字段别名": alias,
            "列名": alias,
            "is_current": True,
            "valid_from": time.strftime("%Y-%m-%d"),
            "valid_to": "",
        })
    field_index.rebuild_query_index(f"自动发现语义模型 {model['模型编号']}")


def _write_audit(source_id: int, source_no: str, action: str) -> None:
    rows = store.rows("query_audit")
    audit_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
    audit = {
        "id": audit_id,
        "记录编号": f"AUDIT-{uuid.uuid4().hex[:8]}",
        "查询语句": f"source_registry.{action}:{source_no}",
        "执行耗时": "120ms",
        "扫描行数": str(source_id),
        "返回行数": "1",
        "数据来源": "source_registry",
        "操作用户": "auto_discover",
        "查询时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "正常",
        "pending": False,
        "abnormal": False,
    }
    rows.append(audit)


def _create_contract_draft(source_id: int, source_no: str) -> None:
    rows = store.rows("data_contract")
    contract_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
    contract = {
        "id": contract_id,
        "契约编号": f"DATA-{uuid.uuid4().hex[:8]}",
        "数据集": f"auto_{source_no}",
        "Schema约束": "自动推导",
        "SLA定义": "每小时更新",
        "更新频率": "3600",
        "负责人": "auto_discover",
        "消费者": "semantic_model",
        "生效时间": time.strftime("%Y-%m-%d"),
        "status": "草稿",
        "pending": True,
        "abnormal": False,
    }
    rows.append(contract)


def _get_semantic_models_for_source(source_id: int) -> list[dict[str, Any]]:
    rows = store.rows("semantic_model")
    return [r for r in rows if f"source_registry:{source_id}" in str(r.get("源表映射", ""))]


def _record_schema_change(source_id: int, new_fields: list[str], removed_fields: list[str]) -> None:
    rows = store.rows("data_lake_read")
    change_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
    change = {
        "id": change_id,
        "路径编号": f"DATA-SC{source_id:04d}",
        "存储位置": f"source_registry/{source_id}",
        "文件格式": "schema_change",
        "分区模式": "N/A",
        "Schema推导": f"+{','.join(new_fields)} / -{','.join(removed_fields)}",
        "文件列表": "",
        "最后扫描": time.strftime("%Y-%m-%d %H:%M:%S"),
        "数据量": f"{len(new_fields) + len(removed_fields)}",
        "status": "Schema变更",
        "pending": True,
        "abnormal": False,
    }
    rows.append(change)