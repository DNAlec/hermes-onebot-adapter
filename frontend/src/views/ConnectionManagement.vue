<script setup lang="ts">
import { ref, computed, onMounted } from "vue";
import {
  checkHermesDir, installPlugin, uninstallPlugin, type HermesDirStatus,
  getHermesMode, putHermesMode, refreshHermesMode, type HermesMode,
} from "../api";
import { useConfig } from "../composables/useConfig";

const { cfg, load, save: saveConfig } = useConfig();

const saving = ref(false);
const installing = ref(false);
const uninstalling = ref(false);
const msg = ref("");
const msgType = ref<"success" | "error" | "warning">("success");
const installResult = ref<any>(null);
const showToken = ref(false);
const showOnebotToken = ref(false);
const hermesDirStatus = ref<HermesDirStatus | null>(null);
const checkingDir = ref(false);

// ── Hermes 会话隔离(group_sessions_per_user)──
const hermesMode = ref<HermesMode | null>(null);
const editingPerUser = ref(false);       // 修改表单中的临时值
const savingMode = ref(false);
const refreshingMode = ref(false);
const modeMsg = ref("");
const modeMsgType = ref<"success" | "error" | "warning">("success");

const baseline = ref({ port: 0, path: "", token: "" });

const needsReinstall = computed(() => {
  if (!cfg.value) return false;
  return (
    cfg.value.hermes_ws_port !== baseline.value.port ||
    cfg.value.hermes_ws_path !== baseline.value.path ||
    cfg.value.hermes_ws_token !== baseline.value.token
  );
});

function syncBaseline() {
  if (!cfg.value) return;
  baseline.value = {
    port: cfg.value.hermes_ws_port,
    path: cfg.value.hermes_ws_path,
    token: cfg.value.hermes_ws_token,
  };
}

async function checkDir() {
  checkingDir.value = true;
  try {
    hermesDirStatus.value = await checkHermesDir();
  } catch {
    hermesDirStatus.value = null;
  } finally {
    checkingDir.value = false;
  }
}

onMounted(async () => {
  try {
    await load();
    syncBaseline();
    checkDir();
    fetchHermesMode();
  } catch (e: any) {
    msg.value = "加载配置失败: " + (e.response?.data?.error || e.message);
    msgType.value = "error";
  }
});

async function fetchHermesMode() {
  try {
    hermesMode.value = await getHermesMode();
  } catch (e: any) {
    modeMsg.value = "❌ 读取 Hermes 配置失败: " + (e.response?.data?.error || e.message);
    modeMsgType.value = "error";
  }
}

async function saveHermesMode(value: boolean) {
  savingMode.value = true;
  modeMsg.value = "";
  try {
    const res = await putHermesMode(value);
    modeMsg.value = "✅ " + res.note;
    modeMsgType.value = "warning";  // 因为需重启网关
    editingPerUser.value = false;
    // 重新读取(读文件回显,不一定是插件上报的最新值)
    await fetchHermesMode();
  } catch (e: any) {
    modeMsg.value = "❌ " + (e.response?.data?.error || e.message);
    modeMsgType.value = "error";
  } finally {
    savingMode.value = false;
  }
}

async function refreshHermesModeReport() {
  refreshingMode.value = true;
  modeMsg.value = "";
  try {
    const res = await refreshHermesMode();
    if (res.ok) {
      modeMsg.value = "✅ " + (res.note || "已请求插件重新上报");
      modeMsgType.value = "success";
      // 等一下让插件上报后再拉取
      setTimeout(() => fetchHermesMode(), 800);
    } else {
      modeMsg.value = "⚠ " + (res.error || "刷新失败");
      modeMsgType.value = "warning";
    }
  } catch (e: any) {
    modeMsg.value = "❌ " + (e.response?.data?.error || e.message);
    modeMsgType.value = "error";
  } finally {
    refreshingMode.value = false;
  }
}

