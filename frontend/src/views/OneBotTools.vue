<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import {
  getOneBotToolPolicies,
  putOneBotToolPolicies,
  resetOneBotToolPolicies,
  type OneBotToolCatalogEntry,
  type OneBotToolPermission,
  type OneBotToolPoliciesState,
  type OneBotToolPolicy,
} from "../api";

interface ToolRow extends OneBotToolCatalogEntry {
  category: string;
}

const tools = ref<ToolRow[]>([]);
const policies = ref<Record<string, OneBotToolPolicy>>({});
const search = ref("");
const category = ref("all");
const loading = ref(true);
const saving = ref(false);
const message = ref("");
const messageType = ref<"success" | "error">("success");

function validPermission(value: unknown): OneBotToolPermission {
  return value === "admin" ? "admin" : "everyone";
}

function errorMessage(error: any): string {
  const payload = error?.response?.data?.error;
  if (typeof payload === "string") return payload;
  if (payload && typeof payload.message === "string") return payload.message;
  return error?.message || "请求失败";
}

function catalogEntries(data: OneBotToolPoliciesState): OneBotToolCatalogEntry[] {
  const raw = data.catalog ?? data.tools ?? [];
  if (Array.isArray(raw)) return raw;
  if (!raw || typeof raw !== "object") return [];
  return Object.entries(raw).map(([name, entry]) => {
    const value: Partial<OneBotToolCatalogEntry> = entry && typeof entry === "object" ? entry : {};
    return { ...value, name: typeof value.name === "string" && value.name ? value.name : name };
  });
}

function effectivePolicies(data: OneBotToolPoliciesState): Record<string, Partial<OneBotToolPolicy>> {
  const raw = data.effective_policies ?? data.effective ?? data.policies ?? {};
  if (Array.isArray(raw)) {
    return Object.fromEntries(raw.filter((item) => item?.name).map((item) => [item.name, item]));
  }
  return raw && typeof raw === "object" ? raw : {};
}

function applyState(data: OneBotToolPoliciesState) {
  const effective = effectivePolicies(data);
  const seen = new Set<string>();
  const catalog = catalogEntries(data).filter((entry) => {
    if (!entry || typeof entry.name !== "string" || !entry.name || seen.has(entry.name)) return false;
    seen.add(entry.name);
    return true;
  });

  tools.value = catalog.map((entry) => ({
    ...entry,
    description: typeof entry.description === "string"
      ? entry.description
      : typeof entry.schema?.description === "string" ? entry.schema.description : "",
    category: typeof entry.category === "string" && entry.category ? entry.category : "其他",
  }));
  policies.value = Object.fromEntries(catalog.map((entry) => {
    const embedded = entry.effective_policy ?? entry.effective ?? {};
    const candidate = effective[entry.name] ?? embedded;
    const policy = candidate && typeof candidate === "object" ? candidate : {};
    const registered = typeof policy.registered === "boolean"
      ? policy.registered
      : typeof entry.registered === "boolean"
        ? entry.registered
        : entry.default_registered ?? true;
    return [entry.name, {
      registered,
      permission: validPermission(policy.permission ?? entry.permission ?? entry.default_permission),
    }];
  }));
}

async function load() {
  loading.value = true;
  message.value = "";
  try {
    applyState(await getOneBotToolPolicies());
  } catch (error: any) {
    message.value = "加载失败: " + errorMessage(error);
    messageType.value = "error";
  } finally {
    loading.value = false;
  }
}

const categories = computed(() => [...new Set(tools.value.map((tool) => tool.category))].sort());
const filteredTools = computed(() => {
  const query = search.value.trim().toLowerCase();
  return tools.value.filter((tool) => {
    if (category.value !== "all" && tool.category !== category.value) return false;
    if (!query) return true;
    return [
      tool.name,
      tool.description,
      tool.category,
      indicatorText(tool.scope),
      indicatorText(tool.packet),
      indicatorText(tool.caveat),
    ]
      .some((value) => String(value ?? "").toLowerCase().includes(query));
  });
});
const stats = computed(() => {
  const values = tools.value.map((tool) => policies.value[tool.name]).filter(Boolean);
  return {
    total: tools.value.length,
    registered: values.filter((policy) => policy.registered).length,
    admin: values.filter((policy) => policy.permission === "admin").length,
    visible: filteredTools.value.length,
  };
});

