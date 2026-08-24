<script setup lang="ts">
import { ref, onMounted } from "vue";
import {
  getGroups, putGroup, deleteGroup, syncGroups,
  type GroupConfig,
  getHermesMode, putHermesMode, refreshHermesMode, type HermesMode,
  getBotBlacklist, deleteBotBlacklistEntry, type BotBlacklistEntry,
  getRateLimitQuota, resetRateLimitQuota, type RateLimitQuota,
} from "../api";
import { useConfig } from "../composables/useConfig";

const { cfg, load, save: saveConfig } = useConfig();
const groups = ref<GroupConfig[]>([]);
const saving = ref(false);
const syncing = ref(false);
const msg = ref("");
const msgType = ref<"success" | "error">("success");
const editingGroup = ref<GroupConfig | null>(null);
const showEditor = ref(false);
const editingExistingGroup = ref(false);
const blacklistEntries = ref<BotBlacklistEntry[]>([]);
const blacklistLoading = ref(false);
const blacklistMaxHours = ref(24);
const quotaScope = ref<"global" | "group" | "user">("global");
const quotaTargetId = ref("");
const quota = ref<RateLimitQuota | null>(null);
const quotaLoading = ref(false);
const quotaMessage = ref("");

const hermesMode = ref<HermesMode | null>(null);
const editingPerUser = ref(false);
const savingMode = ref(false);
const refreshingMode = ref(false);
const modeMsg = ref("");
const modeMsgType = ref<"success" | "error" | "warning">("success");

function ensureOutboundFilterFields() {
  if (!cfg.value) return;
  const listFields = [
    "outbound_filter_patterns",
    "group_trigger_keywords",
    "global_admins",
    "dm_user_list",
  ] as const;
  for (const key of listFields) {
    if (!Array.isArray(cfg.value[key])) {
      cfg.value[key] = [];
    }
  }
  if (typeof cfg.value.outbound_filter_enabled !== "boolean") {
    cfg.value.outbound_filter_enabled = false;
  }
}

onMounted(async () => {
  try {
    await load();
    ensureOutboundFilterFields();
    groups.value = await getGroups();
    blacklistMaxHours.value = (cfg.value?.bot_blacklist_max_duration_seconds || 86400) / 3600;
    await fetchQuota();
    await fetchBotBlacklist();
    fetchHermesMode();
  } catch (e: any) {
    msg.value = "加载失败: " + (e.response?.data?.error || e.message);
    msgType.value = "error";
  }
});

async function fetchHermesMode() {
  try {
    hermesMode.value = await getHermesMode();
  } catch (e: any) {
    modeMsg.value = "读取 Hermes 配置失败: " + (e.response?.data?.error || e.message);
    modeMsgType.value = "error";
  }
}

async function fetchBotBlacklist() {
  blacklistLoading.value = true;
  try {
    blacklistEntries.value = await getBotBlacklist();
  } catch (e: any) {
    msg.value = "动态黑名单加载失败: " + (e.response?.data?.error || e.message);
    msgType.value = "error";
  } finally {
    blacklistLoading.value = false;
  }
}

async function removeBlacklistEntry(entry: BotBlacklistEntry) {
  if (!confirm(`确认解除用户 ${entry.user_id} 的这条动态拉黑记录？`)) return;
  try {
    await deleteBotBlacklistEntry(entry.id);
    await fetchBotBlacklist();
    msg.value = "动态黑名单记录已解除";
    msgType.value = "success";
  } catch (e: any) {
    msg.value = e.response?.data?.error || e.message;
    msgType.value = "error";
  }
}

function formatTimestamp(value: number) {
  return new Date(value * 1000).toLocaleString();
}

function scopeLabel(entry: BotBlacklistEntry) {
  if (entry.scope === "group") return `群聊 ${entry.group_id}`;
  if (entry.scope === "dm") return "私聊";
  return "全部会话";
}

async function saveHermesMode(value: boolean) {
  savingMode.value = true;
  modeMsg.value = "";
  try {
    const res = await putHermesMode(value);
    modeMsg.value = res.note;
    modeMsgType.value = "warning";
    editingPerUser.value = false;
    await fetchHermesMode();
  } catch (e: any) {
    modeMsg.value = (e.response?.data?.error || e.message);
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
      modeMsg.value = res.note || "已请求插件重新上报";
      modeMsgType.value = "success";
      setTimeout(() => fetchHermesMode(), 800);
    } else {
      modeMsg.value = res.error || "刷新失败";
      modeMsgType.value = "warning";
    }
  } catch (e: any) {
    modeMsg.value = (e.response?.data?.error || e.message);
    modeMsgType.value = "error";
  } finally {
    refreshingMode.value = false;
  }
}

async function saveGlobal() {
  if (!cfg.value) return;
  saving.value = true;
  msg.value = "";
  const c = cfg.value;
    try {
    await saveConfig({
      group_require_mention: c.group_require_mention,
      group_mention_first_only: c.group_mention_first_only,
      group_trigger_keywords: c.group_trigger_keywords,
      group_keyword_first_only: c.group_keyword_first_only,
      group_strip_first_mention: c.group_strip_first_mention,
      global_admins: c.global_admins,
      dm_user_filter_mode: c.dm_user_filter_mode,
      dm_user_list: c.dm_user_list,
      message_show_group_id: c.message_show_group_id,
      reaction_emoji_enabled: c.reaction_emoji_enabled,
      reaction_emoji_id: c.reaction_emoji_id,
      reaction_emoji_id_queued: c.reaction_emoji_id_queued,
      event_queue_enabled: c.event_queue_enabled,
      event_queue_max_per_chat: c.event_queue_max_per_chat,
      event_queue_idle_timeout: c.event_queue_idle_timeout,
      event_queue_clear_on_session_reset: c.event_queue_clear_on_session_reset,
      event_queue_clean_command_enabled: c.event_queue_clean_command_enabled,
      rate_limit_enabled: c.rate_limit_enabled,
      global_rate_limit_algorithm: c.global_rate_limit_algorithm,
      global_rate_limit_messages: c.global_rate_limit_messages,
      global_rate_limit_window_seconds: c.global_rate_limit_window_seconds,
      group_rate_limit_algorithm: c.group_rate_limit_algorithm,
      group_rate_limit_messages: c.group_rate_limit_messages,
      group_rate_limit_window_seconds: c.group_rate_limit_window_seconds,
      user_rate_limit_algorithm: c.user_rate_limit_algorithm,
      user_rate_limit_messages: c.user_rate_limit_messages,
      user_rate_limit_window_seconds: c.user_rate_limit_window_seconds,
      rate_limit_reject_message: c.rate_limit_reject_message,
      rate_limit_storage_failure_mode: c.rate_limit_storage_failure_mode,
      media_delivery_mode: c.media_delivery_mode,
      global_channel_prompt: c.global_channel_prompt,
      notify_poke_enabled: c.notify_poke_enabled,
      notify_member_change_enabled: c.notify_member_change_enabled,
      outbound_filter_enabled: c.outbound_filter_enabled,
      outbound_filter_patterns: c.outbound_filter_patterns,
      bot_blacklist_enabled: c.bot_blacklist_enabled,
      bot_blacklist_max_duration_seconds: Math.max(1, Math.round(blacklistMaxHours.value * 3600)),
      bot_blacklist_reject_message: c.bot_blacklist_reject_message,
    });
    msg.value = "全局设置已保存";
    await fetchBotBlacklist();
    msgType.value = "success";
  } catch (e: any) {
    msg.value = (e.response?.data?.error || e.message);
    msgType.value = "error";
  } finally { saving.value = false; }
}