async function save() {
  if (!cfg.value) return;
  saving.value = true;
  msg.value = "";
  const c = cfg.value;
  const wasDirty = needsReinstall.value;
  try {
    await saveConfig({
      onebot_mode: c.onebot_mode,
      onebot_reverse_ws_port: c.onebot_reverse_ws_port,
      onebot_reverse_ws_path: c.onebot_reverse_ws_path,
      onebot_forward_ws_url: c.onebot_forward_ws_url,
      onebot_ws_token: c.onebot_ws_token,
      self_id: c.self_id,
      hermes_install_dir: c.hermes_install_dir,
      hermes_ws_port: c.hermes_ws_port,
      hermes_ws_path: c.hermes_ws_path,
      hermes_ws_token: c.hermes_ws_token,
      event_queue_enabled: c.event_queue_enabled,
      event_queue_max_per_chat: c.event_queue_max_per_chat,
      event_queue_idle_timeout: c.event_queue_idle_timeout,
    });
    syncBaseline();
    if (wasDirty) {
      msg.value = "配置已保存（检测到 Hermes 连接参数变更，请重新安装插件刷新 Hermes 端配置）";
      msgType.value = "warning";
    } else {
      msg.value = "配置已保存";
      msgType.value = "success";
    }
  } catch (e: any) {
    msg.value = (e.response?.data?.error || e.message);
    msgType.value = "error";
  } finally {
    saving.value = false;
  }
}

async function install() {
  installing.value = true;
  installResult.value = null;
  msg.value = "";
  try {
    installResult.value = await installPlugin(cfg.value?.hermes_install_dir || "");
    if (installResult.value.error) {
      msg.value = "安装失败: " + installResult.value.error;
      msgType.value = "error";
    } else {
      msg.value = "插件安装成功";
      msgType.value = "success";
      await load(true);
      syncBaseline();
    }
  } catch (e: any) {
    msg.value = (e.response?.data?.error || e.message);
    msgType.value = "error";
  } finally {
    installing.value = false;
  }
}

async function uninstall() {
  uninstalling.value = true;
  installResult.value = null;
  msg.value = "";
  try {
    installResult.value = await uninstallPlugin(cfg.value?.hermes_install_dir || "");
    if (installResult.value.error) {
      msg.value = "卸载失败: " + installResult.value.error;
      msgType.value = "error";
    } else {
      msg.value = "插件已卸载" + (installResult.value.env_cleaned ? "（环境变量已清理）" : "");
      msgType.value = "success";
    }
  } catch (e: any) {
    msg.value = (e.response?.data?.error || e.message);
    msgType.value = "error";
  } finally {
    uninstalling.value = false;
  }
}

function copyToken() {
  if (cfg.value?.hermes_ws_token) {
    navigator.clipboard.writeText(cfg.value.hermes_ws_token);
    msg.value = "Hermes WS Token 已复制到剪贴板";
    msgType.value = "success";
  }
}

function copyOnebotToken() {
  if (cfg.value?.onebot_ws_token) {
    navigator.clipboard.writeText(cfg.value.onebot_ws_token);
    msg.value = "OneBot WS Token 已复制到剪贴板";
    msgType.value = "success";
  }
}

function regenerateToken() {
  if (!cfg.value) return;
  const arr = new Uint8Array(24);
  crypto.getRandomValues(arr);
  const b64 = btoa(String.fromCharCode(...arr))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  cfg.value.hermes_ws_token = b64;
}

function regenerateOnebotToken() {
  if (!cfg.value) return;
  const arr = new Uint8Array(24);
  crypto.getRandomValues(arr);
  const b64 = btoa(String.fromCharCode(...arr))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  cfg.value.onebot_ws_token = b64;
}

function getAdapterUrl() {
  const port = cfg.value?.hermes_ws_port || 18810;
  return `ws://127.0.0.1:${port}/hermes`;
}

function copyAdapterUrl() {
  navigator.clipboard.writeText(getAdapterUrl());
  msg.value = "适配器 URL 已复制到剪贴板";
  msgType.value = "success";
}
</script>

