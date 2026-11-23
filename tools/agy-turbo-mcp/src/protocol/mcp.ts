import type { Readable, Writable } from "node:stream";
import { fileURLToPath } from "node:url";

export interface McpToolDefinition {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
}

export type McpToolDefinitionProvider =
  | McpToolDefinition
  | (() => McpToolDefinition);

export interface McpClientInfo {
  name?: string;
  version?: string;
}

export interface McpToolResult {
  text: string;
  isError?: boolean;
}

export type McpToolHandler = (
  args: Record<string, unknown>,
) => Promise<McpToolResult>;

export interface RegisteredTool {
  definition: McpToolDefinitionProvider;
  handler: McpToolHandler;
}

interface JsonRpcRequest {
  jsonrpc?: string;
  id?: string | number | null;
  method?: string;
  params?: Record<string, unknown>;
}

interface JsonRpcResponse {
  jsonrpc: "2.0";
  id: string | number | null;
  result?: unknown;
  error?: { code: number; message: string; data?: unknown };
}

interface JsonRpcOutgoingRequest {
  jsonrpc: "2.0";
  id: string;
  method: string;
}

const SUPPORTED_PROTOCOL_VERSIONS = new Set([
  "2025-11-25",
  "2025-06-18",
  "2025-03-26",
  "2024-11-05",
]);
const DEFAULT_PROTOCOL_VERSION = "2025-03-26";

const ROOTS_REQUEST_TIMEOUT_MS = 1500;
const MAX_KNOWN_ROOTS_IDS = 64;

export class McpStdioServer {
  private readonly tools = new Map<string, RegisteredTool>();
  private readonly serverName: string;
  private readonly serverVersion: string;
  private inFlight = 0;
  private stdinEnded = false;
  private onDrainExit?: () => void;
  private readonly onWorkspaceRoots?: (paths: string[]) => void;
  private readonly onInitialize?: (info: {
    clientInfo?: McpClientInfo;
    protocolVersion: string;
  }) => void;
  private sendToClient?: (request: JsonRpcOutgoingRequest, useContentLength: boolean) => void;
  private clientSupportsRoots = false;
  private rootsRequestCount = 0;
  private lastAppliedRootsSeq = 0;
  private readonly pendingRoots = new Map<string, { seq: number; timer: NodeJS.Timeout }>();
  private readonly knownRootsIds = new Map<string, number>();
  private readonly rootsWaiters = new Set<{ minSeq: number; resolve: () => void }>();
  private activeOutgoingQueue: Array<() => void> | null = null;
  private readonly rootsTimeoutMs: number;

  constructor(options: {
    name: string;
    version: string;
    // Receives the client's workspace root directories (MCP roots).
    onWorkspaceRoots?: (paths: string[]) => void;
    // Receives client metadata during MCP initialize before tools/list is served.
    onInitialize?: (info: {
      clientInfo?: McpClientInfo;
      protocolVersion: string;
    }) => void;
    rootsTimeoutMs?: number;
  }) {
    this.serverName = options.name;
    this.serverVersion = options.version;
    this.onWorkspaceRoots = options.onWorkspaceRoots;
    this.onInitialize = options.onInitialize;
    this.rootsTimeoutMs =
      typeof options.rootsTimeoutMs === "number" &&
      Number.isFinite(options.rootsTimeoutMs) &&
      options.rootsTimeoutMs > 0
        ? Math.trunc(options.rootsTimeoutMs)
        : ROOTS_REQUEST_TIMEOUT_MS;
  }

  private flushRootsWaiters(): void {
    for (const waiter of this.rootsWaiters) {
      let stillWaiting = false;
      for (const pending of this.pendingRoots.values()) {
        if (pending.seq <= waiter.minSeq) {
          stillWaiting = true;
          break;
        }
      }
      if (!stillWaiting) {
        this.rootsWaiters.delete(waiter);
        waiter.resolve();
      }
    }
  }

  private settleAllPendingRoots(): void {
    for (const pending of this.pendingRoots.values()) {
      clearTimeout(pending.timer);
    }
    this.pendingRoots.clear();
    for (const waiter of this.rootsWaiters) {
      waiter.resolve();
    }
    this.rootsWaiters.clear();
  }