function indicatorText(value: OneBotToolCatalogEntry["packet"] | OneBotToolCatalogEntry["scope"]): string {
  if (Array.isArray(value)) return value.filter((item) => typeof item === "string").join("、");
  if (typeof value === "string") return value;
  return value === true ? "是" : "";
}

function setRegistered(name: string, registered: boolean) {
  const current = policies.value[name];
  if (current) policies.value[name] = { ...current, registered };
}

function setPermission(name: string, permission: unknown) {
  const current = policies.value[name];
  if (current) policies.value[name] = { ...current, permission: validPermission(permission) };
}

function setVisibleRegistration(registered: boolean) {
  for (const tool of filteredTools.value) setRegistered(tool.name, registered);
}

function setVisiblePermission(permission: OneBotToolPermission) {
  for (const tool of filteredTools.value) setPermission(tool.name, permission);
}

async function save() {
  saving.value = true;
  message.value = "";
  try {
    // Rebuild from the current catalog so stale or unknown policy keys cannot be persisted.
    const payload = Object.fromEntries(tools.value.map((tool) => [tool.name, policies.value[tool.name]]));
    await putOneBotToolPolicies(payload);
    applyState(await getOneBotToolPolicies());
    message.value = "配置已保存。注册可见性变更需重启 Hermes 后生效。";
    messageType.value = "success";
  } catch (error: any) {
    message.value = "保存失败: " + errorMessage(error);
    messageType.value = "error";
  } finally {
    saving.value = false;
  }
}

async function reset() {
  if (!confirm("确定将全部 OneBot 工具策略重置为默认值吗？")) return;
  saving.value = true;
  message.value = "";
  try {
    await resetOneBotToolPolicies();
    applyState(await getOneBotToolPolicies());
    message.value = "已重置为默认策略。注册可见性变更需重启 Hermes 后生效。";
    messageType.value = "success";
  } catch (error: any) {
    message.value = "重置失败: " + errorMessage(error);
    messageType.value = "error";
  } finally {
    saving.value = false;
  }
}

onMounted(load);
</script>