<template>
  <div>
    <h2>连接管理</h2>

    <div v-if="msg" :class="['message', msgType]">{{ msg }}</div>
    <div v-if="!cfg" class="loading">加载配置中...</div>

    <div v-if="cfg">
      <div class="section">
        <h3>OneBot 连接（上行）</h3>
        <p class="section-desc">配置 OneBot 客户端（NapCat/go-cqhttp）与适配器的连接方式</p>

        <div class="subsection">
          <h4>连接模式</h4>
          <div class="radio-group">
            <label class="radio-label" :class="{ selected: cfg.onebot_mode === 'reverse' }">
              <input type="radio" v-model="cfg.onebot_mode" value="reverse" />
              <div>
                <strong>反向 WS</strong>
                <p class="hint">OneBot 主动连接到适配器（推荐）</p>
              </div>
            </label>
            <label class="radio-label" :class="{ selected: cfg.onebot_mode === 'forward' }">
              <input type="radio" v-model="cfg.onebot_mode" value="forward" />
              <div>
                <strong>正向 WS</strong>
                <p class="hint">适配器主动连接到 OneBot</p>
              </div>
            </label>
          </div>
        </div>

        <div class="subsection">
          <h4>{{ cfg.onebot_mode === 'reverse' ? '反向 WS 配置' : '正向 WS 配置' }}</h4>
          <label v-if="cfg.onebot_mode === 'reverse'">
            监听端口
            <input type="number" v-model.number="cfg.onebot_reverse_ws_port" min="1" max="65535" />
            <span class="hint">OneBot 将连接到此端口</span>
          </label>
          <label v-if="cfg.onebot_mode === 'reverse'">
            WS 路径
            <input v-model="cfg.onebot_reverse_ws_path" placeholder="/onebot" />
            <span class="hint">OneBot 连接的 WebSocket 路径</span>
          </label>
          <label v-if="cfg.onebot_mode === 'forward'">
            OneBot WS 地址
            <input v-model="cfg.onebot_forward_ws_url" placeholder="ws://127.0.0.1:3001" />
            <span class="hint">OneBot 提供的 WebSocket 地址（统一接口，同时接收事件和调用 API）</span>
          </label>
          <label>
            WS Token
            <div class="token-input-wrapper">
              <input
                :type="showOnebotToken ? 'text' : 'password'"
                v-model="cfg.onebot_ws_token"
                class="token-input"
                placeholder="自动生成"
              />
              <button @click="showOnebotToken = !showOnebotToken" class="icon-btn" :title="showOnebotToken ? '隐藏' : '显示'">
                {{ showOnebotToken ? '🙈' : '👁️' }}
              </button>
              <button @click="copyOnebotToken" class="icon-btn" title="复制">📋</button>
              <button @click="regenerateOnebotToken" class="icon-btn" title="重新生成">🔄</button>
            </div>
            <span class="hint">OneBot WebSocket 鉴权令牌；反向模式校验入站连接（query ?token= 或 Authorization: Bearer）；正向模式作为出站 Authorization: Bearer 头</span>
          </label>
        </div>

        <div class="subsection last">
          <h4>Bot 标识</h4>
          <label>
            Bot QQ (self_id)
            <input v-model="cfg.self_id" placeholder="留空自动探测" />
            <span class="hint">机器人的 QQ 号，留空会自动探测</span>
          </label>
        </div>
      </div>

      <div class="section">
        <h3>Hermes 连接（下行）</h3>
        <p class="section-desc">管理 Hermes 插件的安装与连接配置</p>

        <div v-if="needsReinstall" class="reinstall-banner">
          检测到 Hermes 连接参数（端口/路径/Token）变更，请先「保存配置」再点击「安装插件到 Hermes」刷新插件端配置
        </div>

        <div class="subsection">
          <h4>Hermes 安装目录</h4>
          <div class="dir-input-wrapper">
            <input v-model="cfg.hermes_install_dir" placeholder="留空则使用 ~/.hermes" class="dir-input" />
            <span v-if="hermesDirStatus !== null" :class="['dir-status', hermesDirStatus.exists ? 'exists' : 'not-exists']">
              {{ hermesDirStatus.exists ? '目录存在' : '目录不存在' }}
            </span>
            <button @click="checkDir" :disabled="checkingDir" class="dir-check-btn">
              {{ checkingDir ? '检测中...' : '重新检测' }}
            </button>
          </div>
          <span class="hint">Hermes Agent 的安装路径</span>
        </div>

        <div class="subsection">
          <h4>WS 连接参数</h4>
          <div class="form-row">
            <label>
              WS 端口
              <input type="number" v-model.number="cfg.hermes_ws_port" min="1" max="65535" />
            </label>
            <label>
              WS 路径
              <input v-model="cfg.hermes_ws_path" placeholder="/hermes" />
            </label>
          </div>
        </div>

        <div class="subsection">
          <h4>WS Token</h4>
          <div class="token-input-wrapper">
            <input
              :type="showToken ? 'text' : 'password'"
              v-model="cfg.hermes_ws_token"
              class="token-input"
            />
            <button @click="showToken = !showToken" class="icon-btn" :title="showToken ? '隐藏' : '显示'">
              {{ showToken ? '🙈' : '👁️' }}
            </button>
            <button @click="copyToken" class="icon-btn" title="复制">📋</button>
            <button @click="regenerateToken" class="icon-btn" title="重新生成">🔄</button>
          </div>
          <span class="hint">Hermes 插件连接时的 WS Token；修改后需保存并重新安装插件以同步到 Hermes 端</span>
        </div>

        <div class="subsection">
          <h4>适配器 URL</h4>
          <div class="url-container">
            <code class="url-display">{{ getAdapterUrl() }}</code>
            <button @click="copyAdapterUrl" class="icon-btn" title="复制">📋</button>
          </div>
          <span class="hint">将此 URL 配置到 Hermes 的 ONEBOT_ADAPTER_URL 环境变量</span>
        </div>

        <div class="subsection last">
          <h4>插件管理</h4>
          <div v-if="needsReinstall" class="section-warning">
            有未保存的连接参数改动，请先「保存配置」再安装插件（安装使用已保存的值）
          </div>
          <div class="plugin-actions">
            <button @click="install" :disabled="installing || hermesDirStatus?.exists === false" class="install-btn">
              {{ installing ? "安装中..." : "安装插件到 Hermes" }}
              <span v-if="needsReinstall" class="badge-warning">需刷新</span>
            </button>
            <button @click="uninstall" :disabled="uninstalling || hermesDirStatus?.exists === false" class="uninstall-btn">
              {{ uninstalling ? "卸载中..." : "卸载插件" }}
            </button>
          </div>

          <div v-if="installResult" class="install-result">
            <h4>{{ installResult.removed !== undefined ? (installResult.removed ? '卸载结果' : '已卸载') : '安装结果' }}</h4>
            <div v-if="installResult.plugin_dest" class="result-item">
              <strong>插件路径:</strong>
              <code>{{ installResult.plugin_dest }}</code>
            </div>
            <div v-if="installResult.copied && installResult.copied.length" class="result-item">
              <strong>已复制文件:</strong>
              <ul>
                <li v-for="file in installResult.copied" :key="file">{{ file }}</li>
              </ul>
            </div>
            <div v-if="installResult.env_vars && Object.keys(installResult.env_vars).length" class="env-vars">
              <strong>环境变量已写入:</strong>
              <ul>
                <li v-for="(val, key) in installResult.env_vars" :key="key"><code>{{ key }}={{ val }}</code></li>
              </ul>
              <p class="hint">环境变量已自动写入 ~/.hermes/.env，重启 Hermes 网关生效</p>
            </div>
            <div v-if="installResult.removed" class="note">插件目录已删除</div>
            <div v-if="installResult.env_cleaned" class="note">环境变量已清理</div>
            <div v-if="installResult.note" class="note">{{ installResult.note }}</div>
          </div>
        </div>
      </div>

      <button @click="save" :disabled="saving" class="save-btn">
        {{ saving ? "保存中..." : "保存配置" }}
      </button>

      <!-- Hermes 会话隔离 + 群聊排队配置 -->
      <div class="section">
        <h3>Hermes 会话隔离配置</h3>
        <p class="hint">
          此值由 Hermes 决定,适配器只读取并据此判断是否需要群聊排队。<br>
          <strong>true(默认)</strong>:每个群成员独立 session,无需排队。<br>
          <strong>false</strong>:全群共享 session,适配器对群消息排队串行处理(防止不同成员互相打断)。
        </p>
        <div v-if="modeMsg" :class="['message', modeMsgType]">{{ modeMsg }}</div>

        <!-- 当前值显示(来自插件上报 / Hermes config.yaml)-->
        <div class="mode-display">
          <span class="mode-label">当前值:</span>
          <span :class="['mode-value', hermesMode?.group_sessions_per_user ? 'isolation-on' : 'isolation-off']">
            {{ hermesMode?.group_sessions_per_user ? 'true(隔离)' : 'false(共享)' }}
          </span>
          <span class="mode-source" v-if="hermesMode">
            来源:
            <code v-if="hermesMode.source === 'plugin_report'">插件上报</code>
            <code v-else-if="hermesMode.source === 'hermes_config_yaml'">Hermes config.yaml(插件未连接)</code>
            <code v-else>默认值(插件未连接)</code>
          </span>
          <button @click="refreshHermesModeReport" :disabled="refreshingMode" class="mode-refresh-btn">
            {{ refreshingMode ? "刷新中..." : "↻ 刷新上报值" }}
          </button>
        </div>

        <!-- 修改表单 -->
        <div class="mode-edit" v-if="!editingPerUser">
          <button @click="editingPerUser = true" class="mode-edit-btn">✏ 修改 Hermes 配置</button>
        </div>
        <div class="mode-edit" v-else>
          <label class="checkbox-row">
            <input
              type="checkbox"
              :checked="!hermesMode?.group_sessions_per_user"
              @change="(e) => { editingPerUser = false; saveHermesMode(!(e.target as HTMLInputElement).checked) }"
            />
            <span>开启群聊排队(写入 group_sessions_per_user: false 到 Hermes config.yaml)</span>
          </label>
          <button @click="editingPerUser = false" class="mode-cancel-btn">取消</button>
        </div>
        <p class="hint" v-if="editingPerUser">
          ⚠️ 修改后会写入 Hermes <code>config.yaml</code>,需<strong>重启 Hermes 网关</strong>才生效。
          重启后插件会重新上报新值,届时点击"刷新上报值"可看到更新。
        </p>
      </div>

      <div class="section">
        <h3>群聊消息排队</h3>
        <p class="hint">
          当 Hermes 不隔离群成员(group_sessions_per_user=false)时,适配器对群消息排队串行处理。
          同一发送者的消息直接放行(可补充当前任务),不同发送者排队等待,/命令绕过排队。
          若插件崩溃或 idle 信号丢失,看门狗在超时后强制清空 busy 状态。
        </p>
        <label class="checkbox-row">
          <input type="checkbox" v-model="cfg.event_queue_enabled" />
          <span>启用群聊排队</span>
        </label>
        <label>
          单群队列上限
          <input type="number" v-model.number="cfg.event_queue_max_per_chat" min="1" max="500" />
          <span class="hint">每个群聊的排队消息上限(默认 50),超限丢弃最旧的一条。防止刷屏爆内存。</span>
        </label>
        <label>
          busy 超时(秒)
          <input type="number" v-model.number="cfg.event_queue_idle_timeout" min="10" step="10" />
          <span class="hint">plugin 未发 idle 信号的超时阈值(默认 300 秒),超时后强制清空 busy 状态并派发下一条。设太小会误杀长任务,太大会延迟恢复。</span>
        </label>
      </div>

      <button @click="save" :disabled="saving" class="save-btn">
        {{ saving ? "保存中..." : "保存配置" }}
      </button>
    </div>
  </div>
