import { ref } from "vue";
import { getConfig, putConfig, type Config } from "../api";

// Module-level singleton: shared across all views that call useConfig().
// This eliminates the per-view stale-snapshot problem where saving in one
// view would overwrite fields edited in another view.
const cfg = ref<Config | null>(null);
const loaded = ref(false);
let loadPromise: Promise<void> | null = null;

export function useConfig() {
  async function load(force = false): Promise<void> {
    if (loaded.value && !force && cfg.value) return;
    // Coalesce concurrent loads into a single request.
    if (!loadPromise || force) {
      loadPromise = (async () => {
        cfg.value = await getConfig();
        loaded.value = true;
        loadPromise = null;
      })();
    }
    await loadPromise;
  }

  /** Persist an incremental patch of edited fields and merge the server
   * response back into the shared config state. Only the keys present in
   * *patch* are sent; other fields are left untouched server-side. */
  async function save(patch: Partial<Config>): Promise<Config> {
    const updated = await putConfig(patch);
    cfg.value = { ...cfg.value, ...updated } as Config;
    return updated;
  }

  return { cfg, loaded, load, save };
}