async function syncFromOneBot() {
  syncing.value = true;
  msg.value = "";
  try {
    const result = await syncGroups();
    msg.value = "同步完成: 新增 " + result.added.length + " 个群, 总计 " + result.total + " 个";
    msgType.value = "success";
    await load(true);
    ensureOutboundFilterFields();
    groups.value = await getGroups();
  } catch (e: any) {
    msg.value = "同步失败: " + (e.response?.data?.error || e.message);
    msgType.value = "error";
  } finally { syncing.value = false; }
}

function addGroup() {
  editingExistingGroup.value = false;
  editingGroup.value = {
    group_id: "", name: "", enabled: true, require_mention: null,
    mention_first_only: null, trigger_keywords: null, keyword_first_only: null, strip_first_mention: null,
    custom_prompt: "", admins: [],
    group_user_filter_mode: "blacklist", group_user_list: [],
    message_show_group_id: null,
    reaction_emoji_enabled: null,
    command_filter_enabled: null, command_filter_unknown: null, command_permissions: null,
    outbound_filter_enabled: null, outbound_filter_patterns: null,
    notify_poke_enabled: null, notify_member_change_enabled: null,
    group_rate_limit_algorithm: null,
    group_rate_limit_messages: null,
    group_rate_limit_window_seconds: null,
  };
  showEditor.value = true;
}

function editGroup(g: GroupConfig) {
  editingExistingGroup.value = true;
  editingGroup.value = {
    ...g,
    require_mention: g.require_mention ?? null,
    mention_first_only: g.mention_first_only ?? null,
    trigger_keywords: g.trigger_keywords ?? null,
    keyword_first_only: g.keyword_first_only ?? null,
    strip_first_mention: g.strip_first_mention ?? null,
    message_show_group_id: g.message_show_group_id ?? null,
    reaction_emoji_enabled: g.reaction_emoji_enabled ?? null,
    command_filter_enabled: g.command_filter_enabled ?? null,
    command_filter_unknown: g.command_filter_unknown ?? null,
    command_permissions: g.command_permissions ?? null,
    outbound_filter_enabled: g.outbound_filter_enabled ?? null,
    outbound_filter_patterns: g.outbound_filter_patterns ?? null,
    notify_poke_enabled: g.notify_poke_enabled ?? null,
    notify_member_change_enabled: g.notify_member_change_enabled ?? null,
    group_rate_limit_algorithm: g.group_rate_limit_algorithm ?? null,
    group_rate_limit_messages: g.group_rate_limit_messages ?? null,
    group_rate_limit_window_seconds: g.group_rate_limit_window_seconds ?? null,
  };
  showEditor.value = true;
}

function setTriggerKeywordsMode(mode: string) {
  if (!editingGroup.value) return;
  if (mode === "global") {
    editingGroup.value.trigger_keywords = null;
    return;
  }
  if (editingGroup.value.trigger_keywords == null) {
    editingGroup.value.trigger_keywords = [];
  }
}

async function saveGroup() {
  if (!editingGroup.value) return;
  const g = editingGroup.value;
  if (!g.group_id) {
    msg.value = "群号不能为空";
    msgType.value = "error";
    return;
  }
  try {
    await putGroup(g.group_id, g);
    msg.value = "群配置已保存";
    msgType.value = "success";
    showEditor.value = false;
    groups.value = await getGroups();
  } catch (e: any) {
    msg.value = (e.response?.data?.error || e.message);
    msgType.value = "error";
  }
}

async function removeGroup(gid: string) {
  if (!confirm("确认删除群 " + gid + " 的配置？")) return;
  try {
    await deleteGroup(gid);
    groups.value = await getGroups();
    msg.value = "群配置已删除";
    msgType.value = "success";
  } catch (e: any) {
    msg.value = (e.response?.data?.error || e.message);
    msgType.value = "error";
  }
}

const globalAdminInput = ref("");
const dmUserInput = ref("");
const groupAdminInput = ref("");
const groupUserInput = ref("");
const triggerKeywordsInput = ref("");
const groupKeywordsInput = ref("");
const outboundFilterInput = ref("");
const groupOutboundFilterInput = ref("");
function addTag(list: string[] | null | undefined, value: string) {
  if (!list) return;
  const v = value.trim();
  if (v && !list.includes(v)) list.push(v);
}
function removeTag(list: string[] | null | undefined, idx: number) {
  list?.splice(idx, 1);
}

function toggleGroupOutboundFilterPatterns(enabled: boolean) {
  if (!editingGroup.value) return;
  if (!enabled) {
    editingGroup.value.outbound_filter_patterns = null;
    return;
  }
  editingGroup.value.outbound_filter_patterns = [...(cfg.value?.outbound_filter_patterns || [])];
}

function toggleGroupRateLimitOverride(enabled: boolean) {
  if (!editingGroup.value) return;
  if (!enabled) {
    editingGroup.value.group_rate_limit_algorithm = null;
    editingGroup.value.group_rate_limit_messages = null;
    editingGroup.value.group_rate_limit_window_seconds = null;
    return;
  }
  editingGroup.value.group_rate_limit_algorithm = cfg.value?.group_rate_limit_algorithm || "sliding_window";
  editingGroup.value.group_rate_limit_messages = cfg.value?.group_rate_limit_messages || 0;
  editingGroup.value.group_rate_limit_window_seconds = cfg.value?.group_rate_limit_window_seconds || 0;
}

async function fetchQuota() {
  const target = quotaScope.value === "global" ? undefined : quotaTargetId.value.trim();
  if (quotaScope.value !== "global" && !/^\d+$/.test(target || "")) {
    quotaMessage.value = "请输入有效的数字 ID";
    return;
  }
  quotaLoading.value = true;
  quotaMessage.value = "";
  try {
    quota.value = await getRateLimitQuota(quotaScope.value, target);
  } catch (e: any) {
    quotaMessage.value = e.response?.data?.error || e.message;
  } finally {
    quotaLoading.value = false;
  }
}