</template>

<style scoped>
h2 {
  margin: 0 0 1rem 0;
}

.section {
  background: var(--card-bg);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 1.5rem;
  margin-bottom: 1.5rem;
}

.section h3 {
  margin: 0 0 0.25rem 0;
  font-size: 1.1rem;
  color: var(--text);
}

.section-desc {
  margin: 0 0 1.5rem 0;
  font-size: 0.9rem;
  color: var(--text-muted);
}

.subsection {
  border-bottom: 1px solid var(--border);
  padding: 1rem 0;
}

.subsection.last {
  border-bottom: none;
  padding-bottom: 0;
}

.subsection h4 {
  margin: 0 0 0.75rem 0;
  font-size: 0.95rem;
  color: #555;
}

.radio-group {
  display: flex;
  gap: 1rem;
}

.radio-label {
  flex: 1;
  display: flex;
  align-items: flex-start;
  gap: 0.75rem;
  padding: 0.75rem;
  border: 1px solid var(--border);
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.2s;
}

.radio-label:hover {
  border-color: var(--primary);
  background: var(--bg);
}

.radio-label.selected {
  border-color: var(--primary);
  background: rgba(74, 144, 226, 0.04);
}

.radio-label input[type="radio"] {
  margin-top: 0.25rem;
}

.radio-label div {
  flex: 1;
}