  private waitForRoots(): Promise<void> {
    if (this.pendingRoots.size === 0 || this.stdinEnded) {
      return Promise.resolve();
    }
    // If a roots/list request was queued earlier in the same JSON-RPC batch,
    // flush it now so awaiting it inside Promise.all cannot deadlock the batch.
    if (this.activeOutgoingQueue && this.activeOutgoingQueue.length > 0) {
      const tasks = this.activeOutgoingQueue.splice(0, this.activeOutgoingQueue.length);
      for (const task of tasks) {
        task();
      }
    }
    const minSeq = this.rootsRequestCount;
    return new Promise<void>((resolve) => {
      this.rootsWaiters.add({ minSeq, resolve });
    });
  }

  private pruneKnownRootsIds(): void {
    if (this.knownRootsIds.size <= MAX_KNOWN_ROOTS_IDS) return;
    for (const key of this.knownRootsIds.keys()) {
      if (this.knownRootsIds.size <= MAX_KNOWN_ROOTS_IDS) break;
      if (!this.pendingRoots.has(key)) {
        this.knownRootsIds.delete(key);
      }
    }
  }

  // Tool calls wait for the pending answer (bounded by rootsTimeoutMs and stdin EOF),
  // so the first call already sees the workspace roots without ever deadlocking.
  private requestRoots(useContentLength: boolean): void {
    if (
      this.stdinEnded ||
      !this.clientSupportsRoots ||
      !this.onWorkspaceRoots ||
      !this.sendToClient
    ) {
      return;
    }
    this.rootsRequestCount += 1;
    const seq = this.rootsRequestCount;
    const id = `agy-fast-roots-${seq}`;
    const timer = setTimeout(() => {
      if (this.pendingRoots.delete(id)) {
        this.pruneKnownRootsIds();
        this.flushRootsWaiters();
      }
    }, this.rootsTimeoutMs);
    timer.unref();
    this.pendingRoots.set(id, { seq, timer });
    this.knownRootsIds.set(id, seq);
    this.pruneKnownRootsIds();
    const send = this.sendToClient;
    const sendTask = () => {
      send({ jsonrpc: "2.0", id, method: "roots/list" }, useContentLength);
    };
    if (this.activeOutgoingQueue) {
      this.activeOutgoingQueue.push(sendTask);
    } else {
      sendTask();
    }
  }

  private receiveRoots(result: unknown): void {
    if (!result || typeof result !== "object") return;
    const rawRoots = (result as { roots?: unknown }).roots;
    if (!Array.isArray(rawRoots)) return;
    const paths: string[] = [];
    for (const root of rawRoots) {
      if (!root || typeof root !== "object") continue;
      const uri = (root as { uri?: unknown }).uri;
      if (typeof uri !== "string" || !/^file:/i.test(uri)) continue;
      try {
        paths.push(fileURLToPath(uri));
      } catch {
        // Ignore malformed or non-local file: URIs
      }
    }
    if (paths.length > 0) {
      try {
        this.onWorkspaceRoots?.(paths);
      } catch {
        // Never crash the MCP server if a client-reported root is inaccessible
      }
    }
  }

  registerTool(
    definition: McpToolDefinitionProvider,
    handler: McpToolHandler,
  ): void {
    const name = typeof definition === "function" ? definition().name : definition.name;
    this.tools.set(name, { definition, handler });
  }

  async handleMessage(
    raw: unknown,
    useContentLength = false,
  ): Promise<JsonRpcResponse | null> {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
      return {
        jsonrpc: "2.0",
        id: null,
        error: { code: -32600, message: "Invalid Request" },
      };
    }

    const msg = raw as JsonRpcRequest;
    const hasId =
      Object.prototype.hasOwnProperty.call(msg, "id") && msg.id !== undefined;
    const id = hasId ? (msg.id ?? null) : null;
    const method = typeof msg.method === "string" ? msg.method : "";

