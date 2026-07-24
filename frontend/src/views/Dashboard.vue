<script setup lang="ts">
import { BarChart, LineChart } from "echarts/charts";
import { GridComponent, TitleComponent, TooltipComponent } from "echarts/components";
import * as echarts from "echarts/core";
import type { EChartsType } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import {
  getConfig,
  getStatus,
  getUsageDimensions,
  getUsageStats,
  type Config,
  type Status,
  type UsageDimension,
  type UsageStats,
} from "../api";

echarts.use([BarChart, LineChart, GridComponent, TitleComponent, TooltipComponent, CanvasRenderer]);

const status = ref<Status | null>(null);
const cfg = ref<Config | null>(null);
const stats = ref<UsageStats | null>(null);
const groups = ref<UsageDimension[]>([]);
const users = ref<UsageDimension[]>([]);
const error = ref("");
const statsError = ref("");
const loading = ref(true);
const statsLoading = ref(true);
const rangePreset = ref<"today" | "7d" | "30d" | "custom">("7d");
const customStart = ref("");
const customEnd = ref("");
const chatFilter = ref("all");
const userFilter = ref("");
const trendEl = ref<HTMLDivElement | null>(null);
const groupEl = ref<HTMLDivElement | null>(null);
const userEl = ref<HTMLDivElement | null>(null);
let trendChart: EChartsType | null = null;
let groupChart: EChartsType | null = null;
let userChart: EChartsType | null = null;
let statusTimer: number | undefined;
let statsTimer: number | undefined;

function currentRange(): { start: number; end: number; bucket: "hour" | "day" } | null {
  const now = new Date();
  let start: Date;
  let end = now;
  if (rangePreset.value === "today") {
    start = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  } else if (rangePreset.value === "7d" || rangePreset.value === "30d") {
    const days = rangePreset.value === "7d" ? 7 : 30;
    start = new Date(now.getFullYear(), now.getMonth(), now.getDate() - days + 1);
  } else {
    if (!customStart.value || !customEnd.value) return null;
    start = new Date(`${customStart.value}T00:00:00`);
    end = new Date(`${customEnd.value}T00:00:00`);
    end.setDate(end.getDate() + 1);
    if (!Number.isFinite(start.getTime()) || end <= start) return null;
  }
  const bucket = end.getTime() - start.getTime() <= 48 * 3600 * 1000 ? "hour" : "day";
  return { start: start.getTime() / 1000, end: end.getTime() / 1000, bucket };
}

function usageQuery() {
  const range = currentRange();
  if (!range) return null;
  const scope = chatFilter.value === "dm" ? "dm" : chatFilter.value.startsWith("group:") ? "group" : "all";
  return {
    ...range,
    scope: scope as "all" | "dm" | "group",
    group_id: chatFilter.value.startsWith("group:") ? chatFilter.value.slice(6) : undefined,
    user_id: userFilter.value || undefined,
    // JavaScript's offset is minutes west of UTC; the API expects minutes east.
    tz_offset_minutes: -new Date().getTimezoneOffset(),
  };
}

async function refreshStatus() {
  try {
    [status.value, cfg.value] = await Promise.all([getStatus(), getConfig()]);
    error.value = "";
  } catch (e: any) {
    error.value = String(e.response?.data?.error || e.message || e);
  } finally {
    loading.value = false;
  }
}

async function refreshStats(refreshDimensions = false) {
  const query = usageQuery();
  if (!query) {
    statsError.value = "请选择有效的自定义日期范围";
    return;
  }
  statsLoading.value = true;
  try {
    const requests: Promise<any>[] = [getUsageStats(query)];
    if (refreshDimensions) requests.push(getUsageDimensions(query.start, query.end));
    const result = await Promise.all(requests);
    stats.value = result[0];
    if (refreshDimensions) {
      groups.value = result[1].groups;
      users.value = result[1].users;
    }
    statsError.value = "";
    await nextTick();
    renderCharts();
  } catch (e: any) {
    statsError.value = String(e.response?.data?.error || e.message || e);
  } finally {
    statsLoading.value = false;
  }
}

function dimensionLabel(item: UsageDimension): string {
  return item.name ? `${item.name} · ${item.id}` : item.id;
}

