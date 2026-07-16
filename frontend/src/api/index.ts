import axios from "axios";

const TOKEN_KEY = "hermes_onebot_webui_token";

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) || "";
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
}

/** Exchange the raw webui_token for a signed session token via POST /api/login.
 * Uses fetch() directly (not the axios instance) so the 401 response
 * interceptor does not fire and cause a redirect loop on the login page.
 * On failure throws an Error with a `.status` property (401 or 429). */
export async function login(rawToken: string): Promise<number> {
  const resp = await fetch("/api/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token: rawToken }),
  });
  if (!resp.ok) {
    const err = new Error("login failed") as Error & { status: number; body?: any };
    err.status = resp.status;
    try { err.body = await resp.json(); } catch {}
    throw err;
  }
  const data = await resp.json();
  setToken(data.session_token);
  return data.expires_in as number;
}

export const api = axios.create({ baseURL: "/api" });

api.interceptors.request.use((config) => {
  const token = getToken();
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      clearToken();
      if (window.location.pathname !== "/login") {
        window.location.href = "/login";
      }
    }
    return Promise.reject(error);
  },
);

export interface Status {
  adapter_version: string;
  plugin_version: string | null;
  version_mismatch: boolean;
  onebot_connected: boolean;
  hermes_plugin_connected: boolean;
  onebot_mode: string;
  self_id: string;
  onebot_ws_port: number;
  hermes_ws_port: number;
  webui_port: number;
  hermes_group_sessions_per_user: boolean;
}

export interface GroupConfig {
  group_id: string;
  name: string;
  enabled: boolean;
  require_mention: boolean | null;
  mention_first_only: boolean | null;
  trigger_keywords: string[] | null;
  keyword_first_only: boolean | null;
  strip_first_mention: boolean | null;
  custom_prompt: string;
  admins: string[];
  group_user_filter_mode: string;
  group_user_list: string[];
  message_show_group_id: boolean | null;
  reaction_emoji_enabled: boolean | null;
  command_filter_enabled: boolean | null;
  command_filter_unknown: boolean | null;
  command_permissions: Record<string, string> | null;
}

export interface Config {
  onebot_mode: string;
  onebot_reverse_ws_port: number;
  onebot_reverse_ws_path: string;
  onebot_forward_ws_url: string;
  onebot_ws_token: string;
  self_id: string;
  group_require_mention: boolean;
  group_mention_first_only: boolean;
  group_trigger_keywords: string[];
  group_keyword_first_only: boolean;
  group_strip_first_mention: boolean;
  global_admins: string[];

  // ── 私聊设置 ──
  dm_user_filter_mode: string;
  dm_user_list: string[];
  groups: Record<string, GroupConfig>;
  global_channel_prompt: string;
  hermes_ws_port: number;
  hermes_ws_path: string;
  hermes_ws_token: string;
  hermes_install_dir: string;
  webui_port: number;
  webui_token?: string;
  webui_token_lifetime_hours: number;
  log_level: string;
  log_message_preview: number;
  log_file_enabled: boolean;
  log_file_dir: string;
  log_retention_days: number;
  message_show_group_id: boolean;
  seq_map_size: number;
  reaction_emoji_enabled: boolean;
  reaction_emoji_id: string;
  reaction_emoji_id_queued: string;
  // ── 发送去重 ──
  send_dedup_enabled: boolean;
  send_dedup_ttl_seconds: number;
  // ── 群聊排队 ──
  event_queue_enabled: boolean;
  event_queue_max_per_chat: number;
  event_queue_idle_timeout: number;
  // ── 媒体投递 ──
  media_delivery_mode: string;
  // ── /指令过滤 ──
  command_filter_enabled: boolean;
  command_filter_unknown: boolean;
  command_permissions: Record<string, string>;
  command_reject_message: string;
}

export const getStatus = () => api.get<Status>("/status").then((r) => r.data);
export const getConfig = () => api.get<Config>("/config").then((r) => r.data);
export const putConfig = (cfg: Partial<Config>) => api.put<Config>("/config", cfg).then((r) => r.data);
export interface HermesDirStatus {
  hermes_dir: string;
  exists: boolean;
}

export const checkHermesDir = () =>
  api.get<HermesDirStatus>("/hermes_dir_status").then((r) => r.data);
export const installPlugin = (hermes_install_dir: string) =>
  api.post("/install_plugin", { hermes_install_dir }).then((r) => r.data);
export const uninstallPlugin = (hermes_install_dir: string) =>
  api.post("/uninstall_plugin", { hermes_install_dir }).then((r) => r.data);
export const getLogs = () => api.get<{ logs: string[] }>("/logs").then((r) => r.data.logs);
export const getGroups = () => api.get<{ groups: GroupConfig[] }>("/groups").then((r) => r.data.groups);
export const putGroup = (groupId: string, cfg: Partial<GroupConfig>) =>
  api.put(`/groups/${groupId}`, cfg).then((r) => r.data);
export const deleteGroup = (groupId: string) =>
  api.delete(`/groups/${groupId}`).then((r) => r.data);
export const syncGroups = () => api.post("/groups/sync").then((r) => r.data);

// ── /command filter ──

export interface CommandInfo {
  name: string;
  description: string;
  source: string;
  aliases: string[];
  args_hint: string;
}

export const getCommands = () =>
  api.get<{ commands: CommandInfo[] }>("/commands").then((r) => r.data.commands);
export const refreshCommands = () =>
  api.post("/commands/refresh").then((r) => r.data);

// ── Hermes tools management (OneBot platform) ──

export interface ToolsetInfo {
  key: string;
  label: string;
  description: string;
  tools: string[];
  is_plugin: boolean;
}

export interface McpServerInfo {
  name: string;
  enabled: boolean;
}

export interface HermesToolsState {
  configurable: ToolsetInfo[];
  mcp_servers: McpServerInfo[];
  current_enabled: string[];
  hermes_dir_ok: boolean;
}

export const getHermesTools = () =>
  api.get<HermesToolsState>("/hermes_tools").then((r) => r.data);
export const putHermesTools = (payload: {
  toolsets: string[];
  mcp_servers: string[];
  no_mcp: boolean;
}) => api.put<{ ok: boolean; saved: string[] }>("/hermes_tools", payload).then((r) => r.data);
export const resetHermesTools = () =>
  api.post<{ ok: boolean }>("/hermes_tools/reset").then((r) => r.data);

// ── Hermes session-isolation mode (group_sessions_per_user) ──

export interface HermesMode {
  group_sessions_per_user: boolean;
  source: "plugin_report" | "hermes_config_yaml" | "default";
  plugin_connected: boolean;
}

export const getHermesMode = () =>
  api.get<HermesMode>("/hermes_mode").then((r) => r.data);
export const putHermesMode = (group_sessions_per_user: boolean) =>
  api.put<{ ok: boolean; written: boolean; restart_required: boolean; note: string }>(
    "/hermes_mode", { group_sessions_per_user },
  ).then((r) => r.data);
export const refreshHermesMode = () =>
  api.post<{ ok: boolean; note?: string; error?: string }>("/hermes_mode/refresh").then((r) => r.data);

// ── Version update check ──

export interface UpdateInfo {
  current_version: string;
  latest_version: string;
  has_update: boolean;
  changelog_url: string;
}

export const getUpdateCheck = () =>
  api.get<UpdateInfo>("/update_check").then((r) => r.data);
