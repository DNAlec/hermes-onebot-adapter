<script setup lang="ts">
import { ref, computed, onMounted } from "vue";
import {
  getHermesTools, putHermesTools, resetHermesTools,
  type ToolsetInfo, type McpServerInfo,
} from "../api";
import { useConfig } from "../composables/useConfig";

const { cfg, load } = useConfig();
const toolsets = ref<ToolsetInfo[]>([]);
const mcpServers = ref<McpServerInfo[]>([]);
const currentEnabled = ref<Set<string>>(new Set());
const hermesDirOk = ref(true);
const loading = ref(true);
const saving = ref(false);
const msg = ref("");
const msgType = ref<"success" | "error">("success");
const expanded = ref<Set<string>>(new Set());

// 本地勾选状态(toolset key → enabled)
const checked = ref<Set<string>>(new Set());
// MCP 服务器勾选
const mcpChecked = ref<Set<string>>(new Set());
// no_mcp sentinel
const noMcp = ref(false);

onMounted(async () => {
  try {
    await load();
    await fetchTools();
  } catch (e: any) {
    msg.value = "❌ 加载失败: " + (e.response?.data?.error || e.message);
    msgType.value = "error";
  }
});

async function fetchTools() {
  loading.value = true;
  msg.value = "";
  try {
    const data = await getHermesTools();
    toolsets.value = data.configurable || [];
    mcpServers.value = data.mcp_servers || [];
    hermesDirOk.value = data.hermes_dir_ok;
    const cur = new Set(data.current_enabled || []);
    currentEnabled.value = cur;
    // 初始化勾选状态:current_enabled 中存在且不是 no_mcp 的 toolset 勾选;
    // MCP server 在 current_enabled 中勾选;no_mcp sentinel 单独标记
    checked.value = new Set(
      toolsets.value.filter((t) => cur.has(t.key)).map((t) => t.key),
    );
    mcpChecked.value = new Set(mcpServers.value.filter((m) => cur.has(m.name)).map((m) => m.name));
    noMcp.value = cur.has("no_mcp");
  } catch (e: any) {
    if (e.response?.status === 400) {
      hermesDirOk.value = false;
      msg.value = "⚠ " + (e.response?.data?.error || "hermes_install_dir 未配置");
      msgType.value = "error";
    } else {
      const err = e.response?.data?.error || e.message;
      const detail = e.response?.data?.detail;
      msg.value = "❌ " + err + (detail ? ` (${detail})` : "");
      msgType.value = "error";
    }
  } finally {
    loading.value = false;
  }
}

const pluginToolsets = computed(() => toolsets.value.filter((t) => t.is_plugin));
const builtinToolsets = computed(() => toolsets.value.filter((t) => !t.is_plugin));

function toggle(key: string) {
  const s = new Set(checked.value);
  if (s.has(key)) s.delete(key);
  else s.add(key);
  checked.value = s;
}

function toggleMcp(name: string) {
  const s = new Set(mcpChecked.value);
  if (s.has(name)) s.delete(name);
  else s.add(name);
  mcpChecked.value = s;
}

function toggleExpand(key: string) {
  const s = new Set(expanded.value);
  if (s.has(key)) s.delete(key);
  else s.add(key);
  expanded.value = s;
}

async function save() {
  if (!cfg.value) return;
  saving.value = true;
  msg.value = "";
  try {
    const toolsetsArr = Array.from(checked.value);
    const mcpArr = Array.from(mcpChecked.value);
    const res = await putHermesTools({
      toolsets: toolsetsArr,
      mcp_servers: mcpArr,
      no_mcp: noMcp.value,
    });
    msg.value = `✅ 已保存 ${res.saved.length} 个条目到 Hermes config.yaml,请重启 Hermes 网关生效`;
    msgType.value = "success";
    // 刷新当前启用状态
    await fetchTools();
  } catch (e: any) {
    msg.value = "❌ " + (e.response?.data?.error || e.message);
    msgType.value = "error";
  } finally {
    saving.value = false;
  }
}

async function reset() {
  if (!confirm("确定要重置 OneBot 平台的工具集配置吗?将删除 platform_toolsets.onebot,回到默认未配置状态。")) return;
  saving.value = true;
  msg.value = "";
  try {
    await resetHermesTools();
    msg.value = "✅ 已重置,请重启 Hermes 网关生效";
    msgType.value = "success";
    await fetchTools();
  } catch (e: any) {
    msg.value = "❌ " + (e.response?.data?.error || e.message);
    msgType.value = "error";
  } finally {
    saving.value = false;
  }
}

const stats = computed(() => ({
  total: toolsets.value.length,
  enabled: checked.value.size + mcpChecked.value.size + (noMcp.value ? 1 : 0),
  mcp: mcpServers.value.length,
}));
</script>