function formatBucket(timestamp: number, bucket: "hour" | "day"): string {
  const date = new Date(timestamp * 1000);
  return bucket === "hour"
    ? `${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")} ${String(date.getHours()).padStart(2, "0")}:00`
    : `${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

function renderCharts() {
  if (!stats.value || !trendEl.value || !groupEl.value || !userEl.value) return;
  trendChart ||= echarts.init(trendEl.value);
  groupChart ||= echarts.init(groupEl.value);
  userChart ||= echarts.init(userEl.value);
  const common = { animationDuration: 300, textStyle: { fontFamily: "system-ui, sans-serif" } };
  trendChart.setOption({
    ...common,
    title: stats.value.trend.length ? undefined : { text: "暂无数据", left: "center", top: "middle", textStyle: { color: "#999", fontSize: 14 } },
    tooltip: { trigger: "axis" },
    grid: { left: 50, right: 20, top: 25, bottom: 45 },
    xAxis: {
      type: "category",
      data: stats.value.trend.map((item) => formatBucket(item.bucket_start, stats.value!.bucket)),
      axisLabel: { rotate: stats.value.trend.length > 12 ? 35 : 0 },
    },
    yAxis: { type: "value", minInterval: 1 },
    series: [{ type: "line", smooth: true, data: stats.value.trend.map((item) => item.count), areaStyle: {} }],
  });
  const barOption = (items: (UsageDimension & { count: number })[]) => ({
    ...common,
    title: items.length ? undefined : { text: "暂无数据", left: "center", top: "middle", textStyle: { color: "#999", fontSize: 14 } },
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
    grid: { left: 20, right: 35, top: 15, bottom: 20, containLabel: true },
    xAxis: { type: "value", minInterval: 1 },
    yAxis: { type: "category", inverse: true, data: items.map(dimensionLabel), axisLabel: { width: 160, overflow: "truncate" } },
    series: [{ type: "bar", data: items.map((item) => item.count), itemStyle: { borderRadius: [0, 4, 4, 0] } }],
  });
  groupChart.setOption(barOption(stats.value.top_groups), true);
  userChart.setOption(barOption(stats.value.top_users), true);
}

function resizeCharts() {
  trendChart?.resize();
  groupChart?.resize();
  userChart?.resize();
}

function groupCount() {
  return cfg.value ? Object.keys(cfg.value.groups || {}).length : 0;
}

watch([chatFilter, userFilter], () => refreshStats(false));
watch(rangePreset, () => {
  if (rangePreset.value !== "custom") refreshStats(true);
});

onMounted(async () => {
  await Promise.all([refreshStatus(), refreshStats(true)]);
  statusTimer = window.setInterval(refreshStatus, 5000);
  statsTimer = window.setInterval(() => refreshStats(false), 30000);
  window.addEventListener("resize", resizeCharts);
});

onUnmounted(() => {
  if (statusTimer) clearInterval(statusTimer);
  if (statsTimer) clearInterval(statsTimer);
  window.removeEventListener("resize", resizeCharts);
  trendChart?.dispose();
  groupChart?.dispose();
  userChart?.dispose();
});
</script>

<template>
  <div>
    <h2>仪表盘</h2>
    <div v-if="loading" class="loading">加载中...</div>
    <div v-else-if="error" class="error">❌ {{ error }}</div>
    <div v-else-if="status" class="dashboard">
      <div v-if="status.version_mismatch && status.hermes_plugin_connected" class="card card-warn wide">
        <h3>⚠️ 版本不匹配</h3>
        <p>适配器版本 <strong>v{{ status.adapter_version }}</strong> 与插件版本
          <strong>v{{ status.plugin_version || '未知' }}</strong> 不一致。请<a href="/connections">重新安装插件</a>。</p>
      </div>
      <div
        v-if="status.latest_plugin_status?.level === 'error'"
        class="card card-warn wide"
      >
        <h3>⚠️ Hermes 插件处理异常</h3>
        <p>
          {{ status.latest_plugin_status.event }}：{{ status.latest_plugin_status.message }}
          （{{ new Date(status.latest_plugin_status.timestamp * 1000).toLocaleString() }}）
        </p>
      </div>
      <div class="card">
        <h3>端口</h3>
        <dl>
          <dt>OneBot WS</dt><dd>{{ status.onebot_ws_port }}</dd>
          <dt>Hermes WS</dt><dd>{{ status.hermes_ws_port }}</dd>
          <dt>WebUI</dt><dd>{{ status.webui_port }}</dd>
        </dl>
      </div>
      <div class="card">
        <h3>群聊状态</h3>
        <dl>
          <dt>已配置群</dt><dd>{{ groupCount() }}</dd>
          <dt>群成员隔离</dt><dd>{{ status.hermes_group_sessions_per_user ? '是（每人独立会话）' : '否（全群共享会话）' }}</dd>
          <dt>全局管理员</dt><dd>{{ cfg?.global_admins?.length || 0 }} 人</dd>
        </dl>
      </div>

      <section class="usage wide">
        <div class="usage-heading">
          <div>
            <h3>消息用量统计</h3>
            <p v-if="stats && !stats.enabled" class="disabled-note">统计已关闭，以下展示关闭前的历史数据。</p>
          </div>
          <span v-if="statsLoading" class="muted">更新中...</span>
        </div>
        <div class="filters">
          <label>时间范围
            <select v-model="rangePreset">
              <option value="today">今天</option><option value="7d">最近 7 天</option>
              <option value="30d">最近 30 天</option><option value="custom">自定义</option>
            </select>
          </label>
          <template v-if="rangePreset === 'custom'">
            <label>开始日期<input v-model="customStart" type="date" /></label>
            <label>结束日期<input v-model="customEnd" type="date" /></label>
            <button class="apply-btn" @click="refreshStats(true)">应用</button>
          </template>
          <label>群聊
            <select v-model="chatFilter">
              <option value="all">全部会话</option><option value="dm">私聊</option>
              <option v-for="group in groups" :key="group.id" :value="`group:${group.id}`">{{ dimensionLabel(group) }}</option>
            </select>
          </label>
          <label>用户
            <input v-model.trim="userFilter" list="usage-users" placeholder="全部用户（输入昵称或 QQ 号）" />
            <datalist id="usage-users"><option v-for="user in users" :key="user.id" :value="user.id">{{ dimensionLabel(user) }}</option></datalist>
          </label>
          <button v-if="userFilter" class="clear-btn" @click="userFilter = ''">清除用户</button>
        </div>
        <div v-if="statsError" class="error">❌ {{ statsError }}</div>
        <template v-else-if="stats">
          <div class="summary-grid">
            <div class="metric"><span>通过过滤消息</span><strong>{{ stats.summary.total }}</strong></div>
            <div class="metric"><span>活跃群聊</span><strong>{{ stats.summary.active_groups }}</strong></div>
            <div class="metric"><span>活跃用户</span><strong>{{ stats.summary.active_users }}</strong></div>
          </div>
          <div class="chart-card wide-chart"><h4>消息趋势</h4><div ref="trendEl" class="chart"></div></div>
          <div class="chart-grid">
            <div class="chart-card"><h4>群聊 Top 10</h4><div ref="groupEl" class="chart"></div></div>
            <div class="chart-card"><h4>用户 Top 10</h4><div ref="userEl" class="chart"></div></div>
          </div>
        </template>
      </section>
    </div>
  </div>
</template>

<style scoped>
.dashboard { display: grid; gap: 1.5rem; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); }
.wide { grid-column: 1 / -1; }
.card, .usage, .chart-card, .metric { background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; padding: 1.25rem; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }
.card h3, .usage h3 { margin: 0 0 1rem; font-size: 1rem; border-bottom: 2px solid var(--primary); padding-bottom: 0.5rem; }
dl { display: grid; grid-template-columns: auto 1fr; gap: 0.5rem 1rem; margin: 0; }
dt { font-weight: 500; color: var(--text-muted); } dd { margin: 0; }
.usage-heading { display: flex; justify-content: space-between; align-items: start; gap: 1rem; }
.usage-heading h3 { margin-bottom: 0.25rem; }
.disabled-note { margin: 0 0 1rem; color: #856404; }
.muted { color: var(--text-muted); font-size: 0.85rem; }
.filters { display: flex; flex-wrap: wrap; align-items: end; gap: 0.75rem; margin: 1rem 0; }
.filters label { display: grid; gap: 0.3rem; font-size: 0.85rem; color: var(--text-muted); }
.filters select, .filters input { min-width: 150px; padding: 0.5rem; border: 1px solid var(--border); border-radius: 5px; background: white; color: var(--text); }
.apply-btn, .clear-btn { padding: 0.55rem 0.85rem; border: 0; border-radius: 5px; cursor: pointer; }
.apply-btn { background: var(--primary); color: white; } .clear-btn { background: #e9ecef; color: var(--text); }
.summary-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1rem; margin: 1rem 0; }
.metric { display: flex; flex-direction: column; gap: 0.4rem; }
.metric span { color: var(--text-muted); font-size: 0.85rem; } .metric strong { font-size: 1.8rem; }
.chart-card { min-width: 0; } .chart-card h4 { margin: 0 0 0.5rem; font-size: 0.95rem; }
.chart { height: 320px; width: 100%; }
.chart-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 1rem; margin-top: 1rem; }
.loading { text-align: center; padding: 2rem; color: var(--text-muted); }
.error { color: var(--danger); background: #fee; padding: 1rem; border-radius: 6px; border-left: 4px solid var(--danger); }
.card-warn { background: #fff8e1; border-color: var(--warning); }
.card-warn h3 { border-bottom-color: var(--warning); color: #856404; } .card-warn p { margin: 0.5rem 0 0; }
@media (max-width: 760px) {
  .summary-grid, .chart-grid { grid-template-columns: 1fr; }
  .filters label, .filters select, .filters input { width: 100%; }
  .filters label { flex: 1 1 100%; }
}
</style>
