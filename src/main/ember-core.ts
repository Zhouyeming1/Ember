/**
 * ember-core：桌面主进程的本地数据面。
 *
 * 提供桌面端需要的全部数据能力：数据 home / 会话目录解析、会话分区与硬链、
 * SQLite 会话索引、文件型凭据存取、DeepSeek 配置、provider 元数据。命名统一
 * 成 Ember：
 *   - 数据 home 默认 ``~/.ember``（env ``EMBER_HOME`` 可覆盖）；
 *   - 会话目录 env ``EMBER_SESSIONS_DIR``（desktop 初始化时写，子进程引擎读取）；
 *   - 归档目录 ``~/.ember/archived_sessions``（env ``EMBER_ARCHIVED_SESSIONS_DIR``）；
 *   - 凭据/配置文件名（auth.json / settings.json / config.json / web-search.json
 *     / mcp.json …）即 renderer 消费契约，改动需两端同步。
 */
import { randomUUID } from "node:crypto";
import fsSync from "node:fs";
import fs from "node:fs/promises";
import type { FileHandle } from "node:fs/promises";
import { createRequire } from "node:module";
import os from "node:os";
import path from "node:path";
import process from "node:process";

// ---------------------------------------------------------------------------
// env 助手
// ---------------------------------------------------------------------------

/** 读一个 Ember 运行时环境变量（``EMBER_<name>``）。 */
function emberEnv(name: string): string | undefined {
  return process.env[`EMBER_${name}`];
}

function resolveHomePath(value: string): string {
  if (value === "~") return os.homedir();
  if (value.startsWith(`~${path.sep}`))
    return path.join(os.homedir(), value.slice(2));
  return path.resolve(value);
}

// ---------------------------------------------------------------------------
// home / 会话目录
// ---------------------------------------------------------------------------

export function getEmberHome(): string {
  return resolveHomePath(emberEnv("HOME") ?? path.join(os.homedir(), ".ember"));
}

export function getEmberSessionsDir(): string {
  return resolveHomePath(
    emberEnv("SESSIONS_DIR") ?? path.join(getEmberHome(), "sessions"),
  );
}

export function getEmberArchivedSessionsDir(): string {
  return resolveHomePath(
    emberEnv("ARCHIVED_SESSIONS_DIR") ??
      path.join(getEmberHome(), "archived_sessions"),
  );
}

/**
 * 初始化数据 home：建目录、把会话目录 env 写进子进程环境（引擎据此落盘会话）。
 * 返回 home 绝对路径。
 */
export async function initializeEmberHome(): Promise<string> {
  const home = getEmberHome();
  const sessions = getEmberSessionsDir();
  // 子进程（emberpy 引擎）先读 EMBER_SESSIONS_DIR；未设时引擎默认 ~/.ember/sessions，
  // 因此即便不设 env 两端默认也对齐。显式写上是保证 EMBER_HOME 覆盖时也一致。
  process.env.EMBER_SESSIONS_DIR = sessions;
  await fs.mkdir(home, { recursive: true, mode: 0o700 });
  await fs.chmod(home, 0o700).catch(() => undefined);
  await fs.mkdir(sessions, { recursive: true, mode: 0o700 });
  await fs.chmod(sessions, 0o700).catch(() => undefined);
  await fs.mkdir(getEmberArchivedSessionsDir(), { recursive: true, mode: 0o700 });
  await fs
    .chmod(getEmberArchivedSessionsDir(), 0o700)
    .catch(() => undefined);
  await ensureWebSearchDefaults(home);
  await partitionExistingSessions(sessions);
  return home;
}

/** 桌面端跳过 TUI curator；已有配置则不覆盖。 */
async function ensureWebSearchDefaults(home: string): Promise<void> {
  const file = path.join(home, "web-search.json");
  try {
    await fs.access(file);
  } catch {
    await fs.writeFile(
      file,
      `${JSON.stringify({ workflow: "auto-summary" }, null, 2)}\n`,
      { mode: 0o600 },
    );
  }
}

// ---------------------------------------------------------------------------
// 会话分区（平铺 <-> YYYY/MM/DD 硬链），与旧引擎行为保持一致
// ---------------------------------------------------------------------------

export interface PartitionedSessionPath {
  /** 平铺硬链：引擎 --session / resume 用的路径。 */
  runtimePath: string;
  /** 规范化归档路径：sessions/YYYY/MM/DD/*.jsonl。 */
  storagePath: string;
}

/**
 * 把平铺会话文件移进日期分区并保留平铺硬链。两个名字指向同一 inode：
 * 运行时完全兼容、内容不重复。
 */
