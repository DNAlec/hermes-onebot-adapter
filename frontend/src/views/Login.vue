<script setup lang="ts">
import { ref } from "vue";
import { useRouter, useRoute } from "vue-router";
import { login } from "../api";

const router = useRouter();
const route = useRoute();
const tokenInput = ref("");
const error = ref("");
const loading = ref(false);

async function handleLogin() {
  if (!tokenInput.value.trim()) {
    error.value = "请输入 token";
    return;
  }
  loading.value = true;
  error.value = "";
  try {
    await login(tokenInput.value.trim());
    const redirect = (route.query.redirect as string) || "/";
    router.replace(redirect);
  } catch (e: any) {
    if (e?.status === 429) {
      const retry = e?.body?.retry_after;
      error.value = retry
        ? `登录失败次数过多,请 ${Math.ceil(retry / 60)} 分钟后重试`
        : "登录失败次数过多,请稍后重试";
    } else if (e?.status === 401) {
      error.value = "Token 无效,请检查后重试";
    } else {
      error.value = "登录失败,请稍后重试";
    }
    tokenInput.value = "";
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <div class="login-page">
    <div class="login-card">
      <div class="login-header">
        <h1>Hermes OneBot Adapter</h1>
      </div>
      <p class="login-subtitle">请输入 WebUI 鉴权 Token 登录</p>
      <form @submit.prevent="handleLogin">
        <input
          v-model="tokenInput"
          type="password"
          placeholder="输入 Token"
          class="token-input"
          autocomplete="current-password"
          :disabled="loading"
        />
        <div v-if="error" class="error-msg">{{ error }}</div>
        <button type="submit" class="login-btn" :disabled="loading">
          {{ loading ? "验证中..." : "登录" }}
        </button>
      </form>
      <p class="hint">
        Token 在适配器启动日志中打印,也可在
        <code>~/.onebot_adapter/config.json</code> 的 <code>webui_token</code> 字段查看。
      </p>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--bg);
}
.login-card {
  background: var(--card-bg);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 2.5rem 2rem;
  max-width: 400px;
  width: 90%;
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.06);
}
.login-header {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  margin-bottom: 0.5rem;
}
.login-header .logo { font-size: 1.8rem; }
.login-header h1 { font-size: 1.2rem; margin: 0; }
.login-subtitle {
  color: var(--text-muted);
  font-size: 0.9rem;
  margin: 0.5rem 0 1.5rem;
}
.token-input {
  width: 100%;
  padding: 0.7rem;
  border: 1px solid #ccc;
  border-radius: 6px;
  font-size: 0.95rem;
  margin-bottom: 0.75rem;
}
.token-input:focus {
  outline: none;
  border-color: var(--primary);
}
.error-msg {
  color: var(--danger);
  font-size: 0.85rem;
  margin-bottom: 0.75rem;
}
.login-btn {
  width: 100%;
  padding: 0.7rem;
  background: var(--primary);
  color: white;
  border: none;
  border-radius: 6px;
  cursor: pointer;
  font-size: 0.95rem;
}
.login-btn:disabled {
  background: #ccc;
  cursor: not-allowed;
}
.hint {
  margin-top: 1.25rem;
  font-size: 0.8rem;
  color: var(--text-muted);
  line-height: 1.5;
}
.hint code {
  background: #f4f4f4;
  padding: 0.1rem 0.3rem;
  border-radius: 3px;
  font-size: 0.78rem;
}
</style>