<template>
  <div>
    <h2>工具管理</h2>
    <div v-if="msg" :class="['message', msgType]">{{ msg }}</div>

    <div v-if="!hermesDirOk" class="section warn-section">
      <h3>⚠ Hermes 安装目录未就绪</h3>
      <p class="hint">
        工具管理需要先配置 <code>hermes_install_dir</code> 并确保目录存在。
        请前往 <RouterLink to="/hermes">Hermes 插件管理页</RouterLink> 配置安装目录并安装插件,
        然后回到本页配置工具集。
      </p>
    </div>

    <div v-else-if="loading" class="loading">加载工具集列表中...</div>

    <div v-else>
      <!-- 说明卡片 -->
      <div class="section info-section">
        <h3>说明</h3>
        <p class="hint">
          本页直接读写 Hermes 的 <code>config.yaml</code>(<code>platform_toolsets.onebot</code> +
          <code>known_plugin_toolsets.onebot</code>),管理 OneBot 平台可用的工具集。
          修改后需 <strong>重启 Hermes 网关</strong> 生效。WebUI 只控制 OneBot 平台的工具集白名单,
          MCP 服务器的全局 <code>enabled</code> 标志由 Hermes 端管理。
        </p>
      </div>

      <!-- 统计 -->
      <div class="section stats-section">
        <div class="stat-item">
          <span class="stat-label">可配置工具集</span>
          <span class="stat-value">{{ stats.total }}</span>
        </div>
        <div class="stat-item">
          <span class="stat-label">已启用</span>
          <span class="stat-value" style="color: var(--success);">{{ stats.enabled }}</span>
        </div>
        <div class="stat-item">
          <span class="stat-label">MCP 服务器</span>
          <span class="stat-value">{{ stats.mcp }}</span>
        </div>
      </div>

      <!-- 插件工具集(含 OneBot 28 个 QQ 工具) -->
      <div v-if="pluginToolsets.length" class="section">
        <h3>🔌 插件工具集</h3>
        <p class="hint" style="margin-bottom: 0.75rem;">由插件提供的工具集(含 OneBot 28 个 QQ 工具)。</p>
        <div v-for="t in pluginToolsets" :key="t.key" class="toolset-row">
          <label class="checkbox-row">
            <input type="checkbox" :checked="checked.has(t.key)" @change="toggle(t.key)" />
            <span class="toolset-label">{{ t.label }}</span>
            <span v-if="t.is_plugin" class="badge-plugin">插件</span>
          </label>
          <p class="toolset-desc">{{ t.description }}</p>
          <button class="expand-btn" @click="toggleExpand(t.key)">
            {{ expanded.has(t.key) ? "▼ 收起" : "▶ 展开工具" }}（{{ t.tools.length }}）
          </button>
          <div v-if="expanded.has(t.key)" class="tools-list">
            <span v-for="tool in t.tools" :key="tool" class="tool-chip">{{ tool }}</span>
          </div>
        </div>
      </div>

      <!-- 内置工具集 -->
      <div class="section">
        <h3>🛠️ 内置工具集</h3>
        <p class="hint" style="margin-bottom: 0.75rem;">Hermes 核心工具集,勾选后 OneBot 平台会话可用。</p>
        <div v-for="t in builtinToolsets" :key="t.key" class="toolset-row">
          <label class="checkbox-row">
            <input type="checkbox" :checked="checked.has(t.key)" @change="toggle(t.key)" />
            <span class="toolset-label">{{ t.label }}</span>
          </label>
          <p class="toolset-desc">{{ t.description }}</p>
          <button class="expand-btn" @click="toggleExpand(t.key)">
            {{ expanded.has(t.key) ? "▼ 收起" : "▶ 展开工具" }}（{{ t.tools.length }}）
          </button>
          <div v-if="expanded.has(t.key)" class="tools-list">
            <span v-for="tool in t.tools" :key="tool" class="tool-chip">{{ tool }}</span>
          </div>
        </div>
      </div>

      <!-- MCP 服务器 -->
      <div v-if="mcpServers.length" class="section">
        <h3>🌐 MCP 服务器</h3>
        <p class="hint" style="margin-bottom: 0.75rem;">
          勾选的 MCP 服务器会列入 OneBot 平台白名单;不勾任何 MCP 则使用全局默认(所有已启用的 MCP)。
          勾选"禁用所有 MCP"会向 OneBot 平台屏蔽全部 MCP 服务器。
        </p>
        <div v-for="m in mcpServers" :key="m.name" class="toolset-row">
          <label class="checkbox-row">
            <input
              type="checkbox"
              :checked="mcpChecked.has(m.name)"
              :disabled="noMcp"
              @change="toggleMcp(m.name)"
            />
            <span class="toolset-label">{{ m.name }}</span>
            <span :class="['badge-mcp', m.enabled ? 'enabled' : 'disabled']">
              {{ m.enabled ? "全局启用" : "全局禁用" }}
            </span>
          </label>
        </div>
        <label class="checkbox-row no-mcp-row">
          <input type="checkbox" v-model="noMcp" />
          <span class="toolset-label">🚫 禁用所有 MCP 服务器(写 no_mcp sentinel)</span>
        </label>
      </div>

      <!-- 操作按钮 -->
      <div class="action-row">
        <button @click="save" :disabled="saving" class="save-btn">
          {{ saving ? "保存中..." : "保存配置" }}
        </button>
        <button @click="reset" :disabled="saving" class="reset-btn">
          重置为默认
        </button>
        <button @click="fetchTools" :disabled="saving" class="reload-btn">
          ↻ 重新加载
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.section { background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; padding: 1.25rem; margin-bottom: 1.5rem; }
.section h3 { margin: 0 0 1rem; font-size: 1rem; border-bottom: 2px solid var(--primary); padding-bottom: 0.5rem; }
.hint { font-size: 0.85rem; color: var(--text-muted); margin: 0; line-height: 1.5; }
.hint code { background: var(--bg); padding: 0.1rem 0.3rem; border-radius: 3px; font-size: 0.85em; }
.hint strong { color: var(--danger); }

