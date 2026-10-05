<template>
  <div class="stock-screening">
    <div class="page-header">
      <h1 class="page-title">
        <el-icon><Search /></el-icon>
        策略选股
      </h1>
      <p class="page-description">
        一键运行趋势 / 价值策略，直接列出符合规则的候选股
      </p>
    </div>

    <el-card class="strategy-panel" shadow="never">
      <div class="strategy-actions">
        <el-button
          type="primary"
          size="large"
          class="strategy-btn"
          :loading="screeningLoading && activeMode === 'trend'"
          :disabled="screeningLoading"
          @click="runStrategy('trend')"
        >
          <el-icon><TrendCharts /></el-icon>
          趋势交易
        </el-button>
        <el-button
          type="success"
          size="large"
          class="strategy-btn"
          :loading="screeningLoading && activeMode === 'value'"
          :disabled="screeningLoading"
          @click="runStrategy('value')"
        >
          <el-icon><Coin /></el-icon>
          价值交易
        </el-button>
        <el-button size="large" :disabled="screeningLoading" @click="resetScreen">
          <el-icon><Refresh /></el-icon>
          重置
        </el-button>
      </div>
      <div class="strategy-hints">
        <p><strong>趋势交易</strong>：close &gt; MA60 &gt; MA250，ATR%&lt;5%，中期动量&gt;10%，跑赢大盘；排序优先<strong>回调买点</strong>（贴近 MA20、短线回撤），其次 S4 评分</p>
        <p><strong>价值交易</strong>：PE 5~25，PB≤3，ROE≥15%，市值≥50亿，排除 ST/金融；排序优先<strong>右侧起步</strong>（站上均线且中期刚转正），其次 ROE/PE</p>
      </div>
    </el-card>

    <el-card v-if="screeningResults.length > 0" class="results-panel" shadow="never">
      <template #header>
        <div class="card-header">
          <div class="result-title">
            <span>筛选结果 ({{ screeningResults.length }}只)</span>
            <el-tag v-if="modeLabel" type="primary" effect="plain">{{ modeLabel }}</el-tag>
            <el-tag v-if="asOfDate" type="info" size="small">交易日 {{ asOfDate }}</el-tag>
            <el-tag v-if="fromCache" type="success" size="small">缓存 · 1天</el-tag>
            <el-tag v-if="marketNote" :type="marketOk ? 'warning' : 'danger'" size="small">
              {{ marketNote }}
            </el-tag>
          </div>
          <div class="header-actions">
            <el-button
              type="primary"
              @click="batchAnalyze"
              :disabled="selectedStocks.length === 0"
            >
              <el-icon><TrendCharts /></el-icon>
              批量分析 ({{ selectedStocks.length }})
            </el-button>
            <el-button type="text" @click="exportResults">
              <el-icon><Download /></el-icon>
              导出结果
            </el-button>
          </div>
        </div>
      </template>

      <el-table
        :data="paginatedResults"
        @selection-change="handleSelectionChange"
        stripe
        style="width: 100%"
      >
        <el-table-column type="selection" width="55" />

        <el-table-column label="排名" width="70" align="center">
          <template #default="{ $index }">
            <span class="rank-badge" :class="rankClass((currentPage - 1) * pageSize + $index + 1)">
              {{ (currentPage - 1) * pageSize + $index + 1 }}
            </span>
          </template>
        </el-table-column>

        <el-table-column prop="code" label="股票代码" width="110">
          <template #default="{ row }">
            <el-link type="primary" @click.prevent="viewStockDetail(row)">
              {{ row.code }}
            </el-link>
          </template>
        </el-table-column>

        <el-table-column prop="name" label="股票名称" width="130" />

        <el-table-column prop="setup_tag" label="形态" width="100" align="center">
          <template #default="{ row }">
            <el-tag
              v-if="row.setup_tag"
              size="small"
              :type="setupTagType(row.setup_tag)"
              effect="plain"
            >
              {{ row.setup_tag }}
            </el-tag>
            <span v-else class="text-muted">-</span>
          </template>
        </el-table-column>

        <!-- 趋势列 -->
        <template v-if="activeMode === 'trend'">
          <el-table-column prop="close" label="收盘" width="100" align="right">
            <template #default="{ row }">
              <span v-if="row.close != null">¥{{ Number(row.close).toFixed(2) }}</span>
              <span v-else class="text-muted">-</span>
            </template>
          </el-table-column>
          <el-table-column prop="score" label="评分" width="90" align="right">
            <template #default="{ row }">
              {{ row.score != null ? Number(row.score).toFixed(1) : '-' }}
            </template>
          </el-table-column>
          <el-table-column prop="mom_mid" label="中期动量%" width="110" align="right">
            <template #default="{ row }">
              <span :class="getChangeClass(row.mom_mid)">
                {{ formatSigned(row.mom_mid) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column prop="excess_20" label="20日超额%" width="110" align="right">
            <template #default="{ row }">
              <span :class="getChangeClass(row.excess_20)">
                {{ formatSigned(row.excess_20) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column prop="atr_pct" label="ATR%" width="90" align="right">
            <template #default="{ row }">
              {{ row.atr_pct != null ? Number(row.atr_pct).toFixed(2) : '-' }}
            </template>
          </el-table-column>
          <el-table-column prop="hard_stop" label="硬止损价" width="110" align="right">
            <template #default="{ row }">
              {{ row.hard_stop != null ? Number(row.hard_stop).toFixed(2) : '-' }}
            </template>
          </el-table-column>
        </template>

        <!-- 价值列 -->
        <template v-else>
          <el-table-column prop="pe" label="PE" width="90" align="right">
            <template #default="{ row }">
              {{ row.pe != null ? Number(row.pe).toFixed(2) : '-' }}
            </template>
          </el-table-column>
          <el-table-column prop="pb" label="PB" width="90" align="right">
            <template #default="{ row }">
              {{ row.pb != null ? Number(row.pb).toFixed(2) : '-' }}
            </template>
          </el-table-column>
          <el-table-column prop="roe" label="ROE(%)" width="100" align="right">
            <template #default="{ row }">
              {{ row.roe != null ? Number(row.roe).toFixed(2) : '-' }}
            </template>
          </el-table-column>
          <el-table-column prop="total_mv" label="市值(亿)" width="110" align="right">
            <template #default="{ row }">
              {{ row.total_mv != null ? Number(row.total_mv).toFixed(2) : '-' }}
            </template>
          </el-table-column>
          <el-table-column prop="score" label="得分(ROE/PE)" width="120" align="right">
            <template #default="{ row }">
              {{ row.score != null ? Number(row.score).toFixed(2) : '-' }}
            </template>
          </el-table-column>
          <el-table-column prop="pct_chg" label="涨跌幅" width="100" align="right">
            <template #default="{ row }">
              <span v-if="row.pct_chg != null" :class="getChangeClass(row.pct_chg)">
                {{ formatSigned(row.pct_chg) }}
              </span>
              <span v-else class="text-muted">-</span>
            </template>
          </el-table-column>
          <el-table-column prop="industry" label="行业" min-width="100" />
        </template>

        <el-table-column label="操作" width="180" fixed="right">
          <template #default="{ row }">
            <el-button type="text" size="small" @click="analyzeSingle(row)">
              分析
            </el-button>
            <el-button type="text" size="small" @click="toggleFavorite(row)">
              <el-icon><Star /></el-icon>
              {{ isFavorited(row.code) ? '取消自选' : '加入自选' }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="pagination-wrapper">
        <el-pagination
          v-model:current-page="currentPage"
          v-model:page-size="pageSize"
          :page-sizes="[20, 50, 100]"
          :total="screeningResults.length"
          layout="total, sizes, prev, pager, next, jumper"
          @size-change="handleSizeChange"
          @current-change="handleCurrentChange"
        />
      </div>
    </el-card>

    <el-empty
      v-else-if="!screeningLoading && hasSearched"
      description="未找到符合条件的股票"
      :image-size="180"
    >
      <el-button type="primary" @click="resetScreen">重新筛选</el-button>
    </el-empty>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Search, Refresh, TrendCharts, Download, Star, Coin } from '@element-plus/icons-vue'
import type { StockInfo } from '@/types/analysis'
import { screeningApi, type StrategyMode } from '@/api/screening'
import { favoritesApi } from '@/api/favorites'
import { normalizeMarketForAnalysis, exchangeCodeToMarket, getMarketByStockCode } from '@/utils/market'

const screeningLoading = ref(false)
const hasSearched = ref(false)
const screeningResults = ref<StockInfo[]>([])
const selectedStocks = ref<StockInfo[]>([])
const currentPage = ref(1)
const pageSize = ref(20)
const activeMode = ref<StrategyMode | ''>('')
const modeLabel = ref('')
const asOfDate = ref('')
const marketOk = ref(true)
const marketNote = ref('')
const fromCache = ref(false)

const router = useRouter()
const favoriteSet = ref<Set<string>>(new Set())

const paginatedResults = computed(() => {
  const start = (currentPage.value - 1) * pageSize.value
  const end = start + pageSize.value
  return screeningResults.value.slice(start, end)
})

const setupTagType = (tag: string) => {
  if (tag === '回调买点' || tag === '右侧起步') return 'success'
  if (tag === '偏右侧' || tag === '趋势中') return 'warning'
  if (tag === '偏高位' || tag === '仍左侧') return 'danger'
  return 'info'
}

const mapItems = (items: any[]): StockInfo[] =>
  items.map((it: any) => ({
    symbol: it.symbol || it.code,
    code: it.symbol || it.code,
    name: it.name || it.symbol || it.code,
    market: it.market || 'A股',
    industry: it.industry,
    close: it.close,
    pct_chg: it.pct_chg,
    total_mv: it.total_mv,
    pe: it.pe,
    pb: it.pb,
    roe: it.roe,
    score: it.score,
    mom_mid: it.mom_mid,
    excess_20: it.excess_20,
    atr_pct: it.atr_pct,
    hard_stop: it.hard_stop,
    setup_tag: it.setup_tag,
    setup_score: it.setup_score,
  })) as StockInfo[]

const runStrategy = async (mode: StrategyMode) => {
  screeningLoading.value = true
  hasSearched.value = true
  activeMode.value = mode
  screeningResults.value = []
  selectedStocks.value = []
  currentPage.value = 1
  modeLabel.value = ''
  asOfDate.value = ''
  marketNote.value = ''
  marketOk.value = true
  fromCache.value = false

  try {
    const res: any =
      mode === 'trend'
        ? await screeningApi.strategyTrend(20, { timeout: 180000 })
        : await screeningApi.strategyValue(20, { timeout: 120000 })

    const data = res?.data || res
    const items = data?.items || []
    modeLabel.value = data?.label || (mode === 'trend' ? '趋势交易' : '价值交易')
    asOfDate.value = data?.as_of || ''
    marketOk.value = data?.market_ok !== false
    marketNote.value = data?.market_note || ''
    fromCache.value = !!data?.cached
    screeningResults.value = mapItems(items)

    if (marketNote.value && !marketOk.value) {
      ElMessage.warning(marketNote.value)
    } else if (marketNote.value && items.length > 0) {
      ElMessage.warning(marketNote.value)
    }

    if (items.length === 0) {
      ElMessage.info(marketNote.value || '当日无符合条件标的')
    } else {
      ElMessage.success(`${modeLabel.value}：找到 ${items.length} 只股票`)
    }
  } catch (error: any) {
    console.error('策略选股失败', error)
    ElMessage.error(error?.message || '策略选股失败，请稍后重试')
  } finally {
    screeningLoading.value = false
  }
}

const resetScreen = () => {
  screeningResults.value = []
  selectedStocks.value = []
  hasSearched.value = false
  currentPage.value = 1
  activeMode.value = ''
  modeLabel.value = ''
  asOfDate.value = ''
  marketNote.value = ''
  marketOk.value = true
  fromCache.value = false
}

const handleSelectionChange = (selection: StockInfo[]) => {
  selectedStocks.value = selection
}

const batchAnalyze = async () => {
  if (selectedStocks.value.length === 0) {
    ElMessage.warning('请先选择要分析的股票')
    return
  }
  try {
    await ElMessageBox.confirm(
      `确定要对选中的 ${selectedStocks.value.length} 只股票进行批量分析吗？`,
      '确认批量分析',
      { confirmButtonText: '确定', cancelButtonText: '取消', type: 'info' }
    )
    router.push({
      name: 'BatchAnalysis',
      query: {
        stocks: selectedStocks.value.map(s => s.code || s.symbol || '').filter(Boolean).join(','),
        market: normalizeMarketForAnalysis('A股')
      }
    })
  } catch {
    // cancel
  }
}

const analyzeSingle = (stock: StockInfo) => {
  const stockCode = stock.code || stock.symbol || ''
  if (!stockCode) return
  router.push({
    name: 'SingleAnalysis',
    query: {
      stock: stockCode,
      market: normalizeMarketForAnalysis((stock as any).market || 'A股')
    }
  })
}

const viewStockDetail = (stock: StockInfo) => {
  const stockCode = stock.code || stock.symbol || ''
  if (!stockCode) return
  const { href } = router.resolve({ name: 'StockDetail', params: { code: stockCode } })
  window.open(href, '_blank', 'noopener,noreferrer')
}

const rankClass = (rank: number) => {
  if (rank === 1) return 'rank-1'
  if (rank === 2) return 'rank-2'
  if (rank === 3) return 'rank-3'
  return ''
}

const isFavorited = (code: string) => favoriteSet.value.has(code)

const toggleFavorite = async (stock: StockInfo) => {
  try {
    const code = stock.code || stock.symbol || ''
    if (!code) {
      ElMessage.error('股票代码缺失，无法加入自选')
      return
    }
    if (favoriteSet.value.has(code)) {
      const res = await favoritesApi.remove(code)
      if ((res as any)?.success === false) throw new Error((res as any)?.message || '取消失败')
      favoriteSet.value.delete(code)
      ElMessage.success(`已取消自选：${stock.name || code}`)
    } else {
      let marketType = 'A股'
      if ((stock as any).market) {
        marketType = exchangeCodeToMarket((stock as any).market)
      } else {
        marketType = getMarketByStockCode(code)
      }
      const res = await favoritesApi.add({
        symbol: code,
        stock_code: code,
        stock_name: stock.name || code,
        market: marketType
      })
      if ((res as any)?.success === false) throw new Error((res as any)?.message || '添加失败')
      favoriteSet.value.add(code)
      ElMessage.success(`已加入自选：${stock.name || code}`)
    }
  } catch (error: any) {
    ElMessage.error(error?.message || '自选操作失败')
  }
}

const exportResults = () => {
  ElMessage.info('导出功能开发中...')
}

const getChangeClass = (v?: number | null) => {
  if (v == null) return ''
  if (v > 0) return 'text-red'
  if (v < 0) return 'text-green'
  return ''
}

const formatSigned = (v?: number | null) => {
  if (v == null || Number.isNaN(Number(v))) return '-'
  const n = Number(v)
  return `${n > 0 ? '+' : ''}${n.toFixed(2)}%`
}

const handleSizeChange = (size: number) => {
  pageSize.value = size
  currentPage.value = 1
}

const handleCurrentChange = (page: number) => {
  currentPage.value = page
}

const loadFavorites = async () => {
  try {
    const resp = await favoritesApi.list()
    const list = (resp as any)?.data || resp
    const set = new Set<string>()
    ;(list || []).forEach((item: any) => {
      const code = item.symbol || item.stock_code || item.code
      if (code) set.add(code)
    })
    favoriteSet.value = set
  } catch (e) {
    console.warn('加载自选列表失败', e)
  }
}

onMounted(() => {
  loadFavorites()
})
</script>

<style lang="scss" scoped>
.stock-screening {
  .page-header {
    margin-bottom: 24px;

    .page-title {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 24px;
      font-weight: 600;
      color: var(--el-text-color-primary);
      margin: 0 0 8px 0;
    }

    .page-description {
      color: var(--el-text-color-regular);
      margin: 0;
    }
  }

  .strategy-panel {
    margin-bottom: 24px;

    .strategy-actions {
      display: flex;
      justify-content: center;
      flex-wrap: wrap;
      gap: 16px;
    }

    .strategy-btn {
      min-width: 160px;
    }

    .strategy-hints {
      margin-top: 20px;
      padding: 12px 16px;
      background: var(--el-fill-color-light);
      border-radius: 8px;
      color: var(--el-text-color-secondary);
      font-size: 13px;
      line-height: 1.6;

      p {
        margin: 0 0 4px;

        &:last-child {
          margin-bottom: 0;
        }
      }
    }
  }

  .results-panel {
    .card-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
    }

    .result-title {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }

    .header-actions {
      display: flex;
      gap: 8px;
    }

    .pagination-wrapper {
      display: flex;
      justify-content: center;
      margin-top: 24px;
    }
  }

  .text-red {
    color: #f56c6c;
  }

  .text-green {
    color: #67c23a;
  }

  .text-muted {
    color: var(--el-text-color-placeholder);
  }

  .rank-badge {
    display: inline-block;
    min-width: 22px;
    font-weight: 600;
    color: var(--el-text-color-regular);

    &.rank-1 { color: #e6a23c; }
    &.rank-2 { color: #909399; }
    &.rank-3 { color: #cd7f32; }
  }
}
</style>
