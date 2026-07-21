<script setup lang="ts">
import { ref, onMounted } from "vue";
import { useConfig } from "../composables/useConfig";
import { clearUsageStats, login } from "../api";

const { cfg, load, save: saveConfig } = useConfig();
const saving = ref(false);
const msg = ref("");
const msgType = ref<"success" | "error">("success");

// ── Token 修改 ──
const oldToken = ref("");
const newToken = ref("");
const confirmToken = ref("");
const tokenMsg = ref("");
const tokenMsgType = ref<"success" | "error">("success");
const changingToken = ref(false);
const clearingUsage = ref(false);

onMounted(async () => {
  try {
    await load();
  } catch (e: any) {
    msg.value = "❌ 加载配置失败: " + (e.response?.data?.error || e.message);
    msgType.value = "error";
  }
});

async function save() {
  if (!cfg.value) return;
  saving.value = true;
  msg.value = "";
  const c = cfg.value;
    try {
    await saveConfig({
      log_level: c.log_level,
      log_message_preview: c.log_message_preview,
      log_file_enabled: c.log_file_enabled,
      log_file_dir: c.log_file_dir,
      log_retention_days: c.log_retention_days,
      usage_stats_enabled: c.usage_stats_enabled,
      usage_stats_retention_days: c.usage_stats_retention_days,
      webui_port: c.webui_port,
      webui_token_lifetime_hours: c.webui_token_lifetime_hours,
      send_dedup_enabled: c.send_dedup_enabled,
      send_dedup_ttl_seconds: c.send_dedup_ttl_seconds,
      seq_map_size: c.seq_map_size,
    });
    msg.value = "✅ 配置已保存";
    msgType.value = "success";
  } catch (e: any) {
    msg.value = "❌ " + (e.response?.data?.error || e.message);
    msgType.value = "error";
  } finally { saving.value = false; }
}

async function clearUsage() {
  const confirmation = window.prompt("此操作不可恢复。请输入“清空”以删除全部用量统计数据：");
  if (confirmation !== "清空") return;
  clearingUsage.value = true;
  msg.value = "";
  try {
    const result = await clearUsageStats();
    msg.value = `✅ 已清空 ${result.deleted} 条统计记录`;
    msgType.value = "success";
  } catch (e: any) {
    msg.value = "❌ " + (e.response?.data?.error || e.message);
    msgType.value = "error";
  } finally {
    clearingUsage.value = false;
  }
}

async function changeToken() {
  tokenMsg.value = "";
  if (!oldToken.value || !newToken.value || !confirmToken.value) {
    tokenMsg.value = "❌ 请填写所有三个字段";
    tokenMsgType.value = "error";
    return;
  }
  if (newToken.value !== confirmToken.value) {
    tokenMsg.value = "❌ 两次输入的新 Token 不一致";
    tokenMsgType.value = "error";
    return;
  }
  if (newToken.value.length < 8) {
    tokenMsg.value = "❌ 新 Token 长度至少 8 个字符";
    tokenMsgType.value = "error";
    return;
  }
  changingToken.value = true;
  try {
    // Verify the old token server-side via /api/login. The webui_token is never
    // exposed over the API, so verification can only happen on the backend.
    // We do NOT persist the returned session token here — the current session
    // keeps its existing token until we re-login with the new one below.
    const verifyResp = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: oldToken.value }),
    });
    if (!verifyResp.ok) {
      if (verifyResp.status === 429) {
        let body: any = null;
        try { body = await verifyResp.json(); } catch {}
        const retry = body?.retry_after;
        tokenMsg.value = `❌ 尝试过于频繁${retry ? `,请 ${retry} 秒后再试` : ",请稍后再试"}`;
      } else {
        tokenMsg.value = "❌ 旧 Token 不正确";
      }
      tokenMsgType.value = "error";
      return;
    }
    await saveConfig({ webui_token: newToken.value });
    // Re-login silently with the new raw token so the current session keeps
    // working (other sessions are invalidated because the signed token no
    // longer matches the new webui_token secret).
    await login(newToken.value);
    oldToken.value = "";
    newToken.value = "";
    confirmToken.value = "";
    tokenMsg.value = "✅ Token 已修改,当前会话已自动续期,其他已登录会话将失效";
    tokenMsgType.value = "success";
  } catch (e: any) {
    tokenMsg.value = "❌ " + (e.response?.data?.error || e.message);
    tokenMsgType.value = "error";
  } finally {
    changingToken.value = false;
  }
}

