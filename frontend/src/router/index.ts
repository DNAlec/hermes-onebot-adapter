import { createRouter, createWebHistory } from "vue-router";
import Login from "../views/Login.vue";
import { getToken, clearToken } from "../api";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/login", name: "login", component: Login },
    { path: "/", name: "dashboard", component: () => import("../views/Dashboard.vue") },
    { path: "/connections", name: "connections", component: () => import("../views/ConnectionManagement.vue") },
    { path: "/chat", name: "chat", component: () => import("../views/Chat.vue") },
    { path: "/commands", name: "commands", component: () => import("../views/Commands.vue") },
    { path: "/tools", name: "tools", component: () => import("../views/Tools.vue") },
    { path: "/advanced", name: "advanced", component: () => import("../views/Advanced.vue") },
    { path: "/logs", name: "logs", component: () => import("../views/Logs.vue") },
  ],
});

router.beforeEach(async (to, _from, next) => {
  const token = getToken();
  if (to.name === "login") {
    if (token) {
      try {
        const resp = await fetch("/api/status", {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (resp.ok) {
          next({ path: "/" });
          return;
        }
      } catch {}
      clearToken();
      next();
    } else {
      next();
    }
  } else {
    if (!token) {
      next({ path: "/login", query: { redirect: to.fullPath } });
      return;
    }
    try {
      const resp = await fetch("/api/status", {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (resp.ok) {
        next();
      } else {
        clearToken();
        next({ path: "/login", query: { redirect: to.fullPath } });
      }
    } catch {
      clearToken();
      next({ path: "/login", query: { redirect: to.fullPath } });
    }
  }
});

export default router;
