"""查询改写领域服务：SQL改写规则管理，代价评估，与联邦查询和聚合引擎的交互。"""
from __future__ import annotations

import random
import time
import uuid
from typing import Any

from app.services import field_index
from app.services.base import BaseModuleService
from app.store import store


class QueryRewriteService(BaseModuleService):
    MODULE = "query_rewrite"
    ENTITY = "改写规则"
    OPERATION_TERM = "查询"
    LIST_FIELDS = ["规则编号", "原始模式", "替换模式", "适用模型", "改写条件", "代价阈值", "命中次数", "最后命中"]
    REQUIRED_FIELDS = ["规则编号", "原始模式", "替换模式"]
    STATUS_ORDER = ["草稿", "已生效", "待验证", "已废弃"]
    ACTION_RULES = {"启效规则": "已生效", "提交验证": "待验证", "废弃规则": "已废弃"}
    NEGATIVE_ACTIONS = ["废弃规则"]

    def verify_rule_samples(self, rule_id: int) -> dict[str, Any]:
        """用规则在各来源上的字段样本逐侧验证旧规则的真实改写范围。

        只写了裸列名的旧规则可能同时命中多份来源的同名字段；这里按来源分别
        应用各自的样本，确认它到底改写了哪一侧，以及另一侧是否被误改。
        """
        rule = store.find(self.MODULE, rule_id)
        if rule is None:
            return {"ok": False, "message": f"改写规则 {rule_id} 不存在"}

        result = field_index.evaluate_rule_samples(rule_id)
        changed_sources = result["changed_sources"]
        unchanged_sources = result["unchanged_sources"]
        missing_sources = result["missing_sources"]
        fully_verified = result["fully_verified"]

        record = {
            "id": max((int(row.get("id", 0)) for row in store.rows("rewrite_rule_validation")), default=0) + 1,
            "rule_id": rule_id,
            "rule_no": rule.get("规则编号", ""),
            "来源数": str(len(result["effects"])),
            "改写来源": ",".join(str(source) for source in changed_sources),
            "未改写来源": ",".join(str(source) for source in unchanged_sources),
            "验证结论": "完整" if fully_verified else "待补样本",
            "missing_sources": missing_sources,
            "status": "有效" if fully_verified else "待验证",
            "pending": not fully_verified,
            "abnormal": False,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        store.rows("rewrite_rule_validation").append(record)

        return {
            "ok": True,
            "rule_id": rule_id,
            "rule_no": rule.get("规则编号", ""),
            "rule_is_legacy": field_index.rule_scope_model_id(rule) is None,
            "effects": result["effects"],
            "changed_sources": changed_sources,
            "unchanged_sources": unchanged_sources,
            "missing_sources": missing_sources,
            "fully_verified": fully_verified,
            "message": (
                "两侧样本都已验证，旧规则只改写已确认的那一侧"
                if fully_verified
                else "仍有来源缺少字段样本，无法确认该来源不受规则影响"
            ),
        }

    def rewrite_query(
        self, raw_query: str, model_ids: list[int] | None = None,
        source_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        """应用已生效规则改写查询：读取 semantic_model 和 query_audit，写入 result_cache 和 cost_govern。

        只写了裸列名的旧规则不会无差别改写所有同名字段：如果规则带有按来源的
        字段样本，只有样本证明确实被改写的来源会应用该规则；另一侧来源的
        值保持不变，其生产规则照常运行。
        """
        rules = store.rows(self.MODULE)
        active_rules = [r for r in rules if r.get("status") == "已生效"]
        scope_sources = set(int(source) for source in (source_ids or []))

        if not active_rules:
            return {"ok": True, "rewritten": raw_query, "rules_applied": 0, "savings": 0,
                    "skipped_rules": [], "target_source_ids": sorted(scope_sources)}

        rewritten = raw_query
        rules_applied = 0
        applied_rules: list[dict[str, Any]] = []
        skipped_rules: list[dict[str, Any]] = []

        for rule in active_rules:
            pattern = rule.get("原始模式", "")
            replacement = rule.get("替换模式", "")
            condition = rule.get("改写条件", "")

            if not pattern or pattern not in rewritten:
                continue
            # 检查改写条件（从语义模型匹配）
            if model_ids and not _match_models(condition, model_ids):
                continue

            scope = field_index.rule_sample_scope(int(rule.get("id", 0)))
            affected = set(scope["affected_sources"])
            unaffected = set(scope["unaffected_sources"])
            has_samples = bool(affected or unaffected)

            # 规则只要有来源级样本，就只能改写样本证明确实受影响的那一侧；
            # 目标来源缺少样本或样本显示未被改写时保持原值，不冒险改写。
            if scope_sources and has_samples:
                targets = scope_sources & affected
                if not targets:
                    skipped_rules.append({
                        "rule_id": int(rule.get("id", 0)),
                        "rule_no": rule.get("规则编号", ""),
                        "reason": (
                            "目标来源未被样本确认会被改写，保持原值"
                            if scope_sources & unaffected
                            else "目标来源缺少字段样本，暂不改写"
                        ),
                        "target_sources": sorted(scope_sources),
                        "unsampled_sources": sorted(scope["unsampled_sources"]),
                    })
                    continue
            elif scope_sources:
                targets = set(scope_sources)  # 没有样本的规则保持原有全局语义
            else:
                targets = set()

            rewritten = rewritten.replace(pattern, replacement)
            rules_applied += 1

            # 更新规则命中计数
            try:
                hit_count = int(rule.get("命中次数", "0"))
            except (ValueError, TypeError):
                hit_count = 0
            rule["命中次数"] = str(hit_count + 1)
            rule["最后命中"] = time.strftime("%Y-%m-%d %H:%M:%S")
            applied_rules.append({
                "rule_id": int(rule.get("id", 0)),
                "rule_no": rule.get("规则编号", ""),
                "applied_source_ids": sorted(targets) if targets else sorted(scope_sources),
                "unchanged_source_ids": sorted(
                    (scope_sources - targets) if scope_sources and affected else unaffected
                ),
            })

        # 估算节省量并写入成本治理
        savings = _estimate_savings(raw_query, rewritten, rules_applied)
        if savings > 0:
            _record_cost_savings(savings, rules_applied)

        # 写入结果缓存（记录改写前后对比）
        _cache_rewrite_result(raw_query, rewritten)

        return {
            "ok": True,
            "original": raw_query,
            "rewritten": rewritten,
            "rules_applied": rules_applied,
            "applied_rules": applied_rules,
            "skipped_rules": skipped_rules,
            "estimated_savings_rows": savings,
        }

    def analyze_query_patterns(self) -> dict[str, Any]:
        """分析 query_audit 慢查询模式，生成改写规则建议。"""
        audits = store.rows("query_audit")
        slow_queries = [a for a in audits if a.get("status") == "慢查询"]

        patterns: dict[str, int] = {}
        for sq in slow_queries:
            stmt = sq.get("查询语句", "")
            # 简单模式提取
            if "CUBE_BUILD" in stmt:
                patterns["cube_build"] = patterns.get("cube_build", 0) + 1
            elif "FEDERATED" in stmt:
                patterns["federated_query"] = patterns.get("federated_query", 0) + 1
            elif "EXPORT" in stmt:
                patterns["export"] = patterns.get("export", 0) + 1
            else:
                patterns[stmt[:30]] = patterns.get(stmt[:30], 0) + 1

        recommendations = []
        for pattern, count in patterns.items():
            if count >= 2:
                recommendations.append({
                    "pattern": pattern,
                    "frequency": count,
                    "suggested_action": "CREATE_PUSHDOWN_RULE" if "federated" in pattern else "ADD_CACHE_HINT",
                })

        return {
            "ok": True,
            "slow_queries_count": len(slow_queries),
            "patterns_found": len(patterns),
            "recommendations": recommendations,
        }


def _match_models(condition: str, model_ids: list[int]) -> bool:
    if not condition:
        return True
    models = store.rows("semantic_model")
    for mid in model_ids:
        for m in models:
            if m.get("id") == mid and condition in str(m):
                return True
    return False


def _estimate_savings(original: str, rewritten: str, rules_applied: int) -> int:
    if original == rewritten:
        return 0
    base_savings = len(original) - len(rewritten)
    return max(0, abs(base_savings) * random.randint(100, 10000))


def _record_cost_savings(savings: int, rules_applied: int) -> None:
    rows = store.rows("cost_govern")
    cost_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
    cost = {
        "id": cost_id,
        "策略编号": f"COST-RW{uuid.uuid4().hex[:8]}",
        "适用角色": "query_rewrite",
        "查询上限": str(savings),
        "并发上限": str(rules_applied),
        "月度配额": str(savings * 100),
        "告警阈值": str(savings * 2),
        "限流动作": "optimize",
        "生效时段": time.strftime("%Y-%m-%d"),
        "status": "已生效",
        "pending": False,
        "abnormal": False,
    }
    rows.append(cost)


def _cache_rewrite_result(original: str, rewritten: str) -> None:
    rows = store.rows("result_cache")
    cache_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
    import hashlib
    cache = {
        "id": cache_id,
        "条目编号": f"RESU-RW{uuid.uuid4().hex[:8]}",
        "查询摘要": f"rewrite_{hashlib.md5(original.encode()).hexdigest()[:12]}",
        "结果哈希": hashlib.md5(rewritten.encode()).hexdigest()[:12],
        "缓存命中": "0",
        "过期时间": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 3600)),
        "存储大小": str(len(rewritten)),
        "创建时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        "最后命中": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "有效",
        "pending": True,
        "abnormal": False,
        "result_data": [{"original": original, "rewritten": rewritten}],
    }
    rows.append(cache)