export async function partitionSessionFile(
  file: string,
): Promise<PartitionedSessionPath> {
  const sessions = getEmberSessionsDir();
  const runtimePath = path.resolve(file);
  const relative = path.relative(sessions, runtimePath);
  if (relative.startsWith("..") || path.isAbsolute(relative)) {
    return { runtimePath, storagePath: runtimePath };
  }
  if (path.dirname(relative) !== ".") {
    return { runtimePath: path.join(sessions, path.basename(file)), storagePath: runtimePath };
  }
  const stat = await fs.stat(runtimePath);
  const timestamp = await sessionTimestamp(runtimePath, stat.mtime);
  const date = timestamp.toISOString().slice(0, 10).split("-");
  const storagePath = path.join(sessions, ...date, path.basename(runtimePath));
  await fs.mkdir(path.dirname(storagePath), { recursive: true, mode: 0o700 });
  await fs.chmod(path.dirname(storagePath), 0o700).catch(() => undefined);
  const existing = await statOrUndefined(storagePath);
  if (existing) {
    if (sameFile(stat, existing)) {
      await fs.chmod(runtimePath, 0o600).catch(() => undefined);
      return { runtimePath, storagePath };
    }
    // 路径撞上不同内容时绝不覆盖。
    return { runtimePath, storagePath: runtimePath };
  }
  try {
    await fs.rename(runtimePath, storagePath);
    try {
      await fs.link(storagePath, runtimePath);
    } catch (error) {
      await fs.rename(storagePath, runtimePath).catch(() => undefined);
      throw error;
    }
    await fs.chmod(storagePath, 0o600).catch(() => undefined);
    return { runtimePath, storagePath };
  } catch {
    // 部分文件系统不支持硬链；保留原平铺文件比复制/破坏续跑语义更安全。
    return { runtimePath, storagePath: runtimePath };
  }
}

/** 把 sessions 下所有平铺 jsonl 分区一遍。 */
export async function partitionExistingSessions(
  sessions: string = getEmberSessionsDir(),
): Promise<PartitionedSessionPath[]> {
  let entries: fsSync.Dirent[];
  try {
    entries = await fs.readdir(sessions, { withFileTypes: true });
  } catch (error) {
    if (isNodeError(error) && error.code === "ENOENT") return [];
    throw error;
  }
  const partitioned: PartitionedSessionPath[] = [];
  for (const entry of entries) {
    if (!entry.isFile() || !entry.name.endsWith(".jsonl")) continue;
    partitioned.push(await partitionSessionFile(path.join(sessions, entry.name)));
  }
  return partitioned;
}

/**
 * 引擎经平铺 runtime 路径续跑；若硬链丢失，从分区存储文件重建它，
 * 避免 open-session 读到空 transcript。
 */
export async function ensureSessionRuntimeLink(
  sessionPath: string,
  storagePath: string = sessionPath,
): Promise<string> {
  const runtime = path.resolve(sessionPath);
  const storage = path.resolve(storagePath);
  const storageStat = await statOrUndefined(storage);
  if (!storageStat) return runtime;
  const runtimeStat = await statOrUndefined(runtime);
  if (runtimeStat && sameFile(runtimeStat, storageStat)) return runtime;
  if (runtimeStat) {
    await fs.unlink(runtime).catch(() => undefined);
  } else {
    await fs
      .mkdir(path.dirname(runtime), { recursive: true, mode: 0o700 })
      .catch(() => undefined);
  }
  try {
    await fs.link(storage, runtime);
  } catch {
    return storage; // 硬链不可用时退回存储路径
  }
  await fs.chmod(runtime, 0o600).catch(() => undefined);
  return runtime;
}

async function sessionTimestamp(file: string, fallback: Date): Promise<Date> {
  let handle: FileHandle | undefined;
  try {
    handle = await fs.open(file, "r");
    const buffer = Buffer.alloc(16 * 1024);
    const { bytesRead } = await handle.read(buffer, 0, buffer.length, 0);
    const firstLine = buffer
      .subarray(0, bytesRead)
      .toString("utf8")
      .split("\n", 1)[0];
    if (!firstLine) return fallback;
    const header: unknown = JSON.parse(firstLine);
    if (isRecord(header) && typeof header.timestamp === "string") {
      const timestamp = new Date(header.timestamp);
      if (!Number.isNaN(timestamp.getTime())) return timestamp;
    }
    return fallback;
  } catch {
    return fallback;
  } finally {
    await handle?.close().catch(() => undefined);
  }
}

async function statOrUndefined(file: string): Promise<fsSync.Stats | undefined> {
  try {
    return await fs.stat(file);
  } catch (error) {
    if (isNodeError(error) && error.code === "ENOENT") return undefined;
    throw error;
  }
}

function sameFile(left: fsSync.Stats, right: fsSync.Stats): boolean {
  return left.dev === right.dev && left.ino === right.ino;
}

