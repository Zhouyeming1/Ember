/**
 * 桥接端到端测试：agent-host（TS）真的 spawn 一个 Python worker 跑 emberpy.rpc，
 * 验证 JSON-RPC over stdio 的响应匹配 + 事件原样转发是通的。
 *
 * 需要本机有 python 且能在 runtime-py 导入 emberpy，否则整组跳过。
 * prompt 用例故意把 baseUrl 指向 127.0.0.1:1（无服务、立即连不上），
 * 让模型请求离线失败——从而离线验证"失败也被转成界面可见的助手消息"，不发外网请求。
 */
import { describe, expect, it, afterAll } from "vitest";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import type { EmberThread } from "./ember-core";
import { AgentHost } from "./agent-host";

// 事件里我们只关心 message_start 的文本，用宽松类型收即可
type RawEvent = {
  type?: string;
  message?: {
    role?: string;
    content?: Array<{ type?: string; text?: string } | string>;
  };
};

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, "../..");
const runtimePyDir = path.join(repoRoot, "runtime-py");
const python =
  (process.env.EMBERPY_PYTHON || (process.platform === "win32" ? "python" : "python3")).trim() ||
  "python";

function messageText(event: RawEvent): string {
  const parts = Array.isArray(event.message?.content) ? event.message.content : [];
  return parts
    .map((part) => (typeof part === "string" ? part : (part?.text ?? "")))
    .join("");
}

function probePython(): boolean {
  if (!fs.existsSync(path.join(runtimePyDir, "emberpy", "rpc.py"))) return false;
  const result = spawnSync(python, ["-c", "import emberpy.rpc"], {
    encoding: "utf8",
    timeout: 20_000,
    env: { ...process.env, PYTHONPATH: runtimePyDir, PYTHONUTF8: "1" },
  });
  return result.status === 0;
}

const pythonOk = probePython();

describe.skipIf(!pythonOk)("agent-host spawns the Python engine", () => {
  const events: RawEvent[] = [];
  const errors: string[] = [];
  const host = new AgentHost(
    (event) => events.push(event as unknown as RawEvent),
    (message) => errors.push(message),
  );
  const workspace = fs.mkdtempSync(path.join(os.tmpdir(), "emberpy-bridge-"));
  let started = false;

  afterAll(async () => {
    if (started) await host.stop();
    fs.rmSync(workspace, { recursive: true, force: true });
  });

  it("spawns python worker and answers snapshot/model commands", async () => {
    const snapshot = await host.start({
      provider: "deepseek",
      permission: "auto",
      sandbox: "workspace-write",
      model: "deepseek-chat",
      cwd: workspace,
      apiKey: "sk-offline-test",
      baseUrl: "http://127.0.0.1:1", // 离线：让模型请求立即连不上，不走外网
      python,
      pythonPath: runtimePyDir,
    });
    started = true;
    expect(errors).toEqual([]);
    expect(snapshot.state.mode).toBe("auto");
    expect(snapshot.state.isStreaming).toBe(false);
    expect(snapshot.messages).toEqual([]);

    const models = await host.request<{ models: Array<{ id: string }> }>(
      "get_available_models",
    );
    expect(models.models.map((model) => model.id)).toContain("deepseek-chat");
  }, 60_000);

  it("streams prompt events to the end (agent_settled) over stdio", async () => {
    const resp = await host.request("prompt", { message: "你好" });
    expect(resp).toEqual({}); // prompt 命令的 data 是空对象

    // 等收尾事件（在 prompt 响应之后到达）
    const deadline = Date.now() + 15_000;
    while (
      !events.some((event) => event.type === "agent_settled") &&
      Date.now() < deadline
    ) {
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
    expect(events.some((event) => event.type === "agent_settled")).toBe(true);

    const user = events.find(
      (event) =>
        event.type === "message_start" &&
        event.message?.role === "user" &&
        messageText(event).includes("你好"),
    );
    expect(user).toBeTruthy();

    // 模型调用离线失败也要变成界面可见的助手消息，而不是悄悄死掉
    const assistant = events.find(
      (event) =>
        event.type === "message_start" &&
        event.message?.role === "assistant" &&
        messageText(event).includes("模型调用失败"),
    );
    expect(assistant).toBeTruthy();

    // TS 侧不应把 stderr/退出当错误抛出
    expect(errors).toEqual([]);
  }, 60_000);
});

/**
 * M3：python 会话跨进程持久化（进程契约）。
 *
 * 模型调用故意离线失败（127.0.0.1:1），因此对话只有 user 轮次可靠——但足够验证
 * "同一 session 文件跨 host 复用、历史在、能续跑"，这是 resume 的进程级契约。
 */
function anyMessageText(messages: unknown[], needle: string): boolean {
  return messages.some((raw) => {
    const msg = raw as { role?: string; content?: Array<{ text?: string } | string> };
    const parts = Array.isArray(msg.content) ? msg.content : [];
    return parts.some((part) =>
      typeof part === "string" ? part.includes(needle) : part.text?.includes(needle),
    );
  });
}

async function waitSettled(events: RawEvent[], timeoutMs = 20_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!events.some((event) => event.type === "agent_settled") && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
}

function pythonStart(
  host: AgentHost,
  workspace: string,
  extra: { sessionPath?: string } = {},
) {
  return host.start({
    provider: "deepseek",
    permission: "auto",
    sandbox: "workspace-write",
    model: "deepseek-chat",
    cwd: workspace,
    apiKey: "sk-offline-test",
    baseUrl: "http://127.0.0.1:1", // 离线
    python,
    pythonPath: runtimePyDir,
    ...extra,
  });
}

describe.skipIf(!pythonOk)("python session persists & resumes across hosts", () => {
  it("reopens the same session file and continues its history", async () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "emberpy-resume-"));
    const sessionFile = path.join(root, "threads", "s1.jsonl");
    fs.mkdirSync(path.dirname(sessionFile), { recursive: true });
    try {
      // host A：把第一轮写进 sessionFile 后"进程退出"
      const evA: RawEvent[] = [];
      const hostA = new AgentHost((event) => evA.push(event as unknown as RawEvent), () => {});
      const snapA = await pythonStart(hostA, root, { sessionPath: sessionFile });
      await hostA.request("prompt", { message: "重启后要还在" });
      await waitSettled(evA);
      await hostA.stop();
      expect(snapA.state.sessionFile).toBe(sessionFile);
      expect(fs.existsSync(sessionFile)).toBe(true);

      // host B：同一个 sessionFile 起来，历史在发消息前就该可见
      const evB: RawEvent[] = [];
      const hostB = new AgentHost((event) => evB.push(event as unknown as RawEvent), () => {});
      const snapB = await pythonStart(hostB, root, { sessionPath: sessionFile });
      const before = (await hostB.snapshot()).messages;
      expect(anyMessageText(before, "重启后要还在")).toBe(true);
      expect(snapB.state.sessionFile).toBe(sessionFile);

      // 再跑一轮：同一文件续写，消息里有旧 + 新
      await hostB.request("prompt", { message: "再跑一轮" });
      await waitSettled(evB);
      const after = (await hostB.snapshot()).messages;
      expect(anyMessageText(after, "重启后要还在")).toBe(true);
      expect(anyMessageText(after, "再跑一轮")).toBe(true);
      await hostB.stop();
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  }, 90_000);
});

