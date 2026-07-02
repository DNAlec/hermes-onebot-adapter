<script setup lang="ts">
import { ref, computed, onMounted } from "vue";
import { checkHermesDir, installPlugin, uninstallPlugin, type HermesDirStatus } from "../api";
import { useConfig } from "../composables/useConfig";

const { cfg, load, save: saveConfig } = useConfig();

const saving = ref(false);
const installing = ref(false);
const uninstalling = ref(false);
const msg = ref("");
const msgType = ref<"success" | "error" | "warning">("success");
const installResult = ref<any>(null);
const showToken = ref(false);
const hermesDirStatus = ref<HermesDirStatus | null>(null);
const checkingDir = ref(false);

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
  } catch (e: any) {
    msg.value = "加载配置失败: " + (e.response?.data?.error || e.message);
    msgType.value = "error";
  }
});

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
    msg.value = "Token 已复制到剪贴板";
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
            <input v-model="cfg.onebot_ws_token" type="password" placeholder="自动生成" />
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
          <h4>认证令牌</h4>
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
          <span class="hint">Hermes 插件连接时的认证令牌；修改后需保存并重新安装插件以同步到 Hermes 端</span>
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
</style>