    if (!method && typeof id === "string" && this.knownRootsIds.has(id)) {
      const seq = this.knownRootsIds.get(id)!;
      this.knownRootsIds.delete(id);
      const pending = this.pendingRoots.get(id);
      if (pending) {
        clearTimeout(pending.timer);
        this.pendingRoots.delete(id);
      }
      const errField = (msg as { error?: unknown }).error;
      const hasError = errField !== undefined && errField !== null;
      try {
        if (!hasError && seq >= this.lastAppliedRootsSeq) {
          this.lastAppliedRootsSeq = seq;
          // Any older still-pending roots/list request is now superseded
          for (const [oldId, oldPending] of this.pendingRoots.entries()) {
            if (oldPending.seq < seq) {
              clearTimeout(oldPending.timer);
              this.pendingRoots.delete(oldId);
            }
          }
          this.receiveRoots((msg as { result?: unknown }).result);
        }
      } finally {
        this.pruneKnownRootsIds();
        this.flushRootsWaiters();
      }
      return null;
    }

    if (!method) {
      return {
        jsonrpc: "2.0",
        id,
        error: { code: -32600, message: "Invalid Request: missing method" },
      };
    }

    // Notifications (no id) must not produce a response
    if (!hasId) {
      if (method === "notifications/initialized" || method === "notifications/roots/list_changed") {
        this.requestRoots(useContentLength);
      }
      return null;
    }

    if (method === "initialize") {
      const requested =
        typeof msg.params?.protocolVersion === "string"
          ? msg.params.protocolVersion.trim()
          : "";
      const protocolVersion = SUPPORTED_PROTOCOL_VERSIONS.has(requested)
        ? requested
        : DEFAULT_PROTOCOL_VERSION;
      const capabilities = msg.params?.capabilities as { roots?: unknown } | undefined;
      this.clientSupportsRoots =
        capabilities?.roots !== null &&
        typeof capabilities?.roots === "object" &&
        !Array.isArray(capabilities?.roots);
      const rawClientInfo = msg.params?.clientInfo;
      let clientInfo: McpClientInfo | undefined;
      if (
        rawClientInfo !== null &&
        typeof rawClientInfo === "object" &&
        !Array.isArray(rawClientInfo)
      ) {
        const ci = rawClientInfo as { name?: unknown; version?: unknown };
        clientInfo = {
          name: typeof ci.name === "string" ? ci.name : undefined,
          version: typeof ci.version === "string" ? ci.version : undefined,
        };
      }
      try {
        this.onInitialize?.({ clientInfo, protocolVersion });
      } catch {
        // Never fail initialize due to optional host-compat inspection
      }
      return {
        jsonrpc: "2.0",
        id,
        result: {
          protocolVersion,
          capabilities: {
            tools: {},
          },
          serverInfo: {
            name: this.serverName,
            version: this.serverVersion,
          },
        },
      };
    }

    if (method === "ping") {
      return { jsonrpc: "2.0", id, result: {} };
    }

    if (method === "tools/list") {
      const definitions = [...this.tools.values()].map((t) =>
        typeof t.definition === "function" ? t.definition() : t.definition,
      );
      return { jsonrpc: "2.0", id, result: { tools: definitions } };
    }

    if (method === "resources/list") {
      return { jsonrpc: "2.0", id, result: { resources: [] } };
    }

    if (method === "resources/templates/list") {
      return { jsonrpc: "2.0", id, result: { resourceTemplates: [] } };
    }

    if (method === "prompts/list") {
      return { jsonrpc: "2.0", id, result: { prompts: [] } };
    }

    if (method === "tools/call") {
      const name =
        typeof msg.params?.name === "string" ? msg.params.name : "";
      const tool = this.tools.get(name);
      if (!tool) {
        return {
          jsonrpc: "2.0",
          id,
          error: { code: -32602, message: `Unknown tool: ${name}` },
        };
      }

      const rawArgs = msg.params?.arguments;
      let args: Record<string, unknown>;
      if (rawArgs === undefined || rawArgs === null) {
        args = {};
      } else if (typeof rawArgs === "object" && !Array.isArray(rawArgs)) {
        args = rawArgs as Record<string, unknown>;
      } else if (typeof rawArgs === "string" && rawArgs.trim().startsWith("{")) {
        try {
          const parsedArgs: unknown = JSON.parse(rawArgs);
          if (parsedArgs && typeof parsedArgs === "object" && !Array.isArray(parsedArgs)) {
            args = parsedArgs as Record<string, unknown>;
          } else {
            return {
              jsonrpc: "2.0",
              id,
              error: {
                code: -32602,
                message: `Invalid params: 'arguments' for '${name}' must be a JSON object`,
              },
            };
          }
        } catch {
          return {
            jsonrpc: "2.0",
            id,
            error: {
              code: -32602,
              message: `Invalid params: malformed JSON string in 'arguments' for '${name}'`,
            },
          };
        }
      } else {
        return {
          jsonrpc: "2.0",
          id,
          error: {
            code: -32602,
            message: `Invalid params: 'arguments' for '${name}' must be an object (received ${Array.isArray(rawArgs) ? "array" : typeof rawArgs})`,
          },
        };
      }

      try {
        await this.waitForRoots();
        const res = await tool.handler(args);
        return {
          jsonrpc: "2.0",
          id,
          result: {
            content: [{ type: "text", text: res.text }],
            isError: Boolean(res.isError),
          },
        };
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        return {
          jsonrpc: "2.0",
          id,
          result: {
            content: [{ type: "text", text: `Tool error: ${message}` }],
            isError: true,
          },
        };
      }
    }