describe.skipIf(!pythonOk)("python desktop auto-allocates a session file", () => {
  it("writes to the sessions env dir on first prompt, and no earlier", async () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "emberpy-autosess-"));
    const sessions = path.join(root, "sessions");
    process.env.EMBER_SESSIONS_DIR = sessions;
    const events: RawEvent[] = [];
    const host = new AgentHost(
      (event) => events.push(event as unknown as RawEvent),
      () => {},
    );
    try {
      const snap = await pythonStart(host, root);
      // 还没 prompt：不产生空文件、不污染侧栏
      expect(snap.state.sessionFile).toBeUndefined();
      expect(fs.existsSync(sessions)).toBe(false);

      await host.request("prompt", { message: "首个会话" });
      await waitSettled(events);
      const stats = await host.request<{ sessionFile?: string }>("get_session_stats");
      expect(stats.sessionFile).toBeTruthy();
      expect(path.dirname(stats.sessionFile!)).toBe(sessions);
      expect(fs.existsSync(stats.sessionFile!)).toBe(true);

      // 文件首行是 Ember 索引器能识别的 session 头
      const first = fs
        .readFileSync(stats.sessionFile!, "utf8")
        .split("\n")
        .filter(Boolean)[0];
      expect((JSON.parse(first) as { type?: string }).type).toBe("session");
    } finally {
      delete process.env.EMBER_SESSIONS_DIR;
      await host.stop();
      fs.rmSync(root, { recursive: true, force: true });
    }
  }, 60_000);
});

describe("ember indexer lists a python-written session (best-effort)", () => {
  it("listEmberThreads sees the flat JSONL the engine wrote", async () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "emberpy-indexer-"));
    const home = path.join(root, "home");
    const sessions = path.join(home, "sessions");
    // 索引器与 python 引擎都指到同一个临时目录，绝不动真机 ~/.ember
    process.env.EMBER_HOME = home;
    process.env.EMBER_SESSIONS_DIR = sessions;
    process.env.EMBER_SQLITE_HOME = home;

    // 索引器依赖 node:sqlite 等运行能力，导入失败就整体静默跳过，不阻塞其它用例
    let listEmberThreads: ((options?: { cwd?: string }) => Promise<EmberThread[]>) | undefined;
    try {
      const core = await import("./ember-core");
      listEmberThreads = core.listEmberThreads;
    } catch {
      return;
    }

    const events: RawEvent[] = [];
    const host = new AgentHost(
      (event) => events.push(event as unknown as RawEvent),
      () => {},
    );
    try {
      await pythonStart(host, root);
      await host.request("prompt", { message: "索引你好" });
      await waitSettled(events);
      await host.stop();

      const threads = await listEmberThreads!();
      const mine = threads.filter(
        (thread) => thread.messageCount >= 1 && thread.title.includes("索引你好"),
      );
      expect(mine.length).toBeGreaterThan(0);
      expect(mine[0].cwd).toBe(root);
    } finally {
      delete process.env.EMBER_HOME;
      delete process.env.EMBER_SESSIONS_DIR;
      delete process.env.EMBER_SQLITE_HOME;
      await host.stop();
      fs.rmSync(root, { recursive: true, force: true });
    }
  }, 60_000);
});
