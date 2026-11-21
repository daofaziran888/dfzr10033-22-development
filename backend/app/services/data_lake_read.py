"""湖仓读取领域服务：数据湖路径注册、Schema自动推导、分区发现与差异追踪。"""
from __future__ import annotations

import random
import time
import uuid
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class DataLakeReadService(BaseModuleService):
    MODULE = "data_lake_read"
    ENTITY = "湖仓路径"
    OPERATION_TERM = "读取"
    LIST_FIELDS = ["路径编号", "存储位置", "文件格式", "分区模式", "Schema推导", "文件列表", "最后扫描", "数据量"]
    REQUIRED_FIELDS = ["路径编号", "存储位置", "文件格式"]
    STATUS_ORDER = ["待扫描", "已注册", "Schema变更", "已停用"]
    ACTION_RULES = {"启动扫描": "已注册", "注册路径": "Schema变更", "停用路径": "已停用"}
    NEGATIVE_ACTIONS = ["停用路径"]

    def scan_and_register(self, path_id: int) -> dict[str, Any]:
        """扫描湖仓路径：写入 source_registry、semantic_model、query_audit、data_contract。"""
        path_entry = store.find(self.MODULE, path_id)
        if path_entry is None:
            return {"ok": False, "message": "湖仓路径不存在"}

        storage = path_entry.get("存储位置", "")
        fmt = path_entry.get("文件格式", "parquet")
        partition = path_entry.get("分区模式", "dt")

        # 模拟扫描：生成文件列表和Schema
        files = [f"{storage}/dt={dt}/part-{i:05d}.{fmt}"
                 for dt in ["2026-11-01", "2026-11-02", "2026-11-03"]
                 for i in range(random.randint(1, 10))]

        schema = _derive_schema(fmt)
        data_size = random.randint(100 * 1024, 1024 * 1024 * 1024)  # 1GB range

        # 更新路径信息
        path_entry["文件列表"] = ", ".join(files[:5]) + f"... ({len(files)} files)"
        path_entry["Schema推导"] = ", ".join(schema)
        path_entry["最后扫描"] = time.strftime("%Y-%m-%d %H:%M:%S")
        path_entry["数据量"] = f"{data_size / (1024*1024):.0f}MB"
        path_entry["status"] = "已注册"
        path_entry["pending"] = False

        # 注册为数据源
        source_id = _register_as_source(storage, fmt, schema)

        # 创建语义模型
        _create_semantic_model_for_lake(source_id, storage, schema)

        # 写入审计
        _audit_lake_scan(path_id, storage, len(files))

        return {
            "ok": True,
            "path_id": path_id,
            "storage": storage,
            "files_found": len(files),
            "schema_fields": len(schema),
            "data_size_mb": round(data_size / (1024 * 1024)),
            "source_id": source_id,
        }

    def detect_schema_drift(self, path_id: int) -> dict[str, Any]:
        """检测湖仓 Schema 漂移：对比 source_registry，通知 freshness_mon，更新 data_contract。"""
        path_entry = store.find(self.MODULE, path_id)
        if path_entry is None:
            return {"ok": False, "message": "湖仓路径不存在"}

        storage = path_entry.get("存储位置", "")
        recorded_schema = path_entry.get("Schema推导", "").split(",")
        recorded_schema = [s.strip() for s in recorded_schema if s.strip()]

        # 重新扫描
        new_schema = _derive_schema(path_entry.get("文件格式", "parquet"))
        new_fields = [f for f in new_schema if f not in recorded_schema]
        removed_fields = [f for f in recorded_schema if f not in new_schema]

        has_drift = len(new_fields) > 0 or len(removed_fields) > 0

        if has_drift:
            path_entry["Schema推导"] = ", ".join(new_schema)
            path_entry["status"] = "Schema变更"
            path_entry["pending"] = True
            path_entry["最后扫描"] = time.strftime("%Y-%m-%d %H:%M:%S")

            # 通知 freshness_mon
            _notify_schema_drift(path_id, new_fields, removed_fields)

            # 更新数据契约
            _update_contract_on_drift(storage, has_drift)

        return {
            "ok": True,
            "path_id": path_id,
            "has_drift": has_drift,
            "new_fields": new_fields,
            "removed_fields": removed_fields,
            "recorded_count": len(recorded_schema),
            "new_count": len(new_schema),
        }

    def estimate_partition_scan_cost(
        self, path_id: int, date_range: tuple[str, str]
    ) -> dict[str, Any]:
        """估算分区扫描代价并写入 cost_govern。"""
        path_entry = store.find(self.MODULE, path_id)
        if path_entry is None:
            return {"ok": False, "message": "湖仓路径不存在"}

        file_count = len(path_entry.get("文件列表", "").split(","))
        estimated_rows = file_count * random.randint(1000, 100000)
        estimated_io = estimated_rows * 0.002  # MB

        # 写入成本治理
        cost_rows = store.rows("cost_govern")
        cost_id = max((int(r.get("id", 0)) for r in cost_rows), default=0) + 1
        cost = {
            "id": cost_id,
            "策略编号": f"COST-LK{path_id:04d}",
            "适用角色": "lake_reader",
            "查询上限": str(estimated_rows),
            "并发上限": str(file_count),
            "月度配额": str(estimated_rows * 30),
            "告警阈值": str(int(estimated_io * 1.5)),
            "限流动作": "throttle",
            "生效时段": f"{date_range[0]}~{date_range[1]}",
            "status": "已生效" if estimated_io < 5000 else "已告警",
            "pending": estimated_io >= 5000,
            "abnormal": estimated_io >= 10000,
        }
        cost_rows.append(cost)

        return {
            "ok": True,
            "estimated_rows": estimated_rows,
            "estimated_io_mb": round(estimated_io, 2),
            "file_count": file_count,
            "cost_entry_id": cost_id,
        }