// ---------------------------------------------------------------------------
// provider 元数据 + 默认模型（与 renderer 的 ProviderStatus 形状 1:1）
// ---------------------------------------------------------------------------

export const SUPPORTED_PROVIDER_IDS = [
  "deepseek",
  "openai-codex",
  "openai",
  "anthropic",
  "openrouter",
  "zai",
  "kimi-coding",
  "minimax",
  "xai",
  "opencode-go",
] as const;

export type SupportedProviderId = (typeof SUPPORTED_PROVIDER_IDS)[number];

const DEFAULT_MODELS: Record<SupportedProviderId, string> = {
  deepseek: "deepseek-v4-flash",
  "openai-codex": "gpt-5.6-sol",
  openai: "gpt-5.6-sol",
  anthropic: "claude-opus-4-8",
  openrouter: "moonshotai/kimi-k2.6",
  zai: "glm-5.1",
  "kimi-coding": "kimi-for-coding",
  minimax: "MiniMax-M2.7",
  xai: "grok-4.5",
  "opencode-go": "kimi-k2.6",
};

const PROVIDER_NAMES: Record<SupportedProviderId, string> = {
  deepseek: "DeepSeek",
  "openai-codex": "OpenAI Codex (ChatGPT plan)",
  openai: "OpenAI API",
  anthropic: "Anthropic",
  openrouter: "OpenRouter",
  zai: "Z.AI Coding Plan",
  "kimi-coding": "Kimi For Coding",
  minimax: "MiniMax",
  xai: "xAI (Grok)",
  "opencode-go": "OpenCode Zen Go",
};

const PROVIDER_ENVIRONMENT_KEYS: Partial<Record<SupportedProviderId, string>> = {
  deepseek: "DEEPSEEK_API_KEY",
  openai: "OPENAI_API_KEY",
  anthropic: "ANTHROPIC_API_KEY",
  openrouter: "OPENROUTER_API_KEY",
  zai: "ZAI_API_KEY",
  "kimi-coding": "KIMI_API_KEY",
  minimax: "MINIMAX_API_KEY",
  xai: "XAI_API_KEY",
  "opencode-go": "OPENCODE_API_KEY",
};

const PROVIDER_ALIASES: Record<string, SupportedProviderId> = {
  grok: "xai",
  kimi: "kimi-coding",
};

export function isSupportedProviderId(value: string): value is SupportedProviderId {
  return (SUPPORTED_PROVIDER_IDS as readonly string[]).includes(value);
}

export function defaultModelForProvider(providerId: SupportedProviderId): string {
  return DEFAULT_MODELS[providerId];
}

export function providerDisplayName(providerId: SupportedProviderId): string {
  return PROVIDER_NAMES[providerId];
}

export function providerEnvironmentKey(
  providerId: SupportedProviderId,
): string | undefined {
  return PROVIDER_ENVIRONMENT_KEYS[providerId];
}

function normalizeProviderId(value: string): SupportedProviderId | undefined {
  const normalized = value.trim().toLocaleLowerCase("en-US");
  const providerId = PROVIDER_ALIASES[normalized] ?? normalized;
  return isSupportedProviderId(providerId) ? providerId : undefined;
}

export interface StoredModelSelection {
  providerId: SupportedProviderId;
  modelId?: string;
}

/** 读 settings.json 的 defaultProvider/defaultModel（供 auth 面板恢复上次选择）。 */
export function getStoredModelSelection(
  settingsPath: string = path.join(getEmberHome(), "settings.json"),
): StoredModelSelection | undefined {
  try {
    const settings: unknown = JSON.parse(fsSync.readFileSync(settingsPath, "utf8"));
    if (!isRecord(settings) || typeof settings.defaultProvider !== "string")
      return undefined;
    const providerId = normalizeProviderId(settings.defaultProvider);
    if (!providerId) return undefined;
    return {
      providerId,
      ...(typeof settings.defaultModel === "string" && settings.defaultModel.trim()
        ? { modelId: settings.defaultModel.trim() }
        : {}),
    };
  } catch {
    return undefined;
  }
}

// ---------------------------------------------------------------------------
// DeepSeek 配置（config.json，与凭据文件分离，迁移不错位）
// ---------------------------------------------------------------------------

export const DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com";

function emberConfigPath(): string {
  return emberEnv("CONFIG_PATH") ?? path.join(getEmberHome(), "config.json");
}

export function normalizeDeepSeekBaseUrl(value: string): string {
  const trimmed = value.trim().replace(/\/+$/, "");
  if (!trimmed) throw new Error("API base URL cannot be empty");
  let parsed: URL;
  try {
    parsed = new URL(trimmed);
  } catch {
    throw new Error("API base URL must be a valid http(s) URL");
  }
  if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
    throw new Error("API base URL must use http or https");
  }
  if (parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new Error("API base URL cannot contain credentials, query parameters, or a fragment");
  }
  return trimmed;
}