<template>
  <div class="onebot-tools">
    <div class="page-heading">
      <div>
        <h2>OneBot 工具</h2>
        <p>管理 Hermes 中 OneBot 工具的注册状态和调用权限。</p>
      </div>
      <div class="heading-mark">OB11</div>
    </div>

    <div v-if="message" :class="['message', messageType]">{{ message }}</div>

    <div class="notice">
      <strong>作用范围</strong>
      <span>注册状态决定工具是否对 Hermes 可见，变更后需要重启 Hermes；权限策略仅约束 Hermes 调用。自动化 API 始终保留完整 OneBot 工具权限，不受本页设置影响。</span>
    </div>

    <div v-if="loading" class="loading">加载 OneBot 工具目录中...</div>

    <template v-else>
      <div class="stats">
        <div><span>目录工具</span><strong>{{ stats.total }}</strong></div>
        <div><span>已注册</span><strong class="ok">{{ stats.registered }}</strong></div>
        <div><span>仅管理员</span><strong class="warn">{{ stats.admin }}</strong></div>
        <div><span>当前结果</span><strong>{{ stats.visible }}</strong></div>
      </div>

      <section class="section">
        <div class="toolbar">
          <input v-model="search" type="search" placeholder="搜索工具名、描述或提示..." aria-label="搜索工具" />
          <select v-model="category" aria-label="按分类筛选">
            <option value="all">全部分类</option>
            <option v-for="item in categories" :key="item" :value="item">{{ item }}</option>
          </select>
          <button class="reload-btn" :disabled="saving" @click="load">重新加载</button>
        </div>

        <div v-if="filteredTools.length" class="bulk-actions">
          <span>操作当前 {{ filteredTools.length }} 项</span>
          <button @click="setVisibleRegistration(true)">全部注册</button>
          <button @click="setVisibleRegistration(false)">全部取消</button>
          <button @click="setVisiblePermission('everyone')">所有人可用</button>
          <button @click="setVisiblePermission('admin')">仅管理员</button>
        </div>

        <div class="table-wrap">
          <table v-if="filteredTools.length">
            <thead><tr><th>注册</th><th>工具</th><th>分类</th><th>权限</th><th>注意事项</th></tr></thead>
            <tbody>
              <tr v-for="tool in filteredTools" :key="tool.name">
                <td data-label="注册">
                  <input
                    type="checkbox"
                    :checked="policies[tool.name]?.registered"
                    :aria-label="`注册 ${tool.name}`"
                    @change="setRegistered(tool.name, ($event.target as HTMLInputElement).checked)"
                  />
                </td>
                <td data-label="工具" class="tool-cell">
                  <code>{{ tool.name }}</code>
                  <span>{{ tool.description || "暂无描述" }}</span>
                </td>
                <td data-label="分类"><span class="category-badge">{{ tool.category }}</span></td>
                <td data-label="权限">
                  <select
                    class="permission"
                    :value="policies[tool.name]?.permission"
                    @change="setPermission(tool.name, ($event.target as HTMLSelectElement).value)"
                  >
                    <option value="everyone">所有人</option>
                    <option value="admin">仅管理员</option>
                  </select>
                </td>
                <td data-label="注意事项" class="indicators">
                  <span v-if="indicatorText(tool.scope)" class="indicator scope" :title="indicatorText(tool.scope)">范围: {{ indicatorText(tool.scope) }}</span>
                  <span v-if="indicatorText(tool.packet)" class="indicator packet" :title="indicatorText(tool.packet)">Packet: {{ indicatorText(tool.packet) }}</span>
                  <span v-if="indicatorText(tool.caveat)" class="indicator caveat" :title="indicatorText(tool.caveat)">注意: {{ indicatorText(tool.caveat) }}</span>
                  <span v-if="!indicatorText(tool.scope) && !indicatorText(tool.packet) && !indicatorText(tool.caveat)" class="muted">-</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p v-if="!tools.length" class="empty">后端未返回可管理的 OneBot 工具目录。</p>
        <p v-else-if="!filteredTools.length" class="empty">没有匹配当前筛选条件的工具。</p>
      </section>

      <div class="action-row">
        <button class="save-btn" :disabled="saving || !tools.length" @click="save">{{ saving ? "处理中..." : "保存配置" }}</button>
        <button class="reset-btn" :disabled="saving" @click="reset">重置为默认</button>
      </div>
    </template>
  </div>
</template>