def _derive_schema(fmt: str) -> list[str]:
    schemas = {
        "parquet": ["uid", "action", "action_time", "platform", "version", "event_type", "properties"],
        "orc": ["id", "user_id", "order_amount", "order_time", "region", "category"],
        "json": ["event_id", "event_type", "event_time", "payload", "source"],
        "csv": ["col_1", "col_2", "col_3", "col_4", "col_5"],
        "avro": ["user_id", "session_id", "page", "duration", "actions", "device"],
    }
    return schemas.get(fmt, ["field_1", "field_2", "field_3"])


def _register_as_source(storage: str, fmt: str, schema: list[str]) -> int:
    rows = store.rows("source_registry")
    source_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
    source = {
        "id": source_id,
        "源编号": f"SOUR-L{source_id:04d}",
        "源类型": fmt,
        "连接地址": storage,
        "驱动配置": f"lakehouse/{fmt}",
        "数据库名": storage.rsplit("/", 1)[-1],
        "认证方式": "lakehouse_iam",
        "接入状态": "已接入",
        "最近同步": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "已注册",
        "pending": True,
        "abnormal": False,
    }
    rows.append(source)
    return source_id


def _create_semantic_model_for_lake(source_id: int, storage: str, schema: list[str]) -> None:
    from app.services import field_index

    rows = store.rows("semantic_model")
    model_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
    model = {
        "id": model_id,
        "模型编号": f"SEMA-L{source_id:04d}",
        "模型名称": f"湖仓自动发现_{storage.rsplit('/',1)[-1]}",
        "源表映射": f"data_lake_read:{source_id}",
        "字段别名": ", ".join(schema),
        "度量定义": "COUNT(*), SUM(metrics)",
        "维度定义": ", ".join(schema[:3]),
        "计算口径": "湖仓自动推导",
        "版本序号": "v1.0-lake",
        "status": "已投产",
        "pending": True,
        "abnormal": False,
    }
    rows.append(model)

    # 同步登记字段身份映射，湖仓自动发现的字段也能参与改写规则反查。
    mappings = store.rows("model_field_mapping")
    for alias in schema:
        mappings.append({
            "id": max((int(row.get("id", 0)) for row in mappings), default=0) + 1,
            "status": "可用",
            "pending": False,
            "abnormal": False,
            "model_id": model_id,
            "model_no": model["模型编号"],
            "model_version": "v1.0-lake",
            "source_id": source_id,
            "source_no": f"SOUR-L{source_id:04d}",
            "字段别名": alias,
            "列名": alias,
            "is_current": True,
            "valid_from": time.strftime("%Y-%m-%d"),
            "valid_to": "",
        })
    field_index.rebuild_query_index(f"湖仓自动发现语义模型 {model['模型编号']}")


def _audit_lake_scan(path_id: int, storage: str, file_count: int) -> None:
    rows = store.rows("query_audit")
    audit_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
    audit = {
        "id": audit_id,
        "记录编号": f"AUDIT-{uuid.uuid4().hex[:8]}",
        "查询语句": f"LAKE_SCAN path_id={path_id} storage={storage}",
        "执行耗时": "500ms",
        "扫描行数": str(file_count),
        "返回行数": str(file_count),
        "数据来源": "data_lake_read",
        "操作用户": "lake_scanner",
        "查询时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "正常",
        "pending": False,
        "abnormal": False,
    }
    rows.append(audit)


def _notify_schema_drift(path_id: int, new_fields: list[str], removed_fields: list[str]) -> None:
    rows = store.rows("freshness_mon")
    mon_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
    mon = {
        "id": mon_id,
        "规则编号": f"FRES-D{path_id:04d}",
        "目标数据集": f"data_lake_read:{path_id}",
        "SLA秒数": "3600",
        "检测频率": "on_change",
        "延迟阈值": "Schema漂移检测",
        "告警级别": "WARNING" if len(new_fields) > 2 else "INFO",
        "触发次数": "1",
        "最后检查": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "已告警",
        "pending": True,
        "abnormal": len(removed_fields) > 0,
    }
    rows.append(mon)


def _update_contract_on_drift(storage: str, has_drift: bool) -> None:
    contracts = store.rows("data_contract")
    for c in contracts:
        if storage in str(c.get("数据集", "")):
            if has_drift and c.get("status") == "已承诺":
                c["status"] = "已违约"