    return {
      jsonrpc: "2.0",
      id,
      error: { code: -32601, message: `Method not found: ${method}` },
    };
  }

  listen(
    input: Readable = process.stdin,
    output: Writable = process.stdout,
    onExit?: () => void,
  ): void {
    this.onDrainExit = onExit;
    let buffer: Buffer = Buffer.alloc(0);

    const writeResponse = (
      payload: JsonRpcResponse | JsonRpcResponse[] | JsonRpcOutgoingRequest,
      useContentLength: boolean,
    ): void => {
      const serialized = JSON.stringify(payload);
      if (useContentLength) {
        const len = Buffer.byteLength(serialized, "utf8");
        output.write(`Content-Length: ${len}\r\n\r\n${serialized}`);
      } else {
        output.write(`${serialized}\n`);
      }
    };

    this.sendToClient = (request, useContentLength) =>
      writeResponse(request, useContentLength);

    let drainExitCalled = false;
    const triggerDrainExit = () => {
      if (drainExitCalled) return;
      drainExitCalled = true;
      this.onDrainExit?.();
    };

    const dispatchRawJson = (jsonText: string, useContentLength: boolean): void => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(jsonText);
      } catch {
        writeResponse(
          {
            jsonrpc: "2.0",
            id: null,
            error: { code: -32700, message: "Parse error" },
          },
          useContentLength,
        );
        return;
      }

      this.inFlight += 1;
      const finish = () => {
        this.inFlight -= 1;
        if (this.stdinEnded && this.inFlight === 0) {
          triggerDrainExit();
        }
      };

      const outgoingForCall: Array<() => void> = [];
      const flushOutgoingForCall = () => {
        for (const task of outgoingForCall) {
          task();
        }
      };

      if (Array.isArray(parsed)) {
        if (parsed.length === 0) {
          writeResponse(
            {
              jsonrpc: "2.0",
              id: null,
              error: { code: -32600, message: "Invalid Request: empty batch" },
            },
            useContentLength,
          );
          finish();
          return;
        }
        const prevQueue = this.activeOutgoingQueue;
        this.activeOutgoingQueue = outgoingForCall;
        let promises: Array<Promise<JsonRpcResponse | null>>;
        try {
          promises = parsed.map((item) => this.handleMessage(item, useContentLength));
        } finally {
          this.activeOutgoingQueue = prevQueue;
        }
        Promise.all(promises)
          .then((responses) => {
            const nonNull = responses.filter(
              (r): r is JsonRpcResponse => r !== null,
            );
            if (nonNull.length > 0) {
              writeResponse(nonNull, useContentLength);
            }
            flushOutgoingForCall();
          })
          .catch(() => {
            // Isolate unexpected handler rejections from crashing stdio loop
          })
          .finally(finish);
        return;
      }

      const prevQueue = this.activeOutgoingQueue;
      this.activeOutgoingQueue = outgoingForCall;
      let promise: Promise<JsonRpcResponse | null>;
      try {
        promise = this.handleMessage(parsed, useContentLength);
      } finally {
        this.activeOutgoingQueue = prevQueue;
      }
      promise
        .then((response) => {
          if (response !== null) {
            writeResponse(response, useContentLength);
          }
          flushOutgoingForCall();
        })
        .catch(() => {
          // Isolate unexpected handler rejections from crashing stdio loop
        })
        .finally(finish);
    };

    const drainBuffer = (): void => {
      while (buffer.length > 0) {
        // Trim leading blank lines / whitespace
        let offset = 0;
        while (
          offset < buffer.length &&
          (buffer[offset] === 0x0a || buffer[offset] === 0x0d)
        ) {
          offset++;
        }
        if (offset > 0) {
          buffer = buffer.subarray(offset);
          if (buffer.length === 0) break;
        }

        // Check for optional Content-Length / Content-Type LSP-style header framing
        const headerProbe = buffer.subarray(0, 16).toString("latin1").toLowerCase();
        if (
          headerProbe.startsWith("content-length:") ||
          headerProbe.startsWith("content-type:")
        ) {
          const headerScan = buffer
            .subarray(0, Math.min(buffer.length, 16_384))
            .toString("latin1");
          const sepMatch = /\r?\n\r?\n/.exec(headerScan);
          if (!sepMatch) {
            if (buffer.length >= 16_384) {
              writeResponse(
                {
                  jsonrpc: "2.0",
                  id: null,
                  error: { code: -32600, message: "Invalid Request: header too large" },
                },
                true,
              );
              buffer = Buffer.alloc(0);
            }
            break; // wait for complete header
          }
          const sepIdx = sepMatch.index;
          const sepLen = sepMatch[0].length;

          const headerText = headerScan.slice(0, sepIdx);
          const clMatches = [...headerText.matchAll(/^content-length:[ \t]*(\d+)[ \t]*\r?$/gim)];
          const maxFrameBytes = 64 * 1024 * 1024;
          if (clMatches.length !== 1) {
            writeResponse(
              {
                jsonrpc: "2.0",
                id: null,
                error: { code: -32600, message: "Invalid Request: invalid Content-Length header" },
              },
              true,
            );
            buffer = buffer.subarray(sepIdx + sepLen);
            continue;
          }
          const contentLength = Number(clMatches[0]![1]!);
          if (!Number.isSafeInteger(contentLength) || contentLength < 0 || contentLength > maxFrameBytes) {
            writeResponse(
              {
                jsonrpc: "2.0",
                id: null,
                error: { code: -32600, message: "Invalid Request: Content-Length out of bounds" },
              },
              true,
            );
            buffer = Buffer.alloc(0);
            break;
          }
          const bodyStart = sepIdx + sepLen;
          if (buffer.length < bodyStart + contentLength) {
            break; // wait for complete body
          }
          const bodyText = buffer
            .subarray(bodyStart, bodyStart + contentLength)
            .toString("utf8");
          buffer = buffer.subarray(bodyStart + contentLength);
          dispatchRawJson(bodyText, true);
          continue;
        }

        // Standard MCP Stdio newline-delimited JSON framing
        const newlineIdx = buffer.indexOf(0x0a);
        if (newlineIdx === -1) break;

        const lineBuf = buffer.subarray(0, newlineIdx);
        buffer = buffer.subarray(newlineIdx + 1);
        const line = lineBuf.toString("utf8").trim();
        if (line.length > 0) {
          dispatchRawJson(line, false);
        }
      }
    };

    input.on("data", (chunk: Buffer | string) => {
      const buf = typeof chunk === "string" ? Buffer.from(chunk, "utf8") : chunk;
      buffer = buffer.length === 0 ? buf : Buffer.concat([buffer, buf]);
      drainBuffer();
    });

    input.on("end", () => {
      // Flush any trailing line without newline
      const remaining = buffer.toString("utf8").trim();
      buffer = Buffer.alloc(0);
      this.stdinEnded = true;
      const lowerRem = remaining.toLowerCase();
      if (
        remaining.length > 0 &&
        !lowerRem.startsWith("content-length:") &&
        !lowerRem.startsWith("content-type:")
      ) {
        dispatchRawJson(remaining, false);
      }
      this.settleAllPendingRoots();
      if (this.inFlight === 0) {
        triggerDrainExit();
      }
    });

    input.on("error", () => {
      this.stdinEnded = true;
      this.settleAllPendingRoots();
      if (this.inFlight === 0) {
        triggerDrainExit();
      }
    });
  }
}
