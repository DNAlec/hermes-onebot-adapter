<script setup lang="ts">
import { ref, onMounted, onUnmounted } from "vue";
import { RouterView, RouterLink, useRouter } from "vue-router";
import { getStatus, getUpdateCheck, type Status, type UpdateInfo, clearToken } from "./api";

const status = ref<Status | null>(null);
const updateInfo = ref<UpdateInfo | null>(null);
let timer: number | undefined;
const router = useRouter();

async function refreshStatus() {
  try {
    status.value = await getStatus();
  } catch {
    status.value = null;
  }
}

async function checkUpdate() {
  try {
    const info = await getUpdateCheck();
    if (info.has_update) {
      updateInfo.value = info;
    }
  } catch {
    // silently ignore
  }
}

function logout() {
  clearToken();
  router.push("/login");
}

onMounted(() => {
  refreshStatus();
  checkUpdate();
  timer = window.setInterval(refreshStatus, 5000);
});
onUnmounted(() => {
  if (timer) clearInterval(timer);
});
</script>

<template>
  <div class="app">
    <header class="topbar">
      <div class="topbar-left">
        <h1>Hermes OneBot Adapter</h1>
        <span v-if="status" class="version-tag">v{{ status.adapter_version }}</span>
        <a
          v-if="updateInfo?.has_update"
          class="update-badge"
          :href="updateInfo.changelog_url"
          target="_blank"
          rel="noopener"
          :title="`有新版本 v${updateInfo.latest_version}，点击查看更新日志`"
        >
          v{{ updateInfo.latest_version }} →
        </a>
      </div>
      <nav>
        <RouterLink to="/">仪表盘</RouterLink>
        <RouterLink to="/connections">连接管理</RouterLink>
        <RouterLink to="/chat">聊天配置</RouterLink>
        <RouterLink to="/commands">指令过滤</RouterLink>
        <RouterLink to="/tools">工具管理</RouterLink>
        <RouterLink to="/advanced">高级设置</RouterLink>
        <RouterLink to="/logs">日志</RouterLink>
      </nav>
      <div class="status-badges">
        <span
          v-if="status"
          :class="['badge', status.onebot_connected ? 'badge-ok' : 'badge-err']"
        >
          {{ status.onebot_connected ? '●' : '○' }} OneBot
        </span>
        <span
          v-if="status"
          :class="['badge', status.hermes_plugin_connected ? 'badge-ok' : 'badge-err']"
        >
          {{ status.hermes_plugin_connected ? '●' : '○' }} 插件
        </span>
        <span v-if="!status" class="badge badge-err">● 离线</span>
        <button class="logout-btn" @click="logout" title="退出登录">退出</button>
      </div>
    </header>
    <main class="content">
      <RouterView />
    </main>
  </div>
</template>

<style>
:root {
  --primary: #4a90e2;
  --primary-dark: #357abd;
  --success: #28a745;
  --danger: #dc3545;
  --warning: #ffc107;
  --bg: #f5f6fa;
  --card-bg: #fff;
  --border: #e0e0e0;
  --text: #222;
  --text-muted: #666;
}

* { box-sizing: border-box; }
body { margin: 0; font-family: system-ui, -apple-system, sans-serif; color: var(--text); background: var(--bg); }

.app { min-height: 100vh; display: flex; flex-direction: column; }

.topbar {
  display: flex;
  align-items: center;
  gap: 1.5rem;
  padding: 0.75rem 1.5rem;
  background: var(--card-bg);
  border-bottom: 1px solid var(--border);
  box-shadow: 0 1px 4px rgba(0,0,0,0.04);
  flex-wrap: wrap;
}
.topbar-left { display: flex; align-items: center; gap: 0.5rem; }
.logo { font-size: 1.3rem; }
.topbar h1 { font-size: 1.1rem; margin: 0; white-space: nowrap; }
.topbar nav { display: flex; gap: 0.5rem; flex: 1; flex-wrap: wrap; }
.topbar nav a {
  color: var(--text-muted);
  text-decoration: none;
  padding: 0.35rem 0.75rem;
  border-radius: 4px;
  transition: all 0.15s;
  font-size: 0.95rem;
}
.topbar nav a:hover { background: var(--bg); color: var(--text); }
.topbar nav a.router-link-active { color: var(--primary); font-weight: 600; background: rgba(74,144,226,0.08); }

.status-badges { display: flex; gap: 0.5rem; }
.badge {
  font-size: 0.8rem;
  padding: 0.25rem 0.6rem;
  border-radius: 12px;
  white-space: nowrap;
  font-weight: 500;
}
.badge-ok { background: rgba(40,167,69,0.12); color: var(--success); }
.badge-err { background: rgba(220,53,69,0.12); color: var(--danger); }
.logout-btn {
  font-size: 0.8rem;
  padding: 0.25rem 0.6rem;
  border-radius: 4px;
  border: 1px solid var(--border);
  background: var(--bg);
  color: var(--text-muted);
  cursor: pointer;
  white-space: nowrap;
}
.logout-btn:hover { color: var(--danger); border-color: var(--danger); }

.version-tag {
  font-size: 0.75rem;
  color: var(--text-muted);
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 0.15rem 0.4rem;
  font-family: monospace;
}

.update-badge {
  font-size: 0.75rem;
  color: #d97706;
  background: rgba(245,158,11,0.12);
  border: 1px solid rgba(245,158,11,0.3);
  border-radius: 4px;
  padding: 0.15rem 0.4rem;
  font-family: monospace;
  text-decoration: none;
  transition: background 0.15s;
}
.update-badge:hover {
  background: rgba(245,158,11,0.22);
}

.content { padding: 1.5rem; max-width: 1000px; width: 100%; margin: 0 auto; flex: 1; }

/* Shared primary save button — used by all page-level save buttons.
   Non-page-level buttons (modal save, change token) use different classes
   to avoid inheriting these styles. */
.save-btn {
  background: var(--primary);
  color: white;
  border: none;
  padding: 0.6rem 1.5rem;
  border-radius: 6px;
  cursor: pointer;
  font-size: 0.95rem;
  transition: background 0.2s;
}
.save-btn:hover:not(:disabled) { background: var(--primary-dark); }
.save-btn:disabled { background: #ccc; cursor: not-allowed; }

@media (max-width: 768px) {
  .topbar { gap: 0.5rem; padding: 0.5rem 1rem; }
  .topbar h1 { font-size: 0.95rem; }
  .topbar nav { gap: 0.15rem; }
  .topbar nav a { padding: 0.2rem 0.4rem; font-size: 0.85rem; }
  .status-badges { flex-wrap: wrap; }
  .content { padding: 1rem; }
}
</style>