export function getStoredDeepSeekBaseUrl(
  configPath: string = emberConfigPath(),
): string | undefined {
  try {
    const settings: unknown = JSON.parse(fsSync.readFileSync(configPath, "utf8"));
    return baseUrlFromSettings(settings);
  } catch (error) {
    if (isNodeError(error) && error.code === "ENOENT") return undefined;
    if (error instanceof SyntaxError) {
      throw new Error(`Cannot parse Ember settings file: ${configPath}`);
    }
    throw error;
  }
}

export async function saveDeepSeekBaseUrl(
  baseUrl: string,
  configPath: string = emberConfigPath(),
): Promise<string> {
  const normalized = normalizeDeepSeekBaseUrl(baseUrl);
  const settings = await readSettings(configPath);
  settings.deepseek = { ...(settings.deepseek ?? {}), baseUrl: normalized };
  await persistSettings(settings, configPath);
  return normalized;
}

function baseUrlFromSettings(value: unknown): string | undefined {
  if (!isRecord(value) || !isRecord(value.deepseek)) return undefined;
  const baseUrl = value.deepseek.baseUrl;
  return typeof baseUrl === "string" ? normalizeDeepSeekBaseUrl(baseUrl) : undefined;
}

async function readSettings(settingsPath: string): Promise<Record<string, unknown>> {
  try {
    const parsed: unknown = JSON.parse(await fs.readFile(settingsPath, "utf8"));
    return isRecord(parsed) ? parsed : {};
  } catch (error) {
    if (isNodeError(error) && error.code === "ENOENT") return {};
    if (error instanceof SyntaxError) {
      throw new Error(`Cannot parse Ember settings file: ${settingsPath}`);
    }
    throw error;
  }
}

async function persistSettings(
  settings: Record<string, unknown>,
  settingsPath: string,
): Promise<void> {
  const directory = path.dirname(settingsPath);
  await fs.mkdir(directory, { recursive: true, mode: 0o700 });
  await fs.chmod(directory, 0o700).catch(() => undefined);
  const temporaryPath = `${settingsPath}.${process.pid}.${randomUUID()}.tmp`;
  try {
    await fs.writeFile(
      temporaryPath,
      `${JSON.stringify(settings, null, 2)}\n`,
      { mode: 0o600 },
    );
    await fs.chmod(temporaryPath, 0o600);
    await fs.rename(temporaryPath, settingsPath);
    await fs.chmod(settingsPath, 0o600);
  } finally {
    await fs.unlink(temporaryPath).catch(() => undefined);
  }
}

// ---------------------------------------------------------------------------
// 凭据存取（file 型：<home>/auth.json，0600；desktop 强制 file，免系统钥匙串）
// ---------------------------------------------------------------------------

const LOCK_STALE_MS = 30_000;
const LOCK_TIMEOUT_MS = 15_000;

function defaultAuthPath(): string {
  return path.join(getEmberHome(), "auth.json");
}

export interface Credential {
  type: "api_key" | "oauth";
  key?: string;
  access?: string;
  refresh?: string;
  expires?: number;
}

export interface CredentialInfo {
  providerId: string;
  type: Credential["type"];
}

/** owner-only JSON 凭据文件（auth.json），与旧引擎的读写语义一致。 */
export class FileCredentialStore {
  readonly authPath: string;

  constructor(authPath: string = defaultAuthPath()) {
    this.authPath = authPath;
  }

  async read(providerId: string): Promise<Credential | undefined> {
    return (await readCredentialData(this.authPath))[providerId];
  }

  async list(): Promise<readonly CredentialInfo[]> {
    const data = await readCredentialData(this.authPath);
    return Object.entries(data).map(([providerId, credential]) => ({
      providerId,
      type: credential.type,
    }));
  }

  async modify(
    providerId: string,
    fn: (current: Credential | undefined) => Promise<Credential | undefined>,
  ): Promise<Credential | undefined> {
    return withDirectoryLock(`${this.authPath}.lock`, async () => {
      const data = await readCredentialData(this.authPath);
      const current = data[providerId];
      const next = await fn(current);
      if (next === undefined) return current;
      data[providerId] = next;
      await writePrivateJson(this.authPath, data);
      return next;
    });
  }

  async delete(providerId: string): Promise<void> {
    await withDirectoryLock(`${this.authPath}.lock`, async () => {
      const data = await readCredentialData(this.authPath);
      if (!(providerId in data)) return;
      delete data[providerId];
      await writePrivateJson(this.authPath, data);
    });
  }
}

/**
 * 建凭据库。桌面端固定 file 型（index.ts 里强制 EMBER_CREDENTIALS_STORE=file），
 * 因此这里始终返回 FileCredentialStore——与旧版在"file 模式"下的行为一致。
 */