</script>

<template>
  <div>
    <h2>高级设置</h2>
    <div v-if="msg" :class="['message', msgType]">{{ msg }}</div>

    <div v-if="!cfg" class="loading">加载配置中...</div>

    <div v-if="cfg">

    <div class="section">
      <h3>WebUI 鉴权 Token</h3>
      <p class="hint">修改 WebUI 登录 Token。修改后当前会话自动使用新 Token,其他已登录会话将失效。</p>
      <div v-if="tokenMsg" :class="['message', tokenMsgType]">{{ tokenMsg }}</div>
      <label>
        旧 Token
        <input v-model="oldToken" type="password" placeholder="输入当前 Token" autocomplete="current-password" />
      </label>
      <label>
        新 Token
        <input v-model="newToken" type="password" placeholder="输入新 Token(至少 8 个字符)" autocomplete="new-password" />
      </label>
      <label>
        确认新 Token
        <input v-model="confirmToken" type="password" placeholder="再次输入新 Token" autocomplete="new-password" />
      </label>
      <button @click="changeToken" :disabled="changingToken" class="btn-token-change">
        {{ changingToken ? "修改中..." : "修改 Token" }}
      </button>

      <h3 class="subsection">登录有效期</h3>
      <p class="hint">WebUI 登录会话的有效时长(小时),最小 1,默认 168(7 天)。</p>
      <p class="hint">⚠️ 修改后所有已登录会话(包括当前)立即失效,需要重新登录。</p>
      <label>
        有效期(小时)
        <input type="number" v-model.number="cfg.webui_token_lifetime_hours" min="1" max="87600" />
        <span class="hint">最小 1 小时;修改后点击下方"保存配置"生效</span>
      </label>
    </div>

    <div class="section">
      <h3>其他</h3>
      <label>
        日志正文预览长度
        <input type="number" v-model.number="cfg.log_message_preview" min="0" max="500" />
        <span class="hint">日志体中消息正文截断字符数（0=不截断，默认100）</span>
      </label>
      <label class="checkbox-row">
        <input type="checkbox" v-model="cfg.log_file_enabled" />
        <span>启用文件日志</span>
      </label>
      <label>
        日志文件目录
        <input v-model="cfg.log_file_dir" placeholder="留空则使用 ~/.onebot_adapter/logs/" />
        <span class="hint">文件日志持久化目录，留空使用默认路径</span>
      </label>
      <label>
        日志保留天数
        <input type="number" v-model.number="cfg.log_retention_days" min="1" max="365" />
        <span class="hint">日志文件按天轮转，超过此天数的自动删除（默认3天）</span>
      </label>
      <label>
        日志级别
        <select v-model="cfg.log_level">
          <option value="DEBUG">DEBUG</option>
          <option value="INFO">INFO</option>
          <option value="WARNING">WARNING</option>
          <option value="ERROR">ERROR</option>
        </select>
      </label>
      <label>
        WebUI 端口
        <input type="number" v-model.number="cfg.webui_port" min="1" max="65535" />
        <span class="hint">修改后需重启适配器服务才生效</span>
      </label>
      <label>
        消息序号映射容量
        <input type="number" v-model.number="cfg.seq_map_size" min="100" max="5000" />
        <span class="hint">real_seq→message_id 全局 FIFO 上限（默认 4500，与 NapCat 5000 对齐）</span>
      </label>
    </div>

    <div class="section">
      <h3>用量统计</h3>
      <p class="hint">记录成功通过消息过滤的元数据，不保存消息正文或媒体地址。关闭后停止新增，已有历史仍可查询。</p>
      <label class="checkbox-row">
        <input type="checkbox" v-model="cfg.usage_stats_enabled" />
        <span>启用用量统计</span>
      </label>
      <label>
        数据保留天数
        <input type="number" v-model.number="cfg.usage_stats_retention_days" min="1" step="1" />
        <span class="hint">默认 365 天；缩短后保存配置会立即清理过期数据。</span>
      </label>
      <button @click="clearUsage" :disabled="clearingUsage" class="danger-btn">
        {{ clearingUsage ? "清空中..." : "清空全部统计数据" }}
      </button>
    </div>

    <div class="section">
      <h3>发送去重</h3>
      <p class="hint">Gateway 的 send_text 超时重试会导致同一条消息被多次发送到 QQ。启用后,适配器在 TTL 内对相同内容(chat_id+action+内容指纹+reply_to)的重复发送直接返回缓存结果,不再实际下发。</p>
      <label class="checkbox-row">
        <input type="checkbox" v-model="cfg.send_dedup_enabled" />
        <span>启用发送去重</span>
      </label>
      <label>
        去重 TTL(秒)
        <input type="number" v-model.number="cfg.send_dedup_ttl_seconds" min="1" step="1" />
        <span class="hint">相同内容在此时间内被重复发送时直接返回缓存结果(默认 10 秒)。建议覆盖 Gateway _send_with_retry 的完整重试窗口(30s 超时 + 2.7s 退避 + 30s 超时 + 4.6s 退避 ≈ 67s),否则间隔较长的重试可能漏掉去重导致重复发送。值越小误去重风险越低,但可能漏掉间隔较长的重试。</span>
      </label>
    </div>

    <button @click="save" :disabled="saving" class="save-btn">
      {{ saving ? "保存中..." : "保存配置" }}
    </button>
    </div>
  </div>
