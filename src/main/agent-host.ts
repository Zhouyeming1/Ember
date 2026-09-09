import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import path from "node:path";
import type { AgentEvent, AgentSessionStats, AgentSnapshot, AgentStartOptions } from "../shared/types";
import { parseSkillCommands } from "../shared/skills";
import { killProcessTree } from "./process-tree";
import { drainUtf8Lines } from "./rpc-lines";

interface PendingRequest {
  resolve(value: unknown): void;
  reject(error: Error): void;
  timeout: NodeJS.Timeout;
}

const DEFAULT_RPC_TIMEOUT_MS = 45_000;
const LONG_RPC_TIMEOUT_MS = 30 * 60_000;
const LONG_RUNNING_REQUESTS = new Set([
  "prompt",
  "steer",
  "abort",
  "get_entries",
  "get_fork_messages",
  "get_messages",
  "get_session_stats",
  "fork",
  "compact",
]);

export class AgentHost {
  private child?: ChildProcessWithoutNullStreams;
  private lineBuffer = Buffer.alloc(0);
  private stderr = "";
  private requestId = 0;
  private pending = new Map<string, PendingRequest>();
  private static readonly STDERR_CAP = 200_000;

  constructor(
    private readonly emitEvent: (event: AgentEvent) => void,
    private readonly emitError: (message: string) => void,
  ) {}

  isRunning(): boolean {
    return Boolean(this.child && this.child.exitCode === null);
  }

  async snapshot(): Promise<AgentSnapshot> {
    const [state, messages] = await Promise.all([
      this.request<Record<string, unknown>>("get_state"),
      this.request<{ messages: unknown[] }>("get_messages"),
    ]);
    void this.emitSnapshotMeta();
    return {
      state,
      messages: messages.messages,
      models: [],
      thinkingLevels: [],
      skills: [],
    };
  }

  /** Non-blocking follow-up for models/skills/stats after first paint. */
  private async emitSnapshotMeta(): Promise<void> {
    try {
      const [models, thinkingLevels, stats, commands] = await Promise.all([
        this.request<{ models: AgentSnapshot["models"] }>("get_available_models"),
        this.request<{ levels: string[] }>("get_available_thinking_levels"),
        this.request<AgentSessionStats>("get_session_stats").catch(() => undefined),
        this.request<{
          commands: Array<{
            name: string;
            description?: string;
            source?: string;
            sourceInfo?: { path?: string; baseDir?: string };
          }>;
        }>("get_commands").catch(() => ({ commands: [] })),
      ]);
      this.emitEvent({
        type: "desktop_snapshot_meta",
        models: models.models,
        thinkingLevels: thinkingLevels.levels,
        skills: parseSkillCommands(commands.commands),
        ...(stats ? { stats } : {}),
      });
    } catch {
      // First paint already succeeded; meta is best-effort.
    }
  }

  async start(options: AgentStartOptions & {
    cwd: string;
    apiKey?: string;      // DeepSeek key：主进程读取后经 env 注入，不经渲染进程
    python?: string;      // Python 可执行文件（缺省按平台取 python / python3）
    pythonPath?: string;  // PYTHONPATH：指向含 emberpy 包的 runtime-py 目录
    visionConfig?: string;
    visionUploads?: string;
  }): Promise<AgentSnapshot> {
    await this.stop();
    // worker 是 Python 引擎：
    //   python -m emberpy.rpc --cwd ... --permission ... --sandbox ... --provider ...
    // 参数必须与 runtime-py/emberpy/rpc.py 的 CLI 保持一致。
    const python = options.python?.trim() || defaultPythonExecutable();
    const args = [
      "-m",
      "emberpy.rpc",
      "--cwd",
      options.cwd,
      "--permission",
      options.permission,
      "--sandbox",
      options.sandbox,
      "--provider",
      options.provider,
    ];
    if (options.model) args.push("--model", options.model);
    if (options.baseUrl) args.push("--base-url", options.baseUrl);
    if (options.sessionPath) args.push("--session", options.sessionPath);

    this.lineBuffer = Buffer.alloc(0);
    this.stderr = "";
    const env: NodeJS.ProcessEnv = {
      ...process.env,
      PYTHONUNBUFFERED: "1",
      PYTHONUTF8: "1",
      PYTHONIOENCODING: "utf-8",
    };
    const runtimePy = options.pythonPath?.trim();
    if (runtimePy) {
      // 保留外部已有的 PYTHONPATH，避免覆盖掉用户对额外包的搜索路径
      env.PYTHONPATH = [runtimePy, process.env.PYTHONPATH]
        .filter(Boolean)
        .join(path.delimiter);
    }
    // 密钥只进子进程环境，不落盘、不经渲染进程
    if (options.apiKey) env.DEEPSEEK_API_KEY = options.apiKey;
    if (options.baseUrl) env.DEEPSEEK_BASE_URL = options.baseUrl;

    const child = spawn(python, args, {
      cwd: options.cwd,
      env,
      detached: process.platform !== "win32",
      stdio: ["pipe", "pipe", "pipe"],
    });
    this.child = child;
    child.stdout.on("data", (chunk: Buffer) => this.handleChunk(chunk));
    child.stderr.on("data", (chunk: Buffer) => {
      this.stderr = `${this.stderr}${chunk.toString()}`.slice(-AgentHost.STDERR_CAP);
    });
    child.stdin.on("error", (error) => {
      // EPIPE when the RPC worker exits mid-write must not crash the Electron main process.
      if (this.child !== child) return;
      const detail = error instanceof Error ? error.message : String(error);
      if (!/EPIPE|ECONNRESET|broken pipe/i.test(detail)) this.emitError(detail);
    });
    child.once("error", (error) => {
      if (this.child !== child) return;
      this.child = undefined;
      if (child.pid !== undefined) killProcessTree(child.pid, "SIGTERM");
      this.handleExit(error);
    });
    child.once("exit", (code, signal) => {
      if (this.child !== child) return;
      this.child = undefined;
      // Worker may die before its own wipe; reap leftover shells/delegates.
      if (child.pid !== undefined) killProcessTree(child.pid, "SIGTERM");
      this.handleExit(new Error(`Agent stopped (code ${code ?? "unknown"}${signal ? `, ${signal}` : ""})`));
    });

    return this.snapshot();
  }

