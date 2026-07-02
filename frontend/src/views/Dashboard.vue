<script setup lang="ts">
import { ref, onMounted, onUnmounted } from "vue";
import { getStatus, getConfig, type Status, type Config } from "../api";

const status = ref<Status | null>(null);
const cfg = ref<Config | null>(null);
const error = ref("");
const loading = ref(true);
let timer: number | undefined;

async function refresh() {
  try {
    status.value = await getStatus();
    cfg.value = await getConfig();
    error.value = "";
  } catch (e: any) {
    error.value = String(e.message || e);
  } finally {
    loading.value = false;
  }
}

onMounted(() => { refresh(); timer = window.setInterval(refresh, 3000); });
onUnmounted(() => { if (timer) clearInterval(timer); });

const groupCount = () => cfg.value ? Object.keys(cfg.value.groups || {}).length : 0;
</script>

<template>
  <div>
    <h2>仪表盘</h2>
    <div v-if="loading" class="loading">加载中...</div>
    <div v-else-if="error" class="error">❌ {{ error }}</div>
    <div v-else-if="status" class="dashboard">
      <div class="card">
        <h3>连接状态</h3>
        <div class="status-grid">
          <div class="status-item">
            <span class="label">OneBot</span>
            <span :class="status.onebot_connected ? 'connected' : 'disconnected'">
              {{ status.onebot_connected ? '✅ 已连接' : '❌ 未连接' }}
            </span>
          </div>
          <div class="status-item">
            <span class="label">Hermes 插件</span>
            <span :class="status.hermes_plugin_connected ? 'connected' : 'disconnected'">
              {{ status.hermes_plugin_connected ? '✅ 已连接' : '❌ 未连接' }}
            </span>
          </div>
        </div>
      </div>
      <div class="card">
        <h3>适配器信息</h3>
        <dl>
          <dt>版本</dt><dd>{{ status.adapter_version }}</dd>
          <dt>连接模式</dt><dd>{{ status.onebot_mode === 'reverse' ? '反向 WS' : '正向 WS' }}</dd>
          <dt>Bot QQ</dt><dd>{{ status.self_id || '未探测' }}</dd>
        </dl>
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
        <h3>群聊统计</h3>
        <dl>
          <dt>已配置群</dt><dd>{{ groupCount() }}</dd>
          <dt>默认 Session</dt><dd>{{ cfg?.group_session_mode === 'per_user' ? '独立会话' : '共享会话' }}</dd>
          <dt>全局管理员</dt><dd>{{ cfg?.global_admins?.length || 0 }} 人</dd>
        </dl>
      </div>
    </div>
  </div>
</template>

<style scoped>
.dashboard { display: grid; gap: 1.5rem; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); }
.card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; padding: 1.25rem; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }
.card h3 { margin: 0 0 1rem; font-size: 1rem; border-bottom: 2px solid var(--primary); padding-bottom: 0.5rem; }
.status-grid { display: flex; flex-direction: column; gap: 0.75rem; }
.status-item { display: flex; justify-content: space-between; }
.label { font-weight: 500; color: var(--text-muted); }
.connected { color: var(--success); font-weight: 600; }
.disconnected { color: var(--danger); font-weight: 600; }
dl { display: grid; grid-template-columns: auto 1fr; gap: 0.5rem 1rem; margin: 0; }
dt { font-weight: 500; color: var(--text-muted); }
dd { margin: 0; }
.loading { text-align: center; padding: 2rem; color: var(--text-muted); }
.error { color: var(--danger); background: #fee; padding: 1rem; border-radius: 6px; border-left: 4px solid var(--danger); }
</style>