export async function createEmberCredentialStore(): Promise<FileCredentialStore> {
  return new FileCredentialStore();
}

/** 存一个 API key（provider 未指定 authPath 时落 <home>/auth.json）。 */
export async function saveProviderApiKey(
  providerId: ApiKeyProviderId | SupportedProviderId,
  key: string,
  authPath: string = defaultAuthPath(),
): Promise<void> {
  const trimmed = key.trim();
  if (!trimmed)
    throw new Error(`${providerDisplayName(providerId)} API key cannot be empty`);
  const store = new FileCredentialStore(authPath);
  await store.modify(providerId, async () => ({ type: "api_key", key: trimmed }));
}

export async function removeStoredProviderCredential(
  providerId: SupportedProviderId,
  authPath: string = defaultAuthPath(),
): Promise<boolean> {
  const store = new FileCredentialStore(authPath);
  if (!(await store.read(providerId))) return false;
  await store.delete(providerId);
  return true;
}

async function readCredentialData(
  authPath: string,
): Promise<Record<string, Credential>> {
  try {
    const parsed: unknown = JSON.parse(await fs.readFile(authPath, "utf8"));
    if (!isRecord(parsed)) return {};
    return Object.fromEntries(
      Object.entries(parsed).filter(
        (entry): entry is [string, Credential] => isCredential(entry[1]),
      ),
    );
  } catch (error) {
    if (isNodeError(error) && error.code === "ENOENT") return {};
    if (error instanceof SyntaxError) {
      throw new Error(`Cannot parse Ember auth file: ${authPath}`);
    }
    throw error;
  }
}

async function writePrivateJson(file: string, value: unknown): Promise<void> {
  const directory = path.dirname(file);
  await fs.mkdir(directory, { recursive: true, mode: 0o700 });
  await fs.chmod(directory, 0o700).catch(() => undefined);
  const temporary = `${file}.${process.pid}.${randomUUID()}.tmp`;
  try {
    await fs.writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, {
      mode: 0o600,
    });
    await fs.chmod(temporary, 0o600);
    await fs.rename(temporary, file);
    await fs.chmod(file, 0o600);
  } finally {
    await fs.unlink(temporary).catch(() => undefined);
  }
}

async function withDirectoryLock<T>(
  lockPath: string,
  task: () => Promise<T>,
): Promise<T> {
  await fs.mkdir(path.dirname(lockPath), { recursive: true, mode: 0o700 });
  const deadline = Date.now() + LOCK_TIMEOUT_MS;
  while (true) {
    try {
      await fs.mkdir(lockPath, { mode: 0o700 });
      break;
    } catch (error) {
      if (!isNodeError(error) || error.code !== "EEXIST") throw error;
      const stale = await fs
        .stat(lockPath)
        .then((stat) => Date.now() - stat.mtimeMs > LOCK_STALE_MS, () => false);
      if (stale) {
        await fs.rmdir(lockPath).catch(() => undefined);
        continue;
      }
      if (Date.now() >= deadline)
        throw new Error(`Timed out waiting for credential lock: ${lockPath}`);
      await new Promise((resolve) => setTimeout(resolve, 75));
    }
  }
  try {
    return await task();
  } finally {
    await fs.rmdir(lockPath).catch(() => undefined);
  }
}

function isCredential(value: unknown): value is Credential {
  if (!isRecord(value)) return false;
  if (value.type === "api_key") {
    return value.key === undefined || typeof value.key === "string";
  }
  return (
    value.type === "oauth" &&
    typeof value.access === "string" &&
    typeof value.refresh === "string" &&
    typeof value.expires === "number"
  );
}

// ---------------------------------------------------------------------------
// SQLite 会话索引（node:sqlite，惰性加载）；JSONL 仍是 transcript 唯一真源
// ---------------------------------------------------------------------------

const require = createRequire(import.meta.url);

interface SqliteStatement {
  get(...params: unknown[]): unknown;
  all(...params: unknown[]): unknown[];
  run(...params: unknown[]): { changes: number };
}

interface SqliteDatabase {
  exec(sql: string): void;
  prepare(sql: string): SqliteStatement;
  close(): void;
}

export function getEmberStatePath(): string {
  // EMBER_SQLITE_HOME 覆盖的是"放 state.sqlite 的目录"。
  return path.join(
    resolveHomePath(emberEnv("SQLITE_HOME") ?? getEmberHome()),
    "state.sqlite",
  );
}

export interface EmberThread {
  id: string;
  sessionPath: string;
  storagePath: string;
  cwd: string;
  title: string;
  preview?: string;
  provider?: string;
  model?: string;
  createdAt: string;
  updatedAt: string;
  messageCount: number;
  pinned: boolean;
  archived: boolean;
}