<style scoped>
.page-heading { display: flex; align-items: center; justify-content: space-between; margin-bottom: 1rem; }
.page-heading h2 { margin: 0 0 0.2rem; }
.page-heading p { margin: 0; color: var(--text-muted); font-size: 0.9rem; }
.heading-mark { font: 700 0.8rem monospace; letter-spacing: 0.12em; color: var(--primary); border: 1px solid rgba(74,144,226,0.35); background: rgba(74,144,226,0.08); padding: 0.45rem 0.6rem; border-radius: 5px; }
.notice { display: grid; grid-template-columns: 6rem 1fr; gap: 0.75rem; padding: 1rem 1.25rem; margin-bottom: 1.25rem; border: 1px solid #f0c36a; border-left: 4px solid var(--warning); border-radius: 8px; background: #fff9e8; font-size: 0.87rem; line-height: 1.55; }
.notice strong { color: #765400; }
.stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 0.75rem; margin-bottom: 1.25rem; }
.stats div { display: flex; flex-direction: column; gap: 0.25rem; padding: 0.9rem 1rem; background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; }
.stats span { color: var(--text-muted); font-size: 0.8rem; }
.stats strong { font-size: 1.45rem; }
.stats .ok { color: var(--success); }
.stats .warn { color: #b77900; }
.section { background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; padding: 1.25rem; }
.toolbar { display: grid; grid-template-columns: minmax(220px, 1fr) auto auto; gap: 0.65rem; margin-bottom: 0.85rem; }
.toolbar input, .toolbar select, .permission { padding: 0.5rem; border: 1px solid #ccc; border-radius: 4px; background: var(--card-bg); font-size: 0.88rem; }
.reload-btn, .bulk-actions button, .reset-btn { background: var(--card-bg); border: 1px solid var(--border); border-radius: 5px; cursor: pointer; padding: 0.5rem 0.75rem; color: var(--text-muted); }
.reload-btn:hover, .bulk-actions button:hover, .reset-btn:hover { background: var(--bg); color: var(--text); }
.bulk-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 0.45rem; padding: 0.65rem; margin-bottom: 0.75rem; background: var(--bg); border-radius: 5px; }
.bulk-actions span { margin-right: 0.35rem; color: var(--text-muted); font-size: 0.82rem; }
.bulk-actions button { padding: 0.3rem 0.6rem; font-size: 0.8rem; }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 0.86rem; }
th { text-align: left; padding: 0.6rem; border-bottom: 2px solid var(--border); color: var(--text-muted); font-weight: 600; }
td { padding: 0.7rem 0.6rem; border-bottom: 1px solid var(--border); vertical-align: top; }
tbody tr:last-child td { border-bottom: none; }
td input[type="checkbox"] { width: 1rem; height: 1rem; accent-color: var(--primary); }
.tool-cell { min-width: 250px; }
.tool-cell code { display: block; color: var(--primary-dark); font-weight: 600; margin-bottom: 0.25rem; }
.tool-cell span { display: block; color: var(--text-muted); line-height: 1.4; }
.category-badge { white-space: nowrap; background: rgba(74,144,226,0.1); color: var(--primary-dark); border-radius: 10px; padding: 0.15rem 0.45rem; font-size: 0.75rem; }
.permission { min-width: 110px; padding: 0.35rem 0.45rem; }
.indicators { max-width: 250px; }
.indicator { display: block; width: fit-content; max-width: 100%; margin-bottom: 0.25rem; padding: 0.15rem 0.4rem; border-radius: 4px; font-size: 0.73rem; line-height: 1.35; overflow-wrap: anywhere; }
.scope { color: #5e477d; background: #f2edfa; }
.packet { color: #355b8c; background: #edf4fc; }
.caveat { color: #795500; background: #fff4d7; }
.muted { color: var(--text-muted); }
.action-row { display: flex; gap: 0.75rem; margin-top: 1rem; }
.reset-btn { padding: 0.6rem 1.2rem; font-size: 0.95rem; }
button:disabled { cursor: not-allowed; opacity: 0.55; }
.message { padding: 0.75rem 1rem; border-radius: 6px; margin-bottom: 1rem; }
.message.success { background: #d4edda; color: #155724; border-left: 4px solid var(--success); }
.message.error { background: #f8d7da; color: #721c24; border-left: 4px solid var(--danger); }
.loading, .empty { text-align: center; padding: 2rem; color: var(--text-muted); }

@media (max-width: 700px) {
  .stats { grid-template-columns: repeat(2, 1fr); }
  .toolbar { grid-template-columns: 1fr; }
  .notice { grid-template-columns: 1fr; gap: 0.25rem; }
  .table-wrap { overflow: visible; }
  table, tbody { display: block; }
  thead { display: none; }
  tr { display: grid; grid-template-columns: 1fr 1fr; gap: 0.65rem; padding: 0.85rem 0; border-bottom: 1px solid var(--border); }
  td { display: block; padding: 0; border: 0; min-width: 0; }
  td::before { content: attr(data-label); display: block; margin-bottom: 0.25rem; color: var(--text-muted); font-size: 0.7rem; font-weight: 600; }
  .tool-cell, .indicators { grid-column: 1 / -1; max-width: none; min-width: 0; }
  .action-row { position: sticky; bottom: 0; padding: 0.75rem 0; background: var(--bg); }
}
</style>
