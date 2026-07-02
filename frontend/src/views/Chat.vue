<script setup lang="ts">
import { ref, onMounted } from "vue";
import {
  getGroups, putGroup, deleteGroup, syncGroups,
  type GroupConfig,
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

onMounted(async () => {
  try {
    await load();
    groups.value = await getGroups();
  } catch (e: any) {
    msg.value = "加载失败: " + (e.response?.data?.error || e.message);
    msgType.value = "error";
  }
});

async function saveGlobal() {
  if (!cfg.value) return;
  saving.value = true;
  msg.value = "";
  const c = cfg.value;
    try {
    await saveConfig({
      group_session_mode: c.group_session_mode,
      group_require_mention: c.group_require_mention,
      group_mention_first_only: c.group_mention_first_only,
      group_trigger_keywords: c.group_trigger_keywords,
      group_keyword_first_only: c.group_keyword_first_only,
      group_keep_mention: c.group_keep_mention,
      group_auto_join: c.group_auto_join,
      global_admins: c.global_admins,
      media_max_mb: c.media_max_mb,
      media_max_count: c.media_max_count,
      media_limit_reject_enabled: c.media_limit_reject_enabled,
      media_limit_reject_message: c.media_limit_reject_message,
      dm_user_filter_mode: c.dm_user_filter_mode,
      dm_user_list: c.dm_user_list,
      message_show_group_id: c.message_show_group_id,
      reaction_emoji_enabled: c.reaction_emoji_enabled,
      reaction_emoji_id: c.reaction_emoji_id,
      platform_hint: c.platform_hint,
    });
    msg.value = "全局设置已保存";
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
    groups.value = await getGroups();
  } catch (e: any) {
    msg.value = "同步失败: " + (e.response?.data?.error || e.message);
    msgType.value = "error";
  } finally { syncing.value = false; }
}

function addGroup() {
  editingGroup.value = {
    group_id: "", name: "", enabled: true, require_mention: null,
    mention_first_only: null, trigger_keywords: null, keyword_first_only: null, keep_mention: null,
    session_mode: "default", custom_prompt: "", admins: [],
    group_user_filter_mode: "blacklist", group_user_list: [],
    welcome_enabled: false, welcome_message: "", media_max_mb: null, media_max_count: null, media_limit_reject_enabled: null, auto_join: false,
    message_show_group_id: null,
    reaction_emoji_enabled: null,
    command_filter_enabled: null, command_filter_unknown: null, command_permissions: null,
  };
  showEditor.value = true;
}

function editGroup(g: GroupConfig) {
  editingGroup.value = { ...g };
  showEditor.value = true;
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
function addTag(list: string[], value: string) {
  const v = value.trim();
  if (v && !list.includes(v)) list.push(v);
}
function removeTag(list: string[], idx: number) {
  list.splice(idx, 1);
}

function sessionModeLabel(mode: string): string {
  if (mode === "shared") return "共享会话";
  if (mode === "per_user") return "独立会话";
  return "跟随全局";
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
  cfg.value.platform_hint = "# 平台特性\n你正通过 OneBot(QQ) 对话。QQ 不渲染 Markdown,仅纯文本(系统会自动剥离 Markdown 语法,但请尽量直接输出纯文本)。\n回复当前对话通常直接输出文本即可(系统会自动送达);当你需要主动发送消息(分多条发、推送其他会话、跨会话通知等)时,使用 onebot_send_message 工具。\n群聊需 @bot 触发。消息上限约 4500 字符,超长会自动分段。\n\n# chat_id 格式\n- 私聊: <QQ号>(如 100)\n- 群聊(默认 shared 模式): group:<群号>(如 group:42)\n- 群聊 per_user 会话模式: group:<群号>:user:<QQ号>(如 group:42:user:100)\n\n# 入站消息格式(你看到的样子)\n- 群聊消息前缀: [昵称(QQ号)#群内序号]: 内容;管理员标识为 [昵称(QQ号)(管理员)#群内序号]: 内容\n  #后数字是群内递增序号(real_seq),连续可读,用于发现消息断层;调用 onebot 工具时传此数字\n  私聊前缀无 # 序号;拿不到 real_seq 时回退显示全局消息 ID(message_id)\n- @ 段显示为 @QQ号(昵称);未知用户为 @QQ号(未知用户)\n- 媒体占位符: [图1] [视频1] [语音1] [文件1:report.pdf],编号全局连续\n- 媒体跳过/失败: [图1](已跳过:超出数量限制:已下载10个达到上限10) 或 [语音1](语音转换失败,保留原始格式)\n- 引用回复:被引用消息在 reply_to_text 字段(独立于主 text),格式 [昵称(QQ号)#群内序号]: 文本\n- 合并转发:\n  [合并转发开始:1]\n  [Alice]: msg one\n  [Bob]: msg two\n  [合并转发结束:1]\n  嵌套时层级号递增;超过 4 层显示 [合并转发(已跳过:超过最大深度)]\n  合并转发中仅含昵称,无 QQ 号和群内序号,请勿尝试获取转发中发言者的详细信息\n- 斜杠命令(/reset 等)不加发送者前缀,原样传递\n- 启用群号标识时,消息头部会有 [群:42(测试群)] 行(仅主消息,斜杠命令不加)\n\n# 消息序号与工具调用\n- 群聊前缀 # 后的数字是群内序号(real_seq),不是全局消息 ID(message_id)\n- onebot_get_msg / onebot_recall_message / onebot_set_msg_emoji_like 等工具的 real_seq 参数填此群内序号\n- onebot_get_group_msg_history 的 message_seq 参数例外:填消息 ID(message_id),不是群内序号\n- 适配器内部维护 real_seq→message_id 映射,自动转换;映射过期时工具返回错误,需用 onebot_get_group_msg_history 重新获取\n\n# 出站消息格式(你输出时)\n- 要 @ 某人,使用 {@QQ号} 格式,如 {@123456} 你好(QQ 号 5-11 位数字,大括号包裹)\n- 不要用 Markdown 语法(**粗体**、## 标题、- 列表 等),会被自动剥离;如需结构化展示可用纯文本约定(• 列表、【标题】、「引用」、───── 分隔线)\n- 回复时无需重复发送者前缀,直接输出正文\n\n# 不支持的元素\n- 表情(face/emoji/bface/mface)段在入站时会被丢弃,不要期望看到 QQ 原生表情\n- 不支持打字状态提示(send_typing 为 no-op)";
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
          默认 Session 模式
          <select v-model="cfg.group_session_mode">
            <option value="shared">共享会话（群内所有人共用）</option>
            <option value="per_user">独立会话（每用户独立）</option>
          </select>
        </label>
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
          <input type="checkbox" v-model="cfg.group_keep_mention" />
          <span>保留 @bot 段（触发后不移除，如果@bot出现在开头，会导致/命令无法被解析）</span>
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
        <label>
          <input type="checkbox" v-model="cfg.group_auto_join" />
          <span>自动接受加群请求</span>
        </label>
        <label>
          媒体大小限制 (MB)
          <input type="number" v-model.number="cfg.media_max_mb" min="1" max="100" />
        </label>
        <label>
          媒体数量上限
          <input type="number" v-model.number="cfg.media_max_count" min="1" max="100" />
        </label>
        <label class="full">
          <input type="checkbox" v-model="cfg.media_limit_reject_enabled" />
          <span>媒体超出限制时回发提示（数量/大小超限或下载失败时,回复一条融合提示给用户）</span>
        </label>
        <label class="full">
          回发提示文案模板
          <textarea
            v-model="cfg.media_limit_reject_message"
            rows="3"
            placeholder="输入提示文案模板"
          ></textarea>
          <span class="hint">
            可用变量：<code>{skipped_count}</code> — 被跳过的媒体总数 ·
            <code>{max_count}</code> — 数量上限 ·
            <code>{max_mb}</code> — 单文件大小上限(MB) ·
            <code>{details}</code> — 自动渲染的多行明细（每行 [图N]: 原因）
          </span>
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
          贴表情回应使用的表情 ID
          <input type="text" v-model="cfg.reaction_emoji_id" placeholder="76" />
          <span class="hint">QQ 表情编号（默认 76=👍）</span>
        </label>
      </div>
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

    <!-- 平台提示词 -->
    <div v-if="cfg" class="section">
      <h3>平台提示词 (Platform Hint)</h3>
      <p class="hint">注入到 LLM 系统提示中，告诉模型当前平台特性。群专属提示词非空时会替换此全局设置。</p>
      <textarea v-model="cfg.platform_hint" rows="8" class="hint-editor" placeholder="输入平台提示词..."></textarea>
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

      <table v-if="groups.length" class="group-table">
        <thead>
          <tr>
            <th>群号</th><th>群名</th><th>状态</th><th>Session</th><th>@bot</th><th>首@</th><th>关键词</th><th>操作</th>
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
            <td>{{ sessionModeLabel(g.session_mode) }}</td>
            <td>{{ g.require_mention === null ? '跟随全局' : (g.require_mention ? '是' : '否') }}</td>
            <td>{{ g.mention_first_only === null ? '跟随全局' : (g.mention_first_only ? '是' : '否') }}</td>
            <td>{{ g.trigger_keywords === null ? '跟随全局' : (g.trigger_keywords.length ? g.trigger_keywords.join(', ') : '禁用') }}</td>
            <td>
              <button @click="editGroup(g)" class="row-btn">编辑</button>
              <button @click="removeGroup(g.group_id)" class="row-btn danger">删除</button>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-else class="empty">暂无群配置，点击「从 OneBot 同步」或「添加群」</p>
    </div>

    <button @click="saveGlobal" :disabled="saving" class="save-btn save-main">
      {{ saving ? "保存中..." : "保存所有全局设置" }}
    </button>

    <!-- 群详情编辑弹窗 -->
    <div v-if="showEditor && editingGroup" class="modal-overlay" @click.self="showEditor = false">
      <div class="modal">
        <h3>{{ editingGroup.group_id ? "编辑群 " + editingGroup.group_id : '添加群' }}</h3>

        <label>
          群号
          <input v-model="editingGroup.group_id" :disabled="!!groups.find(g => g.group_id === editingGroup?.group_id)" placeholder="输入群号" />
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
          <select v-model="editingGroup.trigger_keywords">
            <option :value="null">跟随全局</option>
            <option :value="[]">自定义（见下）</option>
          </select>
        </label>

        <label v-if="editingGroup.trigger_keywords !== null">
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
          保留 @bot 段
          <select v-model="editingGroup.keep_mention">
            <option :value="null">跟随全局</option>
            <option :value="true">保留</option>
            <option :value="false">移除</option>
          </select>
        </label>
        <span class="hint" v-if="editingGroup.keep_mention === true">
          ⚠️ 开启后保留 @bot 段, 但 @bot /指令 将无法被识别
        </span>

        <label>
          Session 模式
          <select v-model="editingGroup.session_mode">
            <option value="default">跟随全局</option>
            <option value="shared">共享会话</option>
            <option value="per_user">独立会话</option>
          </select>
        </label>

        <label>
          媒体限制 (MB，空=跟随全局)
          <input type="number" v-model.number="editingGroup.media_max_mb" min="1" max="100" placeholder="留空跟随全局" />
        </label>

        <label>
          媒体数量上限（空=跟随全局）
          <input type="number" v-model.number="editingGroup.media_max_count" min="1" max="100" placeholder="留空跟随全局" />
        </label>

        <label>
          超出媒体限制时回发提示
          <select v-model="editingGroup.media_limit_reject_enabled">
            <option :value="null">跟随全局</option>
            <option :value="true">启用</option>
            <option :value="false">禁用</option>
          </select>
        </label>

        <label>
          群专属提示词（空=用全局 platform_hint）
          <textarea v-model="editingGroup.custom_prompt" rows="4" placeholder="为此群定制系统提示词，留空则使用全局设置"></textarea>
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

        <label>
          <input type="checkbox" v-model="editingGroup.welcome_enabled" />
          <span>新人入群欢迎</span>
        </label>
        <label v-if="editingGroup.welcome_enabled">
          欢迎消息
          <textarea v-model="editingGroup.welcome_message" rows="2" placeholder="欢迎 @新成员 加入本群！"></textarea>
        </label>

        <label>
          <input type="checkbox" v-model="editingGroup.auto_join" />
          <span>自动接受此群的加群请求</span>
        </label>

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

        <div class="modal-actions">
          <button @click="showEditor = false" class="cancel-btn">取消</button>
          <button @click="saveGroup" class="save-btn">保存</button>
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
label { display: block; margin-bottom: 0.75rem; font-weight: 500; font-size: 0.9rem; }
label.full { width: 100%; }
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

.save-btn { background: var(--primary); color: white; border: none; padding: 0.6rem 1.2rem; border-radius: 6px; cursor: pointer; font-size: 0.9rem; }
.save-btn:disabled { background: #ccc; cursor: not-allowed; }
.save-main { padding: 0.75rem 2rem; font-size: 1rem; }
.save-main:hover:not(:disabled) { background: var(--primary-dark); }
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
.loading { text-align: center; padding: 2rem; color: var(--text-muted, #666); }

.hint { display: block; font-size: 0.8rem; color: var(--text-muted); margin: 0.25rem 0 0; line-height: 1.5; }
.hint code { background: var(--bg); padding: 0.1rem 0.3rem; border-radius: 3px; font-size: 0.9em; }
</style>
