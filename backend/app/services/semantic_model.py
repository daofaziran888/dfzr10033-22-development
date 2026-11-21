"""语义模型领域服务：管理表映射、字段别名、度量定义与计算口径，作为查询翻译层。"""
from __future__ import annotations

import time
import uuid
from typing import Any

from app.services import field_index
from app.services.base import BaseModuleService
from app.store import store


class SemanticModelService(BaseModuleService):
    MODULE = "semantic_model"
    ENTITY = "语义定义"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["模型编号", "模型名称", "源表映射", "字段别名", "度量定义", "维度定义", "计算口径", "版本序号"]
    REQUIRED_FIELDS = ["模型编号", "模型名称", "源表映射"]
    STATUS_ORDER = ["草稿", "校验中", "已投产", "已废弃"]
    ACTION_RULES = {"提交校验": "校验中", "投产模型": "已投产", "废弃模型": "已废弃"}
    NEGATIVE_ACTIONS = []

    def query_field_impact(self, **params: Any) -> dict[str, Any]:
        """定位字段变更涉及的活跃改写规则，供语义模型与下游回归使用。"""
        return field_index.query_field_impact(**params)

    def rebuild_query_index(self, reason: str = "语义模型保存变更") -> dict[str, Any]:
        """保存模型变更期间重建查询索引；同一快照的并发重建只确认一份完整索引。"""
        return field_index.rebuild_query_index(reason)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """登记语义模型：保存期间同步重建改写规则字段影响索引。"""
        entry, missing = super().create_entry(values)
        if entry is not None:
            field_index.rebuild_query_index(f"保存语义模型 {entry.get('模型编号', entry['id'])}")
        return entry, missing

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """工作台动作：投产模型走完整校验、字段影响反查和变更清单同步。"""
        if action == "投产模型":
            result = self.validate_and_promote(entry_id)
            entry = store.find(self.MODULE, entry_id)
            if result.get("ok"):
                return entry, "语义模型已投产，字段影响已核对"
            return entry, result.get("message", "投产失败")
        entry, message = super().run_action(entry_id, action)
        if entry is not None:
            field_index.rebuild_query_index(f"语义模型动作 {action}")
        return entry, message

    def validate_and_promote(
        self, model_id: int, *, rebuild_index: bool = True
    ) -> dict[str, Any]:
        """校验语义模型并投产：验证源表映射、字段别名与度量定义。

        跨模块数据流：
        - 读取 source_registry 校验源表是否存在
        - 保存变更时重建改写规则字段影响索引，并按确切字段身份反查活跃规则
        - 用可确认命中关系确定回归校验输入，校验结果同步到字段变更清单
        - 写入数据契约作为Schema承诺
        - 写入审计日志

        只有 confirmed 命中进入回归输入；possible 关联只提示人工核对，
        不会因此停用任何规则，另一份来源上的生产规则照常运行。
        """
        model = store.find(self.MODULE, model_id)
        if model is None:
            return {"ok": False, "message": "语义模型不存在"}

        source_ref = model.get("源表映射", "")
        validation_issues = []

        # 校验源表是否存在
        if "source_registry" in source_ref:
            sources = store.rows("source_registry")
            related = [s for s in sources if str(s.get("id")) in source_ref]
            if not related:
                validation_issues.append("关联的数据源不存在或已停用")

        # 校验字段别名不为空
        aliases = model.get("字段别名", "")
        if not aliases.strip() or aliases.strip() == ",":
            validation_issues.append("字段别名不能为空")

        # 校验度量定义
        measure = model.get("度量定义", "")
        if not measure.strip():
            validation_issues.append("度量定义不能为空")

        if validation_issues:
            model["status"] = "已废弃"
            model["abnormal"] = True
            return {
                "ok": False,
                "message": f"校验失败: {'; '.join(validation_issues)}",
                "issues": validation_issues,
            }

        # 投产前先在保存阶段重建索引，避免用半份字段映射做回归判定。
        if rebuild_index:
            self.rebuild_query_index(f"投产语义模型 {model.get('模型编号', model_id)}")

        # 按当前版本、来源和确切字段身份反查字段改写影响。
        model_version = str(model.get("版本序号", "") or "") or None
        source_ids = _source_ids_for_model(model)
        impact = field_index.query_field_impact(
            model_id=model_id,
            model_version=model_version,
            include_possible=True,
            include_history=True,
        )
        confirmed_hits = impact["confirmed_hits"]
        possible_hits = impact["possible_hits"]

        # 回归校验输入只取可确认的命中关系。
        regression_inputs = [
            {
                "rule_id": hit["rule_id"],
                "rule_no": hit["rule_no"],
                "source_id": hit["source_id"],
                "field_alias": hit["field_alias"],
                "column_name": hit["column_name"],
                "model_version": hit["model_version"],
            }
            for hit in confirmed_hits
        ]
        regression = _run_regression(regression_inputs)

        # 用两边各自的样本验证命中的旧规则；缺少样本会延后该字段的可用时间。
        sample_checks = [
            _rule_sample_check(hit["rule_id"]) for hit in _unique_rule_hits(confirmed_hits)
        ]
        verified_rule_ids = sorted({check["rule_id"] for check in sample_checks if check["fully_verified"]})
        unverified_rule_ids = sorted({check["rule_id"] for check in sample_checks if not check["fully_verified"]})

        checklist = self.build_promotion_checklist(model_id)
        rename_blockers = checklist["rename_blockers"]

        # 通过校验，投产模型
        model["status"] = "已投产"
        model["pending"] = False
        model["abnormal"] = False

        # 校验结果同步回字段变更清单。
        synced = _sync_field_change_list(
            model=model,
            model_version=model_version,
            regression=regression,
            blockers=rename_blockers,
        )

        # 更新数据契约
        _update_contract_on_promotion(model)

        # 写入审计
        _audit_promotion(model_id, model.get("模型编号", ""))

        return {
            "ok": True,
            "model_id": model_id,
            "status": "已投产",
            "fields_count": len(aliases.split(",")),
            "index": field_index.current_index_status(),
            "field_impact": {
                "confirmed": impact["confirmed_field_identities"],
                "possible": impact["possible_associations"],
            },
            "regression_inputs": regression_inputs,
            "regression": regression,
            "sample_checks": sample_checks,
            "verified_rule_ids": verified_rule_ids,
            "unverified_rule_ids": unverified_rule_ids,
            "promotion_checklist": checklist,
            "field_change_sync": synced,
            "production_rules_running": _production_rule_ids_excluding(verified_rule_ids),
        }

    def rename_field(
        self,
        model_id: int,
        *,
        field_alias: str,
        target_alias: str,
        source_id: int | None = None,
        new_version: str | None = None,
    ) -> dict[str, Any]:
        """投产一次字段更名：先核对影响，再关闭旧映射并登记新版本映射。

        只有 can_promote_rename 为真时才写入新映射；被阻塞时保留旧映射和
        字段变更清单中的待验证状态，另一来源上的规则继续按原范围运行。
        """
        model = store.find(self.MODULE, model_id)
        if model is None:
            return {"ok": False, "message": "语义模型不存在"}

        checklist = self.build_promotion_checklist(model_id)
        if checklist.get("rename_blockers"):
            return {
                "ok": False,
                "message": "更名暂不可用：仍有来源缺少字段样本，待核对后再投产",
                "blockers": checklist["rename_blockers"],
                "checklist": checklist,
            }

        old_version = field_index.mapping_version_of_model(model)
        version = new_version or old_version
        source_ids = [int(source_id)] if source_id else _source_ids_for_model(model)
        mappings = store.rows("model_field_mapping")
        today = time.strftime("%Y-%m-%d")
        updated: list[dict[str, Any]] = []

        # 先筛选再写入，避免边遍历边追加新映射。
        targets = [
            mapping for mapping in list(mappings)
            if int(mapping.get("model_id", 0)) == model_id
            and int(mapping.get("source_id", 0)) in source_ids
            and str(mapping.get("字段别名", "")) == field_alias
            and field_index.is_current_mapping(mapping)
        ]
        for mapping in targets:
            # 关闭旧映射，保留有效期，历史改名仍可按当时映射追查。
            mapping["is_current"] = False
            mapping["valid_to"] = today
            mapping["status"] = "历史"
            mappings.append({
                "id": max((int(row.get("id", 0)) for row in mappings), default=0) + 1,
                "status": "可用",
                "pending": False,
                "abnormal": False,
                "model_id": model_id,
                "model_no": model.get("模型编号", ""),
                "model_version": version,
                "source_id": int(mapping.get("source_id", 0)),
                "source_no": mapping.get("source_no", ""),
                "字段别名": target_alias,
                "列名": target_alias,
                "renamed_from": field_alias,
                "is_current": True,
                "valid_from": today,
                "valid_to": "",
            })
            updated.append({
                "source_id": int(mapping.get("source_id", 0)),
                "from_alias": field_alias,
                "to_alias": target_alias,
                "old_version": mapping.get("model_version", ""),
                "new_version": version,
            })

        if not updated:
            return {"ok": False, "message": f"没有可更名的当前字段映射：{field_alias}"}

        # 模型版本与字段别名一起推进，后续反查按新版本定位。
        if version:
            model["版本序号"] = version
        aliases = [item.strip() for item in str(model.get("字段别名", "")).split(",") if item.strip()]
        model["字段别名"] = ",".join(target_alias if alias == field_alias else alias for alias in aliases)

        for change in store.rows("field_change_list"):
            if (int(change.get("model_id", 0)) == model_id
                    and str(change.get("字段别名", "")) == field_alias
                    and change.get("变更类型") == "更名"):
                change["status"] = "已校验"
                change["pending"] = False
                change["可用时间"] = "本轮投产可用"

        rebuild = field_index.rebuild_query_index(f"投产更名字段 {field_alias} -> {target_alias}")
        return {
            "ok": True,
            "model_id": model_id,
            "renamed": updated,
            "model_version": version,
            "checklist": checklist,
            "index": rebuild,
        }

    def build_promotion_checklist(self, model_id: int) -> dict[str, Any]:
        """投产更名前的核对：已验证规则、仍待定位的列、实际可用字段。"""
        model = store.find(self.MODULE, model_id)
        if model is None:
            return {"ok": False, "message": "语义模型不存在"}

        version = str(model.get("版本序号", "") or "") or None
        source_ids = _source_ids_for_model(model)
        mappings = [
            mapping for mapping in store.rows("model_field_mapping")
            if int(mapping.get("model_id", 0)) == model_id
            and (version is None or field_index.mapping_version(mapping) == version)
            and field_index.is_current_mapping(mapping)
        ]
        available_fields = sorted({str(mapping.get("字段别名", "")) for mapping in mappings})
        rename_fields = {
            str(change.get("字段别名", ""))
            for change in store.rows("field_change_list")
            if int(change.get("model_id", 0)) == model_id and change.get("变更类型") == "更名"
        }

        # 已验证规则：规则在本次更名字段上的样本在两侧来源都齐全。
        verified_rules: list[dict[str, Any]] = []
        for hit in _unique_rule_hits(field_index.query_field_impact(
            model_id=model_id, model_version=version, include_history=False,
        )["confirmed_hits"]):
            if rename_fields and str(hit["field_alias"]) not in rename_fields:
                continue
            check = _rule_sample_check(int(hit["rule_id"]))
            verified_rules.append({**check, "rule_no": hit["rule_no"]})

        rename_blockers: list[dict[str, Any]] = []
        pending_columns: list[dict[str, Any]] = []
        for change in store.rows("field_change_list"):
            change_source = int(change.get("source_id", 0))
            same_alias = str(change.get("字段别名", "")) in rename_fields
            if int(change.get("model_id", 0)) == model_id:
                if change.get("变更类型") == "更名":
                    checks = [
                        _rule_sample_check(int(hit["rule_id"]))
                        for hit in _unique_rule_hits(field_index.query_field_impact(
                            model_id=model_id, source_id=change_source,
                            field=change.get("字段别名", ""), model_version=version,
                        )["confirmed_hits"])
                    ]
                    unverified = [check for check in checks if not check["fully_verified"]]
                    if unverified:
                        rename_blockers.append({
                            "change_id": change["id"],
                            "field_alias": change.get("字段别名", ""),
                            "target_alias": change.get("目标别名", ""),
                            "rule_ids": [check["rule_id"] for check in unverified],
                            "missing_sources": sorted({
                                source for check in unverified for source in check["missing_sources"]
                            }),
                            "reason": "旧规则仍有来源缺少字段样本，无法确认另一侧不受影响",
                        })
                if change.get("status") == "待定位" or not str(change.get("目标别名", "") or "").strip():
                    pending_columns.append({
                        "change_id": change["id"],
                        "source_id": change_source,
                        "field_alias": change.get("字段别名", ""),
                        "status": change.get("status", ""),
                    })
            elif same_alias and change_source not in source_ids and change.get("status") == "待定位":
                # 同一别名在另一来源上仍未定位的列，不能当成已确认字段。
                pending_columns.append({
                    "change_id": change["id"],
                    "source_id": change_source,
                    "field_alias": change.get("字段别名", ""),
                    "status": change.get("status", ""),
                })

        return {
            "ok": True,
            "model_id": model_id,
            "model_version": version,
            "available_fields": available_fields,
            "verified_rules": verified_rules,
            "pending_columns": pending_columns,
            "rename_blockers": rename_blockers,
            "can_promote_rename": not rename_blockers,
        }

    def detect_schema_conflicts(self, source_id: int) -> dict[str, Any]:
        """检测数据源Schema变化与已投产语义模型之间的冲突。

        跨模块数据流：
        - 读取 source_registry 的Schema记录
        - 读取 data_lake_read 的Schema变更记录
        - 比较语义模型字段定义，标记冲突
        """
        models = store.rows(self.MODULE)
        conflicts = []

        for model in models:
            if model.get("status") != "已投产":
                continue
            if str(source_id) not in str(model.get("源表映射", "")):
                continue

            current_fields = set(model.get("字段别名", "").split(","))
            current_fields = {f.strip() for f in current_fields if f.strip()}

            # 检测 data_lake_read 中的Schema变更
            lake_rows = store.rows("data_lake_read")
            for lake in lake_rows:
                schema_changes = lake.get("Schema推导", "")
                if "source_registry" in lake.get("存储位置", "") and str(source_id) in lake.get("存储位置", ""):
                    conflicts.append({
                        "model_id": model["id"],
                        "model_name": model.get("模型名称"),
                        "source_id": source_id,
                        "schema_change": schema_changes,
                        "current_fields": list(current_fields),
                    })

        return {
            "ok": True,
            "source_id": source_id,
            "conflict_count": len(conflicts),
            "conflicts": conflicts,
        }


