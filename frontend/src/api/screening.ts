import { ApiClient } from './request'

export interface ScreeningOrderBy { field: string; direction: 'asc' | 'desc' }
export interface ScreeningRunReq {
  market?: 'CN'
  date?: string | null
  adj?: 'qfq' | 'hfq' | 'none'
  conditions: any
  order_by?: ScreeningOrderBy[]
  limit?: number
  offset?: number
}

export interface ScreeningRunItem {
  code: string
  close?: number
  pct_chg?: number
  amount?: number
  ma20?: number
  rsi14?: number
  kdj_k?: number
  kdj_d?: number
  kdj_j?: number
  dif?: number
  dea?: number
  macd_hist?: number
}

export interface ScreeningRunResp { total: number; items: ScreeningRunItem[] }

// 筛选字段配置
export interface FieldInfo {
  name: string
  display_name: string
  field_type: string
  data_type: string
  description: string
  supported_operators: string[]
}

export interface FieldConfigResponse {
  fields: Record<string, FieldInfo>
  categories: Record<string, string[]>
}

// 行业列表响应
export interface IndustryOption {
  value: string
  label: string
  count: number
}

export interface IndustriesResponse {
  industries: IndustryOption[]
  total: number
}

export type StrategyMode = 'trend' | 'value'

export interface StrategyScreenItem {
  code: string
  symbol?: string
  name?: string
  market?: string
  close?: number
  score?: number
  mom_mid?: number
  excess_20?: number
  atr_pct?: number
  hard_stop?: number
  pe?: number
  pb?: number
  roe?: number
  total_mv?: number
  pct_chg?: number
  industry?: string
  mode?: StrategyMode
}

export interface StrategyScreenResp {
  mode: StrategyMode
  label: string
  as_of?: string | null
  market_ok?: boolean
  market_note?: string
  total: number
  items: StrategyScreenItem[]
}

export const screeningApi = {
  run: (payload: ScreeningRunReq, options?: { timeout?: number }) =>
    ApiClient.post<ScreeningRunResp>('/api/screening/run', payload, { timeout: options?.timeout ?? 120000 }),
  getFields: () => ApiClient.get<FieldConfigResponse>('/api/screening/fields'),
  getIndustries: () => ApiClient.get<IndustriesResponse>('/api/screening/industries'),
  strategyTrend: (limit = 20, options?: { timeout?: number }) =>
    ApiClient.post<StrategyScreenResp>('/api/screening/strategy/trend', { limit }, { timeout: options?.timeout ?? 180000 }),
  strategyValue: (limit = 20, options?: { timeout?: number }) =>
    ApiClient.post<StrategyScreenResp>('/api/screening/strategy/value', { limit }, { timeout: options?.timeout ?? 120000 }),
}

