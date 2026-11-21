"""改写规则字段影响索引：把「活跃改写规则」与「语义模型字段映射」建立可反查的索引。

索引不是业务模块，而是查询改写、语义模型和字段变更清单之间的共享查询平面：

- 一次快照同时读取规则、字段映射和规则样本，避免查询过程中看到半份数据；
- 重建时先在锁外准备完整快照，再在锁内原子替换；同一份快照的并发重建
  只有一个调用被确认为「完整索引」，其余调用复用已确认结果；
- 索引保留字段映射的版本和有效期，因此历史改名仍能按当时的映射反查。

命中关系只有两种：

- ``confirmed``：规则身份（显式绑定或该来源的字段样本）与字段映射可以核对，
  是可确认的字段身份；
- ``possible``：只通过裸列名、历史映射或同名字段建立的关联，不能当成确认关系。

只有 confirmed 命中可以作为回归校验输入或触发字段处理；possible 命中只用于
提示人工确认，避免误停用仍然在另一份来源上运行的规则。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from typing import Any

from app.store import store

# 活跃规则：草稿和已废弃规则不参与字段影响查询。
ACTIVE_RULE_STATUSES: tuple[str, ...] = ("已生效", "待验证")

_SQL_KEYWORDS = frozenset({
    "select", "from", "where", "join", "left", "right", "inner", "outer", "on",
    "group", "by", "order", "having", "limit", "offset", "as", "and", "or", "not",
    "in", "is", "null", "case", "when", "then", "else", "end", "distinct", "all",
    "insert", "update", "delete", "into", "values", "set", "union", "unknown",
    "optimized", "always", "true", "false", "varchar", "numeric", "cast",
})
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SCOPE_RE = re.compile(r"semantic_model\s*:\s*(\d+)", re.IGNORECASE)

_lock = threading.RLock()
_state: dict[str, Any] = {
    "generation": 0,
    "fingerprint": "",
    "entries": [],
    "built_at": None,
    "completed_monotonic": 0.0,
    "reason": "",
}
_build_history: list[dict[str, Any]] = []


# ---------------------------------------------------------------------------
# 规则识别与字段映射
# ---------------------------------------------------------------------------

def rule_scope_model_id(rule: dict[str, Any]) -> int | None:
    """解析规则显式绑定的语义模型 id；旧规则通常没有这个绑定。"""
    raw = str(rule.get("适用模型", "") or "")
    match = _SCOPE_RE.search(raw)
    if match:
        return int(match.group(1))
    # 兼容按模型编号引用（例如 SEMA-0001）。
    for model in store.rows("semantic_model"):
        model_no = str(model.get("模型编号", "") or "")
        if model_no and model_no.casefold() in raw.casefold():
            return int(model.get("id", 0))
    return None


def rule_field_tokens(rule: dict[str, Any]) -> set[str]:
    """从原始模式中抽取字段名 token；只写列名的旧规则会得到一个裸列名。"""
    text = " ".join(
        str(rule.get(field, "") or "")
        for field in ("原始模式", "替换模式", "改写条件")
    )
    tokens = {token.casefold() for token in _TOKEN_RE.findall(text)}
    return {token for token in tokens if token not in _SQL_KEYWORDS}


def is_current_mapping(mapping: dict[str, Any]) -> bool:
    """字段映射是否是当前生效版本。"""
    value = mapping.get("is_current")
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"true", "1", "yes", "当前"}


def mapping_version(mapping: dict[str, Any]) -> str:
    """读取字段映射版本，兼容简写与中文键。"""
    return str(mapping.get("model_version") or mapping.get("模型版本") or "")


def mapping_version_of_model(model: dict[str, Any]) -> str:
    """读取模型当前版本号。"""
    return str(model.get("版本序号", "") or "")


def sample_values(row: dict[str, Any]) -> list[str]:
    """读取样本值，兼容列表和逗号分隔两种存储。"""
    raw = row.get("样本值", [])
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item)]
    return [item.strip() for item in str(raw or "").split(",") if item.strip()]


def samples_for_rule(rule_id: int) -> list[dict[str, Any]]:
    return [
        row for row in store.rows("rewrite_rule_sample")
        if int(row.get("rule_id", 0)) == rule_id and sample_values(row)
    ]


def _sample_state(
    sample_rows: list[dict[str, Any]], source_id: int, alias: str, column: str,
    renamed_from: str = "",
) -> str:
    """某一侧的样本对规则的真实作用：改写、未改写或缺少样本。

    ``not_affected`` 来自「样本逐字不变」：这类样本证明规则不改写该侧，
    因此该侧只能算可能的同名字段关联，不能当成可确认的字段身份。
    """
    state = "missing"
    for row in sample_rows:
        if int(row.get("source_id", 0)) != source_id:
            continue
        if not sample_values(row):
            continue
        sample_alias = str(row.get("字段别名", "") or "").casefold()
        sample_column = str(row.get("列名", "") or "").casefold()
        if not (
            sample_alias == alias
            or sample_column == column
            or (renamed_from and renamed_from in {sample_alias, sample_column})
        ):
            continue
        if _sample_changed(row):
            return "affects"
        state = "not_affected"
    return state


def _sample_changed(row: dict[str, Any]) -> bool:
    original = sample_values(row)
    rewritten = row.get("改写后")
    rewritten_values = (
        [str(item) for item in rewritten] if isinstance(rewritten, list)
        else [item.strip() for item in str(rewritten or "").split(",") if item.strip()]
    )
    return bool(rewritten_values) and original != rewritten_values


def sources_sharing_alias(model_id: int, field_alias: str) -> list[int]:
    """同一字段别名在其它模型/来源上的分布，用于核对旧规则的影响边界。"""
    alias = str(field_alias).casefold()
    sources = {
        int(mapping.get("source_id", 0))
        for mapping in store.rows("model_field_mapping")
        if is_current_mapping(mapping)
        and int(mapping.get("model_id", 0)) != model_id
        and str(mapping.get("字段别名", "") or "").casefold() == alias
    }
    return sorted(source for source in sources if source)


def _sources_for_tokens(tokens: set[str], model_id: int | None = None) -> set[int]:
    """当前映射中，字段别名或列名落在规则 token 范围内的来源。

    规则显式绑定某个模型时只统计该模型下的来源，避免把其它模型的同名字段
    误当成缺失样本；这直接决定 old rule 的可用时间是否被延后。
    """
    return {
        int(mapping.get("source_id", 0))
        for mapping in store.rows("model_field_mapping")
        if is_current_mapping(mapping)
        and (model_id is None or int(mapping.get("model_id", 0)) == model_id)
        and (
            str(mapping.get("字段别名", "") or "").casefold() in tokens
            or str(mapping.get("列名", "") or "").casefold() in tokens
            or str(mapping.get("renamed_from", "") or "").casefold() in tokens
        )
    }


def evaluate_rule_samples(rule_id: int) -> dict[str, Any]:
    """按来源逐侧应用规则样本，确认旧规则实际改写了哪一侧。

    ``fully_verified`` 只有在规则涉及的每个来源都有可用的字段样本时为真；
    缺少任一侧样本就无法确认该侧不被误改写，字段可用时间必须延后。
    """
    rule = store.find("query_rewrite", rule_id)
    samples = samples_for_rule(rule_id)
    tokens = rule_field_tokens(rule) if rule else set()

    effects: list[dict[str, Any]] = []
    changed_sources: list[int] = []
    unchanged_sources: list[int] = []
    for sample in samples:
        source_id = int(sample.get("source_id", 0))
        original = sample_values(sample)
        rewritten = sample.get("改写后")
        rewritten_values = (
            [str(item) for item in rewritten] if isinstance(rewritten, list)
            else [item.strip() for item in str(rewritten or "").split(",") if item.strip()]
        )
        if not rewritten_values:
            rewritten_values = list(original)
        changed = original != rewritten_values
        (changed_sources if changed else unchanged_sources).append(source_id)
        effects.append({
            "source_id": source_id,
            "source_no": sample.get("source_no") or sample.get("源编号", ""),
            "field_alias": sample.get("字段别名", ""),
            "column_name": sample.get("列名", ""),
            "original_values": original,
            "rewritten_values": rewritten_values,
            "changed": changed,
            "sample_note": sample.get("样本说明", ""),
        })

    sampled_sources = {int(sample.get("source_id", 0)) for sample in samples}
    scope_model_id = rule_scope_model_id(rule) if rule else None
    missing_sources = sorted(_sources_for_tokens(tokens, scope_model_id) - sampled_sources)

    return {
        "ok": True,
        "rule_id": rule_id,
        "rule_no": rule.get("规则编号", "") if rule else "",
        "effects": effects,
        "changed_sources": sorted(set(changed_sources)),
        "unchanged_sources": sorted(set(unchanged_sources)),
        "missing_sources": missing_sources,
        "fully_verified": bool(effects) and not missing_sources,
    }


def rule_sample_scope(rule_id: int) -> dict[str, Any]:
    """汇总规则的来源级样本作用范围。

    用于让只写裸列名的旧规则只改写样本证明确实受影响的那一侧：
    ``affected_sources`` 会被改写，``unaffected_sources`` 保持原值，
    ``unsampled_sources`` 是仍缺少样本、无法确认的当前来源。
    """
    result = evaluate_rule_samples(rule_id)
    sampled = {int(sample.get("source_id", 0)) for sample in samples_for_rule(rule_id)}
    rule = store.find("query_rewrite", rule_id)
    tokens = rule_field_tokens(rule) if rule else set()
    scope_model_id = rule_scope_model_id(rule) if rule else None
    return {
        "rule_id": rule_id,
        "affected_sources": result["changed_sources"],
        "unaffected_sources": result["unchanged_sources"],
        "unsampled_sources": sorted(_sources_for_tokens(tokens, scope_model_id) - sampled),
        "fully_verified": result["fully_verified"],
    }


# ---------------------------------------------------------------------------
# 索引快照与原子重建
# ---------------------------------------------------------------------------

def _collect_snapshot() -> list[dict[str, Any]]:
    """在锁外准备完整快照：规则、映射与样本一次性读完。"""
    rules = [dict(row) for row in store.rows("query_rewrite")
             if row.get("status") in ACTIVE_RULE_STATUSES]
    mappings = [dict(row) for row in store.rows("model_field_mapping")]
    samples = [dict(row) for row in store.rows("rewrite_rule_sample")]

    entries: list[dict[str, Any]] = []
    for rule in rules:
        tokens = rule_field_tokens(rule)
        if not tokens:
            continue
        rule_id = int(rule.get("id", 0))
        scope_model_id = rule_scope_model_id(rule)
        rule_samples = [row for row in samples if int(row.get("rule_id", 0)) == rule_id]

        for mapping in mappings:
            model_id = int(mapping.get("model_id", 0))
            if scope_model_id is not None and model_id != scope_model_id:
                continue
            alias = str(mapping.get("字段别名", "") or "").casefold()
            column = str(mapping.get("列名", "") or "").casefold()
            renamed_from = str(mapping.get("renamed_from", "") or "").casefold()
            matched_token = None
            match_kind = ""
            for token in sorted(tokens):
                if token == alias:
                    matched_token, match_kind = token, "alias"
                    break
                if token == column:
                    matched_token, match_kind = token, "column"
                    break
            if matched_token is None and renamed_from and renamed_from in tokens:
                matched_token, match_kind = renamed_from, "renamed_from"
            if matched_token is None:
                continue

            source_id = int(mapping.get("source_id", 0))
            sample_state = _sample_state(rule_samples, source_id, alias, column, renamed_from)
            entries.append({
                "rule_id": rule_id,
                "rule_no": rule.get("规则编号", ""),
                "rule_status": rule.get("status", ""),
                "rule_scope_model_id": scope_model_id,
                "rule_is_legacy": scope_model_id is None,
                "rule_tokens": sorted(tokens),
                "matched_token": matched_token,
                "match_kind": match_kind,
                "replacement": rule.get("替换模式", ""),
                "model_id": model_id,
                "model_no": mapping.get("model_no") or mapping.get("模型编号", ""),
                "model_version": mapping_version(mapping),
                "source_id": source_id,
                "source_no": mapping.get("source_no") or mapping.get("源编号", ""),
                "field_alias": mapping.get("字段别名", ""),
                "column_name": mapping.get("列名", ""),
                "renamed_from": mapping.get("renamed_from", ""),
                "is_current": is_current_mapping(mapping),
                "valid_from": mapping.get("valid_from", ""),
                "valid_to": mapping.get("valid_to", ""),
                "sample_scoped": sample_state == "affects",
                "sample_state": sample_state,
                "sample_sources": sorted({int(row.get("source_id", 0)) for row in rule_samples}),
            })
    entries.sort(key=lambda item: (
        item["rule_id"], item["model_id"], item["source_id"], item["model_version"], item["field_alias"],
    ))
    return entries


def _fingerprint(entries: list[dict[str, Any]]) -> str:
    payload = json.dumps(entries, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def rebuild_query_index(reason: str = "") -> dict[str, Any]:
    """原子重建查询索引。

    同一份快照的并发重建只会确认一个完整索引：后到的调用在锁内看到
    已完成的同指纹索引时，返回 ``confirmed=False`` 并复用该索引。
    快照准备失败时保留上一份完整索引，不会把半份索引暴露给查询。
    """
    started = time.monotonic()
    build_id = uuid.uuid4().hex[:8]
    try:
        snapshot = _collect_snapshot()
        fingerprint = _fingerprint(snapshot)
    except Exception as exc:  # 快照失败：旧索引仍然完整可用
        record = {
            "build_id": build_id,
            "ok": False,
            "confirmed": False,
            "reused": False,
            "message": f"索引重建失败，保留上一份完整索引: {exc}",
            "index_generation": _state["generation"],
            "reason": reason,
        }
        with _lock:
            _build_history.append(record)
        return record

    with _lock:
        if _state["generation"] > 0 and _state["fingerprint"] == fingerprint:
            record = {
                "build_id": build_id,
                "ok": True,
                "confirmed": False,
                "reused": True,
                "index_generation": _state["generation"],
                "fingerprint": fingerprint,
                "builds_confirmed": 1,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                "message": "同一份快照已有一份完整索引，本次复用，不重复确认",
                "reason": reason,
                "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            _build_history.append(record)
            return record

        generation = _state["generation"] + 1
        _state.update({
            "generation": generation,
            "fingerprint": fingerprint,
            "entries": snapshot,
            "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "completed_monotonic": time.monotonic(),
            "reason": reason,
        })
        record = {
            "build_id": build_id,
            "ok": True,
            "confirmed": True,
            "reused": False,
            "index_generation": generation,
            "fingerprint": fingerprint,
            "entry_count": len(snapshot),
            "builds_confirmed": 1,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            "message": "查询索引已完整重建并原子替换",
            "reason": reason,
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        _build_history.append(record)
        return record


def ensure_index() -> dict[str, Any]:
    """查询前确保有一份索引；数据已变化时先重建，避免读到过期快照。"""
    if _state["generation"] == 0:
        return rebuild_query_index("首次查询自动建立索引")
    if _state["fingerprint"] != _fingerprint(_collect_snapshot()):
        return rebuild_query_index("字段映射或活跃规则已变化，索引自动跟进")
    return current_index_status()


def current_index_status() -> dict[str, Any]:
    confirmed_builds = sum(1 for item in _build_history if item.get("confirmed"))
    return {
        "ok": True,
        "generation": _state["generation"],
        "fingerprint": _state["fingerprint"],
        "built_at": _state["built_at"],
        "entry_count": len(_state["entries"]),
        "complete": _state["generation"] > 0,
        "reason": _state["reason"],
        "confirmed_builds": confirmed_builds,
        "builds": list(_build_history),
    }


def reset_index() -> None:
    """仅测试或进程重启时使用。"""
    with _lock:
        _state.update({
            "generation": 0,
            "fingerprint": "",
            "entries": [],
            "built_at": None,
            "completed_monotonic": 0.0,
            "reason": "",
        })
        _build_history.clear()


# ---------------------------------------------------------------------------
# 影响查询
# ---------------------------------------------------------------------------

def _in_window(entry: dict[str, Any], as_of: str) -> bool:
    start = str(entry.get("valid_from", "") or "")
    end = str(entry.get("valid_to", "") or "")
    if start and as_of < start:
        return False
    if end and as_of > end:
        return False
    return True


def _resolve_model_id(model_id: int | None, model_no: str | None) -> int | None:
    if model_id:
        return int(model_id)
    if model_no:
        for model in store.rows("semantic_model"):
            if str(model.get("模型编号", "")).casefold() == str(model_no).casefold():
                return int(model.get("id", 0))
    return None


def _relationship(entry: dict[str, Any], *, exact_version: bool) -> str:
    """判定一条索引项的命中关系。

    ``exact_version`` 表示调用方指定了模型版本/时间点，此时该版本映射就是
    可确认身份；否则只有规则身份（绑定或该侧确实被改写的样本）与当前
    映射可核对时才算 confirmed。样本显示「未改写」的一侧只能算 possible。

    通过 ``renamed_from``（字段改名的前身别名）命中的当前映射只说明规则
    文本仍引用旧名，属于可能关联，需要人工决定是否跟进改名。
    """
    if entry.get("match_kind") == "renamed_from":
        return "possible"
    if entry["rule_scope_model_id"] == entry["model_id"]:
        return "confirmed"
    if entry["sample_scoped"]:
        return "confirmed"
    if exact_version and entry["sample_state"] == "missing":
        return "confirmed"
    return "possible"


def _hit(entry: dict[str, Any], relationship: str, reasons: list[str]) -> dict[str, Any]:
    return {
        "rule_id": entry["rule_id"],
        "rule_no": entry["rule_no"],
        "rule_status": entry["rule_status"],
        "relationship": relationship,
        "identity_status": "current" if entry["is_current"] else "historical",
        "model_id": entry["model_id"],
        "model_no": entry["model_no"],
        "model_version": entry["model_version"],
        "source_id": entry["source_id"],
        "source_no": entry["source_no"],
        "field_alias": entry["field_alias"],
        "column_name": entry["column_name"],
        "renamed_from": entry.get("renamed_from", ""),
        "matched_token": entry["matched_token"],
        "match_kind": entry["match_kind"],
        "replacement": entry["replacement"],
        "sample_sources": entry["sample_sources"],
        "sample_state": entry["sample_state"],
        "valid_from": entry["valid_from"],
        "valid_to": entry["valid_to"],
        "reasons": reasons,
    }


def query_field_impact(
    *,
    model_id: int | None = None,
    model_no: str | None = None,
    source_id: int | None = None,
    field: str | None = None,
    model_version: str | None = None,
    as_of: str | None = None,
    include_possible: bool = True,
    include_history: bool = True,
) -> dict[str, Any]:
    """查询字段改写影响：按模型、版本、来源和确切字段身份反查活跃规则。"""
    ensure_index()
    resolved_model_id = _resolve_model_id(model_id, model_no)
    field_key = str(field).casefold() if field else None
    entries = list(_state["entries"])

    def base_match(entry: dict[str, Any]) -> bool:
        if resolved_model_id is not None and entry["model_id"] != resolved_model_id:
            return False
        if source_id is not None and entry["source_id"] != int(source_id):
            return False
        if field_key is not None:
            alias = str(entry["field_alias"]).casefold()
            column = str(entry["column_name"]).casefold()
            if field_key not in (alias, column):
                return False
        return True

    candidates = [entry for entry in entries if base_match(entry)]
    exact_version = bool(model_version or as_of)
    selected: dict[tuple[int, int, int, str], dict[str, Any]] = {}

    def consider(entry: dict[str, Any], relationship: str, reasons: list[str]) -> None:
        key = (entry["rule_id"], entry["model_id"], entry["source_id"], entry["field_alias"])
        existing = selected.get(key)
        if existing is None or (existing["relationship"] != "confirmed" and relationship == "confirmed"):
            selected[key] = _hit(entry, relationship, reasons)

    for entry in candidates:
        historical = not entry["is_current"]
        if model_version and str(entry["model_version"]) != str(model_version):
            continue
        if as_of and not _in_window(entry, as_of):
            continue
        if historical and not exact_version:
            continue
        relationship = _relationship(entry, exact_version=exact_version)
        reasons = []
        if entry["rule_scope_model_id"] == entry["model_id"]:
            reasons.append("规则显式绑定该语义模型")
        if entry["sample_scoped"]:
            reasons.append("规则包含该来源的字段样本")
        if historical:
            reasons.append("命中历史字段映射，可按当时版本追查")
        if relationship == "possible":
            reasons.append("仅建立可能关联，需人工确认，不能直接停用规则")
        consider(entry, relationship, reasons)

    # 历史兜底：当前映射已无法命中时，仍保留历史映射线索，但只标为 possible，
    # 避免把改名前的旧规则当成当前字段身份。
    if include_history and not exact_version:
        for entry in candidates:
            if entry["is_current"]:
                continue
            consider(entry, "possible", [
                "仅命中历史字段映射",
                "不能确认当前字段身份，需人工核对",
            ])

    hits = list(selected.values())
    confirmed_hits = [hit for hit in hits if hit["relationship"] == "confirmed"]
    possible_hits = [hit for hit in hits if hit["relationship"] == "possible"]
    if not include_possible:
        hits = confirmed_hits

    return {
        "ok": True,
        "query": {
            "model_id": resolved_model_id,
            "model_no": model_no,
            "model_version": model_version,
            "source_id": source_id,
            "field": field,
            "as_of": as_of,
        },
        "index_generation": _state["generation"],
        "index_fingerprint": _state["fingerprint"],
        "hits": hits,
        "confirmed_hits": confirmed_hits,
        "possible_hits": possible_hits,
        "confirmed_field_identities": [
            {
                "rule_id": hit["rule_id"],
                "rule_no": hit["rule_no"],
                "model_id": hit["model_id"],
                "model_version": hit["model_version"],
                "source_id": hit["source_id"],
                "field_alias": hit["field_alias"],
                "column_name": hit["column_name"],
                "renamed_from": hit.get("renamed_from", ""),
                "identity_status": hit["identity_status"],
            }
            for hit in confirmed_hits
        ],
        "possible_associations": [
            {
                "rule_id": hit["rule_id"],
                "rule_no": hit["rule_no"],
                "model_id": hit["model_id"],
                "model_version": hit["model_version"],
                "source_id": hit["source_id"],
                "field_alias": hit["field_alias"],
                "column_name": hit["column_name"],
                "identity_status": hit["identity_status"],
                "reasons": hit["reasons"],
            }
            for hit in possible_hits
        ],
        "summary": {
            "active_rules": len({entry["rule_id"] for entry in entries}),
            "hit_rules": len({hit["rule_id"] for hit in hits}),
            "confirmed": len(confirmed_hits),
            "possible": len(possible_hits),
            "confirmed_rules": sorted({hit["rule_id"] for hit in confirmed_hits}),
            "possible_rules": sorted({hit["rule_id"] for hit in possible_hits}),
        },
    }
