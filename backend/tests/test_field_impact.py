"""改写规则字段影响查询的集成测试：索引、身份判定、样本验证与投产回归。"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import field_index
from app.services.query_rewrite import QueryRewriteService
from app.services.semantic_model import SemanticModelService
from app.store import store

client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_index():
    """每个用例使用独立的存储快照，避免改名/校验互相污染。"""
    snapshot = {name: [dict(row) for row in rows] for name, rows in store._tables.items()}
    field_index.reset_index()
    try:
        yield
    finally:
        store._tables = {name: [dict(row) for row in rows] for name, rows in snapshot.items()}
        field_index.reset_index()


class TestIndexRebuild:
    def test_concurrent_rebuild_confirms_one_complete_index(self):
        results: list[dict] = []
        barrier = threading.Barrier(8)

        def rebuild(index: int) -> None:
            barrier.wait()
            results.append(field_index.rebuild_query_index(f"并发 {index}"))

        threads = [threading.Thread(target=rebuild, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sum(1 for item in results if item["confirmed"]) == 1
        assert sum(1 for item in results if item.get("reused")) == len(results) - 1
        assert len({item["index_generation"] for item in results}) == 1
        status = field_index.current_index_status()
        assert status["confirmed_builds"] == 1
        assert status["complete"] and status["entry_count"] > 0

    def test_snapshot_failure_keeps_previous_index(self, monkeypatch):
        field_index.rebuild_query_index("初始")
        before = field_index.current_index_status()

        def broken() -> list[dict]:
            raise RuntimeError("映射表不可读")

        monkeypatch.setattr(field_index, "_collect_snapshot", broken)
        result = field_index.rebuild_query_index("故障重建")
        assert result["ok"] is False and not result["confirmed"]
        after = field_index.current_index_status()
        assert after["generation"] == before["generation"]
        assert after["fingerprint"] == before["fingerprint"]


class TestFieldImpactQuery:
    def test_confirmed_identity_only_for_affected_side(self):
        result = field_index.query_field_impact(model_id=1, field="user_id")
        assert [hit["rule_id"] for hit in result["confirmed_hits"]] == [2]
        assert result["possible_hits"] == []

        # 另一份来源的同名字段只是可能关联，不能当成确认身份。
        other = field_index.query_field_impact(model_id=2, field="user_id")
        assert other["confirmed_hits"] == []
        assert [hit["rule_id"] for hit in other["possible_hits"]] == [2]

    def test_unrelated_rules_are_not_returned(self):
        result = field_index.query_field_impact(field="user_id")
        rule_ids = {hit["rule_id"] for hit in result["hits"]}
        assert 3 not in rule_ids, "order_amount 上的生产规则不应被 user_id 命中"
        assert 1 not in rule_ids, "整句改写规则不应被识别为字段身份"

    def test_historical_rename_resolved_by_version_and_as_of(self):
        by_version = field_index.query_field_impact(model_id=1, model_version="v1.0")
        assert by_version["confirmed_field_identities"][0]["column_name"] == "uid"
        assert by_version["confirmed_field_identities"][0]["identity_status"] == "historical"

        by_date = field_index.query_field_impact(model_id=1, as_of="2026-03-01")
        assert by_date["confirmed_field_identities"][0]["model_version"] == "v1.0"
        assert by_date["confirmed_field_identities"][0]["column_name"] == "uid"

        # 改名之后的时间点不再命中旧列。
        after = field_index.query_field_impact(model_id=1, as_of="2026-09-01")
        assert all(item["column_name"] != "uid" for item in after["confirmed_field_identities"])

    def test_source_filter_scopes_rules(self):
        source_two = field_index.query_field_impact(source_id=2, field="user_id")
        assert source_two["confirmed_hits"] == []
        assert all(hit["source_id"] == 2 for hit in source_two["hits"])

    def test_include_possible_false_hides_associations(self):
        result = field_index.query_field_impact(field="user_id", include_possible=False)
        assert all(hit["relationship"] == "confirmed" for hit in result["hits"])
        assert result["possible_hits"], "底层仍应保留 possible 结果供核对"


class TestRuleSamples:
    def test_same_name_field_samples_split_two_sources(self):
        result = QueryRewriteService().verify_rule_samples(2)
        assert result["changed_sources"] == [1]
        assert result["unchanged_sources"] == [2]
        assert result["fully_verified"] is True
        assert result["missing_sources"] == []

    def test_missing_side_is_not_verified(self):
        original = list(store.rows("rewrite_rule_sample"))
        store.rows("rewrite_rule_sample")[:] = [
            row for row in original if not (row["rule_id"] == 2 and row["source_id"] == 2)
        ]
        try:
            result = QueryRewriteService().verify_rule_samples(2)
            assert result["fully_verified"] is False
            assert result["missing_sources"] == [2]
        finally:
            store.rows("rewrite_rule_sample")[:] = original

    def test_legacy_rule_rewrites_only_verified_side(self):
        service = QueryRewriteService()
        first = service.rewrite_query("SELECT user_id FROM demo", source_ids=[1])
        assert "CAST(user_id AS VARCHAR)" in first["rewritten"]
        assert first["rules_applied"] == 1

        second = service.rewrite_query("SELECT user_id FROM demo", source_ids=[2])
        assert second["rewritten"] == "SELECT user_id FROM demo"
        assert second["rules_applied"] == 0
        assert second["skipped_rules"][0]["rule_id"] == 2


class TestPromotionRegression:
    def test_promotion_uses_confirmed_hits_and_syncs_change_list(self):
        result = SemanticModelService().validate_and_promote(1)
        assert result["ok"] is True
        assert result["regression_inputs"], "应至少有一个可确认命中作为回归输入"
        assert all(item["rule_id"] == 2 for item in result["regression_inputs"])
        assert result["regression"]["ok"] is True
        assert result["verified_rule_ids"] == [2]

        sync = {item["change_id"]: item for item in result["field_change_sync"]}
        assert sync[1]["校验结果"] == "通过"
        assert sync[1]["可用时间"] == "本轮投产可用"

        change = next(row for row in store.rows("field_change_list") if row["id"] == 1)
        assert change["涉及规则"] == "2"
        assert change["校验结果"] == "通过"

    def test_promotion_without_other_side_sample_defers_rename(self):
        original = list(store.rows("rewrite_rule_sample"))
        store.rows("rewrite_rule_sample")[:] = [
            row for row in original if not (row["rule_id"] == 2 and row["source_id"] == 2)
        ]
        try:
            result = SemanticModelService().validate_and_promote(1)
            assert result["ok"] is True, "模型本身仍可投产"
            assert result["verified_rule_ids"] == []
            assert result["unverified_rule_ids"] == [2]
            blockers = result["promotion_checklist"]["rename_blockers"]
            assert [blocker["change_id"] for blocker in blockers] == [1]
            assert blockers[0]["missing_sources"] == [2]

            change = next(row for row in store.rows("field_change_list") if row["id"] == 1)
            assert change["校验结果"] == "待补样本"
            assert "待样本补全" in change["可用时间"]
        finally:
            store.rows("rewrite_rule_sample")[:] = original

    def test_checklist_reports_available_fields_and_pending_columns(self):
        checklist = SemanticModelService().build_promotion_checklist(1)
        assert checklist["available_fields"] == ["order_amount", "region", "user_id"]
        assert [item["change_id"] for item in checklist["pending_columns"]] == [2]
        assert [rule["rule_id"] for rule in checklist["verified_rules"]] == [2]

    def test_other_source_rule_keeps_running_during_promotion(self):
        result = SemanticModelService().validate_and_promote(1)
        running = {item["rule_id"]: item for item in result["production_rules_running"]}
        assert running[3]["status"] == "已生效"
        assert all(row["status"] == "已生效" for row in store.rows("query_rewrite")
                   if row["id"] in {1, 2, 3})


class TestRenamePromotion:
    def test_rename_closes_old_mapping_and_keeps_history(self):
        result = SemanticModelService().rename_field(
            1, field_alias="user_id", target_alias="buyer_id", source_id=1, new_version="v1.3")
        assert result["ok"] is True
        assert result["renamed"][0]["to_alias"] == "buyer_id"

        field_index.reset_index()
        old = field_index.query_field_impact(model_id=1, model_version="v1.2", field="user_id")
        assert old["confirmed_field_identities"][0]["column_name"] == "user_id"
        assert old["confirmed_field_identities"][0]["identity_status"] == "historical"

        # 更名前 2026-03 的历史列名仍可按当时映射追查。
        early = field_index.query_field_impact(model_id=1, as_of="2026-03-01", field="user_id")
        assert early["confirmed_field_identities"][0]["column_name"] == "uid"

        # 更名后旧别名不再作为当前身份出现。
        current = field_index.query_field_impact(model_id=1, field="buyer_id")
        assert all(hit["relationship"] == "possible" for hit in current["hits"])

    def test_rename_blocked_without_other_side_sample(self):
        original = list(store.rows("rewrite_rule_sample"))
        store.rows("rewrite_rule_sample")[:] = [
            row for row in original if not (row["rule_id"] == 2 and row["source_id"] == 2)
        ]
        try:
            result = SemanticModelService().rename_field(
                1, field_alias="user_id", target_alias="buyer_id", source_id=1)
            assert result["ok"] is False
            assert result["blockers"][0]["missing_sources"] == [2]
            # 旧映射没有被提前关闭。
            current_aliases = {
                mapping["字段别名"]
                for mapping in store.rows("model_field_mapping")
                if field_index.is_current_mapping(mapping) and mapping["model_id"] == 1
            }
            assert "user_id" in current_aliases and "buyer_id" not in current_aliases
        finally:
            store.rows("rewrite_rule_sample")[:] = original


class TestApi:
    def test_field_impact_api(self):
        response = client.get("/api/tools/field-impact/query",
                              params={"model_id": 1, "field": "user_id"})
        assert response.status_code == 200
        payload = response.json()
        assert payload["summary"]["confirmed_rules"] == [2]
        assert payload["summary"]["possible_rules"] == []

    def test_index_api_is_idempotent(self):
        first = client.post("/api/tools/field-impact/index/rebuild").json()
        second = client.post("/api/tools/field-impact/index/rebuild").json()
        assert first["confirmed"] is True
        assert second["confirmed"] is False and second["reused"] is True

    def test_verify_samples_api(self):
        response = client.post("/api/query_rewrite/2/verify-samples")
        assert response.status_code == 200
        payload = response.json()
        assert payload["changed_sources"] == [1]
        assert payload["unchanged_sources"] == [2]

    def test_promote_and_change_list_api(self):
        response = client.post("/api/semantic_model/1/promote")
        assert response.status_code == 200
        assert response.json()["ok"] is True

        changes = client.get("/api/tools/field-changes/list", params={"model_id": 1}).json()
        assert changes["total"] == 1
        assert changes["items"][0]["校验结果"] == "通过"
