<script setup lang="ts">
import { ref, onMounted, onUnmounted, nextTick, computed } from "vue";
import { getLogs } from "../api";

const logs = ref<string[]>([]);
const autoScroll = ref(true);
const filter = ref("");
const levelFilter = ref<"all" | "info" | "warning" | "error">("all");
let timer: number | undefined;
const logContainer = ref<HTMLElement | null>(null);

const filteredLogs = computed(() => {
  return logs.value.filter(log => {
    // Text filter
    if (filter.value && !log.toLowerCase().includes(filter.value.toLowerCase())) {
      return false;
    }
    // Level filter
    if (levelFilter.value !== "all") {
      const level = log.split(" ")[2]?.toUpperCase();
      if (!level || !level.includes(levelFilter.value.toUpperCase())) {
        return false;
      }
    }
    return true;
  });
});

async function poll() {
  try {
    const el = logContainer.value;
    const prevScrollTop = el?.scrollTop ?? 0;

    logs.value = await getLogs();

    if (autoScroll.value) {
      await nextTick();
      scrollToBottom();
    } else if (el) {
      await nextTick();
      el.scrollTop = prevScrollTop;
    }
  } catch (e) {
    console.error("Failed to fetch logs:", e);
  }
}

function scrollToBottom() {
  if (logContainer.value) {
    logContainer.value.scrollTop = logContainer.value.scrollHeight;
  }
}

function getLogClass(log: string): string {
  const level = log.split(" ")[2]?.toUpperCase() || "";
  if (level.includes("ERROR")) return "log-error";
  if (level.includes("WARNING")) return "log-warning";
  if (level.includes("INFO")) return "log-info";
  if (level.includes("DEBUG")) return "log-debug";
  return "log-default";
}

onMounted(() => {
  poll();
  timer = window.setInterval(poll, 2000);
});

onUnmounted(() => {
  if (timer) clearInterval(timer);
});
</script>

<template>
  <div>
    <h2>日志</h2>
    
    <div class="controls">
      <div class="filter-group">
        <label>
          <span>文本过滤:</span>
          <input 
            v-model="filter" 
            type="text" 
            placeholder="输入关键词..."
            class="filter-input"
          />
        </label>
      </div>
      
      <div class="filter-group">
        <label>
          <span>级别:</span>
          <select v-model="levelFilter" class="level-select">
            <option value="all">全部</option>
            <option value="error">ERROR</option>
            <option value="warning">WARNING</option>
            <option value="info">INFO</option>
          </select>
        </label>
      </div>

      <div class="filter-group">
        <label class="checkbox-label">
          <input type="checkbox" v-model="autoScroll" />
          <span>自动滚动</span>
        </label>
      </div>

      <div class="filter-group">
        <button @click="scrollToBottom" class="scroll-btn">
          ↓ 滚动到底部
        </button>
      </div>
    </div>

    <div class="log-stats">
      共 {{ filteredLogs.length }} 条日志
      <span v-if="filteredLogs.length !== logs.length">
        (总计 {{ logs.length }} 条)
      </span>
    </div>

    <div ref="logContainer" class="log-container">
      <div v-if="filteredLogs.length === 0" class="no-logs">
        {{ logs.length === 0 ? "（暂无日志）" : "（无匹配日志）" }}
      </div>
      <div 
        v-for="(log, index) in filteredLogs" 
        :key="index"
        :class="['log-line', getLogClass(log)]"
      >
        {{ log }}
      </div>
    </div>
  </div>
</template>

<style scoped>
.controls {
  display: flex;
  gap: 1rem;
  margin-bottom: 1rem;
  flex-wrap: wrap;
  align-items: flex-end;
}

.filter-group {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}

.filter-group label {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.9rem;
  color: #666;
}

.filter-input {
  padding: 0.4rem 0.6rem;
  border: 1px solid #ccc;
  border-radius: 4px;
  font-size: 0.9rem;
  min-width: 200px;
}

.level-select {
  padding: 0.4rem 0.6rem;
  border: 1px solid #ccc;
  border-radius: 4px;
  font-size: 0.9rem;
  background: white;
}

.checkbox-label {
  cursor: pointer;
  user-select: none;
}

.checkbox-label input {
  cursor: pointer;
}

.scroll-btn {
  padding: 0.4rem 0.8rem;
  background: var(--primary);
  color: white;
  border: none;
  border-radius: 4px;
  cursor: pointer;
  font-size: 0.9rem;
  transition: background 0.2s;
}

.scroll-btn:hover {
  background: var(--primary-dark);
}

.log-stats {
  font-size: 0.85rem;
  color: #666;
  margin-bottom: 0.5rem;
  padding: 0.5rem;
  background: var(--bg);
  border-radius: 4px;
}

.log-container {
  background: #1e1e1e;
  color: #ddd;
  padding: 1rem;
  border-radius: 6px;
  max-height: 70vh;
  overflow-y: auto;
  font-family: 'Courier New', monospace;
  font-size: 0.85rem;
  line-height: 1.5;
}

.log-line {
  padding: 0.25rem 0.5rem;
  border-left: 3px solid transparent;
  margin-bottom: 0.25rem;
  word-break: break-all;
}

.log-error {
  background: rgba(220, 53, 69, 0.1);
  border-left-color: var(--danger);
  color: #ff6b6b;
}

.log-warning {
  background: rgba(255, 193, 7, 0.1);
  border-left-color: #ffc107;
  color: #ffd43b;
}

.log-info {
  border-left-color: #17a2b8;
  color: #74c0fc;
}

.log-debug {
  color: #868e96;
}

.log-default {
  color: #ddd;
}

.no-logs {
  text-align: center;
  padding: 2rem;
  color: #888;
  font-style: italic;
}

/* Scrollbar styling */
.log-container::-webkit-scrollbar {
  width: 8px;
}

.log-container::-webkit-scrollbar-track {
  background: #2d2d2d;
}

.log-container::-webkit-scrollbar-thumb {
  background: #555;
  border-radius: 4px;
}

.log-container::-webkit-scrollbar-thumb:hover {
  background: #666;
}
</style>