.radio-label strong {
  display: block;
  margin-bottom: 0.25rem;
}

label {
  display: block;
  margin-bottom: 0.75rem;
  font-weight: 500;
  color: var(--text);
}

input[type="text"],
input[type="password"],
input[type="number"] {
  width: 100%;
  padding: 0.5rem;
  border: 1px solid #ccc;
  border-radius: 4px;
  font-size: 0.9rem;
  margin-top: 0.25rem;
}

input:focus {
  outline: none;
  border-color: var(--primary);
  box-shadow: 0 0 0 2px rgba(74, 144, 226, 0.1);
}

.hint {
  display: block;
  font-size: 0.85rem;
  color: var(--text-muted);
  font-weight: normal;
  margin-top: 0.25rem;
}

.form-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1rem;
}

.form-row label {
  margin-bottom: 0;
}

.dir-input-wrapper {
  display: flex;
  gap: 0.5rem;
  align-items: center;
}

.dir-input {
  flex: 1;
}

.dir-status {
  font-size: 0.85rem;
  font-weight: normal;
  white-space: nowrap;
}

.dir-status.exists { color: var(--success); }
.dir-status.not-exists { color: var(--danger); }

.dir-check-btn {
  background: #f0f0f0;
  border: 1px solid #ccc;
  border-radius: 4px;
  padding: 0.5rem 0.75rem;
  cursor: pointer;
  font-size: 0.85rem;
  white-space: nowrap;
  transition: all 0.2s;
}

