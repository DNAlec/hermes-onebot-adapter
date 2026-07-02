<script setup lang="ts">
import { ref, computed, onMounted } from "vue";
import {
  getCommands, refreshCommands, type CommandInfo,
} from "../api";
import { useConfig } from "../composables/useConfig";

const { cfg, load, save: saveConfig } = useConfig();
const commands = ref<CommandInfo[]>([]);
const saving = ref(false);
const refreshing = ref(false);
const msg = ref("");
const msgType = ref<"success" | "error">("success");
const filterText = ref("");
const filterSource = ref<"all" | "builtin" | "plugin">("all");

onMounted(async () => {
  try {
    await load();
    commands.value = await getCommands();
  } catch (e: any) {
    msg.value = "❌ 加载失败: " + (e.response?.data?.error || e.message);
    msgType.value = "error";
  }
});

const PERMISSION_LEVELS = [
  { value: "everyone", label: "所有人", color: "var(--success)" },
  { value: "admin", label: "仅管理员", color: "var(--warning)" },
  { value: "disabled", label: "禁用", color: "var(--danger)" },
];

function permLabel(v: string): string {
  const found = PERMISSION_LEVELS.find((p) => p.value === v);
  return found ? found.label : v;
}

function permColor(v: string): string {
  const found = PERMISSION_LEVELS.find((p) => p.value === v);
  return found ? found.color : "var(--text-muted)";
}

function getPerm(cmd: string): string {
  if (!cfg.value) return "everyone";
  return cfg.value.command_permissions?.[cmd] ?? "everyone";
}

function setPerm(cmd: string, perm: string) {
  if (!cfg.value) return;
  if (!cfg.value.command_permissions) {
    cfg.value.command_permissions = {};
  }
  cfg.value.command_permissions[cmd] = perm;
}

const filteredCommands = computed(() => {
  let list = commands.value;
  if (filterSource.value !== "all") {
    list = list.filter((c) =>
      filterSource.value === "builtin" ? c.source === "builtin" : c.source !== "builtin"
    );
  }
  const q = filterText.value.trim().toLowerCase();
  if (q) {
    list = list.filter(
      (c) =>
        c.name.toLowerCase().includes(q) ||
        c.description.toLowerCase().includes(q) ||
        c.aliases.some((a) => a.toLowerCase().includes(q))
    );
  }
  return list;
});

const stats = computed(() => {
  const total = commands.value.length;
  const disabled = cfg.value?.command_permissions
    ? Object.values(cfg.value.command_permissions).filter((v) => v === "disabled").length
    : 0;
  const adminOnly = cfg.value?.command_permissions
    ? Object.values(cfg.value.command_permissions).filter((v) => v === "admin").length
    : 0;
  const everyone = total - adminOnly - disabled;
  return { total, everyone, disabled, adminOnly };
});

async function save() {
  if (!cfg.value) return;
  saving.value = true;
  msg.value = "";
  const c = cfg.value;
  if (!c.command_permissions) {
    c.command_permissions = {};
  }
  for (const cmd of commands.value) {
    if (!(cmd.name in c.command_permissions)) {
      c.command_permissions[cmd.name] = "everyone";
    }
  }
  try {
    await saveConfig({
      command_filter_enabled: c.command_filter_enabled,
      command_filter_unknown: c.command_filter_unknown,
      command_permissions: c.command_permissions,
      command_reject_message: c.command_reject_message,
    });
    msg.value = "✅ 指令过滤设置已保存";
    msgType.value = "success";
  } catch (e: any) {
    msg.value = "❌ " + (e.response?.data?.error || e.message);
    msgType.value = "error";
  } finally {
    saving.value = false;
  }
}

async function refresh() {
  refreshing.value = true;
  msg.value = "";
  try {
    await refreshCommands();
    msg.value = "⏳ 已请求插件刷新指令列表，请稍后刷新页面";
    msgType.value = "success";
    // Poll for updated commands after a short delay
    setTimeout(async () => {
      try {
        commands.value = await getCommands();
      } catch {}
    }, 2000);
  } catch (e: any) {
    msg.value = "❌ " + (e.response?.data?.error || e.message);
    msgType.value = "error";
  } finally {
    refreshing.value = false;
  }
}

async function reloadCommands() {
  try {
    commands.value = await getCommands();
  } catch (e: any) {
    msg.value = "❌ 加载指令列表失败: " + (e.response?.data?.error || e.message);
    msgType.value = "error";
  }
}