export interface ListThreadOptions {
  cwd?: string;
  includeArchived?: boolean;
}

interface ThreadRow {
  id: string;
  session_path: string;
  storage_path: string;
  cwd: string;
  title: string;
  preview: string | null;
  provider: string | null;
  model: string | null;
  created_at: number;
  updated_at: number;
  message_count: number;
  pinned: number;
  archived: number;
  file_size: number;
  file_mtime_ms: number;
}

export class EmberStateStore {
  readonly statePath: string;
  private readonly database: SqliteDatabase;
  private readonly findByPath: SqliteStatement;

  constructor(statePath: string = getEmberStatePath()) {
    this.statePath = statePath;
    if (statePath !== ":memory:") {
      fsSync.mkdirSync(path.dirname(statePath), { recursive: true, mode: 0o700 });
    }
    const { DatabaseSync: SQLiteDatabase } = require("node:sqlite") as {
      DatabaseSync: new (file: string) => SqliteDatabase;
    };
    this.database = new SQLiteDatabase(statePath);
    this.database.exec("PRAGMA journal_mode = WAL");
    this.database.exec("PRAGMA synchronous = NORMAL");
    this.database.exec("PRAGMA busy_timeout = 5000");
    this.database.exec(`
      CREATE TABLE IF NOT EXISTS threads (
        id TEXT PRIMARY KEY,
        session_path TEXT NOT NULL,
        storage_path TEXT NOT NULL,
        cwd TEXT NOT NULL,
        title TEXT NOT NULL,
        preview TEXT,
        provider TEXT,
        model TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        message_count INTEGER NOT NULL DEFAULT 0,
        pinned INTEGER NOT NULL DEFAULT 0,
        archived INTEGER NOT NULL DEFAULT 0,
        file_size INTEGER NOT NULL DEFAULT 0,
        file_mtime_ms REAL NOT NULL DEFAULT 0
      );
      CREATE INDEX IF NOT EXISTS threads_updated_at_idx ON threads(archived, pinned DESC, updated_at DESC);
      CREATE INDEX IF NOT EXISTS threads_cwd_idx ON threads(cwd, archived, updated_at DESC);
      CREATE INDEX IF NOT EXISTS threads_storage_path_idx ON threads(storage_path);
      PRAGMA user_version = 1;
    `);
    if (statePath !== ":memory:")
      fsSync.chmodSync(statePath, 0o600);
    this.findByPath = this.database.prepare(
      "SELECT * FROM threads WHERE session_path = ? OR storage_path = ? LIMIT 1",
    );
  }

  close(): void {
    this.database.close();
  }

  async refresh(): Promise<void> {
    const seen = new Set<string>();
    for (const file of await directJsonlFiles(getEmberSessionsDir())) {
      const partitioned = await partitionSessionFile(file);
      seen.add(partitioned.storagePath);
      await this.indexFile(partitioned.runtimePath, partitioned.storagePath, false);
    }
    for (const file of await recursiveJsonlFiles(getEmberArchivedSessionsDir())) {
      seen.add(file);
      await this.indexFile(file, file, true);
    }
    const rows = this.database
      .prepare("SELECT id, storage_path FROM threads")
      .all() as Array<{ id: string; storage_path: string }>;
    const remove = this.database.prepare("DELETE FROM threads WHERE id = ?");
    for (const row of rows) {
      if (!seen.has(row.storage_path)) remove.run(row.id);
    }
  }

  async indexSession(file: string): Promise<EmberThread | undefined> {
    const partitioned = await partitionSessionFile(file);
    return this.indexFile(partitioned.runtimePath, partitioned.storagePath, false);
  }

  list(options: ListThreadOptions = {}): EmberThread[] {
    const where: string[] = [];
    const parameters: unknown[] = [];
    if (!options.includeArchived) where.push("archived = 0");
    if (options.cwd) {
      where.push("cwd = ?");
      parameters.push(path.resolve(options.cwd));
    }
    const query = `SELECT * FROM threads${where.length > 0 ? ` WHERE ${where.join(" AND ")}` : ""} ORDER BY pinned DESC, created_at DESC`;
    return (this.database.prepare(query).all(...parameters) as ThreadRow[]).map(
      rowToThread,
    );
  }

  get(id: string): EmberThread | undefined {
    const row = this.database
      .prepare("SELECT * FROM threads WHERE id = ?")
      .get(id) as ThreadRow | undefined;
    return row ? rowToThread(row) : undefined;
  }

  setPinned(id: string, pinned: boolean): boolean {
    return (
      this.database
        .prepare("UPDATE threads SET pinned = ? WHERE id = ?")
        .run(pinned ? 1 : 0, id).changes > 0
    );
  }