.dir-check-btn:hover:not(:disabled) {
  background: var(--border);
  border-color: #999;
}

.dir-check-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.token-input-wrapper {
  display: flex;
  gap: 0.5rem;
  align-items: center;
}

.token-input {
  flex: 1;
  font-family: monospace;
}

.url-container {
  display: flex;
  gap: 0.5rem;
  align-items: center;
}

.url-display {
  flex: 1;
  background: var(--bg);
  padding: 0.5rem;
  border-radius: 4px;
  font-size: 0.9rem;
  word-break: break-all;
}

.icon-btn {
  background: #f0f0f0;
  border: 1px solid #ccc;
  border-radius: 4px;
  padding: 0.5rem 0.75rem;
  cursor: pointer;
  font-size: 1rem;
  transition: all 0.2s;
}

.icon-btn:hover {
  background: var(--border);
  border-color: #999;
}

.plugin-actions {
  display: flex;
  gap: 1rem;
  margin-top: 0.75rem;
  flex-wrap: wrap;
}

.install-btn {
  background: var(--success);
  color: white;
  border: none;
  padding: 0.6rem 1.2rem;
  border-radius: 6px;
  font-size: 0.95rem;
  cursor: pointer;
  transition: background 0.2s;
}

.install-btn:hover:not(:disabled) { background: #218838; }
.install-btn:disabled { background: #ccc; cursor: not-allowed; }

.uninstall-btn {
  background: var(--danger);
  color: white;
  border: none;
  padding: 0.6rem 1.2rem;
  border-radius: 6px;
  font-size: 0.95rem;
  cursor: pointer;
  transition: background 0.2s;
}

.uninstall-btn:hover:not(:disabled) { background: #c82333; }
.uninstall-btn:disabled { background: #ccc; cursor: not-allowed; }

.badge-warning {
  display: inline-block;
  margin-left: 0.4rem;
  background: var(--warning);
  color: #856404;
  font-size: 0.75rem;
  padding: 0.1rem 0.4rem;
  border-radius: 8px;
  vertical-align: middle;
}

.install-result {
  margin-top: 1rem;
  padding: 1rem;
  background: var(--bg);
  border-radius: 6px;
  border-left: 4px solid var(--success);
}

.install-result h4 {
  margin: 0 0 0.75rem 0;
  font-size: 1rem;
  color: var(--text);
}

.install-result code {
  display: block;
  background: var(--card-bg);
  padding: 0.5rem;
  border-radius: 4px;
  margin-top: 0.25rem;
  font-size: 0.85rem;
  word-break: break-all;
}

.install-result ul {
  margin: 0.25rem 0 0 0;
  padding-left: 1.5rem;
}

.install-result li {
  margin: 0.25rem 0;
}

.result-item {
  margin-bottom: 0.75rem;
}

.env-vars {
  margin-top: 0.75rem;
  padding: 0.75rem;
  background: #d4edda;
  border-left: 4px solid var(--success);
  border-radius: 4px;
}

.env-vars ul {
  margin: 0.25rem 0;
  padding-left: 1.5rem;
}

.env-vars code {
  background: var(--card-bg);
  padding: 0.2rem 0.4rem;
  border-radius: 3px;
  font-size: 0.85rem;
  word-break: break-all;
}

.note {
  margin-top: 0.75rem;
  padding: 0.75rem;
  background: #fff3cd;
  border-left: 3px solid var(--warning);
  border-radius: 4px;
  font-size: 0.9rem;
  color: #856404;
}

.save-btn {
  background: var(--primary);
  color: white;
  border: none;
  padding: 0.75rem 2rem;
  border-radius: 6px;
  font-size: 1rem;
  cursor: pointer;
  transition: background 0.2s;
}

.save-btn:hover:not(:disabled) { background: var(--primary-dark); }
.save-btn:disabled { background: #ccc; cursor: not-allowed; }

.message {
  padding: 0.75rem 1rem;
  border-radius: 6px;
  margin-bottom: 1rem;
  font-weight: 500;
}

.message.success {
  background: #d4edda;
  color: #155724;
  border-left: 4px solid var(--success);
}

.message.error {
  background: #f8d7da;
  color: #721c24;
  border-left: 4px solid var(--danger);
}

.message.warning {
  background: #fff3cd;
  color: #856404;
  border-left: 4px solid var(--warning);
}

.loading { text-align: center; padding: 2rem; color: var(--text-muted); }

.reinstall-banner {
  background: #fff3cd;
  color: #856404;
  border: 1px solid var(--warning);
  border-radius: 6px;
  padding: 0.75rem 1rem;
  margin-bottom: 1rem;
  font-weight: 500;
  font-size: 0.9rem;
}

.section-warning {
  background: #fff3cd;
  color: #856404;
  border-left: 3px solid var(--warning);
  border-radius: 4px;
  padding: 0.5rem 0.75rem;
  margin: 0.5rem 0;
  font-size: 0.85rem;
}

@media (max-width: 768px) {
  .radio-group { flex-direction: column; }
  .form-row { grid-template-columns: 1fr; }
  .plugin-actions { flex-direction: column; }
}

/* ── Hermes 会话隔离 + 排队 ── */
.mode-display {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  flex-wrap: wrap;
  padding: 0.75rem 0;
  font-size: 0.9rem;
}
.mode-label { font-weight: 500; }
.mode-value { font-weight: 700; padding: 0.2rem 0.6rem; border-radius: 12px; font-size: 0.85rem; }
.mode-value.isolation-on { background: rgba(40, 167, 69, 0.15); color: var(--success); }
.mode-value.isolation-off { background: rgba(255, 193, 7, 0.15); color: #856404; }
.mode-source { color: var(--text-muted); font-size: 0.8rem; }
.mode-source code { background: var(--bg); padding: 0.1rem 0.3rem; border-radius: 3px; font-size: 0.85em; }
.mode-refresh-btn {
  background: var(--card-bg); color: var(--primary); border: 1px solid var(--primary);
  padding: 0.3rem 0.8rem; border-radius: 6px; cursor: pointer; font-size: 0.85rem;
}
.mode-refresh-btn:hover { background: rgba(74, 144, 226, 0.08); }
.mode-refresh-btn:disabled { opacity: 0.6; cursor: not-allowed; }
.mode-edit { padding: 0.5rem 0; }
.mode-edit-btn {
  background: var(--primary); color: white; border: none;
  padding: 0.4rem 1rem; border-radius: 6px; cursor: pointer; font-size: 0.85rem;
}
.mode-cancel-btn {
  background: var(--card-bg); color: var(--text-muted); border: 1px solid var(--border);
  padding: 0.4rem 1rem; border-radius: 6px; cursor: pointer; font-size: 0.85rem;
  margin-left: 0.5rem;
}
.checkbox-row { display: flex; align-items: center; gap: 0.5rem; cursor: pointer; margin: 0.5rem 0; }
.checkbox-row input[type="checkbox"] { width: auto; cursor: pointer; }
.checkbox-row span { font-weight: 500; }
.message { padding: 0.75rem 1rem; border-radius: 6px; margin: 0.75rem 0; font-size: 0.9rem; }
.message.success { background: #d4edda; color: #155724; border-left: 4px solid var(--success); }
.message.error { background: #f8d7da; color: #721c24; border-left: 4px solid var(--danger); }
.message.warning { background: #fff9e6; color: #856404; border-left: 4px solid var(--warning); }
</style>