  async stop(): Promise<void> {
    const child = this.child;
    if (!child) return;
    this.child = undefined;
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timeout);
      pending.reject(new Error("Agent session closed"));
    }
    this.pending.clear();
    if (child.exitCode !== null || child.pid === undefined) return;
    // Kill the whole RPC tree (delegate explorers, shells, sandboxes) before the
    // desktop process exits — a plain child.kill() leaves detached orphans.
    killProcessTree(child.pid, "SIGTERM");
    await new Promise<void>((resolve) => {
      const timer = setTimeout(() => {
        if (child.exitCode === null && child.pid !== undefined) {
          killProcessTree(child.pid, "SIGKILL");
        }
        resolve();
      }, 2_000);
      child.once("exit", () => {
        clearTimeout(timer);
        resolve();
      });
    });
  }

  async request<T>(type: string, data: Record<string, unknown> = {}): Promise<T> {
    const child = this.child;
    if (!child || child.stdin.destroyed) throw new Error("No workspace session is active");
    const id = `desktop_${++this.requestId}`;
    const command = { ...data, type, id };
    return new Promise<T>((resolve, reject) => {
      const timeout = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`Ember did not respond to ${type}. ${this.stderr}`.trim()));
      }, timeoutForRequest(type));
      this.pending.set(id, {
        resolve: (value) => resolve(value as T),
        reject,
        timeout,
      });
      try {
        child.stdin.write(`${JSON.stringify(command)}\n`);
      } catch (error) {
        clearTimeout(timeout);
        this.pending.delete(id);
        reject(error instanceof Error ? error : new Error(String(error)));
      }
    });
  }

  async respondToUi(id: string, response: Record<string, unknown>): Promise<void> {
    const child = this.child;
    if (!child || child.stdin.destroyed) throw new Error("No workspace session is active");
    try {
      child.stdin.write(`${JSON.stringify({ type: "extension_ui_response", id, ...response })}\n`);
    } catch (error) {
      throw error instanceof Error ? error : new Error(String(error));
    }
  }

  private handleChunk(chunk: Buffer): void {
    const drained = drainUtf8Lines(this.lineBuffer, chunk);
    this.lineBuffer = Buffer.from(drained.rest);
    for (const line of drained.lines) this.handleLine(line);
  }

  private handleLine(line: string): void {
    let data: Record<string, unknown>;
    try {
      data = JSON.parse(line) as Record<string, unknown>;
    } catch {
      return;
    }
    if (data.type === "response" && typeof data.id === "string") {
      const pending = this.pending.get(data.id);
      if (!pending) return;
      clearTimeout(pending.timeout);
      this.pending.delete(data.id);
      if (data.success === false) pending.reject(new Error(String(data.error ?? "Ember command failed")));
      else pending.resolve(data.data);
      return;
    }
    if (typeof data.type === "string") this.emitEvent(data as AgentEvent);
  }

  private handleExit(error: Error): void {
    const detail = this.stderr.trim();
    const message = detail ? `${error.message}\n${detail}` : error.message;
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timeout);
      pending.reject(new Error(message));
    }
    this.pending.clear();
    this.emitError(message);
  }
}

function timeoutForRequest(type: string): number {
  return LONG_RUNNING_REQUESTS.has(type) ? LONG_RPC_TIMEOUT_MS : DEFAULT_RPC_TIMEOUT_MS;
}

/** 桌面端缺省的 Python：Windows 用 python，其余平台用 python3；可被 EMBERPY_PYTHON 覆盖。 */
function defaultPythonExecutable(): string {
  const configured = process.env.EMBERPY_PYTHON?.trim();
  if (configured) return configured;
  return process.platform === "win32" ? "python" : "python3";
}
