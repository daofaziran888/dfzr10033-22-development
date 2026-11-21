/** 改写规则字段影响查询：前端只负责发起查询与展示，身份判定全部由后端完成。 */

import { fetchJson, request } from '@/api/client'

export type FieldIdentity = {
  rule_id: number
  rule_no: string
  model_id: number
  model_version: string
  source_id: number
  field_alias: string
  column_name: string
  identity_status: 'current' | 'historical'
}

export type PossibleAssociation = FieldIdentity & {
  reasons: string[]
}

export type FieldImpactSummary = {
  confirmed: number
  possible: number
  confirmed_rules: number[]
  possible_rules: number[]
  active_rules: number
  hit_rules: number
}

export type FieldImpactResult = {
  ok: boolean
  index_generation: number
  confirmed_field_identities: FieldIdentity[]
  possible_associations: PossibleAssociation[]
  summary: FieldImpactSummary
}

export type IndexStatus = {
  ok: boolean
  complete: boolean
  generation: number
  fingerprint: string
  entry_count: number
  confirmed_builds: number
  built_at: string | null
}

export type RuleSampleVerification = {
  ok: boolean
  rule_id: number
  rule_no: string
  rule_is_legacy: boolean
  changed_sources: number[]
  unchanged_sources: number[]
  missing_sources: number[]
  fully_verified: boolean
  message: string
  effects: {
    source_id: number
    source_no: string
    field_alias: string
    column_name: string
    original_values: string[]
    rewritten_values: string[]
    changed: boolean
    sample_note: string
  }[]
}

export type PromotionChecklist = {
  ok: boolean
  model_id: number
  model_version: string
  available_fields: string[]
  verified_rules: {
    rule_id: number
    rule_no: string
    fully_verified: boolean
    changed_sources: number[]
    unchanged_sources: number[]
    missing_sources: number[]
  }[]
  pending_columns: {
    change_id: number
    source_id: number
    field_alias: string
    status: string
  }[]
  rename_blockers: {
    change_id: number
    field_alias: string
    target_alias: string
    rule_ids: number[]
    missing_sources: number[]
    reason: string
  }[]
  can_promote_rename: boolean
}

export type FieldImpactQuery = {
  modelId?: number
  modelNo?: string
  sourceId?: number
  field?: string
  modelVersion?: string
  asOf?: string
  includePossible?: boolean
  includeHistory?: boolean
}

function toParams(query: FieldImpactQuery): string {
  const params = new URLSearchParams()
  if (query.modelId != null) params.set('model_id', String(query.modelId))
  if (query.modelNo) params.set('model_no', query.modelNo)
  if (query.sourceId != null) params.set('source_id', String(query.sourceId))
  if (query.field) params.set('field', query.field)
  if (query.modelVersion) params.set('model_version', query.modelVersion)
  if (query.asOf) params.set('as_of', query.asOf)
  if (query.includePossible === false) params.set('include_possible', 'false')
  if (query.includeHistory === false) params.set('include_history', 'false')
  return params.toString()
}

/** 按模型版本、来源和确切字段身份反查活跃改写规则。 */
export function queryFieldImpact(query: FieldImpactQuery): Promise<FieldImpactResult> {
  return fetchJson<FieldImpactResult>(`/api/tools/field-impact/query?${toParams(query)}`)
}

/** 索引状态：只有 complete 为真时才代表有一份完整索引。 */
export function loadFieldImpactIndex(): Promise<IndexStatus> {
  return fetchJson<IndexStatus>('/api/tools/field-impact/index')
}

/** 重建索引；并发重建时第二份返回 confirmed=false，会复用同一份完整索引。 */
export async function rebuildFieldImpactIndex(reason = ''): Promise<IndexStatus & {
  confirmed?: boolean
  reused?: boolean
  message?: string
}> {
  const response = await request(`/api/tools/field-impact/index/rebuild?reason=${encodeURIComponent(reason)}`, {
    method: 'POST',
  })
  if (!response.ok) throw new Error('查询索引重建失败')
  return await response.json()
}

/** 用同名字段在两边来源各自的样本验证旧规则实际改写了哪一侧。 */
export async function verifyRuleSamples(ruleId: number): Promise<RuleSampleVerification> {
  const response = await request(`/api/query_rewrite/${ruleId}/verify-samples`, { method: 'POST' })
  if (!response.ok) throw new Error('规则样本验证失败')
  return await response.json()
}

/** 投产更名前的核对：已验证规则、仍待定位的列和实际可用字段。 */
export function loadPromotionChecklist(modelId: number): Promise<PromotionChecklist> {
  return fetchJson<PromotionChecklist>(`/api/semantic_model/${modelId}/promotion-checklist`)
}

/** 字段影响查询结果中，只有 confirmed 身份可以进入回归输入。 */
export function confirmedRuleIds(result: FieldImpactResult): number[] {
  return result.summary.confirmed_rules
}

/** 可能关联只用于人工核对，不能据此停用规则。 */
export function possibleRuleIds(result: FieldImpactResult): number[] {
  return result.summary.possible_rules
}
