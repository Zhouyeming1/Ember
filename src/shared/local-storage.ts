/**
 * localStorage 存取辅助。所有持久设置统一落在 `ember.*` 命名空间下。
 *
 * 失败静默（private mode / quota），按未命中处理。
 */

/** 读 key；失败返回 null。 */
export function readStorage(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    // private mode / quota 失败：按未命中处理。
    return null;
  }
}

/** 写 key。失败静默（private mode / quota）。 */
export function writeStorage(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* private mode / quota failures */
  }
}