async function resetQuota() {
  if (!quota.value) return;
  const scope = quota.value.scope;
  const target = quota.value.target_id || undefined;
  const label = scope === "global" ? "全局额度（将影响所有用户）" :
    `${scope === "group" ? "群聊" : "用户"} ${target}`;
  if (!confirm(`确认重置${label}？其他维度的额度不会被清除。`)) return;
  quotaLoading.value = true;
  quotaMessage.value = "";
  try {
    quota.value = await resetRateLimitQuota(scope, target);
    quotaMessage.value = quota.value.pending_persistence
      ? "已在内存中重置，数据库恢复后将同步"
      : "额度已重置";
  } catch (e: any) {
    quotaMessage.value = e.response?.data?.error || e.message;
  } finally {
    quotaLoading.value = false;
  }
}

function formatQuotaNumber(value: number) {
  return Number.isInteger(value) ? String(value) : value.toFixed(2);
}

function formatDuration(value: number) {
  if (value <= 0) return "现在";
  if (value < 60) return `${Math.ceil(value)} 秒`;
  return `${Math.ceil(value / 60)} 分钟`;
}

const cmdPermsError = ref("");
function tryParseCmdPerms(text: string) {
  if (!editingGroup.value) return;
  const trimmed = text.trim();
  if (!trimmed) {
    editingGroup.value.command_permissions = null;
    cmdPermsError.value = "";
    return;
  }
  try {
    const parsed = JSON.parse(trimmed);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      cmdPermsError.value = "必须是 JSON 对象 {指令名: 权限}";
      return;
    }
    editingGroup.value.command_permissions = parsed;
    cmdPermsError.value = "";
  } catch (e: any) {
    cmdPermsError.value = "JSON 解析错误: " + e.message;
  }
}

function resetHint() {
  if (!cfg.value) return;
  cfg.value.global_channel_prompt = `# 平台特性
你正通过 OneBot(QQ) 对话。QQ 不渲染 Markdown,仅纯文本(系统会自动剥离 Markdown 语法,但请尽量直接输出纯文本)。
回复当前对话通常直接输出文本即可(系统会自动送达);当你需要主动发送消息(分多条发、推送其他会话、跨会话通知等)时,使用 onebot_send_message 工具。
群聊需 @bot 触发。消息上限约 4500 字符,超长会自动分段。

# chat_id 格式
- 私聊: <QQ号>(如 100)
- 群聊: group:<群号>(如 group:42)

# 入站消息格式(你看到的样子)
- 群聊消息前缀: [昵称(QQ号)#群内序号]: 内容;管理员标识为 [昵称(QQ号)(管理员)#群内序号]: 内容
  #后数字是群内递增序号(real_seq),连续可读,用于发现消息断层;调用 onebot 工具时传此数字
  私聊前缀无 # 序号;拿不到 real_seq 时回退显示全局消息 ID(message_id)
- @ 段显示为 @QQ号(昵称);未知用户为 @QQ号(未知用户)
- 媒体占位符: [图1] [视频1] [语音1] [文件1:report.pdf],编号全局连续
- 媒体缓存失败时仍保留对应占位符;文件没有 URL 时可使用 onebot_get_file 工具获取
- 引用回复:被引用消息在 reply_to_text 字段(独立于主 text),格式 [昵称(QQ号)#群内序号]: 文本
- 合并转发:
  [合并转发开始:1]
  [Alice]: msg one
  [Bob]: msg two
  [合并转发结束:1]
  嵌套时层级号递增;超过 4 层显示 [合并转发(已跳过:超过最大深度)]
  合并转发中仅含昵称,无 QQ 号和群内序号,请勿尝试获取转发中发言者的详细信息
- 斜杠命令(/reset 等)不加发送者前缀,原样传递
- 启用群号标识时,消息头部会有 [群:42(测试群)] 行(仅主消息,斜杠命令不加)

# 消息序号与工具调用
- 群聊前缀 # 后的数字是群内序号(real_seq),不是全局消息 ID(message_id)
- onebot_get_msg / onebot_recall_message / onebot_set_msg_emoji_like 等工具的 real_seq 参数填此群内序号
- onebot_get_group_msg_history 的 message_seq 参数例外:填消息 ID(message_id),不是群内序号
- 适配器优先通过 SeqMap 将 real_seq 转为 message_id;未命中时按兼容规则将 real_seq 作为 message_id 透传。如需稳定定位历史消息,可用 onebot_get_group_msg_history 获取实际 message_id

# 出站消息格式(你输出时)
- 直接输出文本只能发纯文本,**无法 @ 人**;要 @ 某人必须用 onebot_send_message 工具,message 参数传 OneBot 11 消息段数组,如 [{"type":"at","data":{"qq":"123456"}},{"type":"text","data":{"text":" 你好"}}]
- 不要用 Markdown 语法(**粗体**、## 标题、- 列表 等),会被自动剥离;如需结构化展示可用纯文本约定(• 列表、【标题】、「引用」、───── 分隔线)
- 回复时无需重复发送者前缀,直接输出正文

# 不支持的元素
- 表情(face/emoji/bface/mface)段在入站时会被丢弃,不要期望看到 QQ 原生表情
- 不支持打字状态提示(send_typing 为 no-op)

# 交互式提问
- 不要使用 clarify 工具提问:OneBot 平台无按钮 UI,且群聊共享会话下用户回复会被排队拦截,导致 agent 卡死直到超时
- 需要向用户提问时直接输出纯文本问题即可,用户回复后会在下一轮被正常处理`;
}
</script>