.warn-section { border-color: var(--warning); background: #fff9e6; }
.warn-section h3 { color: #856404; border-color: var(--warning); }
.warn-section a { color: var(--primary); }

.info-section { background: #eef5ff; border-color: var(--primary); }

.stats-section { display: flex; gap: 2rem; justify-content: space-around; text-align: center; }
.stat-item { display: flex; flex-direction: column; gap: 0.25rem; }
.stat-label { font-size: 0.85rem; color: var(--text-muted); }
.stat-value { font-size: 1.6rem; font-weight: 700; }

.toolset-row { padding: 0.75rem 0; border-bottom: 1px solid var(--border); }
.toolset-row:last-child { border-bottom: none; }
.checkbox-row { display: flex; align-items: center; gap: 0.5rem; cursor: pointer; font-weight: 500; margin-bottom: 0.25rem; }
.checkbox-row input[type="checkbox"] { width: auto; cursor: pointer; }
.toolset-label { font-size: 0.95rem; }
.badge-plugin { font-size: 0.75rem; padding: 0.1rem 0.4rem; border-radius: 10px; background: rgba(255, 193, 7, 0.15); color: #856404; }
.badge-mcp { font-size: 0.75rem; padding: 0.1rem 0.4rem; border-radius: 10px; }
.badge-mcp.enabled { background: rgba(40, 167, 69, 0.12); color: var(--success); }
.badge-mcp.disabled { background: rgba(220, 53, 69, 0.12); color: var(--danger); }
.toolset-desc { font-size: 0.85rem; color: var(--text-muted); margin: 0.25rem 0 0 1.5rem; }
.expand-btn { background: none; border: none; color: var(--primary); cursor: pointer; font-size: 0.8rem; padding: 0.2rem 0; margin-left: 1.5rem; }
.expand-btn:hover { text-decoration: underline; }
.tools-list { margin: 0.5rem 0 0 1.5rem; display: flex; flex-wrap: wrap; gap: 0.3rem; }
.tool-chip { font-size: 0.75rem; font-family: monospace; background: var(--bg); border: 1px solid var(--border); padding: 0.15rem 0.5rem; border-radius: 12px; color: var(--text-muted); }

.no-mcp-row { margin-top: 1rem; padding-top: 0.75rem; border-top: 1px dashed var(--border); }

.action-row { display: flex; gap: 0.75rem; margin-top: 1rem; }
.reset-btn { background: var(--card-bg); color: var(--text-muted); border: 1px solid var(--border); padding: 0.6rem 1.5rem; border-radius: 6px; cursor: pointer; font-size: 0.95rem; }
.reset-btn:hover { background: var(--bg); }
.reset-btn:disabled { cursor: not-allowed; opacity: 0.6; }
.reload-btn { background: var(--card-bg); color: var(--primary); border: 1px solid var(--primary); padding: 0.6rem 1.5rem; border-radius: 6px; cursor: pointer; font-size: 0.95rem; }
.reload-btn:hover { background: rgba(74, 144, 226, 0.08); }
.reload-btn:disabled { cursor: not-allowed; opacity: 0.6; }

.message { padding: 0.75rem 1rem; border-radius: 6px; margin-bottom: 1rem; }
.message.success { background: #d4edda; color: #155724; border-left: 4px solid var(--success); }
.message.error { background: #f8d7da; color: #721c24; border-left: 4px solid var(--danger); }
.loading { text-align: center; padding: 2rem; color: var(--text-muted, #666); }
</style>