</template>

<style scoped>
.section { background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; padding: 1.25rem; margin-bottom: 1.5rem; }
.section h3 { margin: 0 0 1rem; font-size: 1rem; border-bottom: 2px solid var(--primary); padding-bottom: 0.5rem; }
.subsection { margin-top: 1.5rem; }
.hint { display: block; font-size: 0.85rem; color: var(--text-muted); margin: 0.25rem 0 0.75rem; }
.danger-btn { background: var(--danger); color: white; border: 0; border-radius: 5px; padding: 0.6rem 1rem; cursor: pointer; }
.danger-btn:disabled { opacity: 0.6; cursor: not-allowed; }
label { display: block; margin-bottom: 1rem; font-weight: 500; font-size: 0.9rem; }
input, select { width: 100%; padding: 0.5rem; border: 1px solid #ccc; border-radius: 4px; font-size: 0.9rem; margin-top: 0.25rem; }
.checkbox-row { display: flex; align-items: center; gap: 0.5rem; }
.checkbox-row span { font-weight: 500; }
.checkbox-row input[type="checkbox"] { width: auto; margin-top: 0; }

.btn-token-change { background: var(--primary); color: white; border: none; padding: 0.6rem 1.5rem; border-radius: 6px; cursor: pointer; font-size: 0.95rem; }
.btn-token-change:disabled { background: #ccc; cursor: not-allowed; }
.message { padding: 0.75rem 1rem; border-radius: 6px; margin-bottom: 1rem; }
.message.success { background: #d4edda; color: #155724; border-left: 4px solid var(--success); }
.message.error { background: #f8d7da; color: #721c24; border-left: 4px solid var(--danger); }
.loading { text-align: center; padding: 2rem; color: var(--text-muted, #666); }
</style>