<template>
  <div>
    <h2>聊天配置</h2>
    <div v-if="msg" :class="['message', msgType]">{{ msg }}</div>

    <div v-if="!cfg" class="loading">加载配置中...</div>

    <!-- 全局群聊设置 -->
    <div v-if="cfg" class="section">
      <h3>全局群聊设置</h3>
      <div class="grid2">
        <label>
          <input type="checkbox" v-model="cfg.group_require_mention" />
          <span>群聊需 @bot</span>
        </label>
        <label>
          <input type="checkbox" v-model="cfg.group_mention_first_only" />
          <span>仅首@ 触发（@bot 须为消息首段）</span>
        </label>
        <label>
          <input type="checkbox" v-model="cfg.group_keyword_first_only" />
          <span>关键词仅首部匹配</span>
        </label>
        <label>
          <input type="checkbox" v-model="cfg.group_strip_first_mention" />
          <span>移除首 @bot 段（消息以 @bot 开头时去掉该段；非首 @bot 始终保留以保证消息完整）</span>
        </label>
        <label class="full">
          触发关键词（回车添加，空=不启用）
          <div class="tag-input-container">
            <span v-for="(kw, i) in cfg.group_trigger_keywords" :key="i" class="tag">
              {{ kw }}<button @click="removeTag(cfg.group_trigger_keywords, i)">×</button>
            </span>
            <input v-model="triggerKeywordsInput" placeholder="输入关键词后回车" @keydown.enter.prevent="addTag(cfg.group_trigger_keywords, triggerKeywordsInput); triggerKeywordsInput=''" />
          </div>
        </label>
      </div>

      <label class="full">
        全局管理员 QQ 号
        <div class="tag-input-container">
          <span v-for="(qq, i) in cfg.global_admins" :key="i" class="tag">
            {{ qq }}
            <button @click="removeTag(cfg.global_admins || [], i)">×</button>
          </span>
          <input v-model="globalAdminInput" placeholder="输入QQ号后回车" @keydown.enter.prevent="addTag(cfg.global_admins || [], globalAdminInput); globalAdminInput=''" />
        </div>
      </label>

      <hr style="margin: 1rem 0; border: none; border-top: 1px solid var(--border);" />

      <div class="grid2">
        <label class="checkbox-row">
          <input type="checkbox" v-model="cfg.message_show_group_id" />
          <span>消息头部显示群号标识</span>
        </label>
        <label class="checkbox-row">
          <input type="checkbox" v-model="cfg.reaction_emoji_enabled" />
          <span>消息送达后贴表情回应（仅 Hermes 插件在线时触发）</span>
        </label>
        <label>
          消息送达后贴表情回应 ID
          <input type="text" v-model="cfg.reaction_emoji_id" placeholder="124" />
          <span class="hint">QQ 表情编号（默认 124）</span>
        </label>
        <label>
          消息排队时贴表情回应 ID
          <input type="text" v-model="cfg.reaction_emoji_id_queued" placeholder="123" />
          <span class="hint">消息进入排队队列时贴的表情，空=不贴（默认 123）</span>
        </label>
      </div>
    </div>

    <!-- 出站消息过滤 -->
    <div v-if="cfg" class="section">
      <h3>出站消息过滤</h3>
      <p class="hint">
        拦截 Hermes 发往 QQ 的文本（send_text、媒体/文件说明、onebot_send_message 的 text 段）。
        使用 Python 正则，<code>re.search</code> 命中任意一条即丢弃该次发送，并向插件返回成功以免重试。
        适配器自己发出的拒绝提示、HTTP 自动化 API 不受此过滤。空文本（无说明的纯媒体/文件）不会被匹配。
        可在模式中使用 <code>(?i)</code> 忽略大小写、<code>(?s)</code> 让 <code>.</code> 匹配换行。
      </p>
      <label class="checkbox-row">
        <input type="checkbox" v-model="cfg.outbound_filter_enabled" />
        <span>启用出站正则过滤</span>
      </label>
      <label class="full">
        过滤正则（回车添加，命中即丢弃）
        <div class="tag-input-container">
          <span v-for="(pat, i) in cfg.outbound_filter_patterns" :key="i" class="tag">
            {{ pat }}<button @click="removeTag(cfg.outbound_filter_patterns, i)">×</button>
          </span>
          <input
            v-model="outboundFilterInput"
            placeholder="输入正则后回车，如 (?i)https?://"
            @keydown.enter.prevent="addTag(cfg.outbound_filter_patterns, outboundFilterInput); outboundFilterInput=''"
          />
        </div>
        <span class="hint">保存时校验能否编译；最多 50 条，每条最多 256 字符。群配置可单独覆盖开关和正则列表。</span>
      </label>
    </div>

    <!-- 入站消息限流 -->
    <div v-if="cfg" class="section">
      <h3>入站消息限流</h3>
      <p class="hint">
        现有准入和指令过滤通过后再计数。全局、群聊、个人三个维度同时生效，命中任一维度即回复原消息并拦截。
        全局管理员和对应群管理员不受限流；个人计数在私聊和所有群之间共享。限额 0 表示禁用该维度。
      </p>
      <label class="checkbox-row">
        <input type="checkbox" v-model="cfg.rate_limit_enabled" />
        <span>启用消息限流</span>
      </label>
      <label>
        额度存储故障策略
        <select v-model="cfg.rate_limit_storage_failure_mode">
          <option value="memory_fallback">退回内存限流（默认）</option>
          <option value="reject">拒绝受限流的消息</option>
        </select>
        <span class="hint">正常时额度在消息转发前持久化；内存降级期间若进程再次崩溃，待同步操作可能丢失。</span>
      </label>
      <div class="grid2 rate-limit-grid">
        <fieldset>
          <legend>全局总量</legend>
          <label>算法
            <select v-model="cfg.global_rate_limit_algorithm">
              <option value="sliding_window">滑动窗口</option>
              <option value="token_bucket">令牌桶</option>
            </select>
          </label>
          <label>消息上限 <input type="number" v-model.number="cfg.global_rate_limit_messages" min="0" /></label>
          <label>窗口（秒） <input type="number" v-model.number="cfg.global_rate_limit_window_seconds" min="0" step="0.1" /></label>
        </fieldset>
        <fieldset>
          <legend>每个群聊</legend>
          <label>算法
            <select v-model="cfg.group_rate_limit_algorithm">
              <option value="sliding_window">滑动窗口</option>
              <option value="token_bucket">令牌桶</option>
            </select>
          </label>
          <label>消息上限 <input type="number" v-model.number="cfg.group_rate_limit_messages" min="0" /></label>
          <label>窗口（秒） <input type="number" v-model.number="cfg.group_rate_limit_window_seconds" min="0" step="0.1" /></label>
        </fieldset>
        <fieldset>
          <legend>每个用户</legend>
          <label>算法
            <select v-model="cfg.user_rate_limit_algorithm">
              <option value="sliding_window">滑动窗口</option>
              <option value="token_bucket">令牌桶</option>
            </select>
          </label>
          <label>消息上限 <input type="number" v-model.number="cfg.user_rate_limit_messages" min="0" /></label>
          <label>窗口（秒） <input type="number" v-model.number="cfg.user_rate_limit_window_seconds" min="0" step="0.1" /></label>
        </fieldset>
      </div>
      <label class="full">
        拦截提示模板
        <input v-model="cfg.rate_limit_reject_message" />
        <span class="hint">支持 {scope}、{retry_after}、{user_id}；等待时间向上取整为秒。</span>
      </label>

      <div class="quota-panel">
        <h4>额度查询与重置</h4>
        <p class="hint">管理员消息不消耗额度。重置只影响当前维度，目标仍可能被全局或其他维度拦截。</p>
        <div class="quota-query">
          <label>
            查询维度
            <select v-model="quotaScope" @change="quota = null; quotaMessage = ''">
              <option value="user">用户</option>
              <option value="group">群聊</option>
              <option value="global">全局</option>
            </select>
          </label>
          <label v-if="quotaScope !== 'global'">
            {{ quotaScope === "group" ? "群号" : "QQ 号" }}
            <input v-model="quotaTargetId" inputmode="numeric" @input="quota = null" @keyup.enter="fetchQuota" />
          </label>
          <button class="sync-btn" :disabled="quotaLoading" @click="fetchQuota">
            {{ quotaLoading ? "查询中..." : quota ? "刷新" : "查询" }}
          </button>
        </div>
        <p v-if="quotaMessage" class="quota-message">{{ quotaMessage }}</p>
        <div v-if="quota" class="quota-result">
          <div><span>算法</span><strong>{{ quota.algorithm === "sliding_window" ? "滑动窗口" : "令牌桶" }}</strong></div>
          <div><span>限额 / 窗口</span><strong>{{ quota.limit }} / {{ quota.window_seconds }} 秒</strong></div>
          <div><span>已用 / 剩余</span><strong>{{ formatQuotaNumber(quota.used) }} / {{ formatQuotaNumber(quota.remaining) }}</strong></div>
          <div><span>下一额度 / 完全恢复</span><strong>{{ formatDuration(quota.next_available_in_seconds) }} / {{ formatDuration(quota.full_recovery_in_seconds) }}</strong></div>
          <div class="quota-storage">
            <span>持久化</span>
            <strong :class="`storage-${quota.persistence.status}`">{{ quota.persistence.status }}</strong>
            <small>待同步 {{ quota.persistence.pending_operations }} / {{ quota.persistence.pending_limit }} 条</small>
          </div>
          <p v-if="quota.persistence.status !== 'healthy'" class="storage-warning">
            持久化当前处于 {{ quota.persistence.status }} 状态。请检查服务日志和数据目录。
          </p>
          <p v-if="quota.persistence.fallback_exhausted" class="storage-warning">
            待同步队列已满，为避免内存持续增长，新消息将被拒绝，直到数据库恢复。
          </p>
          <button class="row-btn danger" :disabled="quotaLoading" @click="resetQuota">重置当前额度</button>
        </div>
      </div>
    </div>

    <!-- Bot 动态用户黑名单 -->
    <div v-if="cfg" class="section">
      <div class="section-header">
        <h3>Bot 动态用户黑名单</h3>
        <button @click="fetchBotBlacklist" :disabled="blacklistLoading" class="sync-btn">
          {{ blacklistLoading ? "刷新中..." : "↻ 刷新记录" }}
        </button>
      </div>
      <p class="hint">
        独立于上方群聊/私聊准入名单。Bot 可通过 onebot_get_bot_blacklist 和
        onebot_edit_bot_blacklist 工具临时拦截用户；全局管理员和对应群管理员始终豁免。
      </p>
      <div class="grid2">
        <label class="checkbox-row">
          <input type="checkbox" v-model="cfg.bot_blacklist_enabled" />
          <span>允许 bot 查看和编辑动态黑名单</span>
        </label>
        <label>
          允许的最大拉黑时间（小时）
          <input type="number" v-model.number="blacklistMaxHours" min="0.0002778" step="1" />
          <span class="hint">默认 24 小时；bot 请求超过此值时自动截短。</span>
        </label>
        <label class="full">
          拦截提示模板
          <input v-model="cfg.bot_blacklist_reject_message" />
          <span class="hint">
            支持 {user_id}、{scope}、{remaining}、{expires_at}、{reason}。
          </span>
        </label>
      </div>

      <div v-if="blacklistEntries.length" class="table-wrap">
      <table class="group-table blacklist-table">
        <thead>
          <tr><th>用户</th><th>范围</th><th>原因</th><th>发起用户</th><th>创建时间</th><th>到期/剩余</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="entry in blacklistEntries" :key="entry.id">
            <td>{{ entry.user_id }}</td>
            <td>{{ scopeLabel(entry) }}</td>
            <td>{{ entry.reason }}</td>
            <td>{{ entry.created_by_user_id || "—" }}</td>
            <td>{{ formatTimestamp(entry.created_at) }}</td>
            <td>{{ formatTimestamp(entry.expires_at) }}<br><span class="hint">{{ entry.remaining }}</span></td>
            <td><button @click="removeBlacklistEntry(entry)" class="row-btn danger">解除</button></td>
          </tr>
        </tbody>
      </table>
      </div>
      <p v-else-if="!blacklistLoading" class="empty">暂无有效动态黑名单记录</p>
    </div>

    <!-- 会话隔离与消息排队 -->
    <div v-if="cfg" class="section">
      <h3>会话隔离与消息排队</h3>

      <h4>Hermes 会话隔离</h4>
      <p class="hint" style="margin-bottom:0.75rem;">
        <strong>隔离(true)</strong>:每个群成员独立 session。<br>
        <strong>共享(false)</strong>:全群共享 session。
      </p>
      <div v-if="modeMsg" :class="['message', modeMsgType]">{{ modeMsg }}</div>

      <div class="mode-display">
        <span class="mode-label">当前值:</span>
        <span :class="['mode-value', hermesMode?.group_sessions_per_user ? 'isolation-on' : 'isolation-off']">
          {{ hermesMode?.group_sessions_per_user ? '隔离' : '共享' }}
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
          <span>开启群聊共享 session(写入 group_sessions_per_user: false 到 Hermes config.yaml)</span>
        </label>
        <button @click="editingPerUser = false" class="mode-cancel-btn">取消</button>
      </div>
      <p class="hint" v-if="editingPerUser">
        修改后会写入 Hermes <code>config.yaml</code>,需<strong>重启 Hermes 网关</strong>才生效。
      </p>

      <hr style="margin: 1.25rem 0; border: none; border-top: 1px solid var(--border);" />

      <h4>适配器群聊排队</h4>
      <p class="hint" style="margin-bottom:0.75rem;">
        当 Hermes 不隔离群成员时,适配器对群消息排队串行处理。
        群 busy 时所有用户的消息（含 busy 用户自身）一律排队等待,出队时连续同用户消息会自动合并为一条。所有/命令会绕过排队。
      </p>
      <label class="checkbox-row">
        <input type="checkbox" v-model="cfg.event_queue_enabled" />
        <span>启用群聊排队</span>
      </label>
      <label class="checkbox-row">
        <input type="checkbox" v-model="cfg.event_queue_clear_on_session_reset" />
        <span>使用 /new 或 /reset 时清空当前群的待处理队列</span>
      </label>
      <label class="checkbox-row">
        <input type="checkbox" v-model="cfg.event_queue_clean_command_enabled" />
        <span>启用适配器本地 /clean 命令（清空当前群队列，不发送给 Hermes）</span>
      </label>
      <label>
        单群队列上限
        <input type="number" v-model.number="cfg.event_queue_max_per_chat" min="1" max="500" />
        <span class="hint">每个群聊的排队消息上限（默认 50）；达到上限后拒绝新进入的排队消息。</span>
      </label>
      <label>
        busy 超时(秒)
        <input type="number" v-model.number="cfg.event_queue_idle_timeout" min="10" step="10" />
        <span class="hint">Hermes 无响应的超时阈值,超时后强制清空 busy 并派发下一条。</span>
      </label>
    </div>

    <!-- 媒体投递 -->
    <div v-if="cfg" class="section">
      <h3>媒体投递</h3>
      <p class="hint">入站媒体(图片/语音/视频/文件)的投递方式。</p>
      <label>
        投递模式
        <select v-model="cfg.media_delivery_mode">
          <option value="passthrough">URL 直传</option>
          <option value="cache">插件侧下载落盘（默认）</option>
        </select>
        <span class="hint">
          <strong>URL 直传</strong>:媒体 URL 作为文本占位符(如 [图1](https://...))传给 LLM,LLM 按需 fetch。<br>
          <strong>下载落盘</strong>:插件在 Hermes 进程内调用 cache_image_from_url 等下载到 ~/.hermes/cache/,
          填 media_urls 字段供 vision/STT 工具读取。缓存失败则丢弃该媒体,保留空占位符 [图N]。
          file 段无 URL 时一律跳过,LLM 可用 onebot_get_file 工具按需拉取。
        </span>
      </label>
    </div>

    <!-- notice 事件推送 -->
    <div v-if="cfg" class="section">
      <h3>notice 事件推送</h3>
      <p class="hint" style="margin-bottom:0.75rem;">
        将 OneBot notice 事件合成为系统提示文本转发给 agent。事件文本以 [系统] 开头,与普通消息一样走群聊排队。
        群配置可单独覆盖。保存后立即生效（热加载）。
      </p>
      <label class="checkbox-row">
        <input type="checkbox" v-model="cfg.notify_poke_enabled" />
        <span>戳一戳(bot 被戳时推送,含私聊;走群/DM 用户过滤)</span>
      </label>
      <label class="checkbox-row">
        <input type="checkbox" v-model="cfg.notify_member_change_enabled" />
        <span>群成员变动(其他成员进群/退群时推送,区分主动退群和被踢)</span>
      </label>
    </div>

    <!-- 私聊设置 -->
    <div v-if="cfg" class="section">
      <h3>私聊设置</h3>
      <div class="grid2">
        <label>
          私聊过滤模式
          <select v-model="cfg.dm_user_filter_mode">
            <option value="whitelist">白名单（仅名单内可私聊，空=拒绝所有人）</option>
            <option value="blacklist">黑名单（名单内禁用，空=允许所有人）</option>
          </select>
        </label>
        <label>
          私聊名单
          <div class="tag-input-container">
            <span v-for="(u, i) in cfg.dm_user_list" :key="i" class="tag">
              {{ u }}<button @click="removeTag(cfg.dm_user_list || [], i)">×</button>
            </span>
            <input v-model="dmUserInput" placeholder="回车添加QQ号" @keydown.enter.prevent="addTag(cfg.dm_user_list || [], dmUserInput); dmUserInput=''" />
          </div>
        </label>
      </div>
    </div>

    <!-- 全局提示词 -->
    <div v-if="cfg" class="section">
      <h3>全局提示词 (Channel Prompt)</h3>
      <p class="hint">注入到 LLM 系统提示中，告诉模型当前平台特性。保存时物化写入 Hermes config.yaml 的 platforms.onebot.channel_prompts，需重启 Hermes 网关生效。群专属提示词非空时覆盖此全局值。</p>
      <textarea v-model="cfg.global_channel_prompt" rows="8" class="hint-editor" placeholder="输入全局提示词..."></textarea>
      <div class="hint-actions">
        <button @click="resetHint" class="reset-btn">恢复默认</button>
      </div>
    </div>

    <!-- 群列表 -->
    <div class="section">
      <div class="section-header">
        <h3>群列表</h3>
        <div class="actions">
          <button @click="syncFromOneBot" :disabled="syncing" class="sync-btn">
            {{ syncing ? "同步中..." : "🔄 从 OneBot 同步" }}
          </button>
          <button @click="addGroup" class="add-btn">+ 添加群</button>
        </div>
      </div>

      <div v-if="groups.length" class="table-wrap">
      <table class="group-table">
        <thead>
          <tr>
            <th>群号</th><th>群名</th><th>状态</th><th>@bot</th><th>首@</th><th>关键词</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="g in groups" :key="g.group_id">
            <td>{{ g.group_id }}</td>
            <td>{{ g.name || "—" }}</td>
            <td>
              <span :class="g.enabled ? 'status-on' : 'status-off'">
                {{ g.enabled ? '✅ 启用' : '❌ 禁用' }}
              </span>
            </td>
            <td>{{ g.require_mention == null ? '跟随全局' : (g.require_mention ? '是' : '否') }}</td>
            <td>{{ g.mention_first_only == null ? '跟随全局' : (g.mention_first_only ? '是' : '否') }}</td>
            <td>{{ g.trigger_keywords == null ? '跟随全局' : (g.trigger_keywords.length ? g.trigger_keywords.join(', ') : '禁用') }}</td>
            <td>
              <button @click="editGroup(g)" class="row-btn">编辑</button>
              <button @click="removeGroup(g.group_id)" class="row-btn danger">删除</button>
            </td>
          </tr>
        </tbody>
      </table>
      </div>
      <p v-else class="empty">暂无群配置，点击「从 OneBot 同步」或「添加群」</p>
    </div>

    <div class="save-bar">
    <button @click="saveGlobal" :disabled="saving" class="save-btn">
      {{ saving ? "保存中..." : "保存配置" }}
    </button>
    </div>

    <!-- 群详情编辑弹窗 -->
    <div v-if="showEditor && editingGroup" class="modal-overlay" @click.self="showEditor = false">
      <div class="modal">
        <h3>{{ editingGroup.group_id ? "编辑群 " + editingGroup.group_id : '添加群' }}</h3>

        <label>
          群号
          <input v-model="editingGroup.group_id" :disabled="editingExistingGroup" placeholder="输入群号" />
        </label>
        <label>
          群名
          <input v-model="editingGroup.name" placeholder="群显示名（可选）" />
        </label>
        <label>
          <input type="checkbox" v-model="editingGroup.enabled" />
          <span>启用 Bot</span>
        </label>

        <label>
          @bot 要求
          <select v-model="editingGroup.require_mention">
            <option :value="null">跟随全局</option>
            <option :value="true">强制要求</option>
            <option :value="false">强制不要求</option>
          </select>
        </label>

        <label>
          仅首@ 触发
          <select v-model="editingGroup.mention_first_only">
            <option :value="null">跟随全局</option>
            <option :value="true">仅首@</option>
            <option :value="false">任意位置 @</option>
          </select>
        </label>

        <label>
          触发关键词模式
          <select
            :value="editingGroup.trigger_keywords == null ? 'global' : 'custom'"
            @change="setTriggerKeywordsMode(($event.target as HTMLSelectElement).value)"
          >
            <option value="global">跟随全局</option>
            <option value="custom">自定义（见下）</option>
          </select>
        </label>

        <label v-if="editingGroup.trigger_keywords != null">
          关键词列表（回车添加）
          <div class="tag-input-container">
            <span v-for="(kw, i) in editingGroup.trigger_keywords" :key="i" class="tag">
              {{ kw }}<button @click="removeTag(editingGroup.trigger_keywords, i)">×</button>
            </span>
            <input v-model="groupKeywordsInput" placeholder="输入关键词后回车" @keydown.enter.prevent="addTag(editingGroup.trigger_keywords, groupKeywordsInput); groupKeywordsInput=''" />
          </div>
          <span class="hint">留空列表 = 此群禁用关键词触发</span>
        </label>

        <label>
          关键词仅首部匹配
          <select v-model="editingGroup.keyword_first_only">
            <option :value="null">跟随全局</option>
            <option :value="true">仅首部匹配</option>
            <option :value="false">任意位置匹配</option>
          </select>
        </label>

        <label>
          移除首 @bot 段
          <select v-model="editingGroup.strip_first_mention">
            <option :value="null">跟随全局</option>
            <option :value="true">移除首@bot</option>
            <option :value="false">保留所有@bot</option>
          </select>
        </label>
        <span class="hint" v-if="editingGroup.strip_first_mention === false">
          ⚠️ 关闭后保留首 @bot 段, 此时 @bot /指令 将无法被识别
        </span>

        <label>
          群专属提示词（空=用全局提示词）
          <textarea v-model="editingGroup.custom_prompt" rows="4" placeholder="为此群定制系统提示词，留空则使用全局设置"></textarea>
          <span class="hint">保存时物化写入 Hermes config.yaml，需重启 Hermes 网关生效</span>
        </label>

        <label>
          群管理员 QQ 号
          <div class="tag-input-container">
            <span v-for="(qq, i) in editingGroup.admins" :key="i" class="tag">
              {{ qq }}<button @click="removeTag(editingGroup.admins, i)">×</button>
            </span>
            <input v-model="groupAdminInput" placeholder="回车添加" @keydown.enter.prevent="addTag(editingGroup.admins, groupAdminInput); groupAdminInput=''" />
          </div>
        </label>

        <label>
          群成员过滤模式
          <select v-model="editingGroup.group_user_filter_mode">
            <option value="blacklist">黑名单（名单内禁用，空=允许所有人）</option>
            <option value="whitelist">白名单（仅名单内可用，空=拒绝所有人）</option>
          </select>
        </label>

        <label>
          群成员名单
          <div class="tag-input-container">
            <span v-for="(u, i) in editingGroup.group_user_list" :key="i" class="tag">
              {{ u }}<button @click="removeTag(editingGroup.group_user_list, i)">×</button>
            </span>
            <input v-model="groupUserInput" placeholder="回车添加QQ号" @keydown.enter.prevent="addTag(editingGroup.group_user_list, groupUserInput); groupUserInput=''" />
          </div>
        </label>

        <hr style="margin: 1rem 0; border: none; border-top: 1px solid var(--border);" />
        <h4 style="margin: 0 0 0.75rem; font-size: 0.95rem;">出站消息过滤</h4>
        <label>
          出站正则过滤
          <select v-model="editingGroup.outbound_filter_enabled">
            <option :value="null">跟随全局</option>
            <option :value="true">启用</option>
            <option :value="false">禁用</option>
          </select>
        </label>
        <label class="checkbox-row">
          <input
            type="checkbox"
            :checked="editingGroup.outbound_filter_patterns != null"
            @change="toggleGroupOutboundFilterPatterns(($event.target as HTMLInputElement).checked)"
          />
          <span>覆盖全局正则列表</span>
        </label>
        <label v-if="editingGroup.outbound_filter_patterns != null">
          过滤正则（回车添加）
          <div class="tag-input-container">
            <span v-for="(pat, i) in editingGroup.outbound_filter_patterns" :key="i" class="tag">
              {{ pat }}<button @click="removeTag(editingGroup.outbound_filter_patterns, i)">×</button>
            </span>
            <input
              v-model="groupOutboundFilterInput"
              placeholder="输入正则后回车"
              @keydown.enter.prevent="addTag(editingGroup.outbound_filter_patterns || [], groupOutboundFilterInput); groupOutboundFilterInput=''"
            />
          </div>
          <span class="hint">留空列表 = 此群不匹配任何规则（即使开关启用也不过滤）。不与全局列表合并。</span>
        </label>

        <hr style="margin: 1rem 0; border: none; border-top: 1px solid var(--border);" />
        <h4 style="margin: 0 0 0.75rem; font-size: 0.95rem;">群聊消息限流</h4>
        <label class="checkbox-row">
          <input
            type="checkbox"
            :checked="editingGroup.group_rate_limit_messages != null"
            @change="toggleGroupRateLimitOverride(($event.target as HTMLInputElement).checked)"
          />
          <span>覆盖全局群聊限流配置</span>
        </label>
        <template v-if="editingGroup.group_rate_limit_messages != null">
          <label>算法
            <select v-model="editingGroup.group_rate_limit_algorithm">
              <option value="sliding_window">滑动窗口</option>
              <option value="token_bucket">令牌桶</option>
            </select>
          </label>
          <label>消息上限
            <input type="number" v-model.number="editingGroup.group_rate_limit_messages" min="0" />
            <span class="hint">0 表示仅在此群禁用群聊维度；全局和个人维度仍会生效。</span>
          </label>
          <label>窗口（秒）
            <input type="number" v-model.number="editingGroup.group_rate_limit_window_seconds" min="0" step="0.1" />
          </label>
        </template>

        <hr style="margin: 1rem 0; border: none; border-top: 1px solid var(--border);" />
        <h4 style="margin: 0 0 0.75rem; font-size: 0.95rem;">消息显示</h4>

        <label>
          消息头部显示群号标识
          <select v-model="editingGroup.message_show_group_id">
            <option :value="null">跟随全局</option>
            <option :value="true">显示</option>
            <option :value="false">不显示</option>
          </select>
        </label>

        <label>
          消息送达贴表情回应
          <select v-model="editingGroup.reaction_emoji_enabled">
            <option :value="null">跟随全局</option>
            <option :value="true">开启</option>
            <option :value="false">关闭</option>
          </select>
        </label>

        <hr style="margin: 1rem 0; border: none; border-top: 1px solid var(--border);" />
        <h4 style="margin: 0 0 0.75rem; font-size: 0.95rem;">/指令过滤</h4>

        <label>
          指令过滤开关
          <select v-model="editingGroup.command_filter_enabled">
            <option :value="null">跟随全局</option>
            <option :value="true">启用</option>
            <option :value="false">禁用</option>
          </select>
        </label>

        <label>
          过滤未知指令
          <select v-model="editingGroup.command_filter_unknown">
            <option :value="null">跟随全局</option>
            <option :value="true">过滤未知指令</option>
            <option :value="false">放行未知指令</option>
          </select>
        </label>

        <label>
          指令权限覆盖
          <span class="hint">
            覆盖此群的指令权限。JSON 格式: {"指令名": "everyone|admin|disabled"}。
            留空(null)= 跟随全局；{} = 强制清空所有配置；非空 = 覆盖对应指令。
          </span>
          <textarea
            :value="editingGroup.command_permissions === null ? '' : JSON.stringify(editingGroup.command_permissions, null, 2)"
            @input="tryParseCmdPerms(($event.target as HTMLTextAreaElement).value)"
            rows="4"
            placeholder='{"help": "everyone", "kick": "admin"} 或留空跟随全局'
          ></textarea>
          <span v-if="cmdPermsError" class="hint" style="color: var(--danger);">{{ cmdPermsError }}</span>
        </label>

        <hr style="margin: 1.25rem 0; border: none; border-top: 1px solid var(--border);" />
        <h4 style="margin: 0 0 0.75rem; font-size: 0.95rem;">notice 事件推送</h4>

        <label>
          戳一戳推送
          <select v-model="editingGroup.notify_poke_enabled">
            <option :value="null">跟随全局</option>
            <option :value="true">启用</option>
            <option :value="false">禁用</option>
          </select>
        </label>

        <label>
          群成员变动推送
          <select v-model="editingGroup.notify_member_change_enabled">
            <option :value="null">跟随全局</option>
            <option :value="true">启用</option>
            <option :value="false">禁用</option>
          </select>
        </label>

        <div class="modal-actions">
          <button @click="showEditor = false" class="cancel-btn">取消</button>
          <button @click="saveGroup" class="btn-modal-save">保存</button>
        </div>
      </div>
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
.rate-limit-grid fieldset { border: 1px solid var(--border); border-radius: 6px; padding: 0.9rem; display: grid; gap: 0.75rem; }
.rate-limit-grid legend { color: var(--primary); font-weight: 600; padding: 0 0.35rem; }
.quota-panel { margin-top: 1rem; border-top: 1px solid var(--border); padding-top: 1rem; }
.quota-query { display: grid; grid-template-columns: minmax(140px, 1fr) minmax(180px, 2fr) auto; gap: 0.75rem; align-items: end; }
.quota-query label { margin: 0; }
.quota-query button { margin-bottom: 1px; height: 2.25rem; }
.quota-message { color: var(--primary); font-size: 0.85rem; }
.quota-result { margin-top: 0.9rem; padding: 0.9rem; background: var(--bg); border: 1px solid var(--border); border-radius: 6px; display: grid; gap: 0.55rem; }
.quota-result > div { display: flex; gap: 0.75rem; justify-content: space-between; font-size: 0.88rem; }
.quota-result span { color: var(--text-muted); }
.quota-storage { justify-content: flex-start !important; align-items: center; }
.quota-storage small { color: var(--text-muted); margin-left: auto; }
.storage-healthy { color: var(--success); }
.storage-degraded, .storage-recovering { color: var(--warning); }
.storage-not_started { color: var(--text-muted); }
.storage-warning { color: #856404; background: #fff9e6; padding: 0.6rem; border-radius: 4px; font-size: 0.82rem; }
@media (max-width: 700px) { .quota-query { grid-template-columns: 1fr; } }
label { display: block; margin-bottom: 0.75rem; font-weight: 500; font-size: 0.9rem; }
label.full { width: 100%; }
.grid2 > label.full { grid-column: 1 / -1; }
input, select, textarea { width: 100%; padding: 0.5rem; border: 1px solid #ccc; border-radius: 4px; font-size: 0.9rem; margin-top: 0.25rem; }
textarea { resize: vertical; }
input[type="checkbox"] { width: auto; }
.checkbox-row { display: flex; align-items: center; gap: 0.5rem; }
.checkbox-row span { font-weight: 500; }
input:focus, select:focus, textarea:focus { outline: none; border-color: var(--primary); box-shadow: 0 0 0 2px rgba(74,144,226,0.1); }

.tag-input-container { display: flex; flex-wrap: wrap; gap: 0.3rem; align-items: center; padding: 0.4rem; border: 1px solid #ccc; border-radius: 4px; min-height: 2.5rem; }
.tag { background: var(--primary); color: white; padding: 0.15rem 0.5rem; border-radius: 10px; font-size: 0.85rem; display: flex; align-items: center; gap: 0.25rem; }
.tag button { background: none; border: none; color: white; cursor: pointer; font-size: 0.9rem; padding: 0; }
.tag-input-container input { border: none; flex: 1; min-width: 120px; padding: 0.2rem; margin: 0; }

.group-table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
.group-table th { text-align: left; padding: 0.5rem; border-bottom: 2px solid var(--border); color: var(--text-muted); }
.group-table td { padding: 0.5rem; border-bottom: 1px solid var(--border); }
.status-on { color: var(--success); } .status-off { color: var(--danger); }
.row-btn { padding: 0.25rem 0.6rem; margin-right: 0.25rem; border: 1px solid #ccc; border-radius: 4px; cursor: pointer; background: var(--bg); font-size: 0.85rem; }
.row-btn.danger { color: var(--danger); border-color: var(--danger); }
.row-btn:hover { background: #e8e8e8; }

.btn-modal-save { background: var(--primary); color: white; border: none; padding: 0.6rem 1.2rem; border-radius: 6px; cursor: pointer; font-size: 0.9rem; }
.sync-btn { background: var(--bg); border: 1px solid var(--border); padding: 0.4rem 0.8rem; border-radius: 4px; cursor: pointer; font-size: 0.85rem; }
.add-btn { background: var(--success); color: white; border: none; padding: 0.4rem 0.8rem; border-radius: 4px; cursor: pointer; font-size: 0.85rem; }
.empty { color: var(--text-muted); text-align: center; padding: 2rem; }

.hint-editor { width: 100%; padding: 0.75rem; border: 1px solid #ccc; border-radius: 4px; font-family: monospace; font-size: 0.9rem; line-height: 1.6; resize: vertical; }
.hint-editor:focus { outline: none; border-color: var(--primary); }
.hint-actions { margin-top: 0.5rem; }
.reset-btn { background: var(--bg); border: 1px solid var(--border); padding: 0.4rem 0.8rem; border-radius: 4px; cursor: pointer; font-size: 0.85rem; }

.modal-overlay { position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.4); display: flex; align-items: center; justify-content: center; z-index: 100; }
.modal { background: var(--card-bg); border-radius: 8px; padding: 1.5rem; max-width: 550px; width: 90%; max-height: 85vh; overflow-y: auto; }
.modal h3 { margin: 0 0 1rem; font-size: 1.1rem; }
.modal-actions { display: flex; gap: 0.75rem; justify-content: flex-end; margin-top: 1rem; }
.cancel-btn { background: var(--bg); border: 1px solid var(--border); padding: 0.6rem 1.2rem; border-radius: 6px; cursor: pointer; }

.message { padding: 0.75rem 1rem; border-radius: 6px; margin-bottom: 1rem; font-weight: 500; }
.message.success { background: #d4edda; color: #155724; border-left: 4px solid var(--success); }
.message.error { background: #f8d7da; color: #721c24; border-left: 4px solid var(--danger); }
.message.warning { background: #fff9e6; color: #856404; border-left: 4px solid var(--warning); }
.loading { text-align: center; padding: 2rem; color: var(--text-muted, #666); }

.hint { display: block; font-size: 0.8rem; color: var(--text-muted); margin: 0.25rem 0 0; line-height: 1.5; }
.hint code { background: var(--bg); padding: 0.1rem 0.3rem; border-radius: 3px; font-size: 0.9em; }

h4 { margin: 0 0 0.75rem 0; font-size: 0.95rem; color: #555; }

.mode-display {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  flex-wrap: wrap;
  padding: 0.5rem 0;
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
</style>