  async archive(id: string): Promise<EmberThread | undefined> {
    const current = this.get(id);
    if (!current || current.archived) return current;
    const dateParts = current.createdAt.slice(0, 10).split("-");
    const target = path.join(
      getEmberArchivedSessionsDir(),
      ...dateParts,
      path.basename(current.storagePath),
    );
    await fs.mkdir(path.dirname(target), { recursive: true, mode: 0o700 });
    await fs.chmod(path.dirname(target), 0o700).catch(() => undefined);
    let compatibilityLinkRemoved = false;
    if (current.sessionPath !== current.storagePath) {
      await fs.unlink(current.sessionPath).catch((error) => {
        if (!isNodeError(error) || error.code !== "ENOENT") throw error;
      });
      compatibilityLinkRemoved = true;
    }
    try {
      await fs.rename(current.storagePath, target);
    } catch (error) {
      if (compatibilityLinkRemoved) {
        await fs.link(current.storagePath, current.sessionPath).catch(() => undefined);
      }
      throw error;
    }
    this.database
      .prepare(
        "UPDATE threads SET session_path = ?, storage_path = ?, archived = 1, updated_at = ? WHERE id = ?",
      )
      .run(target, target, Date.now(), id);
    return this.get(id);
  }

  async unarchive(id: string): Promise<EmberThread | undefined> {
    const current = this.get(id);
    if (!current || !current.archived) return current;
    const dateParts = current.createdAt.slice(0, 10).split("-");
    const storagePath = path.join(
      getEmberSessionsDir(),
      ...dateParts,
      path.basename(current.storagePath),
    );
    const runtimePath = path.join(
      getEmberSessionsDir(),
      path.basename(current.storagePath),
    );
    await fs.mkdir(path.dirname(storagePath), { recursive: true, mode: 0o700 });
    await fs.rename(current.storagePath, storagePath);
    try {
      await fs.link(storagePath, runtimePath);
    } catch (error) {
      await fs.rename(storagePath, current.storagePath).catch(() => undefined);
      throw error;
    }
    await fs.chmod(storagePath, 0o600).catch(() => undefined);
    this.database
      .prepare(
        "UPDATE threads SET session_path = ?, storage_path = ?, archived = 0, updated_at = ? WHERE id = ?",
      )
      .run(runtimePath, storagePath, Date.now(), id);
    return this.get(id);
  }

  private async indexFile(
    sessionPath: string,
    storagePath: string,
    archived: boolean,
  ): Promise<EmberThread | undefined> {
    let stat: fsSync.Stats;
    try {
      stat = await fs.stat(storagePath);
    } catch (error) {
      if (isNodeError(error) && error.code === "ENOENT") return undefined;
      throw error;
    }
    const cached = this.findByPath.get(sessionPath, storagePath) as
      | ThreadRow
      | undefined;
    if (
      cached &&
      cached.file_size === stat.size &&
      cached.file_mtime_ms === stat.mtimeMs &&
      Boolean(cached.archived) === archived
    ) {
      return rowToThread(cached);
    }
    const parsed = await parseSession(storagePath, stat);
    if (!parsed) return undefined;
    this.database
      .prepare(
        `
        INSERT INTO threads (
          id, session_path, storage_path, cwd, title, preview, provider, model,
          created_at, updated_at, message_count, pinned, archived, file_size, file_mtime_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          session_path = excluded.session_path,
          storage_path = excluded.storage_path,
          cwd = excluded.cwd,
          title = excluded.title,
          preview = excluded.preview,
          provider = excluded.provider,
          model = excluded.model,
          created_at = excluded.created_at,
          updated_at = excluded.updated_at,
          message_count = excluded.message_count,
          archived = excluded.archived,
          file_size = excluded.file_size,
          file_mtime_ms = excluded.file_mtime_ms
      `,
      )
      .run(
        parsed.id,
        sessionPath,
        storagePath,
        parsed.cwd,
        parsed.title,
        parsed.preview ?? null,
        parsed.provider ?? null,
        parsed.model ?? null,
        parsed.createdAt,
        stat.mtimeMs,
        parsed.messageCount,
        archived ? 1 : 0,
        stat.size,
        stat.mtimeMs,
      );
    return this.get(parsed.id);
  }
}

export async function listEmberThreads(
  options: ListThreadOptions = {},
): Promise<EmberThread[]> {
  const store = new EmberStateStore();
  try {
    await store.refresh();
    return store.list(options);
  } finally {
    store.close();
  }
}

export async function indexEmberSession(
  file: string,
): Promise<EmberThread | undefined> {
  const store = new EmberStateStore();
  try {
    return await store.indexSession(file);
  } finally {
    store.close();
  }
}

interface ParsedSession {
  id: string;
  cwd: string;
  title: string;
  preview?: string;
  provider?: string;
  model?: string;
  createdAt: number;
  messageCount: number;
}

