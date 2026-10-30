/** JSON-lines client for the Python engine (designaround_engine.rpc). */
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { readFileSync } from "node:fs";
import { createInterface } from "node:readline";

export class EngineError extends Error {
  code: string;
  details: unknown;
  constructor(code: string, message: string, details?: unknown) {
    super(message);
    this.code = code;
    this.details = details;
  }
}

type Pending = { resolve: (v: any) => void; reject: (e: Error) => void };

export class Engine {
  private proc: ChildProcessWithoutNullStreams;
  private nextId = 1;
  private pending = new Map<number, Pending>();
  private ready: Promise<void>;
  private stderrTail = "";

  constructor(python = process.env.DA_PYTHON || "python3") {
    this.proc = spawn(python, ["-m", "designaround_engine.rpc"], { stdio: ["pipe", "pipe", "pipe"], env: process.env, windowsHide: true });
    this.proc.stderr.on("data", (chunk: Buffer) => {
      this.stderrTail = (this.stderrTail + chunk.toString()).slice(-4000);
    });
    const lines = createInterface({ input: this.proc.stdout });
    let readyResolve!: () => void;
    let readyReject!: (e: Error) => void;
    this.ready = new Promise((res, rej) => {
      readyResolve = res;
      readyReject = rej;
    });
    lines.on("line", (line) => {
      if (!line.trim()) return;
      let msg: any;
      try {
        msg = JSON.parse(line);
      } catch {
        return;
      }
      if (msg.ready) {
        readyResolve();
        return;
      }
      const waiter = this.pending.get(msg.id);
      if (!waiter) return;
      this.pending.delete(msg.id);
      if (msg.error) waiter.reject(new EngineError(msg.error.code, msg.error.message, msg.error.details));
      else waiter.resolve(msg.result);
    });
    this.proc.on("exit", (code) => {
      const err = new EngineError("engine_exit", `engine exited (${code}): ${this.stderrTail.slice(-800)}`);
      readyReject(err);
      for (const waiter of this.pending.values()) waiter.reject(err);
      this.pending.clear();
    });
  }

  async call<T = any>(method: string, params: Record<string, unknown> = {}): Promise<T> {
    await this.ready;
    const id = this.nextId++;
    const promise = new Promise<T>((resolve, reject) => this.pending.set(id, { resolve, reject }));
    this.proc.stdin.write(JSON.stringify({ id, method, params }) + "\n");
    return promise;
  }

  close(): void {
    this.proc.stdin.end();
    this.proc.kill();
  }
}

export function pngContent(path: string): { type: "image"; data: string; mimeType: string } {
  return { type: "image", data: readFileSync(path).toString("base64"), mimeType: "image/png" };
}
