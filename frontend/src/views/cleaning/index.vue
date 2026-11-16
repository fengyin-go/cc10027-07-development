<template>
  <section class="page" data-module="cleaning">
    <header class="page-head">
      <div>
        <h2>数据清洗管理</h2>
        <p class="page-desc">维护清洗规则的输入、执行约束与结果记录，为数据清洗提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记清洗规则</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出数据清洗清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无数据清洗数据，可先登记清洗规则</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记清洗规则</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <section class="panel" data-testid="run-panel">
      <h3>清洗批次（状态按草稿→运行→完成/失败→归档单向流转）</h3>
      <div class="panel-actions">
        <input v-model="newRunCode" data-testid="run-code" placeholder="清洗编号，如 CLEA-0018" />
        <button class="btn" type="button" data-testid="create-run" @click="createRun">创建清洗批次</button>
        <button class="btn ghost" type="button" @click="reloadRuns">刷新批次</button>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th>批次号</th><th>清洗编号</th><th>规则版本</th><th>游标</th><th>失败行号</th><th>状态</th><th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="batch in batches" :key="String(batch.id)" :data-testid="`run-${batch.id}`">
            <td>{{ batch.批次号 }}</td>
            <td>{{ batch.清洗编号 }}</td>
            <td>{{ batch.规则版本 }}</td>
            <td>{{ batch.游标 }}</td>
            <td>{{ batch.失败行号 ?? '—' }}</td>
            <td>{{ batch.status }}</td>
            <td class="row-actions">
              <button v-if="batch.status === '草稿'" class="link" type="button" :data-testid="`start-${batch.id}`" @click="startRun(batch)">启动</button>
              <button v-if="batch.status === '失败'" class="link" type="button" :data-testid="`resume-${batch.id}`" @click="resumeRun(batch)">恢复</button>
              <button v-if="batch.status === '完成' || batch.status === '失败'" class="link" type="button" :data-testid="`archive-${batch.id}`" @click="archiveRun(batch)">归档</button>
              <button class="link" type="button" :data-testid="`records-${batch.id}`" @click="viewRunRecords(batch)">本段记录</button>
            </td>
          </tr>
          <tr v-if="!batches.length">
            <td colspan="7" class="empty-state">暂无清洗批次，可先创建批次</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="panel" data-testid="report-panel">
      <h3>质量报告（保留异常阈值与生效日期，规则版本变化时另起版本）</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th>报告编号</th><th>规则版本</th><th>生效日期</th><th>异常阈值</th>
            <th>处理总数</th><th>成功数</th><th>异常数</th><th>跳过数量</th><th>报告状态</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="report in reports" :key="String(report.id)">
            <td>{{ report.报告编号 }}</td>
            <td>{{ report.规则版本 }}</td>
            <td>{{ report.生效日期 }}</td>
            <td>{{ report.异常阈值 }}</td>
            <td>{{ report.处理总数 }}</td>
            <td>{{ report.成功数 }}</td>
            <td>{{ report.异常数 }}</td>
            <td>{{ report.跳过数量 }}</td>
            <td>{{ report.报告状态 }}</td>
          </tr>
          <tr v-if="!reports.length">
            <td colspan="9" class="empty-state">暂无质量报告</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="panel" data-testid="dataset-panel">
      <h3>清洗后数据集（每条记录保留清洗编号、规则版本和异常处理结果）</h3>
      <div class="panel-actions">
        <input v-model="datasetCode" data-testid="dataset-code" placeholder="清洗编号，如 CLEA-0018" />
        <button class="btn" type="button" data-testid="view-published" @click="viewPublished">查询已发布</button>
        <button class="btn ghost" type="button" data-testid="view-anomalies" @click="viewAnomalies">异常回查</button>
        <span v-if="recordTitle" class="stat-label">{{ recordTitle }}</span>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th>行号</th><th>原始行标识</th><th>规则版本</th><th>异常处理结果</th>
            <th>异常原因</th><th>原始值</th><th>清洗后值</th><th>发布状态</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="record in recordRows" :key="String(record.id)">
            <td>{{ record.行号 }}</td>
            <td>{{ record.原始行标识 }}</td>
            <td>{{ record.规则版本 }}</td>
            <td>{{ record.异常处理结果 }}</td>
            <td>{{ record.异常原因 || '—' }}</td>
            <td>{{ record.原始值 === '' ? '（空）' : record.原始值 }}</td>
            <td>{{ record.清洗后值 }}</td>
            <td>{{ record.发布状态 }}</td>
          </tr>
          <tr v-if="!recordRows.length">
            <td colspan="8" class="empty-state">暂无记录，可查询已发布数据集或异常回查</td>
          </tr>
        </tbody>
      </table>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条数据清洗记录</span>
      <span v-if="noticeMessage" class="notice-text">{{ noticeMessage }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/cleaning'
const columns = ["清洗编号", "输入数据集", "空值策略", "格式表达式", "异常阈值", "输出字段", "规则版本", "生效日期"]
const actions = ["提交校验", "启用配置", "停用配置"]
const statuses = ["草稿", "待校验", "已启用", "已停用"]
const metricNames = ["清洗规则总数", "待校验清洗规则", "已启用清洗规则"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const noticeMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const batches = ref<Row[]>([])
const reports = ref<Row[]>([])
const recordRows = ref<Row[]>([])
const recordTitle = ref('')
const newRunCode = ref('CLEA-0018')
const datasetCode = ref('CLEA-0018')
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'cleaning.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('数据清洗动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据清洗操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('清洗规则列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据清洗列表读取失败'
  }
}

async function reloadRuns() {
  try {
    const [runResp, reportResp] = await Promise.all([
      request(`${ENDPOINT}/runs`),
      request(`${ENDPOINT}/reports`),
    ])
    if (runResp.ok) batches.value = (await runResp.json()).items ?? []
    if (reportResp.ok) reports.value = (await reportResp.json()).items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '清洗批次读取失败'
  }
}

async function runBatchAction(batch: Row, action: string) {
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/runs/${batch.id}/${action}`, { method: 'POST' })
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.message || '批次动作未生效')
    noticeMessage.value = payload.message ?? ''
    await reloadRuns()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '清洗批次操作失败'
  }
}

async function createRun() {
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/runs`, {
      method: 'POST', body: JSON.stringify({ 清洗编号: newRunCode.value }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '创建批次失败')
    noticeMessage.value = payload.message ?? ''
    await reloadRuns()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '创建批次失败'
  }
}

const startRun = (batch: Row) => runBatchAction(batch, 'start')
const resumeRun = (batch: Row) => runBatchAction(batch, 'resume')
const archiveRun = (batch: Row) => runBatchAction(batch, 'archive')

async function viewRunRecords(batch: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/runs/${batch.id}/records`)
    if (!response.ok) throw new Error('批次记录读取失败')
    const payload = await response.json()
    recordRows.value = payload.items ?? []
    recordTitle.value = `批次 ${batch.批次号} 当前执行段记录（恢复后的新执行批次只含失败行起的记录）`
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '批次记录读取失败'
  }
}

async function viewPublished() {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/datasets/${encodeURIComponent(datasetCode.value)}`)
    if (!response.ok) throw new Error('清洗后数据集读取失败')
    const payload = await response.json()
    recordRows.value = payload.items ?? []
    recordTitle.value = `清洗后数据集 ${datasetCode.value}（已发布部分）`
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '清洗后数据集读取失败'
  }
}

async function viewAnomalies() {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/datasets/${encodeURIComponent(datasetCode.value)}/anomalies`)
    if (!response.ok) throw new Error('异常记录读取失败')
    const payload = await response.json()
    recordRows.value = payload.items ?? []
    recordTitle.value = `清洗编号 ${datasetCode.value} 的异常与失败记录（保留原始值供回查）`
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '异常记录读取失败'
  }
}

onMounted(() => {
  void reload()
  void reloadRuns()
})
</script>