def _source_ids_for_model(model: dict[str, Any]) -> list[int]:
    source_ids: list[int] = []
    for mapping in store.rows("model_field_mapping"):
        if int(mapping.get("model_id", 0)) == int(model.get("id", 0)):
            source_id = int(mapping.get("source_id", 0))
            if source_id and source_id not in source_ids:
                source_ids.append(source_id)
    if not source_ids:
        for source in store.rows("source_registry"):
            if str(source.get("id")) in str(model.get("源表映射", "")):
                source_ids.append(int(source.get("id", 0)))
    return sorted(source_ids)


def _unique_rule_hits(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按规则去重，回归校验与样本核对都以规则为最小单位。"""
    seen: dict[int, dict[str, Any]] = {}
    for hit in hits:
        seen.setdefault(int(hit["rule_id"]), hit)
    return [seen[rule_id] for rule_id in sorted(seen)]


def _run_regression(inputs: list[dict[str, Any]]) -> dict[str, Any]:
    """用可确认命中关系执行回归校验：只校验 confirmed 命中的规则输入。"""
    results = []
    for item in inputs:
        checks = field_index.evaluate_rule_samples(int(item["rule_id"]))
        results.append({
            "rule_id": item["rule_id"],
            "rule_no": item["rule_no"],
            "source_id": item["source_id"],
            "field_alias": item["field_alias"],
            "status": "通过" if checks["fully_verified"] else "待补样本",
            "changed_sources": checks["changed_sources"],
            "unchanged_sources": checks["unchanged_sources"],
            "missing_sources": checks["missing_sources"],
        })
    return {
        "ok": all(result["status"] == "通过" for result in results),
        "checked": len(results),
        "passed": sum(1 for result in results if result["status"] == "通过"),
        "results": results,
    }


def _rule_sample_check(rule_id: int) -> dict[str, Any]:
    check = field_index.evaluate_rule_samples(rule_id)
    return {
        "rule_id": rule_id,
        "fully_verified": check["fully_verified"],
        "changed_sources": check["changed_sources"],
        "unchanged_sources": check["unchanged_sources"],
        "missing_sources": check["missing_sources"],
    }


def _production_rule_ids_excluding(verified_rule_ids: list[int]) -> list[dict[str, Any]]:
    """列出投产期间必须照常运行的生产规则。"""
    running = []
    for rule in store.rows("query_rewrite"):
        if rule.get("status") != "已生效":
            continue
        running.append({
            "rule_id": int(rule.get("id", 0)),
            "rule_no": rule.get("规则编号", ""),
            "verified_in_promotion": int(rule.get("id", 0)) in verified_rule_ids,
            "status": rule.get("status"),
        })
    return running


def _sync_field_change_list(
    *,
    model: dict[str, Any],
    model_version: str | None,
    regression: dict[str, Any],
    blockers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把回归校验结果同步到字段变更清单，并记录受影响规则和可用时间。"""
    model_id = int(model.get("id", 0))
    blocked_change_ids = {int(blocker["change_id"]) for blocker in blockers}
    regression_by_source: dict[int, list[dict[str, Any]]] = {}
    for result in regression["results"]:
        regression_by_source.setdefault(int(result["source_id"]), []).append(result)
    synced: list[dict[str, Any]] = []

    for change in store.rows("field_change_list"):
        change_model = int(change.get("model_id", 0))
        change_source = int(change.get("source_id", 0))
        if change_model != model_id:
            continue
        if model_version and str(change.get("model_version", "")) and str(change.get("model_version")) != model_version:
            continue

        rule_ids = sorted({int(result["rule_id"]) for result in regression_by_source.get(change_source, [])})
        change["涉及规则"] = ", ".join(str(rule_id) for rule_id in rule_ids)
        results = regression_by_source.get(change_source, [])
        change["校验结果"] = "通过" if results and all(r["status"] == "通过" for r in results) else (
            "待补样本" if results else "无已确认规则"
        )

        if int(change["id"]) in blocked_change_ids or change.get("status") == "待定位":
            change["可用时间"] = "待样本补全并核对后再投产"
            change["pending"] = True
            if change.get("status") not in {"待定位", "待验证"}:
                change["status"] = "待验证"
        else:
            change["可用时间"] = "本轮投产可用"
            change["status"] = "已校验"
            change["pending"] = False
        synced.append({
            "change_id": int(change["id"]),
            "status": change.get("status"),
            "校验结果": change.get("校验结果"),
            "涉及规则": change.get("涉及规则"),
            "可用时间": change.get("可用时间"),
        })
    return synced


def _update_contract_on_promotion(model: dict[str, Any]) -> None:
    contracts = store.rows("data_contract")
    for c in contracts:
        if c.get("status") == "草稿" and model.get("模型名称", "") in str(c.get("数据集", "")):
            c["Schema约束"] = model.get("字段别名", "")
            c["status"] = "已承诺"


def _audit_promotion(model_id: int, model_no: str) -> None:
    rows = store.rows("query_audit")
    audit_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
    audit = {
        "id": audit_id,
        "记录编号": f"AUDIT-{uuid.uuid4().hex[:8]}",
        "查询语句": f"SEMANTIC_PROMOTE model={model_no}",
        "执行耗时": "200ms",
        "扫描行数": "1",
        "返回行数": "1",
        "数据来源": "semantic_model",
        "操作用户": "validator",
        "查询时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "正常",
        "pending": False,
        "abnormal": False,
    }
    rows.append(audit)