async function parseSession(
  file: string,
  stat: fsSync.Stats,
): Promise<ParsedSession | undefined> {
  let raw: string;
  try {
    raw = await fs.readFile(file, "utf8");
  } catch (error) {
    if (isNodeError(error) && error.code === "ENOENT") return undefined;
    throw error;
  }
  const lines = raw.split("\n").filter(Boolean);
  const header = lines[0] ? parseLine(lines[0]) : undefined;
  if (header?.type !== "session" || typeof header.cwd !== "string")
    return undefined;
  let firstUserText: string | undefined;
  let preview: string | undefined;
  let namedTitle: string | undefined;
  let provider: string | undefined;
  let model: string | undefined;
  let messageCount = 0;
  for (const line of lines.slice(1)) {
    const entry = parseLine(line);
    if (!entry) continue;
    if (entry.type === "model_change") {
      if (typeof entry.provider === "string") provider = entry.provider;
      if (typeof entry.modelId === "string") model = entry.modelId;
    }
    if (entry.type === "session_info" && typeof entry.name === "string")
      namedTitle = entry.name;
    if (entry.type !== "message" || !isRecord(entry.message)) continue;
    messageCount += 1;
    const text = messageText(entry.message.content);
    if (!text) continue;
    preview = text;
    if (!firstUserText && entry.message.role === "user") firstUserText = text;
  }
  const createdAt =
    typeof header.timestamp === "string" &&
    Number.isFinite(Date.parse(header.timestamp))
      ? Date.parse(header.timestamp)
      : stat.birthtimeMs || stat.mtimeMs;
  return {
    id: typeof header.id === "string" ? header.id : path.basename(file, ".jsonl"),
    cwd: path.resolve(header.cwd),
    title: crop(normalize(namedTitle ?? firstUserText ?? "New thread"), 96),
    ...(preview ? { preview: crop(preview, 240) } : {}),
    ...(provider ? { provider } : {}),
    ...(model ? { model } : {}),
    createdAt,
    messageCount,
  };
}

function rowToThread(row: ThreadRow): EmberThread {
  return {
    id: row.id,
    sessionPath: row.session_path,
    storagePath: row.storage_path,
    cwd: row.cwd,
    title: row.title,
    ...(row.preview ? { preview: row.preview } : {}),
    ...(row.provider ? { provider: row.provider } : {}),
    ...(row.model ? { model: row.model } : {}),
    createdAt: new Date(row.created_at).toISOString(),
    updatedAt: new Date(row.updated_at).toISOString(),
    messageCount: row.message_count,
    pinned: Boolean(row.pinned),
    archived: Boolean(row.archived),
  };
}

async function directJsonlFiles(directory: string): Promise<string[]> {
  try {
    return (await fs.readdir(directory, { withFileTypes: true }))
      .filter((entry) => entry.isFile() && entry.name.endsWith(".jsonl"))
      .map((entry) => path.join(directory, entry.name));
  } catch (error) {
    if (isNodeError(error) && error.code === "ENOENT") return [];
    throw error;
  }
}

async function recursiveJsonlFiles(directory: string): Promise<string[]> {
  let entries: fsSync.Dirent[];
  try {
    entries = await fs.readdir(directory, { withFileTypes: true });
  } catch (error) {
    if (isNodeError(error) && error.code === "ENOENT") return [];
    throw error;
  }
  const files: string[] = [];
  for (const entry of entries) {
    const child = path.join(directory, entry.name);
    if (entry.isDirectory()) files.push(...(await recursiveJsonlFiles(child)));
    else if (entry.isFile() && entry.name.endsWith(".jsonl")) files.push(child);
  }
  return files;
}

function parseLine(line: string): Record<string, unknown> | undefined {
  try {
    const parsed: unknown = JSON.parse(line);
    return isRecord(parsed) ? parsed : undefined;
  } catch {
    return undefined;
  }
}

function messageText(content: unknown): string | undefined {
  if (typeof content === "string") return normalize(content);
  if (!Array.isArray(content)) return undefined;
  const text = content
    .filter(isRecord)
    .filter((item) => item.type === "text" && typeof item.text === "string")
    .map((item) => item.text as string)
    .join("\n");
  return text ? normalize(text) : undefined;
}

function normalize(value: string): string {
  return value.replace(/\s+/gu, " ").trim();
}

function crop(value: string, length: number): string {
  return value.length > length ? `${value.slice(0, length - 1)}…` : value;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNodeError(error: unknown): error is NodeJS.ErrnoException {
  return error instanceof Error && "code" in error;
}

// ---------------------------------------------------------------------------
// 类型补全
// ---------------------------------------------------------------------------

/** 有 API-key 登录方式的 provider（auth 面板读/写 key 的对象）。 */
export type ApiKeyProviderId = Exclude<SupportedProviderId, "openai-codex">;