function setAllVisible(perm: string) {
  if (!cfg.value) return;
  if (!cfg.value.command_permissions) {
    cfg.value.command_permissions = {};
  }
  for (const c of filteredCommands.value) {
    cfg.value.command_permissions[c.name] = perm;
  }
}

function clearAllVisible() {
  setAllVisible("everyone");
}
</script>

<template>
  <div>
    <h2>/指令过滤</h2>
    <div v-if="msg" :class="['message', msgType]">{{ msg }}</div>

    <div v-if="!cfg" class="loading">加载配置中...</div>

    <div v-if="cfg">
      <!-- 全局开关 -->
      <div class="section">
        <h3>过滤设置</h3>
        <p class="hint" style="margin-bottom: 1rem;">
          适配器启动后会从 Hermes 获取已注册的 /指令。指令匹配方式：去除所有 @bot 后，从消息开头匹配 /xxx。
          被过滤的指令会向用户发送拒绝消息，不会送入 Hermes 处理。
        </p>
        <div class="grid2">
          <label class="checkbox-row">
            <input type="checkbox" v-model="cfg.command_filter_enabled" />
            <span>启用指令过滤</span>
          </label>
          <label class="checkbox-row">
            <input type="checkbox" v-model="cfg.command_filter_unknown" />
            <span>过滤未知指令（不在 Hermes 列表中的 /xxx）</span>
          </label>
        </div>
        <label class="full">
          拒绝消息模板
          <input v-model="cfg.command_reject_message" placeholder="⛔ 你没有权限使用此指令 /{cmd}" />
          <span class="hint">{cmd} 会被替换为指令名</span>
        </label>
      </div>

      <!-- 统计 -->
      <div class="section stats-section">
        <div class="stat-item">
          <span class="stat-label">已注册指令</span>
          <span class="stat-value">{{ stats.total }}</span>
        </div>
        <div class="stat-item">
          <span class="stat-label">所有人可用</span>
          <span class="stat-value" style="color: var(--success);">{{ stats.everyone }}</span>
        </div>
        <div class="stat-item">
          <span class="stat-label">仅管理员</span>
          <span class="stat-value" style="color: var(--warning);">{{ stats.adminOnly }}</span>
        </div>
        <div class="stat-item">
          <span class="stat-label">已禁用</span>
          <span class="stat-value" style="color: var(--danger);">{{ stats.disabled }}</span>
        </div>
      </div>

      <!-- 指令列表 -->
      <div class="section">
        <div class="section-header">
          <h3>指令列表</h3>
          <div class="actions">
            <button @click="reloadCommands" class="sync-btn">↻ 重新加载</button>
            <button @click="refresh" :disabled="refreshing" class="sync-btn">
              {{ refreshing ? "请求中..." : "🔄 从 Hermes 刷新" }}
            </button>
          </div>
        </div>

        <!-- 过滤器 -->
        <div class="filter-row">
          <input v-model="filterText" placeholder="搜索指令名/描述/别名..." class="filter-input" />
          <select v-model="filterSource" class="filter-select">
            <option value="all">全部来源</option>
            <option value="builtin">内置指令</option>
            <option value="plugin">插件指令</option>
          </select>
        </div>

        <!-- 批量操作 -->
        <div v-if="filteredCommands.length" class="bulk-actions">
          <span class="bulk-label">批量设置当前筛选结果:</span>
          <button @click="setAllVisible('everyone')" class="bulk-btn">全放行</button>
          <button @click="setAllVisible('admin')" class="bulk-btn">仅管理员</button>
          <button @click="setAllVisible('disabled')" class="bulk-btn">全禁用</button>
          <button @click="clearAllVisible" class="bulk-btn clear-btn">重置为所有人</button>
        </div>

        <table v-if="filteredCommands.length" class="cmd-table">
          <thead>
            <tr>
              <th>指令</th>
              <th>描述</th>
              <th>来源</th>
              <th>权限</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="c in filteredCommands" :key="c.name">
              <td class="cmd-name">
                <span class="cmd-slash">/</span>{{ c.name }}
                <span v-if="c.aliases.length" class="cmd-aliases">
                  (别名: {{ c.aliases.join(", ") }})
                </span>
              </td>
              <td class="cmd-desc">{{ c.description || "—" }}</td>
              <td>
                <span :class="['source-badge', c.source === 'builtin' ? 'builtin' : 'plugin']">
                  {{ c.source === "builtin" ? "内置" : c.source }}
                </span>
              </td>
              <td>
                <select
                  :value="getPerm(c.name)"
                  @change="setPerm(c.name, ($event.target as HTMLSelectElement).value)"
                  class="perm-select"
                  :style="{ color: permColor(getPerm(c.name)) }"
                >
                  <option value="everyone">所有人</option>
                  <option value="admin">仅管理员</option>
                  <option value="disabled">禁用</option>
                </select>
              </td>
            </tr>
          </tbody>
        </table>
        <p v-else-if="!commands.length" class="empty">
          暂无指令。请确保 Hermes 插件已连接，然后点击「从 Hermes 刷新」。
        </p>
        <p v-else class="empty">没有匹配的指令</p>
      </div>

      <button @click="save" :disabled="saving" class="save-btn">
        {{ saving ? "保存中..." : "保存设置" }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.section { background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; padding: 1.25rem; margin-bottom: 1.5rem; }
.section h3 { margin: 0 0 1rem; font-size: 1rem; border-bottom: 2px solid var(--primary); padding-bottom: 0.5rem; }
.section-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem; }
.section-header h3 { margin: 0; border: none; padding: 0; }
.actions { display: flex; gap: 0.5rem; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-bottom: 1rem; }
label { display: block; margin-bottom: 0.75rem; font-weight: 500; font-size: 0.9rem; }
label.full { width: 100%; }
.hint { display: block; font-size: 0.85rem; color: var(--text-muted); margin: 0.25rem 0 0.75rem; }
input, select { width: 100%; padding: 0.5rem; border: 1px solid #ccc; border-radius: 4px; font-size: 0.9rem; margin-top: 0.25rem; }
input[type="checkbox"] { width: auto; }
.checkbox-row { display: flex; align-items: center; gap: 0.5rem; }
.checkbox-row span { font-weight: 500; }

.stats-section { display: flex; gap: 2rem; justify-content: space-around; text-align: center; }
.stat-item { display: flex; flex-direction: column; gap: 0.25rem; }
.stat-label { font-size: 0.85rem; color: var(--text-muted); }
.stat-value { font-size: 1.6rem; font-weight: 700; }

.filter-row { display: flex; gap: 0.75rem; margin-bottom: 1rem; }
.filter-input { flex: 1; margin: 0; }
.filter-select { width: auto; margin: 0; }

.bulk-actions { display: flex; gap: 0.5rem; align-items: center; margin-bottom: 1rem; padding: 0.6rem; background: var(--bg); border-radius: 4px; flex-wrap: wrap; }
.bulk-label { font-size: 0.85rem; color: var(--text-muted); margin-right: 0.5rem; }
.bulk-btn { padding: 0.3rem 0.7rem; border: 1px solid #ccc; border-radius: 4px; cursor: pointer; background: var(--card-bg); font-size: 0.85rem; }
.bulk-btn:hover { background: #e8e8e8; }
.clear-btn { color: var(--text-muted); }

.cmd-table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
.cmd-table th { text-align: left; padding: 0.5rem; border-bottom: 2px solid var(--border); color: var(--text-muted); }
.cmd-table td { padding: 0.5rem; border-bottom: 1px solid var(--border); vertical-align: top; }
.cmd-name { font-family: monospace; white-space: nowrap; }
.cmd-slash { color: var(--primary); font-weight: 700; }
.cmd-aliases { font-size: 0.8rem; color: var(--text-muted); margin-left: 0.5rem; }
.cmd-desc { color: var(--text); max-width: 300px; }
.source-badge { font-size: 0.75rem; padding: 0.15rem 0.5rem; border-radius: 10px; white-space: nowrap; }
.source-badge.builtin { background: rgba(74, 144, 226, 0.12); color: var(--primary); }
.source-badge.plugin { background: rgba(255, 193, 7, 0.15); color: #856404; }
.perm-select { width: auto; margin: 0; min-width: 120px; }

.save-btn { background: var(--primary); color: white; border: none; padding: 0.6rem 1.5rem; border-radius: 6px; cursor: pointer; font-size: 0.95rem; }
.save-btn:disabled { background: #ccc; cursor: not-allowed; }
.sync-btn { background: var(--bg); border: 1px solid var(--border); padding: 0.4rem 0.8rem; border-radius: 4px; cursor: pointer; font-size: 0.85rem; }
.sync-btn:disabled { cursor: not-allowed; opacity: 0.6; }
.empty { color: var(--text-muted); text-align: center; padding: 2rem; }

.message { padding: 0.75rem 1rem; border-radius: 6px; margin-bottom: 1rem; }
.message.success { background: #d4edda; color: #155724; border-left: 4px solid var(--success); }
.message.error { background: #f8d7da; color: #721c24; border-left: 4px solid var(--danger); }
.loading { text-align: center; padding: 2rem; color: var(--text-muted, #666); }
</style>