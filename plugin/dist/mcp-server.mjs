// whitemagic-agy-kit Fused Turbo+Fast MCP Stdio Server (bundled zero-dependency ESM)


// src/mcp-server.ts
import fs8 from "node:fs";
import os5 from "node:os";
import path9 from "node:path";

// src/protocol/mcp.ts
import { fileURLToPath } from "node:url";
var SUPPORTED_PROTOCOL_VERSIONS = /* @__PURE__ */ new Set([
  "2025-11-25",
  "2025-06-18",
  "2025-03-26",
  "2024-11-05"
]);
var DEFAULT_PROTOCOL_VERSION = "2025-03-26";
var ROOTS_REQUEST_TIMEOUT_MS = 1500;
var MAX_KNOWN_ROOTS_IDS = 64;
var McpStdioServer = class {
  tools = /* @__PURE__ */ new Map();
  serverName;
  serverVersion;
  inFlight = 0;
  stdinEnded = false;
  onDrainExit;
  onWorkspaceRoots;
  onInitialize;
  sendToClient;
  clientSupportsRoots = false;
  rootsRequestCount = 0;
  lastAppliedRootsSeq = 0;
  pendingRoots = /* @__PURE__ */ new Map();
  knownRootsIds = /* @__PURE__ */ new Map();
  rootsWaiters = /* @__PURE__ */ new Set();
  activeOutgoingQueue = null;
  rootsTimeoutMs;
  constructor(options) {
    this.serverName = options.name;
    this.serverVersion = options.version;
    this.onWorkspaceRoots = options.onWorkspaceRoots;
    this.onInitialize = options.onInitialize;
    this.rootsTimeoutMs = typeof options.rootsTimeoutMs === "number" && Number.isFinite(options.rootsTimeoutMs) && options.rootsTimeoutMs > 0 ? Math.trunc(options.rootsTimeoutMs) : ROOTS_REQUEST_TIMEOUT_MS;
  }
  flushRootsWaiters() {
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
  settleAllPendingRoots() {
    for (const pending of this.pendingRoots.values()) {
      clearTimeout(pending.timer);
    }
    this.pendingRoots.clear();
    for (const waiter of this.rootsWaiters) {
      waiter.resolve();
    }
    this.rootsWaiters.clear();
  }
  waitForRoots() {
    if (this.pendingRoots.size === 0 || this.stdinEnded) {
      return Promise.resolve();
    }
    if (this.activeOutgoingQueue && this.activeOutgoingQueue.length > 0) {
      const tasks = this.activeOutgoingQueue.splice(0, this.activeOutgoingQueue.length);
      for (const task of tasks) {
        task();
      }
    }
    const minSeq = this.rootsRequestCount;
    return new Promise((resolve) => {
      this.rootsWaiters.add({ minSeq, resolve });
    });
  }
  pruneKnownRootsIds() {
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
  requestRoots(useContentLength) {
    if (this.stdinEnded || !this.clientSupportsRoots || !this.onWorkspaceRoots || !this.sendToClient) {
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
  receiveRoots(result) {
    if (!result || typeof result !== "object") return;
    const rawRoots = result.roots;
    if (!Array.isArray(rawRoots)) return;
    const paths = [];
    for (const root of rawRoots) {
      if (!root || typeof root !== "object") continue;
      const uri = root.uri;
      if (typeof uri !== "string" || !/^file:/i.test(uri)) continue;
      try {
        paths.push(fileURLToPath(uri));
      } catch {
      }
    }
    if (paths.length > 0) {
      try {
        this.onWorkspaceRoots?.(paths);
      } catch {
      }
    }
  }
  registerTool(definition, handler) {
    const name = typeof definition === "function" ? definition().name : definition.name;
    this.tools.set(name, { definition, handler });
  }
  async handleMessage(raw, useContentLength = false) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
      return {
        jsonrpc: "2.0",
        id: null,
        error: { code: -32600, message: "Invalid Request" }
      };
    }
    const msg = raw;
    const hasId = Object.prototype.hasOwnProperty.call(msg, "id") && msg.id !== void 0;
    const id = hasId ? msg.id ?? null : null;
    const method = typeof msg.method === "string" ? msg.method : "";
    if (!method && typeof id === "string" && this.knownRootsIds.has(id)) {
      const seq = this.knownRootsIds.get(id);
      this.knownRootsIds.delete(id);
      const pending = this.pendingRoots.get(id);
      if (pending) {
        clearTimeout(pending.timer);
        this.pendingRoots.delete(id);
      }
      const errField = msg.error;
      const hasError = errField !== void 0 && errField !== null;
      try {
        if (!hasError && seq >= this.lastAppliedRootsSeq) {
          this.lastAppliedRootsSeq = seq;
          for (const [oldId, oldPending] of this.pendingRoots.entries()) {
            if (oldPending.seq < seq) {
              clearTimeout(oldPending.timer);
              this.pendingRoots.delete(oldId);
            }
          }
          this.receiveRoots(msg.result);
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
        error: { code: -32600, message: "Invalid Request: missing method" }
      };
    }
    if (!hasId) {
      if (method === "notifications/initialized" || method === "notifications/roots/list_changed") {
        this.requestRoots(useContentLength);
      }
      return null;
    }
    if (method === "initialize") {
      const requested = typeof msg.params?.protocolVersion === "string" ? msg.params.protocolVersion.trim() : "";
      const protocolVersion = SUPPORTED_PROTOCOL_VERSIONS.has(requested) ? requested : DEFAULT_PROTOCOL_VERSION;
      const capabilities = msg.params?.capabilities;
      this.clientSupportsRoots = capabilities?.roots !== null && typeof capabilities?.roots === "object" && !Array.isArray(capabilities?.roots);
      const rawClientInfo = msg.params?.clientInfo;
      let clientInfo;
      if (rawClientInfo !== null && typeof rawClientInfo === "object" && !Array.isArray(rawClientInfo)) {
        const ci = rawClientInfo;
        clientInfo = {
          name: typeof ci.name === "string" ? ci.name : void 0,
          version: typeof ci.version === "string" ? ci.version : void 0
        };
      }
      try {
        this.onInitialize?.({ clientInfo, protocolVersion });
      } catch {
      }
      return {
        jsonrpc: "2.0",
        id,
        result: {
          protocolVersion,
          capabilities: {
            tools: {}
          },
          serverInfo: {
            name: this.serverName,
            version: this.serverVersion
          }
        }
      };
    }
    if (method === "ping") {
      return { jsonrpc: "2.0", id, result: {} };
    }
    if (method === "tools/list") {
      const definitions = [...this.tools.values()].map(
        (t) => typeof t.definition === "function" ? t.definition() : t.definition
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
      const name = typeof msg.params?.name === "string" ? msg.params.name : "";
      const tool = this.tools.get(name);
      if (!tool) {
        return {
          jsonrpc: "2.0",
          id,
          error: { code: -32602, message: `Unknown tool: ${name}` }
        };
      }
      const rawArgs = msg.params?.arguments;
      let args;
      if (rawArgs === void 0 || rawArgs === null) {
        args = {};
      } else if (typeof rawArgs === "object" && !Array.isArray(rawArgs)) {
        args = rawArgs;
      } else if (typeof rawArgs === "string" && rawArgs.trim().startsWith("{")) {
        try {
          const parsedArgs = JSON.parse(rawArgs);
          if (parsedArgs && typeof parsedArgs === "object" && !Array.isArray(parsedArgs)) {
            args = parsedArgs;
          } else {
            return {
              jsonrpc: "2.0",
              id,
              error: {
                code: -32602,
                message: `Invalid params: 'arguments' for '${name}' must be a JSON object`
              }
            };
          }
        } catch {
          return {
            jsonrpc: "2.0",
            id,
            error: {
              code: -32602,
              message: `Invalid params: malformed JSON string in 'arguments' for '${name}'`
            }
          };
        }
      } else {
        return {
          jsonrpc: "2.0",
          id,
          error: {
            code: -32602,
            message: `Invalid params: 'arguments' for '${name}' must be an object (received ${Array.isArray(rawArgs) ? "array" : typeof rawArgs})`
          }
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
            isError: Boolean(res.isError)
          }
        };
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        return {
          jsonrpc: "2.0",
          id,
          result: {
            content: [{ type: "text", text: `Tool error: ${message}` }],
            isError: true
          }
        };
      }
    }
    return {
      jsonrpc: "2.0",
      id,
      error: { code: -32601, message: `Method not found: ${method}` }
    };
  }
  listen(input = process.stdin, output = process.stdout, onExit) {
    this.onDrainExit = onExit;
    let buffer = Buffer.alloc(0);
    const writeResponse = (payload, useContentLength) => {
      const serialized = JSON.stringify(payload);
      if (useContentLength) {
        const len = Buffer.byteLength(serialized, "utf8");
        output.write(`Content-Length: ${len}\r
\r
${serialized}`);
      } else {
        output.write(`${serialized}
`);
      }
    };
    this.sendToClient = (request, useContentLength) => writeResponse(request, useContentLength);
    let drainExitCalled = false;
    const triggerDrainExit = () => {
      if (drainExitCalled) return;
      drainExitCalled = true;
      this.onDrainExit?.();
    };
    const dispatchRawJson = (jsonText, useContentLength) => {
      let parsed;
      try {
        parsed = JSON.parse(jsonText);
      } catch {
        writeResponse(
          {
            jsonrpc: "2.0",
            id: null,
            error: { code: -32700, message: "Parse error" }
          },
          useContentLength
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
      const outgoingForCall = [];
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
              error: { code: -32600, message: "Invalid Request: empty batch" }
            },
            useContentLength
          );
          finish();
          return;
        }
        const prevQueue2 = this.activeOutgoingQueue;
        this.activeOutgoingQueue = outgoingForCall;
        let promises;
        try {
          promises = parsed.map((item) => this.handleMessage(item, useContentLength));
        } finally {
          this.activeOutgoingQueue = prevQueue2;
        }
        Promise.all(promises).then((responses) => {
          const nonNull = responses.filter(
            (r) => r !== null
          );
          if (nonNull.length > 0) {
            writeResponse(nonNull, useContentLength);
          }
          flushOutgoingForCall();
        }).catch(() => {
        }).finally(finish);
        return;
      }
      const prevQueue = this.activeOutgoingQueue;
      this.activeOutgoingQueue = outgoingForCall;
      let promise;
      try {
        promise = this.handleMessage(parsed, useContentLength);
      } finally {
        this.activeOutgoingQueue = prevQueue;
      }
      promise.then((response) => {
        if (response !== null) {
          writeResponse(response, useContentLength);
        }
        flushOutgoingForCall();
      }).catch(() => {
      }).finally(finish);
    };
    const drainBuffer = () => {
      while (buffer.length > 0) {
        let offset = 0;
        while (offset < buffer.length && (buffer[offset] === 10 || buffer[offset] === 13)) {
          offset++;
        }
        if (offset > 0) {
          buffer = buffer.subarray(offset);
          if (buffer.length === 0) break;
        }
        const headerProbe = buffer.subarray(0, 16).toString("latin1").toLowerCase();
        if (headerProbe.startsWith("content-length:") || headerProbe.startsWith("content-type:")) {
          const headerScan = buffer.subarray(0, Math.min(buffer.length, 16384)).toString("latin1");
          const sepMatch = /\r?\n\r?\n/.exec(headerScan);
          if (!sepMatch) {
            if (buffer.length >= 16384) {
              writeResponse(
                {
                  jsonrpc: "2.0",
                  id: null,
                  error: { code: -32600, message: "Invalid Request: header too large" }
                },
                true
              );
              buffer = Buffer.alloc(0);
            }
            break;
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
                error: { code: -32600, message: "Invalid Request: invalid Content-Length header" }
              },
              true
            );
            buffer = buffer.subarray(sepIdx + sepLen);
            continue;
          }
          const contentLength = Number(clMatches[0][1]);
          if (!Number.isSafeInteger(contentLength) || contentLength < 0 || contentLength > maxFrameBytes) {
            writeResponse(
              {
                jsonrpc: "2.0",
                id: null,
                error: { code: -32600, message: "Invalid Request: Content-Length out of bounds" }
              },
              true
            );
            buffer = Buffer.alloc(0);
            break;
          }
          const bodyStart = sepIdx + sepLen;
          if (buffer.length < bodyStart + contentLength) {
            break;
          }
          const bodyText = buffer.subarray(bodyStart, bodyStart + contentLength).toString("utf8");
          buffer = buffer.subarray(bodyStart + contentLength);
          dispatchRawJson(bodyText, true);
          continue;
        }
        const newlineIdx = buffer.indexOf(10);
        if (newlineIdx === -1) break;
        const lineBuf = buffer.subarray(0, newlineIdx);
        buffer = buffer.subarray(newlineIdx + 1);
        const line = lineBuf.toString("utf8").trim();
        if (line.length > 0) {
          dispatchRawJson(line, false);
        }
      }
    };
    input.on("data", (chunk) => {
      const buf = typeof chunk === "string" ? Buffer.from(chunk, "utf8") : chunk;
      buffer = buffer.length === 0 ? buf : Buffer.concat([buffer, buf]);
      drainBuffer();
    });
    input.on("end", () => {
      const remaining = buffer.toString("utf8").trim();
      buffer = Buffer.alloc(0);
      this.stdinEnded = true;
      const lowerRem = remaining.toLowerCase();
      if (remaining.length > 0 && !lowerRem.startsWith("content-length:") && !lowerRem.startsWith("content-type:")) {
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
};

// src/utils/exec-utils.ts
import { spawn, spawnSync } from "node:child_process";
import path from "node:path";
var DEFAULT_MAX_OUTPUT_BYTES = 1024 * 1024;
var WINDOWS_CODEPAGE_LABELS = {
  866: "ibm866",
  874: "windows-874",
  936: "gb18030",
  54936: "gb18030",
  950: "big5",
  932: "shift_jis",
  949: "euc-kr",
  1250: "windows-1250",
  1251: "windows-1251",
  1252: "windows-1252",
  1253: "windows-1253",
  1254: "windows-1254",
  1255: "windows-1255",
  1256: "windows-1256",
  1257: "windows-1257",
  1258: "windows-1258",
  65001: "utf-8"
};
var strictUtf8Decoder = new TextDecoder("utf-8", { fatal: true });
var cachedWindowsFallbackEncoding;
var fallbackDecoderCache = /* @__PURE__ */ new Map();
function parseWindowsCodePage(output) {
  const match = /\b(\d{3,5})\b/.exec(output);
  if (!match) return null;
  const cp = Number.parseInt(match[1], 10);
  const mapped = WINDOWS_CODEPAGE_LABELS[cp];
  return mapped && mapped !== "utf-8" ? mapped : null;
}
function stripTrailingIncompleteUtf8(buf) {
  if (buf.length === 0) return buf;
  let i = buf.length - 1;
  const minIdx = Math.max(0, buf.length - 4);
  while (i >= minIdx && (buf[i] & 192) === 128) {
    i--;
  }
  if (i < minIdx) return buf;
  const lead = buf[i];
  if ((lead & 128) === 0) return buf;
  let expectedLen = 0;
  if ((lead & 224) === 192) expectedLen = 2;
  else if ((lead & 240) === 224) expectedLen = 3;
  else if ((lead & 248) === 240) expectedLen = 4;
  else return buf;
  return buf.length - i < expectedLen ? buf.subarray(0, i) : buf;
}
function getWindowsSystem32Binary(name) {
  const systemRoot = process.env.SystemRoot || process.env.WINDIR || "";
  if (systemRoot) {
    return {
      bin: path.join(systemRoot, "System32", name),
      cwd: systemRoot
    };
  }
  return { bin: name, cwd: process.cwd() };
}
function getWindowsFallbackEncoding() {
  if (cachedWindowsFallbackEncoding !== void 0) {
    return cachedWindowsFallbackEncoding;
  }
  try {
    const { bin: chcpBin, cwd } = getWindowsSystem32Binary("chcp.com");
    const res = spawnSync(chcpBin, [], {
      cwd,
      encoding: "latin1",
      timeout: 1500,
      windowsHide: true
    });
    if (!res.error && res.status === 0) {
      const mapped = parseWindowsCodePage(res.stdout || "");
      if (mapped) {
        cachedWindowsFallbackEncoding = mapped;
        return cachedWindowsFallbackEncoding;
      }
    }
  } catch {
  }
  cachedWindowsFallbackEncoding = null;
  return null;
}
function decodeProcessOutput(buf, truncated = false, fallbackEncoding) {
  if (buf.length === 0) return "";
  const candidate = truncated ? stripTrailingIncompleteUtf8(buf) : buf;
  try {
    return strictUtf8Decoder.decode(candidate);
  } catch {
    const label = fallbackEncoding ?? (process.platform === "win32" ? getWindowsFallbackEncoding() : null);
    if (!label || label === "utf-8") {
      return buf.toString("utf8");
    }
    try {
      let decoder = fallbackDecoderCache.get(label);
      if (!decoder) {
        decoder = new TextDecoder(label);
        fallbackDecoderCache.set(label, decoder);
      }
      return decoder.decode(buf);
    } catch {
      return buf.toString("utf8");
    }
  }
}
var MCP_CALL_BUDGET_SEC = 170;
function killChildTree(pid, detached) {
  if (!pid) return;
  if (process.platform === "win32") {
    try {
      const killer = spawn("taskkill", ["/PID", String(pid), "/T", "/F"], {
        stdio: "ignore",
        windowsHide: true
      });
      killer.unref();
    } catch {
      try {
        process.kill(pid, "SIGTERM");
      } catch {
      }
    }
    return;
  }
  try {
    if (detached) {
      process.kill(-pid, "SIGTERM");
    } else {
      process.kill(pid, "SIGTERM");
    }
  } catch {
  }
  setTimeout(() => {
    try {
      if (detached) {
        process.kill(-pid, "SIGKILL");
      } else {
        process.kill(pid, "SIGKILL");
      }
    } catch {
    }
  }, 400).unref();
}
function execBinaryAsync(bin, args, options) {
  return spawnAndCollect(bin, args, false, options);
}
var WINDOWS_PS_DOUBLE_QUOTABLE_VAR_RE = /(?<![\\`])(?:\$env:[A-Za-z_][A-Za-z0-9_]*|\$(?:LASTEXITCODE|PSVersionTable|PSScriptRoot|PROFILE)\b)/i;
var WINDOWS_PS_UNQUOTED_SYNTAX_RE = /(?<![\\`])\$(?:HOME|PWD|null|true|false|_)\b|\$\([^)]|@\(|(?:^|[;\r\n])\s*\$[A-Za-z_][A-Za-z0-9_]*\s*=/im;
var WINDOWS_PS_CMDLET_RE = /(?:^|[;|&({\r\n]\s*)(?:Get|Set|New|Remove|Test|Select|Where|ForEach|Sort|Group|Measure|Compare|ConvertTo|ConvertFrom|Export|Import|Invoke|Start|Stop|Restart|Wait|Read|Write|Out|Format|Resolve|Join|Split|Copy|Move|Rename|Clear|Add|Push|Pop|Enable|Disable)-[A-Z][A-Za-z0-9]*\b(?!-)/;
var EXPLICIT_POWERSHELL_INVOCATION_RE = /^\s*(?:["']?)(?:pwsh|powershell)(?:\.exe)?(?:["']?)(?:\s|$)/i;
var PS_NON_INVOCATION_TAIL_RE = /^(?:[|;&+}=),*?]|(?:[1-6*])?>|-(?:c|i)?(?:eq|ne|gt|ge|lt|le|like|notlike|match|notmatch|replace|split|join|in|notin|contains|notcontains|is|isnot|as|and|or|xor|shl|shr|band|bor|bxor)\b|-f\b|$)/i;
var PS_UTF8_PREFIX = "try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch {}; $OutputEncoding = [System.Text.UTF8Encoding]::new($false);";
function stripQuotedSpans(command, stripDoubleQuotes) {
  let out = "";
  let quote = null;
  for (let i = 0; i < command.length; i++) {
    const ch = command[i];
    const next = command[i + 1];
    if (quote === "'") {
      if (ch === "'") {
        if (next === "'") {
          i++;
        } else {
          quote = null;
          out += " ";
        }
      }
      continue;
    }
    if (quote === '"') {
      if ((ch === "\\" || ch === "`") && next === '"') {
        i++;
        if (!stripDoubleQuotes) out += ch + next;
        continue;
      }
      if (ch === '"') {
        if (next === '"') {
          i++;
          if (!stripDoubleQuotes) out += '""';
        } else {
          quote = null;
          out += stripDoubleQuotes ? " " : '"';
        }
        continue;
      }
      if (!stripDoubleQuotes) out += ch;
      continue;
    }
    if (ch === "'") {
      quote = "'";
      out += " ";
      continue;
    }
    if (ch === '"') {
      quote = '"';
      out += stripDoubleQuotes ? " " : '"';
      continue;
    }
    out += ch;
  }
  return out;
}
function needsPowerShellOnWindows(command) {
  if (!command || EXPLICIT_POWERSHELL_INVOCATION_RE.test(command)) {
    return false;
  }
  const withoutSingle = stripQuotedSpans(command, false);
  if (WINDOWS_PS_DOUBLE_QUOTABLE_VAR_RE.test(withoutSingle)) {
    return true;
  }
  const unquoted = stripQuotedSpans(command, true);
  return WINDOWS_PS_UNQUOTED_SYNTAX_RE.test(unquoted) || WINDOWS_PS_CMDLET_RE.test(unquoted);
}
function formatPowerShellCommand(command) {
  const trimmed = command.trim();
  if (!trimmed || trimmed[0] !== '"' && trimmed[0] !== "'") {
    return command;
  }
  const quote = trimmed[0];
  let idx = 1;
  while (idx < trimmed.length) {
    if (quote === '"' && trimmed[idx] === "`" && idx + 1 < trimmed.length) {
      idx += 2;
      continue;
    }
    if (trimmed[idx] === quote) {
      if (trimmed[idx + 1] === quote) {
        idx += 2;
        continue;
      }
      const afterQuote = trimmed.slice(idx + 1);
      if (!/^\s+/.test(afterQuote)) {
        return command;
      }
      const rest = afterQuote.trimStart();
      if (rest && !PS_NON_INVOCATION_TAIL_RE.test(rest)) {
        return `& ${trimmed}`;
      }
      return command;
    }
    idx++;
  }
  return command;
}
function buildPowerShellUtf8Command(command) {
  return `${PS_UTF8_PREFIX} ${formatPowerShellCommand(command)}`;
}
var cachedWindowsPowerShell;
var cachedWindowsPowerShellExecPath = null;
var pendingWindowsPowerShellProbe = null;
async function defaultProbeWindowsShell(bin) {
  const { bin: whereBin, cwd } = getWindowsSystem32Binary("where.exe");
  const exeQuery = bin.toLowerCase().endsWith(".exe") ? bin : `${bin}.exe`;
  const whereRes = await execBinaryAsync(whereBin, [exeQuery], {
    cwd,
    timeoutMs: 2e3,
    maxOutputBytes: 4096
  });
  if (!whereRes.error && !whereRes.timedOut) {
    if (whereRes.exitCode !== 0) return false;
    const resolved = whereRes.stdout.split(/\r?\n/).map((line) => line.trim()).find((line) => /\.exe$/i.test(line));
    if (!resolved) return false;
    cachedWindowsPowerShellExecPath = resolved;
    return true;
  }
  const res = await execBinaryAsync(
    exeQuery,
    ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "exit 0"],
    {
      cwd,
      timeoutMs: 3e3,
      maxOutputBytes: 4096
    }
  );
  if (!res.error && !res.timedOut && res.exitCode === 0) {
    cachedWindowsPowerShellExecPath = exeQuery;
    return true;
  }
  return false;
}
async function resolveWindowsPowerShell(probe = defaultProbeWindowsShell) {
  if (cachedWindowsPowerShell !== void 0) {
    return cachedWindowsPowerShell;
  }
  if (pendingWindowsPowerShellProbe) {
    return pendingWindowsPowerShellProbe;
  }
  pendingWindowsPowerShellProbe = (async () => {
    try {
      cachedWindowsPowerShellExecPath = null;
      if (await probe("pwsh")) {
        cachedWindowsPowerShell = "pwsh";
        return cachedWindowsPowerShell;
      }
      cachedWindowsPowerShellExecPath = null;
      if (await probe("powershell.exe")) {
        cachedWindowsPowerShell = "powershell.exe";
        return cachedWindowsPowerShell;
      }
      cachedWindowsPowerShellExecPath = null;
      cachedWindowsPowerShell = null;
      return null;
    } finally {
      pendingWindowsPowerShellProbe = null;
    }
  })();
  return pendingWindowsPowerShellProbe;
}
async function runShellCommandAsync(command, options) {
  if (process.platform === "win32" && needsPowerShellOnWindows(command)) {
    const psBin = await resolveWindowsPowerShell();
    if (psBin) {
      const utf8Command = buildPowerShellUtf8Command(command);
      return spawnAndCollect(
        cachedWindowsPowerShellExecPath ?? psBin,
        ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", utf8Command],
        false,
        options
      );
    }
  }
  return spawnAndCollect(command, [], true, options);
}
function spawnAndCollect(commandOrBin, args, shell, options) {
  const startMs = Date.now();
  const maxOutputBytes = options.maxOutputBytes ?? DEFAULT_MAX_OUTPUT_BYTES;
  const detached = process.platform !== "win32";
  return new Promise((resolve) => {
    let stdoutBytes = 0;
    let stderrBytes = 0;
    let stdoutTruncated = false;
    let stderrTruncated = false;
    let timedOut = false;
    let settled = false;
    const stdoutChunks = [];
    const stderrChunks = [];
    let child;
    try {
      child = spawn(commandOrBin, args, {
        cwd: options.cwd,
        shell,
        detached,
        windowsHide: true,
        stdio: ["ignore", "pipe", "pipe"],
        env: options.env ?? process.env
      });
    } catch (err) {
      resolve({
        exitCode: null,
        signal: null,
        timedOut: false,
        durationMs: Date.now() - startMs,
        stdout: "",
        stderr: "",
        truncated: false,
        error: err instanceof Error ? err.message : String(err)
      });
      return;
    }
    let settleFallbackTimer = null;
    const timer = setTimeout(() => {
      timedOut = true;
      killChildTree(child.pid, detached);
      settleFallbackTimer = setTimeout(() => {
        child.stdout?.destroy();
        child.stderr?.destroy();
        finish(null, "SIGKILL");
      }, 1500);
      settleFallbackTimer.unref();
    }, Math.max(100, options.timeoutMs));
    timer.unref();
    const finish = (exitCode, signal, error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (settleFallbackTimer) clearTimeout(settleFallbackTimer);
      resolve({
        exitCode,
        signal,
        timedOut,
        durationMs: Date.now() - startMs,
        stdout: decodeProcessOutput(Buffer.concat(stdoutChunks), stdoutTruncated),
        stderr: decodeProcessOutput(Buffer.concat(stderrChunks), stderrTruncated),
        truncated: stdoutTruncated || stderrTruncated,
        error
      });
    };
    child.stdout?.on("data", (chunk) => {
      if (stdoutBytes >= maxOutputBytes) {
        stdoutTruncated = true;
        return;
      }
      const remaining = maxOutputBytes - stdoutBytes;
      if (chunk.length > remaining) {
        stdoutChunks.push(chunk.subarray(0, remaining));
        stdoutBytes += remaining;
        stdoutTruncated = true;
      } else {
        stdoutChunks.push(chunk);
        stdoutBytes += chunk.length;
      }
    });
    child.stderr?.on("data", (chunk) => {
      if (stderrBytes >= maxOutputBytes) {
        stderrTruncated = true;
        return;
      }
      const remaining = maxOutputBytes - stderrBytes;
      if (chunk.length > remaining) {
        stderrChunks.push(chunk.subarray(0, remaining));
        stderrBytes += remaining;
        stderrTruncated = true;
      } else {
        stderrChunks.push(chunk);
        stderrBytes += chunk.length;
      }
    });
    child.on("error", (err) => {
      finish(null, null, err.message);
    });
    child.on("close", (code, signal) => {
      finish(code, signal);
    });
  });
}
var cachedRgAvailable = null;
async function isRipgrepAvailable() {
  if (cachedRgAvailable !== null) return cachedRgAvailable;
  const res = await execBinaryAsync("rg", ["--version"], {
    cwd: process.cwd(),
    timeoutMs: 2e3,
    maxOutputBytes: 4096
  });
  cachedRgAvailable = !res.error && res.exitCode === 0;
  return cachedRgAvailable;
}

// src/utils/host-compat.ts
import fs from "node:fs";
import os from "node:os";
import path2 from "node:path";

// src/utils/output-filter.ts
var ANSI_REGEX = (
  // eslint-disable-next-line no-control-regex
  /\x1B(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1B]*(?:\x07|\x1B\\)|[@-Z\\-_])/g
);
function stripAnsi(text) {
  return text.replace(ANSI_REGEX, "");
}
function normalizeTerminalOutput(raw) {
  const clean = stripAnsi(raw).replace(/\r\n/g, "\n");
  return clean.split("\n").map((line) => {
    if (!line.includes("\r")) return line;
    const parts = line.split("\r").filter((p) => p.length > 0);
    return parts[parts.length - 1] ?? "";
  }).join("\n").trimEnd();
}
var DEFAULT_INLINE_MAX_BYTES = 8800;
function sliceUtf8Bytes(text, maxBytes) {
  if (!Number.isFinite(maxBytes) || maxBytes <= 0) return "";
  const limit = Math.trunc(maxBytes);
  if (limit <= 0) return "";
  if (Buffer.byteLength(text, "utf8") <= limit) return text;
  let bytes = 0;
  let endIndex = 0;
  for (let i = 0; i < text.length; ) {
    const cp = text.codePointAt(i);
    const unitLen = cp > 65535 ? 2 : 1;
    const chBytes = cp <= 127 ? 1 : cp <= 2047 ? 2 : cp <= 65535 ? 3 : 4;
    if (bytes + chBytes > limit) break;
    bytes += chBytes;
    i += unitLen;
    endIndex = i;
  }
  return text.slice(0, endIndex);
}
function truncateWithByteNotice(text, maxBytes) {
  if (!Number.isFinite(maxBytes) || maxBytes <= 0) return "";
  const limit = Math.trunc(maxBytes);
  if (limit <= 0) return "";
  const totalBytes = Buffer.byteLength(text, "utf8");
  if (totalBytes <= limit) return text;
  if (limit < 56) {
    if (limit <= 3) return sliceUtf8Bytes(text, limit);
    return `${sliceUtf8Bytes(text, limit - 3)}...`;
  }
  const maxNoticeBytes = Buffer.byteLength(
    `
... [truncated: showed ${totalBytes} of ${totalBytes} bytes]`,
    "utf8"
  );
  const cutoffBytes = Math.max(0, limit - maxNoticeBytes);
  let slice = sliceUtf8Bytes(text, cutoffBytes);
  const lastNewline = slice.lastIndexOf("\n");
  if (lastNewline > 0 && Buffer.byteLength(slice.slice(0, lastNewline), "utf8") > cutoffBytes * 0.6) {
    slice = slice.slice(0, lastNewline);
  }
  const shownBytes = Buffer.byteLength(slice, "utf8");
  const result = `${slice}
... [truncated: showed ${shownBytes} of ${totalBytes} bytes]`;
  return Buffer.byteLength(result, "utf8") <= limit ? result : sliceUtf8Bytes(result, limit);
}
function sliceSafeChars(text, maxChars) {
  if (!Number.isFinite(maxChars) || maxChars <= 0) return "";
  const limit = Math.trunc(maxChars);
  if (limit <= 0) return "";
  if (text.length <= limit) return text;
  let end = limit;
  const lastCode = text.charCodeAt(end - 1);
  if (lastCode >= 55296 && lastCode <= 56319) {
    end -= 1;
  }
  return text.slice(0, end);
}
function truncateWithNotice(text, maxChars) {
  if (!Number.isFinite(maxChars) || maxChars <= 0) return "";
  const limit = Math.trunc(maxChars);
  if (limit <= 0) return "";
  if (text.length <= limit) return text;
  if (limit < 48) {
    if (limit <= 3) return sliceSafeChars(text, limit);
    return `${sliceSafeChars(text, limit - 3)}...`;
  }
  const maxNoticeLen = `
... [truncated: showed ${text.length} of ${text.length} chars]`.length;
  const cutoff = Math.max(0, limit - maxNoticeLen);
  let slice = sliceSafeChars(text, cutoff);
  const lastNewline = slice.lastIndexOf("\n");
  if (lastNewline > cutoff * 0.6) {
    slice = slice.slice(0, lastNewline);
  }
  const result = `${slice}
... [truncated: showed ${slice.length} of ${text.length} chars]`;
  return result.length <= limit ? result : sliceSafeChars(result, limit);
}
function fitSectionsToBudget(sections, maxChars, separator = "\n\n") {
  if (sections.length === 0 || !Number.isFinite(maxChars) || maxChars <= 0) return "";
  const joined = sections.join(separator);
  if (joined.length <= maxChars) return joined;
  const sepTotal = separator.length * (sections.length - 1);
  if (sepTotal >= maxChars) {
    return truncateWithNotice(joined, maxChars);
  }
  let remainingBudget = maxChars - sepTotal;
  const budgets = new Array(sections.length).fill(0);
  const order = sections.map((s, idx) => ({ idx, len: s.length })).sort((a, b) => a.len - b.len);
  for (let i = 0; i < order.length; i++) {
    const remainingCount = order.length - i;
    const share = Math.floor(remainingBudget / remainingCount);
    const item = order[i];
    if (item.len <= share) {
      budgets[item.idx] = item.len;
      remainingBudget -= item.len;
    } else {
      for (let j = i; j < order.length; j++) {
        const rem = order.length - j;
        const alloc = Math.floor(remainingBudget / rem);
        budgets[order[j].idx] = alloc;
        remainingBudget -= alloc;
      }
      break;
    }
  }
  const fitted = sections.map((sec, idx) => truncateWithNotice(sec, budgets[idx] ?? 0)).join(separator);
  return fitted.length <= maxChars ? fitted : truncateWithNotice(fitted, maxChars);
}
function fitSectionsToByteBudget(sections, maxChars, maxBytes, separator = "\n\n") {
  const charFitted = fitSectionsToBudget(sections, maxChars, separator);
  if (maxBytes === void 0) {
    return charFitted;
  }
  if (!Number.isFinite(maxBytes) || maxBytes <= 0) return "";
  if (Buffer.byteLength(charFitted, "utf8") <= maxBytes) {
    return charFitted;
  }
  const sepBytes = Buffer.byteLength(separator, "utf8") * (sections.length - 1);
  if (sepBytes >= maxBytes) {
    return truncateWithByteNotice(charFitted, maxBytes);
  }
  let remainingBytes = maxBytes - sepBytes;
  const byteBudgets = new Array(sections.length).fill(0);
  const order = sections.map((s, idx) => ({ idx, bytes: Buffer.byteLength(s, "utf8") })).sort((a, b) => a.bytes - b.bytes);
  for (let i = 0; i < order.length; i++) {
    const remainingCount = order.length - i;
    const share = Math.floor(remainingBytes / remainingCount);
    const item = order[i];
    if (item.bytes <= share) {
      byteBudgets[item.idx] = item.bytes;
      remainingBytes -= item.bytes;
    } else {
      for (let j = i; j < order.length; j++) {
        const rem = order.length - j;
        const alloc = Math.floor(remainingBytes / rem);
        byteBudgets[order[j].idx] = alloc;
        remainingBytes -= alloc;
      }
      break;
    }
  }
  let fitted = sections.map((sec, idx) => truncateWithByteNotice(sec, byteBudgets[idx] ?? 0)).join(separator);
  if (fitted.length > maxChars) {
    fitted = truncateWithNotice(fitted, maxChars);
  }
  return Buffer.byteLength(fitted, "utf8") <= maxBytes ? fitted : truncateWithByteNotice(fitted, maxBytes);
}
var ERROR_LINE_PATTERN = /(?:\berr!|\b(?:error|fail|failed|failing|exception|traceback|panic|fatal|assertion|syntaxerror|typeerror|referenceerror|zoderror|validationerror|ts\d{4}|e\d{4}|cannot find|not found|unexpected|undefined)\b|caused by:|application failed to start|compilation error|<<<\s*(?:failure|error)|找不到符号|符号:|位置:)/i;
var ROOT_CAUSE_HEADER_PATTERN = /(?:^\s*(?:description|action):|caused by:|application failed to start|⎯+\s*failed tests|compilation error)/i;
var JSON_INFO_LOG_PATTERNS = [
  /^\s*\{"level":(?:10|20|30)\b/,
  /^\s*\{[^}]*"level"\s*:\s*"(?:trace|debug|info)"/i
];
var ALWAYS_NOISE_LINE_PATTERNS = [
  /^\[ERROR\]\s*(?:To see the full stack trace|Re-run Maven using the -X|For more information about the errors|\[Help \d+\]|->\s*\[Help \d+\])/i,
  /^\(node:\d+\)\s*ExperimentalWarning:/,
  /^\(Use `node --trace-warnings/
];
function isFailureNoiseLine(line) {
  if (ALWAYS_NOISE_LINE_PATTERNS.some((re) => re.test(line))) return true;
  if (JSON_INFO_LOG_PATTERNS.some((re) => re.test(line))) {
    return !/"statusCode"\s*:\s*5\d\d|\b(?:error|exception|failed|fatal)\b/i.test(line);
  }
  return false;
}
var SUMMARY_LINE_PATTERN = /\b(passed|tests?|specs?|suites?|ok|warnings?|errors?|built in|compiled|done in|finished)\b/i;
function formatCompactCommandOutput(params) {
  const out = normalizeTerminalOutput(params.stdout);
  const err = normalizeTerminalOutput(params.stderr);
  const combined = [out, err].filter(Boolean).join("\n");
  if (params.error) {
    return truncateWithNotice(`spawn error: ${params.error}`, params.maxChars);
  }
  if (params.timedOut) {
    const tail = getTailLines(combined, 20);
    return truncateWithNotice(
      tail ? `TIMED OUT
${tail}` : "TIMED OUT (no output)",
      params.maxChars
    );
  }
  if (params.exitCode === 0) {
    if (!combined) return "OK";
    const lines2 = combined.split("\n").map((l) => l.trim()).filter(Boolean);
    const summaryLines = lines2.filter((l) => SUMMARY_LINE_PATTERN.test(l));
    const picked = summaryLines.length > 0 ? summaryLines.slice(-2) : lines2.length <= 2 ? lines2 : [lines2[lines2.length - 1]];
    return truncateWithNotice(picked.join(" | "), Math.min(params.maxChars, 320));
  }
  const rawLines = combined.split("\n");
  const filteredLines = rawLines.filter((line) => !isFailureNoiseLine(line));
  const lines = filteredLines.length > 0 ? filteredLines : rawLines;
  const cleanedCombined = lines.join("\n");
  if (cleanedCombined.length <= params.maxChars) {
    return cleanedCombined || `Exited with code ${params.exitCode ?? "unknown"}`;
  }
  const keepIndices = /* @__PURE__ */ new Set();
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (ROOT_CAUSE_HEADER_PATTERN.test(line)) {
      const from = Math.max(0, i - 1);
      const to = Math.min(lines.length - 1, i + 8);
      for (let j = from; j <= to; j++) keepIndices.add(j);
    } else if (ERROR_LINE_PATTERN.test(line)) {
      const from = Math.max(0, i - 1);
      const to = Math.min(lines.length - 1, i + 3);
      for (let j = from; j <= to; j++) keepIndices.add(j);
    }
  }
  const tailStart = Math.max(0, lines.length - 12);
  for (let i = tailStart; i < lines.length; i++) {
    keepIndices.add(i);
  }
  const sorted = [...keepIndices].sort((a, b) => a - b);
  const outLines = [];
  let prev = -1;
  for (const idx of sorted) {
    if (prev !== -1 && idx > prev + 1) {
      outLines.push(`... (${idx - prev - 1} lines omitted) ...`);
    }
    outLines.push(lines[idx]);
    prev = idx;
  }
  return truncateWithNotice(outLines.join("\n"), params.maxChars);
}
function getTailLines(text, count) {
  if (!text) return "";
  const lines = text.split("\n");
  return lines.slice(-count).join("\n");
}

// src/utils/host-compat.ts
var HOST_COMPAT_RULES = [
  {
    id: "agy-cli-1.5-call-mcp-tool-empty-edits",
    surface: "cli",
    minVersion: "1.5.0",
    // Antigravity CLI >= 1.5.8 supports `"eager": true` in mcp_config.json,
    // which registers typed native mcp_agy-fast_fast_* tools and bypasses
    // lazy call_mcp_tool altogether (keeping token-saving native array edits).
    // Pre-eager CLI 1.5.0-1.5.7 keeps the JSON-string workaround automatically;
    // any lazy/custom setup can still opt in via AGY_FAST_EDITS_FORMAT=string.
    maxVersionExclusive: "1.5.8",
    editsFormat: "string",
    reason: "Pre-eager Antigravity CLI 1.5.0-1.5.7 loads tools lazily via call_mcp_tool where long native edits arrays can drop to []; JSON array string avoids payload loss"
  }
];
function parseHostSemverParts(raw) {
  const m = /(?:^|[^0-9.])v?(\d+)\.(\d+)\.(\d+)(?:\.\d+)?(?:-([0-9A-Za-z.-]+))?(?:$|[^0-9.])/.exec(
    raw.trim()
  );
  if (!m) return null;
  const major = Number.parseInt(m[1], 10);
  const minor = Number.parseInt(m[2], 10);
  const patch = Number.parseInt(m[3], 10);
  if (!Number.isSafeInteger(major) || !Number.isSafeInteger(minor) || !Number.isSafeInteger(patch) || major > 999999 || minor > 999999 || patch > 999999) {
    return null;
  }
  return { major, minor, patch, prerelease: m[4] ?? null };
}
function formatHostSemverParts(parts) {
  const base = `${parts.major}.${parts.minor}.${parts.patch}`;
  return parts.prerelease ? `${base}-${parts.prerelease}` : base;
}
function comparePrerelease(a, b) {
  const aParts = a.split(".");
  const bParts = b.split(".");
  const len = Math.max(aParts.length, bParts.length);
  for (let i = 0; i < len; i++) {
    const ai = aParts[i];
    const bi = bParts[i];
    if (ai === void 0) return -1;
    if (bi === void 0) return 1;
    if (ai === bi) continue;
    const aNum = /^\d+$/.test(ai);
    const bNum = /^\d+$/.test(bi);
    if (aNum && bNum) {
      const diff = Number.parseInt(ai, 10) - Number.parseInt(bi, 10);
      if (diff !== 0) return diff > 0 ? 1 : -1;
    } else if (aNum !== bNum) {
      return aNum ? -1 : 1;
    } else {
      return ai < bi ? -1 : 1;
    }
  }
  return 0;
}
function compareHostSemver(a, b) {
  const pa = parseHostSemverParts(a);
  const pb = parseHostSemverParts(b);
  if (!pa || !pb) return a.localeCompare(b);
  if (pa.major !== pb.major) return pa.major > pb.major ? 1 : -1;
  if (pa.minor !== pb.minor) return pa.minor > pb.minor ? 1 : -1;
  if (pa.patch !== pb.patch) return pa.patch > pb.patch ? 1 : -1;
  if (pa.prerelease === pb.prerelease) return 0;
  if (pa.prerelease === null) return 1;
  if (pb.prerelease === null) return -1;
  return comparePrerelease(pa.prerelease, pb.prerelease);
}
function parseEditsFormatMode(raw) {
  if (typeof raw !== "string") return null;
  const v = raw.trim().toLowerCase();
  if (v === "auto" || v === "array" || v === "string") return v;
  return null;
}
function parseHostOverride(raw) {
  if (typeof raw !== "string") return null;
  const trimmed = raw.trim().toLowerCase();
  if (!trimmed) return null;
  const m = /^(cli|desktop|app|antigravity|ide|unknown)(?:[@\-/\s](.+))?$/.exec(trimmed);
  if (!m) return null;
  const surface = m[1] === "app" || m[1] === "antigravity" ? "desktop" : m[1];
  if (m[2] !== void 0) {
    const parts = parseHostSemverParts(m[2]);
    if (!parts) return null;
    return { surface, version: formatHostSemverParts(parts) };
  }
  return { surface, version: null };
}
var EXPLICIT_THIRD_PARTY_CLIENT_RE = /^(?:claude(?:[-_\s]|$)|cursor(?:[-_\s]|$)|windsurf(?:[-_\s]|$)|vscode(?:[-_\s]|$)|visual[\s_-]+studio[\s_-]+code|codex(?:[-_\s]|$)|zed(?:[-_\s]|$)|cline(?:[-_\s]|$)|roo[-_\s]?code|continue(?:[-_\s]|$)|opencode(?:[-_\s]|$)|aider(?:[-_\s]|$)|goose(?:[-_\s]|$)|jetbrains(?:[-_\s]|$)|neovim(?:[-_\s]|$)|nvim(?:[-_\s]|$)|emacs(?:[-_\s]|$)|mcp[-_\s]?inspector(?:[-_\s]|$))/i;
function isExplicitThirdPartyClient(rawName) {
  if (typeof rawName !== "string") return false;
  return EXPLICIT_THIRD_PARTY_CLIENT_RE.test(rawName.trim());
}
function parseSurfaceFromClientName(rawName) {
  if (typeof rawName !== "string") return null;
  const s = rawName.trim().toLowerCase();
  if (!s || s.startsWith("agy-fast")) return null;
  if (/^(?:google[-_\s]?)?antigravity[-_\s]?cli$/.test(s) || /^agy(?:[-_\s]?cli)?$/.test(s)) {
    return "cli";
  }
  if (/^(?:google[-_\s]?)?antigravity[-_\s]?ide$/.test(s)) {
    return "ide";
  }
  if (/^(?:google[-_\s]?)?antigravity(?:[-_\s]?(?:2\.0|app|desktop))?$/.test(s)) {
    return "desktop";
  }
  return null;
}
function parseLsVersion(raw) {
  if (typeof raw !== "string") return null;
  const trimmed = raw.trim();
  if (!trimmed) return null;
  const parts = parseHostSemverParts(trimmed);
  const version = parts ? formatHostSemverParts(parts) : null;
  if (/^(?:antigravity[-_\s]?)?cli(?:[-_/@\s]|$)/i.test(trimmed)) {
    return { surface: "cli", version };
  }
  if (/^(?:antigravity[-_\s]?)?ide(?:[-_/@\s]|$)/i.test(trimmed)) {
    return { surface: "ide", version };
  }
  if (/^(?:antigravity[-_\s]?)?(?:desktop|app)(?:[-_/@\s]|$)/i.test(trimmed)) {
    return { surface: "desktop", version };
  }
  if (/^antigravity(?:[-_/@\s]|$)/i.test(trimmed)) {
    return { surface: "desktop", version };
  }
  if (version) {
    return { surface: null, version };
  }
  return null;
}
function parseSurfaceFromPath(rawPath, allowTrailingAppDir = false) {
  if (typeof rawPath !== "string" || !rawPath.trim()) return null;
  const segs = rawPath.trim().replace(/\\/g, "/").split("/").filter(Boolean).map((s) => s.toLowerCase());
  for (let i = 0; i < segs.length - 1; i++) {
    if (segs[i] !== ".gemini") continue;
    const next = segs[i + 1];
    if (next === "antigravity-cli") return "cli";
    if (next === "antigravity-ide") return "ide";
    if (next === "antigravity") return "desktop";
  }
  if (allowTrailingAppDir && segs.length > 0) {
    const tail = segs[segs.length - 1];
    if (tail === "antigravity-cli") return "cli";
    if (tail === "antigravity-ide") return "ide";
    if (tail === "antigravity") return "desktop";
  }
  return null;
}
function detectHostIdentity(signals = {}) {
  const env = signals.env ?? process.env;
  const rawClientName = typeof signals.clientInfo?.name === "string" && signals.clientInfo.name.trim() ? signals.clientInfo.name.trim() : void 0;
  const rawClientVersion = typeof signals.clientInfo?.version === "string" && signals.clientInfo.version.trim() ? signals.clientInfo.version.trim() : void 0;
  const modelName = typeof signals.modelName === "string" && signals.modelName.trim() ? signals.modelName.trim() : void 0;
  const override = parseHostOverride(env.AGY_FAST_HOST);
  if (override) {
    return {
      surface: override.surface,
      version: override.version,
      source: "override",
      rawClientName,
      rawClientVersion,
      modelName
    };
  }
  if (isExplicitThirdPartyClient(rawClientName)) {
    return {
      surface: "unknown",
      version: null,
      source: "mcp:client-info",
      rawClientName,
      rawClientVersion,
      modelName
    };
  }
  const clientSurface = parseSurfaceFromClientName(rawClientName);
  const clientVerParts = clientSurface && rawClientVersion ? parseHostSemverParts(rawClientVersion) : null;
  const clientVersion = clientVerParts ? formatHostSemverParts(clientVerParts) : null;
  const hookSurface = parseSurfaceFromPath(signals.transcriptPath) ?? parseSurfaceFromPath(signals.artifactDirectoryPath);
  const lsInfo = parseLsVersion(env.ANTIGRAVITY_LS_VERSION);
  const appDataSurface = parseSurfaceFromPath(env.ANTIGRAVITY_APP_DATA_DIR, true);
  let surface = "unknown";
  let source = "unknown";
  if (clientSurface) {
    surface = clientSurface;
    source = "mcp:client-info";
  } else if (hookSurface) {
    surface = hookSurface;
    source = "hook:path";
  } else if (lsInfo?.surface) {
    surface = lsInfo.surface;
    source = "env:ls-version";
  } else if (appDataSurface) {
    surface = appDataSurface;
    source = "env:app-data-dir";
  }
  let version = null;
  if (clientSurface === surface && clientVersion) {
    version = clientVersion;
  } else if (surface !== "unknown" && lsInfo?.version && (lsInfo.surface === null || lsInfo.surface === surface)) {
    version = lsInfo.version;
  }
  return {
    surface,
    version,
    source,
    rawClientName,
    rawClientVersion,
    modelName
  };
}
function ruleMatchesIdentity(rule, identity) {
  if (rule.surface !== identity.surface) return false;
  if (rule.minVersion !== void 0 || rule.maxVersionExclusive !== void 0) {
    if (!identity.version) return false;
    if (rule.minVersion !== void 0 && compareHostSemver(identity.version, rule.minVersion) < 0) {
      return false;
    }
    if (rule.maxVersionExclusive !== void 0 && compareHostSemver(identity.version, rule.maxVersionExclusive) >= 0) {
      return false;
    }
  }
  return true;
}
function resolveHostCompat(signals = {}, rules = HOST_COMPAT_RULES) {
  const env = signals.env ?? process.env;
  const identity = detectHostIdentity(signals);
  let ruleEditsFormat;
  let callBudgetSec = MCP_CALL_BUDGET_SEC;
  let inlineMaxBytes = DEFAULT_INLINE_MAX_BYTES;
  const matchedRuleIds = [];
  const reasons = [];
  for (const rule of rules) {
    if (!ruleMatchesIdentity(rule, identity)) continue;
    matchedRuleIds.push(rule.id);
    if (rule.editsFormat !== void 0) ruleEditsFormat = rule.editsFormat;
    if (rule.callBudgetSec !== void 0) callBudgetSec = rule.callBudgetSec;
    if (rule.inlineMaxBytes !== void 0) inlineMaxBytes = rule.inlineMaxBytes;
    if (rule.reason) reasons.push(rule.reason);
  }
  const envMode = parseEditsFormatMode(env.AGY_FAST_EDITS_FORMAT);
  const cfgMode = parseEditsFormatMode(signals.configEditsFormat);
  let editsFormat = "array";
  let editsFormatSource = "token-saving-default";
  let reason = reasons[0] ?? "Defaulting to native array edits for lower output token usage (~2.6-3.3x cheaper than double-escaped JSON strings)";
  if (envMode === "array" || envMode === "string") {
    editsFormat = envMode;
    editsFormatSource = "env-override";
    reason = `Forced by AGY_FAST_EDITS_FORMAT=${envMode}`;
  } else if (envMode !== "auto" && (cfgMode === "array" || cfgMode === "string")) {
    editsFormat = cfgMode;
    editsFormatSource = "config-override";
    reason = `Configured in agy-fast config.json (editsFormat=${cfgMode})`;
  } else if (ruleEditsFormat !== void 0) {
    editsFormat = ruleEditsFormat;
    editsFormatSource = "compat-rule";
  }
  return {
    identity,
    editsFormat,
    editsFormatSource,
    callBudgetSec,
    inlineMaxBytes,
    matchedRuleIds,
    reason
  };
}
function getConfigPath(env = process.env) {
  const custom = env.AGY_FAST_CACHE_DIR?.trim();
  const home = process.platform === "win32" ? env.USERPROFILE || env.HOME || os.homedir() : env.HOME || env.USERPROFILE || os.homedir();
  const cacheDir = custom ? path2.resolve(custom) : path2.join(home, ".gemini", "cache", "agy-fast");
  return path2.join(cacheDir, "config.json");
}
function readSavedEditsFormatSync(env = process.env) {
  try {
    const raw = fs.readFileSync(getConfigPath(env), "utf8");
    const parsed = JSON.parse(raw);
    return parseEditsFormatMode(parsed?.editsFormat) ?? void 0;
  } catch {
    return void 0;
  }
}

// src/tools/apply-edits.ts
import { promises as fs4 } from "node:fs";
import path5 from "node:path";

// src/utils/arg-validation.ts
function isPlainObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
function describeValueType(value) {
  if (value === null) return "null";
  if (value === void 0) return "undefined";
  if (Array.isArray(value)) return `array (len=${value.length})`;
  if (typeof value === "boolean") return `boolean (${String(value)})`;
  if (typeof value === "number") return `number (${String(value)})`;
  if (typeof value === "string") return `string (len=${value.length})`;
  return typeof value;
}
function parseMaybeJsonArray(value) {
  if (value === void 0) {
    return { ok: false, kind: "missing", detail: "undefined" };
  }
  if (Array.isArray(value)) {
    return { ok: true, value };
  }
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (!trimmed) {
      return { ok: false, kind: "wrong_type", detail: "empty string" };
    }
    try {
      const parsed = JSON.parse(trimmed);
      if (Array.isArray(parsed)) {
        return { ok: true, value: parsed };
      }
      if (parsed === null || typeof parsed === "boolean") {
        return {
          ok: false,
          kind: "corrupted_literal",
          detail: describeValueType(parsed),
          value: parsed
        };
      }
      return {
        ok: false,
        kind: "invalid_json",
        detail: `decoded to ${describeValueType(parsed)} instead of a JSON array`
      };
    } catch (err) {
      return {
        ok: false,
        kind: "invalid_json",
        detail: err instanceof Error ? err.message : String(err)
      };
    }
  }
  return {
    ok: false,
    kind: "wrong_type",
    detail: describeValueType(value)
  };
}
function formatCorruptedArrayError(options) {
  const details = options.items.slice(0, 8).map(
    (item) => `- ${item.label}: received ${describeValueType(item.value)} instead of ${item.expected ?? "an object"}`
  );
  if (options.items.length > 8) {
    details.push(`- ... (+${options.items.length - 8} more corrupted entries)`);
  }
  return [
    `${options.header}: nested argument serialization corruption detected in call_mcp_tool.`,
    ...details,
    "",
    `Hint: untyped call_mcp_tool Arguments can occasionally collapse nested objects/arrays into booleans, null, or an empty array. Do NOT retry the same ${options.toolName} payload \u2014 ${options.fallbackAdvice}`
  ].join("\n");
}

// src/utils/fs-utils.ts
import { promises as fs2 } from "node:fs";
import os2 from "node:os";
import path3 from "node:path";
var DEFAULT_IGNORE_DIRS = /* @__PURE__ */ new Set([
  ".git",
  ".hg",
  ".svn",
  "node_modules",
  "dist",
  "build",
  "out",
  "target",
  ".next",
  ".nuxt",
  ".svelte-kit",
  ".cache",
  ".turbo",
  "coverage",
  "__pycache__",
  ".venv",
  "venv",
  ".tox",
  ".pytest_cache",
  ".mypy_cache",
  ".ruff_cache",
  ".gradle",
  ".idea",
  "vendor"
]);
function expandHome(inputPath) {
  if (inputPath === "~") return os2.homedir();
  if (inputPath.startsWith("~/") || inputPath.startsWith("~\\")) {
    return path3.join(os2.homedir(), inputPath.slice(2));
  }
  return inputPath;
}
function resolvePath(inputPath, baseDir) {
  const expanded = expandHome(inputPath.trim());
  const root = baseDir ? path3.resolve(expandHome(baseDir)) : process.cwd();
  return path3.isAbsolute(expanded) ? path3.normalize(expanded) : path3.resolve(root, expanded);
}
function toPosixPath(p) {
  return p.split(path3.sep).join("/");
}
function isLikelyBinary(buf) {
  const limit = Math.min(buf.length, 8192);
  for (let i = 0; i < limit; i++) {
    if (buf[i] === 0) return true;
  }
  return false;
}
async function readTextFile(filePath, maxBytes = 10 * 1024 * 1024) {
  const stat = await fs2.stat(filePath);
  if (!stat.isFile()) {
    throw new Error(`Not a regular file: ${filePath}`);
  }
  if (stat.size > maxBytes) {
    throw new Error(
      `File exceeds maximum size (${stat.size} bytes > ${maxBytes} bytes): ${filePath}`
    );
  }
  const buf = await fs2.readFile(filePath);
  if (isLikelyBinary(buf)) {
    throw new Error(`Binary file cannot be read as text: ${filePath}`);
  }
  const content = buf.toString("utf8");
  let crlfCount = 0;
  let lfOnlyCount = 0;
  for (let i = 0; i < content.length; i++) {
    if (content[i] === "\n") {
      if (i > 0 && content[i - 1] === "\r") crlfCount++;
      else lfOnlyCount++;
    }
  }
  const eol = crlfCount > lfOnlyCount ? "\r\n" : "\n";
  return { content, eol, size: stat.size, mode: stat.mode & 4095, rawBuffer: buf };
}
function globToRegExp(pattern) {
  const normalized = pattern.trim().replace(/\\/g, "/").replace(/^\.\//, "");
  let re = "^";
  let i = 0;
  while (i < normalized.length) {
    const ch = normalized[i];
    if (ch === "*") {
      if (normalized[i + 1] === "*") {
        if (normalized[i + 2] === "/") {
          re += "(?:.*/)?";
          i += 3;
        } else {
          re += ".*";
          i += 2;
        }
      } else {
        re += "[^/]*";
        i += 1;
      }
    } else if (ch === "?") {
      re += "[^/]";
      i += 1;
    } else if (ch === "[") {
      const close = normalized.indexOf("]", i + 1);
      if (close === -1) {
        re += "\\[";
        i += 1;
      } else {
        let inner = normalized.slice(i + 1, close).replace(/\\/g, "\\\\");
        if (inner.startsWith("!")) inner = "^" + inner.slice(1);
        re += `[${inner}]`;
        i = close + 1;
      }
    } else if (ch === "{") {
      const close = findMatchingBrace(normalized, i);
      if (close === -1) {
        re += "\\{";
        i += 1;
      } else {
        const parts = splitBraceAlternatives(normalized.slice(i + 1, close));
        const sub = parts.map((part) => globToRegExp(part).source.slice(1, -1)).join("|");
        re += `(?:${sub})`;
        i = close + 1;
      }
    } else if (".+^$()|\\".includes(ch)) {
      re += "\\" + ch;
      i += 1;
    } else {
      re += ch;
      i += 1;
    }
  }
  re += "$";
  try {
    return new RegExp(re);
  } catch (err) {
    throw new Error(
      `Invalid glob pattern '${pattern}': ${err instanceof Error ? err.message : String(err)}`
    );
  }
}
function findMatchingBrace(str, openIdx) {
  let depth = 0;
  for (let i = openIdx; i < str.length; i++) {
    if (str[i] === "{") depth++;
    else if (str[i] === "}") {
      depth--;
      if (depth === 0) return i;
    }
  }
  return -1;
}
function splitBraceAlternatives(inner) {
  const parts = [];
  let depth = 0;
  let start = 0;
  for (let i = 0; i < inner.length; i++) {
    const ch = inner[i];
    if (ch === "{") depth++;
    else if (ch === "}") depth--;
    else if (ch === "," && depth === 0) {
      parts.push(inner.slice(start, i));
      start = i + 1;
    }
  }
  parts.push(inner.slice(start));
  return parts;
}
async function walkFiles(rootDir, options = {}) {
  const maxFiles = options.maxFiles ?? 5e3;
  const maxDepth = options.maxDepth ?? 32;
  const includeHidden = options.includeHidden ?? false;
  const ignoreDirs = options.ignoreDirs ?? DEFAULT_IGNORE_DIRS;
  const fileFilter = options.fileFilter;
  const files = [];
  let truncated = false;
  async function visit(dir, relDir, depth) {
    if (truncated || depth > maxDepth) return;
    let entries;
    try {
      entries = await fs2.readdir(dir, { withFileTypes: true });
    } catch {
      return;
    }
    entries.sort((a, b) => a.name < b.name ? -1 : a.name > b.name ? 1 : 0);
    const subdirs = [];
    for (const entry of entries) {
      if (truncated) return;
      const name = entry.name;
      if (!includeHidden && name.startsWith(".")) {
        if (!(entry.isDirectory() && name === ".agents")) {
          continue;
        }
      }
      const absPath = path3.join(dir, name);
      const relPosix = relDir ? `${relDir}/${name}` : name;
      if (entry.isDirectory()) {
        if (ignoreDirs.has(name)) continue;
        subdirs.push({ abs: absPath, rel: relPosix });
      } else if (entry.isFile()) {
        if (fileFilter && !fileFilter(relPosix)) continue;
        files.push(relPosix);
        if (files.length >= maxFiles) {
          truncated = true;
          return;
        }
      }
    }
    const batchSize = 8;
    for (let i = 0; i < subdirs.length && !truncated; i += batchSize) {
      const slice = subdirs.slice(i, i + batchSize);
      await Promise.all(slice.map((sub) => visit(sub.abs, sub.rel, depth + 1)));
    }
  }
  await visit(rootDir, "", 0);
  return { files, truncated };
}

// src/utils/wm-squeeze-bridge.ts
import fs3 from "node:fs";
import os3 from "node:os";
import path4 from "node:path";
var cachedWmSqueezeBin;
function findWmSqueezeBin(env = process.env) {
  if (env.WM_SQUEEZE_BIN !== void 0) {
    const explicit = env.WM_SQUEEZE_BIN.trim();
    if (!explicit) return null;
    try {
      fs3.accessSync(explicit, fs3.constants.X_OK);
      return explicit;
    } catch {
      return null;
    }
  }
  if (cachedWmSqueezeBin !== void 0) return cachedWmSqueezeBin;
  const home = env.HOME || env.USERPROFILE || os3.homedir();
  const candidates = [path4.join(home, ".local", "bin", "wm-squeeze")];
  const pathEnv = env.PATH || "";
  for (const dir of pathEnv.split(path4.delimiter)) {
    if (dir) candidates.push(path4.join(dir, "wm-squeeze"));
  }
  for (const cand of candidates) {
    try {
      if (fs3.statSync(cand).isFile()) {
        fs3.accessSync(cand, fs3.constants.X_OK);
        cachedWmSqueezeBin = cand;
        return cand;
      }
    } catch {
    }
  }
  cachedWmSqueezeBin = null;
  return null;
}
var SHELL_META_RE = /[|><;`\n$*?\[\]{}]|(?<!&)&(?!&)|(?:^|\s)~/;
var SAFE_ENV_KEYS = /* @__PURE__ */ new Set([
  "PYTHONPATH",
  "RUST_BACKTRACE",
  "RUST_LOG",
  "NO_COLOR",
  "CARGO_TERM_COLOR"
]);
function splitSimpleArgs(raw) {
  const tokens = [];
  let cur = "";
  let quote = null;
  let hasToken = false;
  for (let i = 0; i < raw.length; i++) {
    const ch = raw[i];
    if (quote === "'") {
      if (ch === "'") {
        quote = null;
      } else {
        cur += ch;
      }
      continue;
    }
    if (quote === '"') {
      if (ch === "\\" && i + 1 < raw.length) {
        const nxt = raw[i + 1];
        if (nxt === '"' || nxt === "\\" || nxt === "$" || nxt === "`") {
          cur += nxt;
          i++;
          continue;
        }
      }
      if (ch === '"') {
        quote = null;
      } else {
        cur += ch;
      }
      continue;
    }
    if (ch === "'" || ch === '"') {
      quote = ch;
      hasToken = true;
      continue;
    }
    if (ch === "\\" && i + 1 < raw.length) {
      cur += raw[i + 1];
      hasToken = true;
      i++;
      continue;
    }
    if (/\s/.test(ch)) {
      if (hasToken) {
        tokens.push(cur);
        cur = "";
        hasToken = false;
      }
      continue;
    }
    cur += ch;
    hasToken = true;
  }
  if (quote !== null) return null;
  if (hasToken) tokens.push(cur);
  return tokens;
}
function isSafeSqueezableArgs(cmdBase, cmdArgs) {
  if (cmdBase === "git") {
    let i = 0;
    while (i < cmdArgs.length) {
      const a = cmdArgs[i];
      if (a === "-c" || a === "--paginate" || a.startsWith("-c") || a.startsWith("--exec-path") || a.startsWith("--config-env") || a.startsWith("--upload-pack")) {
        return false;
      }
      if (a === "-C") {
        i += 2;
        continue;
      }
      if (a.startsWith("-")) {
        i += 1;
        continue;
      }
      const sub = a;
      const nxt = cmdArgs[i + 1] ?? "";
      if (["status", "diff", "log", "show", "ls-files", "rev-parse", "blame", "shortlog", "describe"].includes(sub)) {
        return true;
      }
      if (sub === "branch" && !cmdArgs.slice(i + 1).some((x) => ["-d", "-D", "-m", "-M", "--delete"].includes(x))) {
        return true;
      }
      if (sub === "tag" && !cmdArgs.slice(i + 1).some((x) => x === "-d" || x === "--delete")) {
        return true;
      }
      if (sub === "stash" && nxt === "list") return true;
      if (sub === "worktree" && nxt === "list") return true;
      if (sub === "remote" && (nxt === "-v" || nxt === "")) return true;
      return false;
    }
    return false;
  }
  if (cmdBase === "cargo") {
    if (cmdArgs.some((a) => a === "--config" || a.startsWith("--config="))) return false;
    const sub = cmdArgs.find((a) => !a.startsWith("-") && !a.startsWith("+")) ?? "";
    return ["check", "test", "clippy", "build", "tree", "metadata"].includes(sub);
  }
  if (cmdBase === "go") {
    const sub = cmdArgs[0] ?? "";
    return ["test", "vet", "list", "build"].includes(sub);
  }
  if (cmdBase === "pytest") return true;
  if (cmdBase === "python3" || cmdBase === "python") {
    return cmdArgs[0] === "-m" && ["pytest", "unittest", "mypy", "ruff"].includes(cmdArgs[1] ?? "");
  }
  if (cmdBase === "npm" || cmdBase === "pnpm" || cmdBase === "yarn") {
    const s0 = cmdArgs[0] ?? "";
    const s1 = cmdArgs[1] ?? "";
    return ["test", "ls", "outdated"].includes(s0) || s0 === "run" && ["typecheck", "lint", "build", "test", "check"].includes(s1);
  }
  if (cmdBase === "rg") {
    return !cmdArgs.some((a) => a === "--pre" || a.startsWith("--pre="));
  }
  if (cmdBase === "grep") return true;
  if (cmdBase === "find") {
    const bad = /* @__PURE__ */ new Set(["-exec", "-execdir", "-ok", "-okdir", "-delete", "-fls", "-fprint", "-fprint0", "-fprintf"]);
    return !cmdArgs.some((a) => bad.has(a));
  }
  if (["ls", "tree", "wc", "du", "df", "stat", "file", "cat", "head", "tail", "nl", "ruff", "mypy", "eslint", "tsc"].includes(cmdBase)) {
    return true;
  }
  if (cmdBase === "wm3" || cmdBase === "wm-gen3") {
    let sub = "";
    for (let i = 0; i < cmdArgs.length; i++) {
      const a = cmdArgs[i];
      if (a === "--store" || a === "--galaxy") {
        i++;
        continue;
      }
      if (a.startsWith("-")) continue;
      sub = a;
      break;
    }
    return ["recall", "evidence-recall", "stats", "status", "session", "list", "build-info", "inspect"].includes(sub) || cmdArgs.includes("--version");
  }
  if (cmdBase === "gh") {
    const s0 = cmdArgs[0] ?? "";
    const s1 = cmdArgs[1] ?? "";
    return ["pr", "issue", "repo", "run", "workflow", "release"].includes(s0) && ["list", "view", "status", "diff", "checks"].includes(s1);
  }
  return false;
}
function parseSafeSqueezeCommand(rawCmd, baseCwd) {
  let s = (rawCmd || "").trim();
  if (!s || s.includes("wm-squeeze")) return null;
  s = s.replace(/\s+2>&1\s*$/, "").trim();
  if (!s) return null;
  let cwdOverride;
  let toks = null;
  if (s.includes("&&")) {
    const parts = s.split("&&");
    if (parts.length !== 2) return null;
    const left = parts[0].trim();
    const right = parts[1].trim();
    if (SHELL_META_RE.test(left) || SHELL_META_RE.test(right)) return null;
    const ltoks = splitSimpleArgs(left);
    const rtoks = splitSimpleArgs(right);
    if (!ltoks || !rtoks || ltoks.length !== 2 || ltoks[0] !== "cd" || rtoks.length === 0) {
      return null;
    }
    cwdOverride = path4.resolve(baseCwd, ltoks[1]);
    toks = rtoks;
  } else {
    if (SHELL_META_RE.test(s)) return null;
    toks = splitSimpleArgs(s);
    if (!toks || toks.length === 0) return null;
  }
  const envVars = [];
  while (toks.length > 0 && toks[0].includes("=")) {
    const eq = toks[0].indexOf("=");
    const key = toks[0].slice(0, eq);
    if (!SAFE_ENV_KEYS.has(key)) return null;
    envVars.push(toks.shift());
  }
  if (toks.length === 0) return null;
  const cmdBase = path4.basename(toks[0]);
  if (!isSafeSqueezableArgs(cmdBase, toks.slice(1))) return null;
  return {
    cwd: cwdOverride,
    envVars,
    cmdArgs: toks
  };
}
async function tryRunWithWmSqueeze(rawCmd, cwd, timeoutMs, maxBytes, env = process.env) {
  if (env.WM_SQUEEZE_OFF === "1") return null;
  const sqBin = findWmSqueezeBin(env);
  if (!sqBin) return null;
  const plan = parseSafeSqueezeCommand(rawCmd, cwd);
  if (!plan) return null;
  const clampedMaxBytes = Math.max(512, Math.min(8200, Math.trunc(maxBytes)));
  const args = ["--safe", "--cwd", plan.cwd ?? cwd, "--max-bytes", String(clampedMaxBytes)];
  for (const ev of plan.envVars) {
    args.push("--env", ev);
  }
  args.push("--", ...plan.cmdArgs);
  const res = await execBinaryAsync(sqBin, args, {
    cwd: plan.cwd ?? cwd,
    timeoutMs,
    maxOutputBytes: 256 * 1024,
    env
  });
  if (res.error) return null;
  if (res.exitCode === 2 && res.stderr.includes("wm-squeeze --safe refused:")) {
    return null;
  }
  return { squeezed: true, result: res };
}

// src/tools/apply-edits.ts
var DESTRUCTIVE_VERIFY_PATTERNS = [
  [
    /(?:^|[;&|]\s*)rm\s+(?:-(?:[^\s]*r[^\s]*f|[^\s]*f[^\s]*r)[^\s]*|-[^\s]*r[^\s]*\s+-[^\s]*f[^\s]*|-[^\s]*f[^\s]*\s+-[^\s]*r[^\s]*)\s+(?:\/\s*$|\/\*|~|\$HOME(?:\s|$)|\/(?:home|Users)\/[^/\s]+(?:\s|$))/i,
    "Recursive force-delete targeting root or home directory"
  ],
  [/\bdrop\s+(?:table|database|schema|view)\b/i, "SQL DROP statement detected"],
  [/\btruncate\s+table\b/i, "SQL TRUNCATE TABLE detected"],
  [/\bgit\s+push\s+(?:--force|-f)\b.*\b(?:main|master)\b/i, "Force-push to main/master branch detected"],
  [/\bmkfs\.[a-z0-9]+\b|\bdd\s+if=.*\bof=\/dev\//i, "Raw disk/block device formatting or overwrite detected"]
];
function computeLineStarts(text) {
  const starts = [0];
  for (let i = 0; i < text.length; i++) {
    if (text[i] === "\n") {
      starts.push(i + 1);
    }
  }
  return starts;
}
function offsetToLine(lineStarts, offset) {
  let lo = 0;
  let hi = lineStarts.length - 1;
  while (lo <= hi) {
    const mid = lo + hi >> 1;
    if (lineStarts[mid] <= offset) {
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return Math.max(1, hi + 1);
}
function getLineWindowOffsets(text, lineStarts, startLine, endLine) {
  if (startLine !== void 0 && (typeof startLine !== "number" || !Number.isFinite(startLine) || startLine < 1)) {
    throw new Error(`Invalid startLine: ${String(startLine)} (must be integer >= 1)`);
  }
  if (endLine !== void 0 && (typeof endLine !== "number" || !Number.isFinite(endLine) || endLine < 1)) {
    throw new Error(`Invalid endLine: ${String(endLine)} (must be integer >= 1)`);
  }
  const totalLines = lineStarts.length;
  const sLine = startLine !== void 0 ? Math.trunc(startLine) : 1;
  const eLine = endLine !== void 0 ? Math.min(totalLines, Math.trunc(endLine)) : totalLines;
  if (sLine > totalLines) {
    return { windowStart: text.length, windowEnd: text.length };
  }
  if (eLine < sLine) {
    throw new Error(`Invalid line range: startLine (${sLine}) > endLine (${eLine})`);
  }
  const windowStart = lineStarts[sLine - 1];
  const windowEnd = eLine < totalLines ? lineStarts[eLine] : text.length;
  return { windowStart, windowEnd };
}
var MAX_NEARBY_LINES = 26;
function formatNearbyLines(text, startLine, endLine) {
  const lines = text.split("\n");
  if (startLine === void 0 && endLine === void 0) return "";
  const from = Math.max(1, (startLine ?? endLine ?? 1) - 2);
  const rawTo = Math.min(lines.length, (endLine ?? startLine ?? 1) + 2);
  const truncated = rawTo - from + 1 > MAX_NEARBY_LINES;
  const to = truncated ? from + MAX_NEARBY_LINES - 1 : rawTo;
  const snippet = [];
  for (let ln = from; ln <= to; ln++) {
    const lineText = lines[ln - 1] ?? "";
    snippet.push(
      `    ${ln}: ${lineText.length > 160 ? `${sliceSafeChars(lineText, 157)}...` : lineText}`
    );
  }
  const extra = truncated ? `
    ... (${rawTo - to} more lines in range omitted)` : "";
  return `
  Nearby lines (${from}-${to}):
${snippet.join("\n")}${extra}`;
}
function extractLineTokens(trimmedLine) {
  const tokens = trimmedLine.match(/[A-Za-z0-9_$]{3,}|[\u4e00-\u9fff]{2,}/g);
  if (tokens && tokens.length > 0) return tokens;
  return trimmedLine.split(/\s+/).map((s) => s.trim()).filter((s) => s.length >= 2);
}
function findClosestRegionSnippet(normalizedContent, normTarget, rangeStartLine, rangeEndLine) {
  const fileLines = normalizedContent.split("\n");
  if (fileLines.length === 0) return "";
  const targetLines = normTarget.split("\n").map((l) => l.trim()).filter((l) => l.length > 0);
  if (targetLines.length === 0) return "";
  const firstAnchor = targetLines.find((l) => l.length >= 6) ?? targetLines[0];
  let longestAnchor = targetLines[0];
  for (const tl of targetLines) {
    if (tl.length > longestAnchor.length) longestAnchor = tl;
  }
  const anchors = [firstAnchor];
  if (longestAnchor !== firstAnchor) anchors.push(longestAnchor);
  const lastAnchor = targetLines[targetLines.length - 1];
  if (lastAnchor.length >= 6 && !anchors.includes(lastAnchor)) {
    anchors.push(lastAnchor);
  }
  const primaryTokens = new Set(extractLineTokens(firstAnchor));
  const longestTokens = new Set(extractLineTokens(longestAnchor));
  const trimmedFileLines = fileLines.map((l) => l.trim());
  let bestIdx = -1;
  let bestScore = 0;
  const scanStart = Math.max(0, (rangeStartLine ?? 1) - 1);
  const scanEnd = Math.min(
    trimmedFileLines.length - 1,
    (rangeEndLine ?? trimmedFileLines.length) - 1
  );
  for (let i = scanStart; i <= scanEnd; i++) {
    const line = trimmedFileLines[i];
    if (!line) continue;
    let score = 0;
    if (line === firstAnchor) {
      score += 10;
    } else if (firstAnchor.length >= 8 && (line.includes(firstAnchor) || firstAnchor.includes(line))) {
      score += 6;
    } else if (primaryTokens.size > 0) {
      const lineTokens = extractLineTokens(line);
      let hits = 0;
      for (const tok of lineTokens) {
        if (primaryTokens.has(tok)) hits += 1;
      }
      if (hits >= 2 || hits >= 1 && primaryTokens.size <= 2 && hits === primaryTokens.size) {
        score += 4 * (hits / primaryTokens.size);
      }
    }
    if (longestAnchor !== firstAnchor) {
      if (line === longestAnchor) {
        score += 8;
      } else if (longestAnchor.length >= 10 && line.includes(longestAnchor)) {
        score += 5;
      } else if (longestTokens.size >= 2) {
        const lineTokens = extractLineTokens(line);
        let hits = 0;
        for (const tok of lineTokens) {
          if (longestTokens.has(tok)) hits += 1;
        }
        if (hits >= 2) {
          score += 3 * (hits / longestTokens.size);
        }
      }
    }
    if (score >= 2 && targetLines.length > 1) {
      const lookahead = Math.min(targetLines.length, 6);
      for (let k = 1; k < lookahead && i + k < trimmedFileLines.length; k++) {
        const nextLine = trimmedFileLines[i + k];
        const tgtLine = targetLines[k];
        if (nextLine && tgtLine && (nextLine === tgtLine || tgtLine.length >= 8 && nextLine.includes(tgtLine))) {
          score += 3;
        }
      }
    }
    if (score > bestScore) {
      bestScore = score;
      bestIdx = i;
    }
  }
  if (bestIdx < 0 || bestScore < 2.5) return "";
  const span = Math.min(Math.max(targetLines.length, 4), 8);
  const from = Math.max(1, bestIdx + 1 - 2);
  const to = Math.min(fileLines.length, Math.min(from + 13, bestIdx + 1 + span + 2));
  const snippet = [];
  for (let ln = from; ln <= to; ln++) {
    const lineText = fileLines[ln - 1] ?? "";
    snippet.push(
      `    ${ln}: ${lineText.length > 160 ? `${sliceSafeChars(lineText, 157)}...` : lineText}`
    );
  }
  return `
  Closest match near line ${bestIdx + 1} (lines ${from}-${to}):
${snippet.join("\n")}`;
}
function findTrailingWsUniqueMatch(normalizedContent, normTarget, windowStart, windowEnd) {
  const targetLines = normTarget.split("\n");
  if (targetLines.length === 0) return null;
  const trimmedTargetLines = targetLines.map((l) => l.replace(/[ \t]+$/, ""));
  const contentLines = normalizedContent.split("\n");
  const lineStarts = computeLineStarts(normalizedContent);
  const matches = [];
  for (let i = 0; i <= contentLines.length - trimmedTargetLines.length; i++) {
    const startOff = lineStarts[i];
    const endLineIdx = i + trimmedTargetLines.length - 1;
    const endOff = lineStarts[endLineIdx] + contentLines[endLineIdx].length;
    if (startOff < windowStart || endOff > windowEnd) continue;
    let ok = true;
    for (let j = 0; j < trimmedTargetLines.length; j++) {
      if (contentLines[i + j].replace(/[ \t]+$/, "") !== trimmedTargetLines[j]) {
        ok = false;
        break;
      }
    }
    if (ok) {
      matches.push({ start: startOff, end: endOff });
      if (matches.length > 1) return null;
    }
  }
  return matches.length === 1 ? matches[0] : null;
}
function planHunksForContent(normalizedContent, hunks, displayPath, defaultStrictWindow = false) {
  const lineStarts = computeLineStarts(normalizedContent);
  const spans = [];
  const notes = [];
  const errors = [];
  for (let idx = 0; idx < hunks.length; idx++) {
    const hunk = hunks[idx];
    if (!isPlainObject(hunk)) {
      errors.push(`${displayPath} hunk #${idx + 1}: must be an object with 'target' and 'replacement'`);
      continue;
    }
    if (typeof hunk.target !== "string" || hunk.target.length === 0) {
      errors.push(`${displayPath} hunk #${idx + 1}: 'target' must be a non-empty string`);
      continue;
    }
    if (typeof hunk.replacement !== "string") {
      errors.push(`${displayPath} hunk #${idx + 1}: 'replacement' must be a string`);
      continue;
    }
    const normTarget = hunk.target.replace(/\r\n/g, "\n");
    const normReplacement = hunk.replacement.replace(/\r\n/g, "\n");
    const strictWin = hunk.strictWindow ?? defaultStrictWindow;
    let windowStart = 0;
    let windowEnd = normalizedContent.length;
    try {
      const win = getLineWindowOffsets(
        normalizedContent,
        lineStarts,
        hunk.startLine,
        hunk.endLine
      );
      windowStart = win.windowStart;
      windowEnd = win.windowEnd;
    } catch (err) {
      errors.push(
        `${displayPath} hunk #${idx + 1}: ${err instanceof Error ? err.message : String(err)}`
      );
      continue;
    }
    const foundOffsets = [];
    let pos = windowStart;
    while (pos <= windowEnd - normTarget.length) {
      const found = normalizedContent.indexOf(normTarget, pos);
      if (found === -1 || found + normTarget.length > windowEnd) break;
      foundOffsets.push(found);
      pos = found + Math.max(1, normTarget.length);
    }
    if (foundOffsets.length === 0) {
      const hasRange = hunk.startLine !== void 0 || hunk.endLine !== void 0;
      const rangeNote = hasRange ? ` within lines ${hunk.startLine ?? 1}-${hunk.endLine ?? lineStarts.length}` : "";
      const globalOffsets = [];
      let gPos = 0;
      while (gPos <= normalizedContent.length - normTarget.length && globalOffsets.length < 5) {
        const found = normalizedContent.indexOf(normTarget, gPos);
        if (found === -1) break;
        globalOffsets.push(found);
        gPos = found + Math.max(1, normTarget.length);
      }
      if (hasRange && !strictWin && globalOffsets.length === 1) {
        const actualOff = globalOffsets[0];
        const actualLine = offsetToLine(lineStarts, actualOff);
        spans.push({
          start: actualOff,
          end: actualOff + normTarget.length,
          replacement: normReplacement,
          hunkIndex: idx
        });
        notes.push(
          `hunk #${idx + 1} auto-relocated L${hunk.startLine ?? 1}-${hunk.endLine ?? lineStarts.length} -> L${actualLine}`
        );
        continue;
      }
      if (!strictWin && globalOffsets.length === 0) {
        const twMatch = findTrailingWsUniqueMatch(normalizedContent, normTarget, windowStart, windowEnd) ?? (hasRange ? findTrailingWsUniqueMatch(normalizedContent, normTarget, 0, normalizedContent.length) : null);
        if (twMatch) {
          spans.push({
            start: twMatch.start,
            end: twMatch.end,
            replacement: normReplacement,
            hunkIndex: idx
          });
          notes.push(`hunk #${idx + 1} trailing-ws normalized at L${offsetToLine(lineStarts, twMatch.start)}`);
          continue;
        }
      }
      let hint = "";
      if (hasRange) {
        if (globalOffsets.length > 0) {
          const hitLines = globalOffsets.map((o) => `L${offsetToLine(lineStarts, o)}`).join(", ");
          hint = ` Exact match found outside range at ${hitLines} \u2014 adjust startLine/endLine or omit them.`;
        } else {
          const effStart = hunk.startLine ?? 1;
          const effEnd = hunk.endLine ?? lineStarts.length;
          if (effEnd - effStart + 1 > MAX_NEARBY_LINES - 4) {
            hint = findClosestRegionSnippet(normalizedContent, normTarget, effStart, effEnd) || findClosestRegionSnippet(normalizedContent, normTarget) || formatNearbyLines(normalizedContent, effStart, effEnd);
          } else {
            hint = formatNearbyLines(normalizedContent, hunk.startLine, hunk.endLine);
          }
        }
      } else {
        hint = findClosestRegionSnippet(normalizedContent, normTarget);
      }
      errors.push(
        `${displayPath} hunk #${idx + 1}: target text not found${rangeNote}.${hint}`
      );
      continue;
    }
    if (foundOffsets.length > 1 && !hunk.replaceAll) {
      const lines = foundOffsets.slice(0, 10).map((o) => offsetToLine(lineStarts, o)).join(", ");
      errors.push(
        `${displayPath} hunk #${idx + 1}: target matched ${foundOffsets.length} times (lines: ${lines}). Provide startLine/endLine, more surrounding context, or set replaceAll: true.`
      );
      continue;
    }
    for (const start of foundOffsets) {
      spans.push({
        start,
        end: start + normTarget.length,
        replacement: normReplacement,
        hunkIndex: idx
      });
    }
  }
  if (errors.length > 0) {
    return { newNormalized: normalizedContent, appliedMatches: 0, notes: [], errors };
  }
  spans.sort((a, b) => a.start - b.start || a.end - b.end);
  for (let i = 1; i < spans.length; i++) {
    const prev = spans[i - 1];
    const curr = spans[i];
    if (curr.start < prev.end) {
      const line = offsetToLine(lineStarts, curr.start);
      errors.push(
        `${displayPath}: hunk #${prev.hunkIndex + 1} and hunk #${curr.hunkIndex + 1} overlap near line ${line}.`
      );
    }
  }
  if (errors.length > 0) {
    return { newNormalized: normalizedContent, appliedMatches: 0, notes: [], errors };
  }
  const pieces = [];
  let cursor = 0;
  for (const span of spans) {
    pieces.push(normalizedContent.slice(cursor, span.start));
    pieces.push(span.replacement);
    cursor = span.end;
  }
  pieces.push(normalizedContent.slice(cursor));
  return {
    newNormalized: pieces.join(""),
    appliedMatches: spans.length,
    notes,
    errors: []
  };
}
function countLines(text) {
  if (!text) return 0;
  const end = text.endsWith("\n") ? text.length - 1 : text.length;
  if (end === 0) return 1;
  let count = 1;
  for (let i = 0; i < end; i++) {
    if (text[i] === "\n") count++;
  }
  return count;
}
async function commitPlannedWrites(plans) {
  const written = [];
  try {
    for (const plan of plans) {
      const target = plan.writeTargetPath;
      const dir = path5.dirname(target);
      await fs4.mkdir(dir, { recursive: true });
      const tmpPath = `${target}.agy-fast-tmp-${process.pid}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      try {
        await fs4.writeFile(tmpPath, plan.newContent, "utf8");
        if (plan.originalMode !== null && process.platform !== "win32") {
          await fs4.chmod(tmpPath, plan.originalMode);
        }
      } catch (tmpErr) {
        try {
          await fs4.unlink(tmpPath);
        } catch {
        }
        throw tmpErr;
      }
      try {
        await fs4.rename(tmpPath, target);
        written.push(plan);
      } catch (renameErr) {
        try {
          await fs4.unlink(tmpPath);
        } catch {
        }
        written.push(plan);
        if (plan.originalSymlinkTarget) {
          try {
            await fs4.unlink(target);
          } catch {
          }
        }
        await fs4.writeFile(target, plan.newContent, "utf8");
        if (plan.originalMode !== null && process.platform !== "win32") {
          try {
            await fs4.chmod(target, plan.originalMode);
          } catch {
          }
        }
        void renameErr;
      }
    }
  } catch (err) {
    for (let i = written.length - 1; i >= 0; i--) {
      const item = written[i];
      const target = item.writeTargetPath;
      try {
        if (item.originalSymlinkTarget) {
          await fs4.unlink(target).catch(() => {
          });
          await fs4.symlink(item.originalSymlinkTarget, target);
        } else if (item.existed && item.originalBuffer !== null) {
          await fs4.writeFile(target, item.originalBuffer);
          if (item.originalMode !== null && process.platform !== "win32") {
            await fs4.chmod(target, item.originalMode);
          }
        } else if (!item.existed) {
          await fs4.unlink(target);
        }
      } catch {
      }
    }
    throw err;
  }
}
function corruptedEditsError(items) {
  return {
    text: formatCorruptedArrayError({
      header: "Validation failed (0 files modified)",
      toolName: "apply_edits",
      items,
      fallbackAdvice: "immediately fall back to parallel built-in edit_file / write_file calls for these file(s)."
    }),
    isError: true
  };
}
async function runApplyEdits(input) {
  const parsedEdits = parseMaybeJsonArray(input?.edits);
  if (!parsedEdits.ok) {
    if (parsedEdits.kind === "corrupted_literal" || parsedEdits.kind === "wrong_type" && (typeof input?.edits === "boolean" || input?.edits === null)) {
      return corruptedEditsError([
        {
          label: "edits",
          value: parsedEdits.kind === "corrupted_literal" ? parsedEdits.value : input.edits,
          expected: "an array of edit objects"
        }
      ]);
    }
    if (parsedEdits.kind === "invalid_json") {
      return {
        text: `Error: 'edits' is not a valid JSON array (0 files modified): ${parsedEdits.detail}`,
        isError: true
      };
    }
    return {
      text: "Error: 'edits' must be a non-empty JSON array string (or array) of file edit objects.",
      isError: true
    };
  }
  const rawEdits = parsedEdits.value;
  if (rawEdits.length === 0) {
    return corruptedEditsError([
      { label: "edits", value: rawEdits, expected: "a non-empty array of edit objects" }
    ]);
  }
  if (rawEdits.length > 50) {
    return {
      text: "Error: 'edits' supports at most 50 file entries per call.",
      isError: true
    };
  }
  const corruptedItems = [];
  const normalizedEdits = [];
  for (let i = 0; i < rawEdits.length; i++) {
    const rawEntry = rawEdits[i];
    if (!isPlainObject(rawEntry)) {
      corruptedItems.push({ label: `edits[${i}]`, value: rawEntry, expected: "an edit object" });
      continue;
    }
    let normalizedHunks;
    if (rawEntry.hunks !== void 0) {
      const parsedHunks = parseMaybeJsonArray(rawEntry.hunks);
      if (!parsedHunks.ok) {
        corruptedItems.push({
          label: `edits[${i}].hunks`,
          value: parsedHunks.kind === "corrupted_literal" ? parsedHunks.value : rawEntry.hunks,
          expected: "an array of hunk objects"
        });
        continue;
      }
      let hunkCorrupted = false;
      for (let j = 0; j < parsedHunks.value.length; j++) {
        if (!isPlainObject(parsedHunks.value[j])) {
          corruptedItems.push({
            label: `edits[${i}].hunks[${j}]`,
            value: parsedHunks.value[j],
            expected: "a hunk object"
          });
          hunkCorrupted = true;
        }
      }
      if (hunkCorrupted) continue;
      normalizedHunks = parsedHunks.value;
    }
    normalizedEdits.push({
      path: rawEntry.path,
      hunks: normalizedHunks,
      create: rawEntry.create,
      overwrite: rawEntry.overwrite
    });
  }
  if (corruptedItems.length > 0) {
    return corruptedEditsError(corruptedItems);
  }
  let validatedVerify = null;
  if (input.verify !== void 0) {
    if (typeof input.verify === "string") {
      if (input.verify.trim()) {
        validatedVerify = { command: input.verify };
      }
    } else if (isPlainObject(input.verify)) {
      const cmd = input.verify.command;
      if (typeof cmd !== "string" || !cmd.trim()) {
        return {
          text: "Validation failed (0 files modified):\n- 'verify.command' must be a non-empty string.",
          isError: true
        };
      }
      if (input.verify.cwd !== void 0 && typeof input.verify.cwd !== "string") {
        return {
          text: "Validation failed (0 files modified):\n- 'verify.cwd' must be a string.",
          isError: true
        };
      }
      const rawSpecTimeout = input.verify.timeoutSec;
      const parsedSpecTimeout = typeof rawSpecTimeout === "number" || typeof rawSpecTimeout === "string" && rawSpecTimeout.trim() !== "" ? Number(rawSpecTimeout) : void 0;
      validatedVerify = {
        command: cmd,
        cwd: input.verify.cwd,
        timeoutSec: parsedSpecTimeout !== void 0 && Number.isFinite(parsedSpecTimeout) ? parsedSpecTimeout : void 0
      };
    } else {
      return {
        text: "Validation failed (0 files modified):\n- 'verify' must be a command string or { command, cwd?, timeoutSec? } object.",
        isError: true
      };
    }
  }
  const baseCwd = resolvePath(input.cwd || process.cwd());
  const mergedByPath = /* @__PURE__ */ new Map();
  const errors = [];
  for (let i = 0; i < normalizedEdits.length; i++) {
    const entry = normalizedEdits[i];
    if (typeof entry.path !== "string" || !entry.path.trim()) {
      errors.push(`edits[${i}]: 'path' must be a non-empty string`);
      continue;
    }
    const absPath = resolvePath(entry.path, baseCwd);
    let writeTargetPath = absPath;
    try {
      writeTargetPath = await fs4.realpath(absPath);
    } catch {
      try {
        const realParent = await fs4.realpath(path5.dirname(absPath));
        writeTargetPath = path5.join(realParent, path5.basename(absPath));
      } catch {
      }
    }
    const relPath = toPosixPath(path5.relative(baseCwd, absPath)) || path5.basename(absPath);
    const hasCreate = typeof entry.create === "string";
    const hasHunks = Array.isArray(entry.hunks) && entry.hunks.length > 0;
    if (!hasCreate && !hasHunks) {
      errors.push(`${relPath}: must specify either non-empty 'hunks' or 'create'`);
      continue;
    }
    if (hasCreate && hasHunks) {
      errors.push(`${relPath}: cannot specify both 'create' and 'hunks' on the same entry`);
      continue;
    }
    const existing = mergedByPath.get(writeTargetPath);
    if (!existing) {
      mergedByPath.set(writeTargetPath, {
        absPath,
        writeTargetPath,
        relPath,
        create: hasCreate ? entry.create : void 0,
        overwrite: entry.overwrite,
        hunks: hasHunks ? [...entry.hunks] : []
      });
    } else {
      if (hasCreate || existing.create !== void 0) {
        errors.push(`${relPath}: duplicate entries cannot mix 'create' with other edits`);
        continue;
      }
      existing.hunks.push(...entry.hunks);
    }
  }
  if (errors.length > 0) {
    return {
      text: `Validation failed (0 files modified):
- ${errors.join("\n- ")}`,
      isError: true
    };
  }
  const plans = [];
  for (const item of mergedByPath.values()) {
    if (item.create !== void 0) {
      let exists = false;
      let originalBuffer = null;
      let originalMode = null;
      let originalSymlinkTarget = null;
      try {
        const lst = await fs4.lstat(item.writeTargetPath);
        if (lst.isSymbolicLink()) {
          try {
            const st = await fs4.stat(item.writeTargetPath);
            if (st.isDirectory()) {
              errors.push(`${item.relPath}: target path is a directory`);
              continue;
            }
            exists = true;
            if (!item.overwrite) {
              errors.push(
                `${item.relPath}: file already exists (set overwrite: true to replace it)`
              );
              continue;
            }
            originalMode = st.mode & 4095;
            originalBuffer = await fs4.readFile(item.writeTargetPath);
          } catch (statErr) {
            if (statErr?.code === "ENOENT") {
              exists = true;
              if (!item.overwrite) {
                errors.push(
                  `${item.relPath}: dangling symlink already exists (set overwrite: true to replace it)`
                );
                continue;
              }
              originalSymlinkTarget = await fs4.readlink(item.writeTargetPath);
            } else {
              errors.push(
                `${item.relPath}: ${statErr instanceof Error ? statErr.message : String(statErr)}`
              );
              continue;
            }
          }
        } else {
          const st = await fs4.stat(item.writeTargetPath);
          if (st.isDirectory()) {
            errors.push(`${item.relPath}: target path is a directory`);
            continue;
          }
          exists = true;
          if (!item.overwrite) {
            errors.push(
              `${item.relPath}: file already exists (set overwrite: true to replace it)`
            );
            continue;
          }
          originalMode = st.mode & 4095;
          originalBuffer = await fs4.readFile(item.writeTargetPath);
        }
      } catch (err) {
        if (err?.code === "ENOENT") {
          exists = false;
          originalBuffer = null;
          originalMode = null;
        } else {
          errors.push(
            `${item.relPath}: ${err instanceof Error ? err.message : String(err)}`
          );
          continue;
        }
      }
      const oldLines = originalBuffer ? countLines(originalBuffer.toString("utf8")) : 0;
      const newLines = countLines(item.create);
      plans.push({
        absPath: item.absPath,
        writeTargetPath: item.writeTargetPath,
        relPath: item.relPath,
        existed: exists,
        originalBuffer,
        originalMode,
        originalSymlinkTarget,
        newContent: item.create,
        hunkCount: 1,
        lineDelta: newLines - oldLines,
        created: !exists
      });
      continue;
    }
    let fileData;
    try {
      fileData = await readTextFile(item.writeTargetPath);
    } catch (err) {
      errors.push(
        `${item.relPath}: ${err instanceof Error ? err.message : String(err)}`
      );
      continue;
    }
    const normalizedOriginal = fileData.content.replace(/\r\n/g, "\n");
    const planned = planHunksForContent(
      normalizedOriginal,
      item.hunks,
      item.relPath,
      Boolean(input.strictWindow)
    );
    if (planned.errors.length > 0) {
      errors.push(...planned.errors);
      continue;
    }
    const finalContent = fileData.eol === "\r\n" ? planned.newNormalized.replace(/\n/g, "\r\n") : planned.newNormalized;
    const lineDelta = countLines(planned.newNormalized) - countLines(normalizedOriginal);
    plans.push({
      absPath: item.absPath,
      writeTargetPath: item.writeTargetPath,
      relPath: item.relPath,
      existed: true,
      originalBuffer: fileData.rawBuffer,
      originalMode: fileData.mode,
      newContent: finalContent,
      hunkCount: planned.appliedMatches,
      lineDelta,
      created: false,
      notes: planned.notes
    });
  }
  const bestEffort = Boolean(input.bestEffort);
  if (errors.length > 0 && (!bestEffort || plans.length === 0)) {
    const prefix = bestEffort ? "Edit aborted (0 files modified):" : "Atomic edit aborted (0 files modified):";
    return {
      text: truncateWithByteNotice(
        `${prefix}
- ${errors.join("\n- ")}`,
        DEFAULT_INLINE_MAX_BYTES
      ),
      isError: true
    };
  }
  if (!input.dryRun) {
    try {
      await commitPlannedWrites(plans);
    } catch (err) {
      return {
        text: `Write failed (changes rolled back): ${err instanceof Error ? err.message : String(err)}`,
        isError: true
      };
    }
  }
  const summaryLines = plans.map((p) => {
    const deltaStr = p.lineDelta >= 0 ? `+${p.lineDelta}` : `${p.lineDelta}`;
    const noteStr = p.notes && p.notes.length > 0 ? `; ${p.notes.join("; ")}` : "";
    const action = p.created ? "created" : `updated (${p.hunkCount} match${p.hunkCount === 1 ? "" : "es"}${noteStr})`;
    return `- ${p.relPath}: ${action}, lines ${deltaStr}`;
  });
  if (bestEffort && errors.length > 0) {
    const totalFiles = mergedByPath.size;
    const failedFileCount = Math.max(1, totalFiles - plans.length);
    const partialHeader = input.dryRun ? `Partial dry-run (${plans.length}/${totalFiles} file(s) valid; ${failedFileCount} file(s) failed):` : `Partial edit applied (${plans.length}/${totalFiles} file(s) written; ${failedFileCount} file(s) failed \u2014 do NOT re-send applied files):`;
    const partialParts = [
      partialHeader,
      `Applied:
${summaryLines.join("\n")}`,
      `Failed (fix only these):
- ${errors.join("\n- ")}`
    ];
    if (!input.dryRun && input.verify) {
      partialParts.push(
        "Verify skipped because failed file(s) remain; fix only the failed file(s) and re-run verify."
      );
    }
    return {
      text: truncateWithByteNotice(
        partialParts.join("\n\n"),
        DEFAULT_INLINE_MAX_BYTES
      ),
      isError: true
    };
  }
  const header = input.dryRun ? `Dry run validated ${plans.length} file(s) (0 files modified):` : `Applied edits to ${plans.length} file(s):`;
  const parts = [`${header}
${summaryLines.join("\n")}`];
  if (!input.dryRun && validatedVerify) {
    const verifySpec = validatedVerify;
    const verifyCwd = verifySpec.cwd ? resolvePath(verifySpec.cwd, baseCwd) : baseCwd;
    const timeoutCandidate = verifySpec.timeoutSec ?? input.verifyTimeoutSec;
    const rawTimeoutSec = typeof timeoutCandidate === "number" || typeof timeoutCandidate === "string" && timeoutCandidate.trim() !== "" ? Number(timeoutCandidate) : 60;
    const timeoutMs = (Number.isFinite(rawTimeoutSec) ? Math.max(1, Math.min(MCP_CALL_BUDGET_SEC, Math.trunc(rawTimeoutSec))) : 60) * 1e3;
    const rawMaxVerifyChars = Number(input.maxVerifyChars ?? 4e3);
    const maxVerifyChars = Number.isFinite(rawMaxVerifyChars) ? Math.max(300, Math.min(7500, Math.trunc(rawMaxVerifyChars))) : 4e3;
    for (const [pat, reason] of DESTRUCTIVE_VERIFY_PATTERNS) {
      if (pat.test(verifySpec.command)) {
        parts.push(`
Verify [BLOCKED]: ${reason} in verify command \`${verifySpec.command}\`.`);
        return {
          text: truncateWithByteNotice(parts.join("\n"), DEFAULT_INLINE_MAX_BYTES),
          isError: true
        };
      }
    }
    const sq = await tryRunWithWmSqueeze(
      verifySpec.command,
      verifyCwd,
      timeoutMs,
      maxVerifyChars
    );
    if (sq) {
      const res = sq.result;
      const status = res.timedOut ? "TIMEOUT" : res.exitCode === 0 ? "PASS" : `FAIL (exit ${res.exitCode ?? "err"})`;
      const combined = [res.stdout.trim(), res.stderr.trim()].filter(Boolean).join("\n");
      const sqTag = combined.includes("cache-hit") ? "squeezed+cached" : "squeezed";
      const formatted = sliceSafeChars(combined || "(no output)", maxVerifyChars);
      parts.push(
        `
Verify [${status}, ${sqTag} in ${res.durationMs}ms] \`${verifySpec.command}\`:
${formatted}`
      );
    } else {
      const res = await runShellCommandAsync(verifySpec.command, {
        cwd: verifyCwd,
        timeoutMs
      });
      const status = res.timedOut ? "TIMEOUT" : res.exitCode === 0 ? "PASS" : `FAIL (exit ${res.exitCode ?? "err"})`;
      const formatted = formatCompactCommandOutput({
        stdout: res.stdout,
        stderr: res.stderr,
        exitCode: res.exitCode,
        timedOut: res.timedOut,
        error: res.error,
        maxChars: maxVerifyChars
      });
      parts.push(
        `
Verify [${status} in ${res.durationMs}ms] \`${verifySpec.command}\`:
${formatted}`
      );
    }
  }
  const outText = parts.join("\n");
  const finalText = truncateWithByteNotice(outText, DEFAULT_INLINE_MAX_BYTES);
  return { text: finalText, isError: false };
}

// src/tools/batch-search.ts
import { promises as fs5 } from "node:fs";
import path6 from "node:path";

// src/utils/fuzzy-match.ts
function findContiguousIndex(haystack, needle) {
  const maxStart = haystack.length - needle.length;
  for (let start = 0; start <= maxStart; start++) {
    let matched = true;
    for (let offset = 0; offset < needle.length; offset++) {
      if (haystack[start + offset] !== needle[offset]) {
        matched = false;
        break;
      }
    }
    if (matched) return start;
  }
  return -1;
}
function isAscii(text) {
  for (let i = 0; i < text.length; i++) {
    if (text.charCodeAt(i) > 127) return false;
  }
  return true;
}
function subsequenceCost(needle, haystack) {
  if (!needle) return 2147483647;
  if (isAscii(needle) && isAscii(haystack)) {
    const query2 = needle.toLowerCase();
    const lowered2 = haystack.toLowerCase();
    const contiguousAt2 = lowered2.indexOf(query2);
    if (contiguousAt2 >= 0) {
      return contiguousAt2 === 0 ? -100 : -60 + Math.min(25, contiguousAt2);
    }
    let cursor2 = 0;
    let first2 = -1;
    let last2 = -1;
    const hLen = lowered2.length;
    for (let i = 0; i < query2.length; i++) {
      const targetCode = query2.charCodeAt(i);
      while (cursor2 < hLen && lowered2.charCodeAt(cursor2) !== targetCode) cursor2++;
      if (cursor2 === hLen) return void 0;
      if (first2 < 0) first2 = cursor2;
      last2 = cursor2++;
    }
    return Math.max(0, last2 - first2 + 1 - query2.length) - (first2 === 0 ? 20 : 0);
  }
  const lowered = [...haystack.toLowerCase()];
  const query = [...needle.toLowerCase()];
  const contiguousAt = findContiguousIndex(lowered, query);
  if (contiguousAt >= 0) {
    return contiguousAt === 0 ? -100 : -60 + Math.min(25, contiguousAt);
  }
  let cursor = 0;
  let first = -1;
  let last = -1;
  for (const ch of query) {
    while (cursor < lowered.length && lowered[cursor] !== ch) cursor++;
    if (cursor === lowered.length) return void 0;
    if (first < 0) first = cursor;
    last = cursor++;
  }
  return Math.max(0, last - first + 1 - query.length) - (first === 0 ? 20 : 0);
}
function scoreSubsequence(query, target) {
  const cost = subsequenceCost(query, target);
  return cost === void 0 ? -1 : Math.max(1, 1e3 - cost);
}
function fuzzyScorePath(query, candidatePosix) {
  const trimmed = query.trim().replace(/\\/g, "/");
  if (!trimmed) return -1;
  const tokens = trimmed.split(/\s+/).filter(Boolean);
  const slashIdx = candidatePosix.lastIndexOf("/");
  const basename = slashIdx === -1 ? candidatePosix : candidatePosix.slice(slashIdx + 1);
  let totalScore = 0;
  for (const token of tokens) {
    const fullScore = scoreSubsequence(token, candidatePosix);
    if (fullScore < 0) return -1;
    const baseScore = !token.includes("/") ? scoreSubsequence(token, basename) : -1;
    totalScore += baseScore >= 0 ? Math.max(fullScore, baseScore + 45) : fullScore;
    if (basename.toLowerCase() === token.toLowerCase()) totalScore += 60;
  }
  return totalScore;
}
function rankFuzzyPaths(query, candidates, maxResults) {
  const scored = [];
  for (const candidate of candidates) {
    const score = fuzzyScorePath(query, candidate);
    if (score >= 0) scored.push({ path: candidate, score });
  }
  scored.sort((a, b) => b.score - a.score || a.path.length - b.path.length || (a.path < b.path ? -1 : a.path > b.path ? 1 : 0));
  return {
    matches: scored.slice(0, maxResults),
    totalMatched: scored.length
  };
}

// src/tools/batch-search.ts
var READONLY_GIT_SUBCOMMANDS = /* @__PURE__ */ new Set([
  "status",
  "diff",
  "log",
  "show",
  "branch",
  "rev-parse",
  "ls-files",
  "blame",
  "shortlog",
  "describe",
  "tag",
  "remote"
]);
var FORBIDDEN_GIT_GLOBAL_FLAGS = /* @__PURE__ */ new Set([
  "-c",
  "--config",
  "--config-env",
  "--exec-path",
  "--ext-diff",
  "--textconv",
  "--output",
  "--exec",
  "--upload-pack",
  "--git-dir",
  "--work-tree"
]);
var ALLOWED_BRANCH_BOOL_FLAGS = /* @__PURE__ */ new Set([
  "--all",
  "--remotes",
  "--verbose",
  "--list",
  "--show-current",
  "--no-abbrev",
  "--no-color",
  "--ignore-case",
  "--omit-empty",
  "--no-column",
  "--quiet"
]);
var ALLOWED_BRANCH_EQ_ONLY_OPTIONAL_FLAGS = /* @__PURE__ */ new Set([
  "--abbrev",
  "--color",
  "--column"
]);
var ALLOWED_BRANCH_COMMIT_OPTIONAL_FLAGS = /* @__PURE__ */ new Set([
  "--merged",
  "--no-merged"
]);
var ALLOWED_BRANCH_COMMIT_VALUE_FLAGS = /* @__PURE__ */ new Set([
  "--contains",
  "--no-contains",
  "--points-at"
]);
var ALLOWED_BRANCH_VALUE_FLAGS = /* @__PURE__ */ new Set([
  "--contains",
  "--no-contains",
  "--points-at",
  "--sort",
  "--format"
]);
var ALLOWED_BRANCH_SHORT_CHARS = /* @__PURE__ */ new Set(["a", "r", "v", "l", "q", "i"]);
var ALLOWED_TAG_BOOL_FLAGS = /* @__PURE__ */ new Set([
  "--list",
  "--no-color",
  "--ignore-case",
  "--omit-empty",
  "--no-column"
]);
var ALLOWED_TAG_EQ_ONLY_OPTIONAL_FLAGS = /* @__PURE__ */ new Set([
  "--color",
  "--column"
]);
var ALLOWED_TAG_COMMIT_OPTIONAL_FLAGS = /* @__PURE__ */ new Set([
  "--merged",
  "--no-merged"
]);
var ALLOWED_TAG_COMMIT_VALUE_FLAGS = /* @__PURE__ */ new Set([
  "--contains",
  "--no-contains",
  "--points-at"
]);
var ALLOWED_TAG_VALUE_FLAGS = /* @__PURE__ */ new Set([
  "--contains",
  "--no-contains",
  "--points-at",
  "--sort",
  "--format"
]);
var ALLOWED_TAG_SHORT_CHARS = /* @__PURE__ */ new Set(["l", "i"]);
var ALLOWED_REMOTE_SUBCOMMANDS = /* @__PURE__ */ new Set(["show", "get-url"]);
function tokenizeArgs(input) {
  const tokens = [];
  let current = "";
  let quote = null;
  let escape = false;
  for (let i = 0; i < input.length; i++) {
    const ch = input[i];
    if (escape) {
      current += ch;
      escape = false;
      continue;
    }
    if (ch === "\\" && quote !== "'") {
      escape = true;
      continue;
    }
    if (quote) {
      if (ch === quote) {
        quote = null;
      } else {
        current += ch;
      }
      continue;
    }
    if (ch === "'" || ch === '"') {
      quote = ch;
      continue;
    }
    if (/\s/.test(ch)) {
      if (current.length > 0) {
        tokens.push(current);
        current = "";
      }
      continue;
    }
    current += ch;
  }
  if (quote) {
    throw new Error("Unterminated quote in git query");
  }
  if (current.length > 0) {
    tokens.push(current);
  }
  return tokens;
}
function validateReadonlyGitArgs(pattern) {
  const rawTokens = tokenizeArgs(pattern.trim());
  const tokens = rawTokens[0] === "git" ? rawTokens.slice(1) : rawTokens;
  if (tokens.length === 0) {
    throw new Error("Empty git query. Example: 'status --short' or 'log -n 5 --oneline'");
  }
  const subcmd = tokens[0];
  if (subcmd.startsWith("-")) {
    throw new Error(
      `Git query must start with a read-only subcommand (got '${subcmd}')`
    );
  }
  if (!READONLY_GIT_SUBCOMMANDS.has(subcmd)) {
    throw new Error(
      `Git subcommand '${subcmd}' is not allowed in batch_search (read-only only: ${[...READONLY_GIT_SUBCOMMANDS].join(", ")})`
    );
  }
  const rest = tokens.slice(1);
  for (const arg of rest) {
    if (arg === "&&" || arg === "||" || arg === ";" || arg === "|") {
      throw new Error(
        `Shell operator '${arg}' is not supported in git queries; split chained commands into separate batch_search queries`
      );
    }
    const flagName = arg.includes("=") ? arg.slice(0, arg.indexOf("=")) : arg;
    if (FORBIDDEN_GIT_GLOBAL_FLAGS.has(flagName) || arg.startsWith("--output=") || flagName === "--ext" || flagName.startsWith("--ext-") || flagName.startsWith("--textc") || arg.startsWith("-c") && arg.length > 2) {
      throw new Error(`Flag '${arg}' is not allowed in read-only git query`);
    }
  }
  if (subcmd === "branch") {
    let hasListFlag = false;
    let expectValue = false;
    let expectNonFlagCommit = false;
    for (const arg of rest) {
      if (expectValue) {
        expectValue = false;
        continue;
      }
      if (expectNonFlagCommit) {
        expectNonFlagCommit = false;
        if (arg.startsWith("-")) {
          throw new Error(
            `Expected commit ref after commit filter flag (got '${arg}')`
          );
        }
        continue;
      }
      if (arg === "--") {
        if (!hasListFlag) {
          throw new Error(
            "Positional branch arguments are only allowed with --list in read-only mode"
          );
        }
        break;
      }
      if (arg.startsWith("--")) {
        const eqIdx = arg.indexOf("=");
        const flagName = eqIdx !== -1 ? arg.slice(0, eqIdx) : arg;
        if (eqIdx !== -1) {
          if (!ALLOWED_BRANCH_VALUE_FLAGS.has(flagName) && !ALLOWED_BRANCH_EQ_ONLY_OPTIONAL_FLAGS.has(flagName) && !ALLOWED_BRANCH_COMMIT_OPTIONAL_FLAGS.has(flagName)) {
            throw new Error(`Flag '${arg}' is not allowed in read-only git branch query`);
          }
        } else if (ALLOWED_BRANCH_COMMIT_VALUE_FLAGS.has(flagName)) {
          expectNonFlagCommit = true;
          hasListFlag = true;
        } else if (ALLOWED_BRANCH_COMMIT_OPTIONAL_FLAGS.has(flagName)) {
          expectNonFlagCommit = true;
          hasListFlag = true;
        } else if (ALLOWED_BRANCH_VALUE_FLAGS.has(flagName)) {
          expectValue = true;
        } else if (!ALLOWED_BRANCH_BOOL_FLAGS.has(flagName) && !ALLOWED_BRANCH_EQ_ONLY_OPTIONAL_FLAGS.has(flagName)) {
          throw new Error(`Flag '${arg}' is not allowed in read-only git branch query`);
        }
        if (flagName === "--list" || ALLOWED_BRANCH_COMMIT_OPTIONAL_FLAGS.has(flagName) || ALLOWED_BRANCH_COMMIT_VALUE_FLAGS.has(flagName)) {
          hasListFlag = true;
        }
      } else if (arg.startsWith("-") && arg.length > 1) {
        for (const ch of arg.slice(1)) {
          if (!ALLOWED_BRANCH_SHORT_CHARS.has(ch)) {
            throw new Error(`Flag '${arg}' is not allowed in read-only git branch query`);
          }
          if (ch === "l") hasListFlag = true;
        }
      } else if (!hasListFlag) {
        throw new Error(
          `Positional branch argument '${arg}' is only allowed with --list in read-only mode`
        );
      }
    }
  } else if (subcmd === "tag") {
    let allowsPositionals = false;
    let expectValue = false;
    let expectNonFlagCommit = false;
    for (const arg of rest) {
      if (expectValue) {
        expectValue = false;
        continue;
      }
      if (expectNonFlagCommit) {
        expectNonFlagCommit = false;
        if (arg.startsWith("-")) {
          throw new Error(
            `Expected commit ref after commit filter flag (got '${arg}')`
          );
        }
        continue;
      }
      if (arg === "--") {
        if (!allowsPositionals) {
          throw new Error(
            "Positional tag arguments are only allowed with --list in read-only mode"
          );
        }
        break;
      }
      if (arg.startsWith("--")) {
        const eqIdx = arg.indexOf("=");
        const flagName = eqIdx !== -1 ? arg.slice(0, eqIdx) : arg;
        if (eqIdx !== -1) {
          if (!ALLOWED_TAG_VALUE_FLAGS.has(flagName) && !ALLOWED_TAG_EQ_ONLY_OPTIONAL_FLAGS.has(flagName) && !ALLOWED_TAG_COMMIT_OPTIONAL_FLAGS.has(flagName)) {
            throw new Error(`Flag '${arg}' is not allowed in read-only git tag query`);
          }
        } else if (ALLOWED_TAG_COMMIT_VALUE_FLAGS.has(flagName)) {
          expectNonFlagCommit = true;
          allowsPositionals = true;
        } else if (ALLOWED_TAG_COMMIT_OPTIONAL_FLAGS.has(flagName)) {
          expectNonFlagCommit = true;
          allowsPositionals = true;
        } else if (ALLOWED_TAG_VALUE_FLAGS.has(flagName)) {
          expectValue = true;
        } else if (!ALLOWED_TAG_BOOL_FLAGS.has(flagName) && !ALLOWED_TAG_EQ_ONLY_OPTIONAL_FLAGS.has(flagName)) {
          throw new Error(`Flag '${arg}' is not allowed in read-only git tag query`);
        }
        if (flagName === "--list" || ALLOWED_TAG_COMMIT_OPTIONAL_FLAGS.has(flagName) || ALLOWED_TAG_COMMIT_VALUE_FLAGS.has(flagName)) {
          allowsPositionals = true;
        }
      } else if (arg.startsWith("-") && arg.length > 1) {
        if (/^-n\d*$/.test(arg)) {
          allowsPositionals = true;
        } else {
          for (const ch of arg.slice(1)) {
            if (!ALLOWED_TAG_SHORT_CHARS.has(ch)) {
              throw new Error(`Flag '${arg}' is not allowed in read-only git tag query`);
            }
            if (ch === "l") allowsPositionals = true;
          }
        }
      } else if (!allowsPositionals) {
        throw new Error(
          `Positional tag argument '${arg}' is only allowed with --list in read-only mode`
        );
      }
    }
  } else if (subcmd === "remote") {
    let remoteSubcmd = null;
    for (const arg of rest) {
      if (!remoteSubcmd) {
        if (arg === "-v" || arg === "--verbose") continue;
        if (arg.startsWith("-")) {
          throw new Error(`Flag '${arg}' is not allowed in read-only git remote query`);
        }
        if (!ALLOWED_REMOTE_SUBCOMMANDS.has(arg)) {
          throw new Error(`Mutating git remote subcommand '${arg}' is not allowed`);
        }
        remoteSubcmd = arg;
      } else if (arg.startsWith("-")) {
        const okFlag = remoteSubcmd === "show" && arg === "-n" || remoteSubcmd === "get-url" && (arg === "--push" || arg === "--all");
        if (!okFlag) {
          throw new Error(
            `Flag '${arg}' is not allowed for read-only git remote ${remoteSubcmd}`
          );
        }
      } else if (arg.includes("://") || arg.includes(":") || arg.includes("/") || arg.includes("\\")) {
        throw new Error(
          `Remote target '${arg}' must be a configured remote name, not a URL or path`
        );
      }
    }
  }
  return ["--no-pager", subcmd, ...rest];
}
var FILE_TYPE_GLOBS = {
  ts: ["*.ts", "*.tsx", "*.mts", "*.cts"],
  typescript: ["*.ts", "*.tsx", "*.mts", "*.cts"],
  js: ["*.js", "*.jsx", "*.mjs", "*.cjs"],
  javascript: ["*.js", "*.jsx", "*.mjs", "*.cjs"],
  py: ["*.py", "*.pyi"],
  python: ["*.py", "*.pyi"],
  go: ["*.go"],
  gd: ["*.gd"],
  gdscript: ["*.gd"],
  rust: ["*.rs"],
  rs: ["*.rs"],
  java: ["*.java"],
  kt: ["*.kt", "*.kts"],
  kotlin: ["*.kt", "*.kts"],
  c: ["*.c", "*.h"],
  cpp: ["*.cc", "*.cpp", "*.cxx", "*.hpp", "*.hh", "*.hxx", "*.h"],
  cs: ["*.cs"],
  csharp: ["*.cs"],
  swift: ["*.swift"],
  rb: ["*.rb"],
  ruby: ["*.rb"],
  php: ["*.php"],
  css: ["*.css", "*.scss", "*.sass", "*.less"],
  html: ["*.html", "*.htm"],
  vue: ["*.vue"],
  svelte: ["*.svelte"],
  md: ["*.md", "*.mdx", "*.markdown"],
  markdown: ["*.md", "*.mdx", "*.markdown"],
  json: ["*.json", "*.jsonc"],
  yaml: ["*.yml", "*.yaml"],
  yml: ["*.yml", "*.yaml"],
  toml: ["*.toml"],
  xml: ["*.xml"],
  sql: ["*.sql"],
  sh: ["*.sh", "*.bash", "*.zsh"],
  shell: ["*.sh", "*.bash", "*.zsh"]
};
function resolveFileTypeGlobs(fileType) {
  if (fileType === void 0) return null;
  if (typeof fileType !== "string") throw new Error("fileType must be a string");
  const key = fileType.trim().toLowerCase().replace(/^\./, "");
  if (!key) return null;
  const mapped = FILE_TYPE_GLOBS[key];
  if (Object.hasOwn(FILE_TYPE_GLOBS, key)) return mapped;
  if (/^[a-z0-9]+$/i.test(key)) return [`*.${key}`];
  throw new Error(`Unsupported fileType '${fileType}'`);
}
function fileTypeMatcher(fileType) {
  const matchers = (resolveFileTypeGlobs(fileType) ?? []).map(globToRegExp);
  return (file) => matchers.length === 0 || matchers.some((matcher) => matcher.test(path6.basename(file)));
}
async function runTextOrRegexWithRg(query, searchRoot, baseCwd) {
  const maxResults = clampInt(query.maxResults ?? 40, 1, 200);
  const contextLines = clampInt(query.contextLines ?? 0, 0, 5);
  const summary = query.outputMode === "count" || query.outputMode === "files_with_matches";
  const typeGlobs = resolveFileTypeGlobs(query.fileType);
  const matchesType = fileTypeMatcher(query.fileType);
  const fileStat = await fs5.stat(searchRoot);
  const isFileTarget = fileStat.isFile();
  if (isFileTarget) {
    if (typeGlobs && !matchesType(searchRoot)) {
      return { files: [], totalMatches: 0, truncated: false };
    }
    if (fileStat.size > 2 * 1024 * 1024) {
      return { files: [], totalMatches: 0, truncated: false };
    }
    if (query.glob) {
      const negated = query.glob.startsWith("!");
      const rawGlob = negated ? query.glob.slice(1) : query.glob;
      const globRe = globToRegExp(rawGlob);
      const relPosix = relativeFile(baseCwd, searchRoot);
      const matched = globRe.test(relPosix) || globRe.test(path6.basename(searchRoot));
      if (negated ? matched : !matched) {
        return { files: [], totalMatches: 0, truncated: false };
      }
    }
    const fh = await fs5.open(searchRoot, "r");
    try {
      const head = Buffer.alloc(Math.min(fileStat.size, 8192));
      const { bytesRead } = await fh.read(head, 0, head.length, 0);
      if (isLikelyBinary(head.subarray(0, bytesRead))) {
        return { files: [], totalMatches: 0, truncated: false };
      }
    } finally {
      await fh.close();
    }
  }
  const perFileCap = isFileTarget ? maxResults : Math.min(maxResults, 50);
  const args = ["--no-config", "--no-messages", "--max-filesize", "2M", "--crlf"];
  if (query.multiline) args.push("--multiline", "--multiline-dotall");
  if (typeGlobs) {
    for (const glob of typeGlobs) args.push("--type-add", `agyfilter:${glob}`);
    args.push("--type", "agyfilter");
  }
  if (query.type === "text") {
    args.push("--fixed-strings");
  }
  if (query.caseSensitive === false) {
    args.push("--ignore-case");
  }
  if (query.glob) {
    args.push(`--glob=${query.glob}`);
  }
  const normRoot = toPosixPath(searchRoot);
  for (const dir of [
    "node_modules",
    ".git",
    "dist",
    "build",
    "target",
    "coverage",
    "__pycache__",
    ".venv",
    ".next"
  ]) {
    if (!normRoot.split("/").includes(dir)) {
      args.push(`--glob=!**/${dir}/**`);
    }
  }
  const runRg = async (runArgs, opts) => {
    const res = await execBinaryAsync("rg", runArgs, {
      cwd: baseCwd,
      timeoutMs: 15e3,
      maxOutputBytes: 2 * 1024 * 1024
    });
    if (res.error) {
      throw new Error(`rg spawn error: ${res.error}`);
    }
    if (res.exitCode === 2 && !(opts?.allowPartialError && !res.stderr.trim())) {
      throw new Error(res.stderr.trim() || `rg failed for pattern: ${query.pattern}`);
    }
    return res;
  };
  const cutShort = (res) => res.truncated || res.timedOut || res.exitCode === null;
  if (summary) {
    const res = await runRg([
      ...args,
      ...query.outputMode === "count" ? [query.multiline ? "--count-matches" : "--count", "--with-filename", "--null"] : ["--files-with-matches", "--null"],
      "--",
      query.pattern,
      searchRoot
    ]);
    const files2 = [];
    const out = res.stdout;
    let pos = 0;
    while (pos < out.length) {
      const nul = out.indexOf("\0", pos);
      if (nul === -1) break;
      const rawFile = out.slice(pos, nul);
      let count = 0;
      if (query.outputMode === "count") {
        const nl = out.indexOf("\n", nul + 1);
        if (nl === -1) break;
        const rawCount = out.slice(nul + 1, nl).trim();
        count = Number(rawCount);
        pos = nl + 1;
      } else {
        pos = nul + 1;
      }
      if (!rawFile || !matchesType(rawFile)) continue;
      files2.push({ file: relativeFile(baseCwd, rawFile), lines: [], count: Number.isFinite(count) ? count : 0 });
    }
    files2.sort((a, b) => a.file < b.file ? -1 : a.file > b.file ? 1 : 0);
    return {
      files: files2.slice(0, maxResults),
      totalMatches: 0,
      truncated: files2.length > maxResults || cutShort(res)
    };
  }
  const maxCountArgs = query.multiline ? [] : ["--max-count", String(perFileCap + 1)];
  let chosen;
  let truncated = false;
  if (isFileTarget) {
    chosen = [
      {
        rawFile: searchRoot,
        relFile: relativeFile(baseCwd, searchRoot),
        count: Number.POSITIVE_INFINITY
      }
    ];
  } else {
    const counts = await runRg([
      ...args,
      ...maxCountArgs,
      "--count",
      "--with-filename",
      "--null",
      "--",
      query.pattern,
      searchRoot
    ]);
    const counted = [];
    for (const record of counts.stdout.split("\n").slice(0, -1)) {
      const sep = record.lastIndexOf("\0");
      const count = Number(record.slice(sep + 1));
      if (sep <= 0 || !Number.isInteger(count) || count <= 0) continue;
      const rawFile = record.slice(0, sep);
      if (!matchesType(rawFile)) continue;
      counted.push({ rawFile, relFile: relativeFile(baseCwd, rawFile), count });
    }
    counted.sort((a, b) => a.relFile < b.relFile ? -1 : a.relFile > b.relFile ? 1 : 0);
    chosen = [];
    let coveredMatches = 0;
    for (const item of counted) {
      if (coveredMatches >= maxResults) break;
      chosen.push(item);
      coveredMatches += query.multiline ? item.count : Math.min(item.count, perFileCap);
    }
    truncated = cutShort(counts) || chosen.length < counted.length;
    if (chosen.length === 0) {
      return { files: [], totalMatches: 0, truncated };
    }
  }
  const toRgArgPath = (rawFile) => {
    const rel = path6.relative(baseCwd, rawFile);
    const chosenPath = rel && !rel.startsWith("..") && !path6.isAbsolute(rel) ? rel : rawFile;
    return chosenPath === "-" ? `.${path6.sep}-` : chosenPath;
  };
  const rgChunks = [];
  let currentChunk = [];
  let currentChunkChars = 0;
  const maxArgChars = 2e4;
  for (const item of chosen) {
    const argPath = toRgArgPath(item.rawFile);
    const est = argPath.length + 3;
    if (currentChunk.length > 0 && currentChunkChars + est > maxArgChars) {
      rgChunks.push(currentChunk);
      currentChunk = [];
      currentChunkChars = 0;
    }
    currentChunk.push(argPath);
    currentChunkChars += est;
  }
  if (currentChunk.length > 0) rgChunks.push(currentChunk);
  const byFile = /* @__PURE__ */ new Map();
  for (const chunk of rgChunks) {
    const res = await runRg(
      [
        ...args,
        ...maxCountArgs,
        "--json",
        ...contextLines > 0 ? ["-C", String(contextLines)] : [],
        "--",
        query.pattern,
        ...chunk
      ],
      { allowPartialError: !isFileTarget }
    );
    if (cutShort(res)) truncated = true;
    for (const rawLine of res.stdout.split("\n")) {
      if (!rawLine.trim()) continue;
      let ev;
      try {
        ev = JSON.parse(rawLine);
      } catch {
        continue;
      }
      if (ev.type !== "match" && ev.type !== "context") continue;
      const rawFile = ev.data?.path?.text ?? (ev.data?.path?.bytes ? Buffer.from(ev.data.path.bytes, "base64").toString("utf8") : void 0);
      const lineNumber = ev.data?.line_number;
      const rawText = ev.data?.lines?.text ?? (ev.data?.lines?.bytes ? Buffer.from(ev.data.lines.bytes, "base64").toString("utf8") : "");
      if (!rawFile || typeof lineNumber !== "number") continue;
      const relFile = relativeFile(baseCwd, rawFile);
      let lineMap = byFile.get(relFile);
      if (!lineMap) {
        lineMap = /* @__PURE__ */ new Map();
        byFile.set(relFile, lineMap);
      }
      const isMatch = ev.type === "match";
      const lineText = rawText.replace(/\r?\n$/, "");
      const segments = isMatch && query.multiline ? lineText.split(/\r?\n/) : [lineText];
      for (const [offset, text] of segments.entries()) {
        const line = lineNumber + offset;
        if (!lineMap.get(line)?.isMatch) lineMap.set(line, { lineNumber: line, text, isMatch });
      }
    }
  }
  const files = [];
  let totalMatches = 0;
  for (const { relFile, count } of chosen) {
    if (totalMatches >= maxResults) {
      truncated = true;
      break;
    }
    const fileCap = query.multiline ? maxResults : Math.min(count, perFileCap);
    const budget = Math.min(fileCap, maxResults - totalMatches);
    const sorted = [...byFile.get(relFile)?.values() ?? []].sort(
      (a, b) => a.lineNumber - b.lineNumber
    );
    const lines = [];
    let kept = 0;
    let lastKeptMatchLine = -1;
    for (const line of sorted) {
      if (line.isMatch) {
        if (kept === budget) {
          truncated = true;
          if (lastKeptMatchLine !== -1 && line.lineNumber <= lastKeptMatchLine + contextLines) {
            lines.push({ ...line, isMatch: false });
          }
          continue;
        }
        kept += 1;
        lastKeptMatchLine = line.lineNumber;
        lines.push(line);
      } else if (kept < budget || line.lineNumber <= lastKeptMatchLine + contextLines) {
        lines.push(line);
      }
    }
    if (kept === 0) continue;
    files.push({ file: relFile, lines });
    totalMatches += kept;
  }
  return { files, totalMatches, truncated };
}
function* multilineMatches(content, regex) {
  let previousEnd = -1;
  for (const match of content.matchAll(regex)) {
    const start = match.index;
    if (start === content.length && (content.length === 0 || content.endsWith("\n"))) break;
    if (match[0] === "") {
      if (start === previousEnd || content[start - 1] === "\r" && content[start] === "\n") continue;
    }
    previousEnd = start + match[0].length;
    yield match;
  }
}
function multilineMatchLines(content, regex, limit) {
  const indices = [];
  let line = 0;
  let nextBreak = content.indexOf("\n");
  for (const match of multilineMatches(content, regex)) {
    const start = match.index;
    const end = start + Math.max(0, match[0].length - 1);
    while (nextBreak !== -1 && nextBreak < start) {
      line++;
      nextBreak = content.indexOf("\n", nextBreak + 1);
    }
    while (true) {
      if (indices.at(-1) !== line) indices.push(line);
      if (indices.length >= limit) return indices;
      if (nextBreak === -1 || nextBreak >= end) break;
      line++;
      nextBreak = content.indexOf("\n", nextBreak + 1);
    }
  }
  return indices;
}
async function runTextOrRegexWithNode(query, searchRoot, baseCwd) {
  const maxResults = clampInt(query.maxResults ?? 40, 1, 200);
  const contextLines = clampInt(query.contextLines ?? 0, 0, 5);
  const flags = (query.caseSensitive === false ? "i" : "") + (query.multiline ? "gmsu" : "");
  let regex;
  if (query.type === "regex") {
    try {
      regex = new RegExp(query.pattern, flags);
    } catch (err) {
      if (flags.includes("u")) {
        try {
          regex = new RegExp(query.pattern, flags.replace("u", ""));
        } catch {
          throw new Error(
            `Invalid regex '${query.pattern}': ${err instanceof Error ? err.message : String(err)}`
          );
        }
      } else {
        throw new Error(
          `Invalid regex '${query.pattern}': ${err instanceof Error ? err.message : String(err)}`
        );
      }
    }
  } else {
    const escaped = query.pattern.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    regex = new RegExp(escaped, flags);
  }
  const summary = query.outputMode === "count" || query.outputMode === "files_with_matches";
  const matchesType = fileTypeMatcher(query.fileType);
  let discoveryTruncated = false;
  const globNegated = Boolean(query.glob && query.glob.startsWith("!"));
  const rawGlob = query.glob ? globNegated ? query.glob.slice(1) : query.glob : null;
  const globRegex = rawGlob ? globToRegExp(rawGlob) : null;
  const globMatchesFile = (relPosix) => {
    if (!matchesType(relPosix)) return false;
    if (!globRegex) return true;
    let matched = globRegex.test(relPosix);
    if (!matched && !rawGlob.includes("/")) {
      const base = relPosix.split("/").pop() ?? relPosix;
      matched = globRegex.test(base);
    }
    return globNegated ? !matched : matched;
  };
  const stat = await fs5.stat(searchRoot);
  const perFileCap = stat.isFile() ? maxResults : Math.min(maxResults, 50);
  let targetFiles;
  if (stat.isFile()) {
    const relPosix = toPosixPath(path6.relative(baseCwd, searchRoot)) || path6.basename(searchRoot);
    targetFiles = globMatchesFile(relPosix) ? [searchRoot] : [];
  } else {
    const includeHidden = Boolean(
      rawGlob && !globNegated && (rawGlob.startsWith(".") || rawGlob.includes("/."))
    );
    const walked = await walkFiles(searchRoot, {
      maxFiles: 8e3,
      includeHidden,
      fileFilter: globMatchesFile
    });
    discoveryTruncated = walked.truncated;
    targetFiles = walked.files.map((rel) => path6.join(searchRoot, rel));
    targetFiles.sort((a, b) => {
      const ra = relativeFile(baseCwd, a);
      const rb = relativeFile(baseCwd, b);
      return ra < rb ? -1 : ra > rb ? 1 : 0;
    });
  }
  const files = [];
  let totalMatches = 0;
  let truncated = false;
  const buildFileMatches = (relFile, allLines, keptIndices) => {
    const lineMap = /* @__PURE__ */ new Map();
    for (const mIdx of keptIndices) {
      const from = Math.max(0, mIdx - contextLines);
      const to = Math.min(allLines.length - 1, mIdx + contextLines);
      for (let j = from; j <= to; j++) {
        const ln = j + 1;
        const prev = lineMap.get(ln);
        lineMap.set(ln, {
          lineNumber: ln,
          text: allLines[j],
          isMatch: j === mIdx || Boolean(prev?.isMatch)
        });
      }
    }
    return {
      file: relFile,
      lines: [...lineMap.values()].sort((a, b) => a.lineNumber - b.lineNumber)
    };
  };
  const batchSize = 16;
  let anyFileTruncated = false;
  for (let i = 0; i < targetFiles.length && !truncated; i += batchSize) {
    const batch = targetFiles.slice(i, i + batchSize);
    const results = await Promise.all(
      batch.map(async (absFile) => {
        try {
          const st = await fs5.stat(absFile);
          if (st.size > 2 * 1024 * 1024) return null;
          const buf = await fs5.readFile(absFile);
          if (isLikelyBinary(buf)) return null;
          const content = buf.toString("utf8");
          const lines = content.split(/\r?\n/);
          if (lines.length > 0 && lines.at(-1) === "") lines.pop();
          const relFile = relativeFile(baseCwd, absFile);
          let matchedIndices = [];
          let count = 0;
          let fileTruncated = false;
          if (query.multiline && summary) {
            for (const _match of multilineMatches(content, regex)) {
              count++;
              if (query.outputMode === "files_with_matches") break;
            }
          } else if (query.multiline) {
            matchedIndices = multilineMatchLines(content, regex, maxResults + 1);
            count = matchedIndices.length;
          } else {
            for (let idx = 0; idx < lines.length; idx++) {
              if (regex.test(lines[idx])) {
                if (!summary && matchedIndices.length >= perFileCap) {
                  fileTruncated = true;
                  break;
                }
                matchedIndices.push(idx);
                if (query.outputMode === "files_with_matches") break;
              }
            }
            count = matchedIndices.length;
          }
          return count ? { relFile, lines, matchedIndices, count, fileTruncated } : null;
        } catch {
          return null;
        }
      })
    );
    for (const item of results) {
      if (!item) continue;
      if (summary) {
        if (files.length === maxResults) {
          truncated = true;
          break;
        }
        files.push({ file: item.relFile, lines: [], count: item.count });
        continue;
      }
      if (item.fileTruncated) {
        anyFileTruncated = true;
      }
      if (totalMatches + item.matchedIndices.length > maxResults) {
        const remaining = maxResults - totalMatches;
        if (remaining > 0) {
          const kept = item.matchedIndices.slice(0, remaining);
          files.push(buildFileMatches(item.relFile, item.lines, kept));
          totalMatches += kept.length;
        }
        truncated = true;
        break;
      }
      files.push(buildFileMatches(item.relFile, item.lines, item.matchedIndices));
      totalMatches += item.matchedIndices.length;
    }
  }
  return {
    files,
    totalMatches,
    truncated: truncated || anyFileTruncated || (summary || query.multiline === true) && discoveryTruncated
  };
}
function buildGlobFallbackPlan(pattern) {
  if (!pattern.includes("{") && pattern.includes("|")) {
    const terms = pattern.split("|").map((part) => part.trim()).filter(Boolean);
    if (terms.length >= 2) {
      const termMatchers = terms.map((term) => {
        const hasGlobMeta = /[*?[\]{}]/.test(term);
        if (hasGlobMeta) {
          try {
            const re = globToRegExp(term);
            const matchBase = !term.includes("/");
            return (relPosix, base) => re.test(relPosix) || matchBase && re.test(base);
          } catch {
          }
        }
        const needle = term.toLowerCase();
        return (relPosix) => relPosix.toLowerCase().includes(needle);
      });
      const allBare = terms.every((t) => !/[*?[\]{}/]/.test(t));
      const label = allBare ? `**/*{${terms.join(",")}}*` : `{${terms.join(",")}}`;
      return {
        match: (relPosix, base) => termMatchers.some((fn) => fn(relPosix, base)),
        matchedNote: `[note: exact glob matched 0 files; auto-expanded regex '|' to "${label}" (glob uses * and {a,b}, not |)]`,
        emptyMessage: "(no files matched; hint: glob uses * and {a,b}, not regex |)"
      };
    }
  }
  if (/^[A-Za-z0-9_-]+$/.test(pattern)) {
    const needle = pattern.toLowerCase();
    return {
      match: (relPosix) => relPosix.toLowerCase().includes(needle),
      matchedNote: `[note: exact glob "${pattern}" matched 0 files; showing case-insensitive path matches for "*${pattern}*"]`,
      emptyMessage: "(no files matched)"
    };
  }
  return null;
}
async function formatGlobWalkResult(walked, searchRoot, baseCwd, maxResults, sortBy, notePrefix) {
  const relPrefix = toPosixPath(path6.relative(baseCwd, searchRoot));
  const sortedFiles = [...walked.files].sort((a, b) => a < b ? -1 : a > b ? 1 : 0);
  if (sortBy === "mtime") {
    const mtimes = /* @__PURE__ */ new Map();
    for (let i = 0; i < sortedFiles.length; i += 32) {
      await Promise.all(sortedFiles.slice(i, i + 32).map(async (file) => {
        const stat = await fs5.lstat(path6.join(searchRoot, file)).catch(() => void 0);
        mtimes.set(file, stat?.mtimeMs ?? -Infinity);
      }));
    }
    sortedFiles.sort((a, b) => mtimes.get(b) - mtimes.get(a) || (a < b ? -1 : a > b ? 1 : 0));
  }
  const items = sortedFiles.slice(0, maxResults).map((f) => {
    if (!relPrefix || relPrefix === ".") return f;
    return `${relPrefix}/${f}`;
  });
  const truncated = walked.truncated || sortedFiles.length > maxResults;
  const header = notePrefix ? `${notePrefix}
` : "";
  if (!truncated) {
    return `${header}${items.join("\n")}`;
  }
  const omitted = sortedFiles.slice(maxResults);
  const dirCounts = /* @__PURE__ */ new Map();
  for (const f of omitted) {
    const slash = f.indexOf("/");
    const bucket = slash === -1 ? "(root)" : `${f.slice(0, slash)}/`;
    dirCounts.set(bucket, (dirCounts.get(bucket) ?? 0) + 1);
  }
  const topDirs = [...dirCounts.entries()].sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0)).slice(0, 6).map(([dir, cnt]) => `${dir} (${cnt})`);
  const totalLabel = walked.truncated ? `${walked.files.length}+` : String(walked.files.length);
  const dirSummary = topDirs.length > 0 ? `; omitted dirs: ${topDirs.join(", ")}` : "";
  const suffix = `
... [truncated at ${maxResults} of ${totalLabel} files${dirSummary}]`;
  return `${header}${items.join("\n")}${suffix}`;
}
async function runGlobQuery(query, searchRoot, baseCwd, isFileTarget) {
  const maxResults = clampInt(query.maxResults ?? 80, 1, 300);
  const pattern = query.pattern.trim() || "**/*";
  const matcher = globToRegExp(pattern);
  const matchBasename = !pattern.includes("/");
  const includeHidden = pattern.startsWith(".") || pattern.includes("/.");
  if (isFileTarget) {
    const rel = toPosixPath(path6.relative(baseCwd, searchRoot)) || path6.basename(searchRoot);
    const base = path6.basename(searchRoot);
    const matched = matcher.test(rel) || matchBasename && matcher.test(base);
    if (matched) return rel;
    const fallback2 = buildGlobFallbackPlan(pattern);
    if (fallback2) {
      return fallback2.match(rel, base) ? `${fallback2.matchedNote}
${rel}` : fallback2.emptyMessage;
    }
    return "(no files matched)";
  }
  const scanLimit = Math.max(maxResults + 1, 2e3);
  const walked = await walkFiles(searchRoot, {
    maxFiles: scanLimit,
    includeHidden,
    fileFilter: (relPosix) => {
      if (matcher.test(relPosix)) return true;
      if (matchBasename) {
        const base = relPosix.split("/").pop() ?? relPosix;
        return matcher.test(base);
      }
      return false;
    }
  });
  if (walked.files.length > 0) {
    return formatGlobWalkResult(walked, searchRoot, baseCwd, maxResults, query.sortBy);
  }
  const fallback = buildGlobFallbackPlan(pattern);
  if (fallback) {
    const fallbackWalked = await walkFiles(searchRoot, {
      maxFiles: scanLimit,
      includeHidden,
      fileFilter: (relPosix) => {
        const base = relPosix.split("/").pop() ?? relPosix;
        return fallback.match(relPosix, base);
      }
    });
    if (fallbackWalked.files.length > 0) {
      return formatGlobWalkResult(
        fallbackWalked,
        searchRoot,
        baseCwd,
        maxResults,
        query.sortBy,
        fallback.matchedNote
      );
    }
    return fallback.emptyMessage;
  }
  return "(no files matched)";
}
function relativeFile(baseCwd, file) {
  return toPosixPath(path6.relative(baseCwd, path6.resolve(baseCwd, file))) || path6.basename(file);
}
async function runFuzzyQuery(query, searchRoot, baseCwd, isFileTarget) {
  const pattern = query.pattern.trim();
  if (!pattern) throw new Error("Fuzzy pattern must not be blank");
  const matchesType = fileTypeMatcher(query.fileType);
  const maxResults = clampInt(query.maxResults ?? 30, 1, 200);
  const walked = isFileTarget ? { files: [searchRoot], truncated: false } : await walkFiles(searchRoot, { maxFiles: 8e3, fileFilter: matchesType });
  const candidates = walked.files.filter(matchesType).map((file) => relativeFile(baseCwd, path6.resolve(searchRoot, file)));
  const { matches, totalMatched } = rankFuzzyPaths(pattern, candidates, maxResults);
  const body = matches.map((match) => match.path).join("\n") || "(no files matched)";
  const footer = walked.truncated || totalMatched > maxResults ? `
... [truncated; showing ${matches.length} files]` : "";
  return body + footer;
}
var SHORTLOG_VALUE_FLAGS = /* @__PURE__ */ new Set([
  "--group",
  "--format",
  "--since",
  "--after",
  "--until",
  "--before",
  "--author",
  "--committer",
  "--grep",
  "--max-count",
  "--skip"
]);
var SHORTLOG_REV_SELECTOR_FLAGS = /* @__PURE__ */ new Set([
  "--all",
  "--branches",
  "--tags",
  "--remotes",
  "--glob",
  "--stdin"
]);
function hardenGitExecArgs(validatedArgs) {
  const subcmd = validatedArgs[1];
  const rest = validatedArgs.slice(2);
  let subArgs = rest;
  if (subcmd === "diff" || subcmd === "log" || subcmd === "show") {
    subArgs = ["--no-ext-diff", "--no-textconv", ...rest];
  } else if (subcmd === "blame") {
    subArgs = ["--no-textconv", ...rest];
  } else if (subcmd === "remote") {
    const remoteSubIdx = rest.findIndex((a) => !a.startsWith("-"));
    if (remoteSubIdx !== -1 && rest[remoteSubIdx] === "show" && !rest.includes("-n")) {
      subArgs = [...rest.slice(0, remoteSubIdx + 1), "-n", ...rest.slice(remoteSubIdx + 1)];
    }
  } else if (subcmd === "shortlog") {
    let hasRev = false;
    let expectVal = false;
    let dashDashIdx = -1;
    for (let i = 0; i < rest.length; i++) {
      const a = rest[i];
      if (expectVal) {
        expectVal = false;
        continue;
      }
      if (a === "--") {
        dashDashIdx = i;
        break;
      }
      if (a.startsWith("-")) {
        const flagName = a.includes("=") ? a.slice(0, a.indexOf("=")) : a;
        if (SHORTLOG_REV_SELECTOR_FLAGS.has(flagName)) {
          hasRev = true;
        } else if (!a.includes("=") && SHORTLOG_VALUE_FLAGS.has(flagName)) {
          expectVal = true;
        }
        continue;
      }
      hasRev = true;
    }
    if (!hasRev) {
      subArgs = dashDashIdx === -1 ? [...rest, "HEAD"] : [...rest.slice(0, dashDashIdx), "HEAD", ...rest.slice(dashDashIdx)];
    }
    subArgs = ["--no-ext-diff", "--no-textconv", ...subArgs];
  }
  return [
    "--no-optional-locks",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "diff.external=",
    "-c",
    "gpg.program=false",
    "-c",
    "gpg.openpgp.program=false",
    "-c",
    "gpg.ssh.program=false",
    "-c",
    "gpg.x509.program=false",
    "--no-pager",
    ...subcmd ? [subcmd, ...subArgs] : subArgs
  ];
}
var LOCKFILE_NAMES = /* @__PURE__ */ new Set([
  "Cargo.lock",
  "package-lock.json",
  "yarn.lock",
  "pnpm-lock.yaml",
  "uv.lock",
  "poetry.lock",
  "go.sum",
  "Gemfile.lock"
]);
function collapseLockfileDiffs(diffText, rawPattern) {
  for (const lf of LOCKFILE_NAMES) {
    if (rawPattern.includes(lf)) return diffText;
  }
  if (!diffText.includes("diff --git ")) return diffText;
  const lines = diffText.split("\n");
  const out = [];
  let skippingLock = null;
  let skippedLines = 0;
  const flushSkip = () => {
    if (skippingLock !== null) {
      out.push(`[lockfile diff omitted: ${skippingLock} (${skippedLines} lines)]`);
      skippingLock = null;
      skippedLines = 0;
    }
  };
  for (const line of lines) {
    if (line.startsWith("diff --git ")) {
      flushSkip();
      const base = line.split(/\s+/).pop()?.split("/").pop() ?? "";
      if (LOCKFILE_NAMES.has(base)) {
        skippingLock = base;
        skippedLines = 1;
        continue;
      }
    }
    if (skippingLock !== null) {
      skippedLines++;
      continue;
    }
    out.push(line);
  }
  flushSkip();
  return out.join("\n");
}
async function runGitQuery(query, searchRoot) {
  const gitArgs = validateReadonlyGitArgs(query.pattern);
  const execArgs = hardenGitExecArgs(gitArgs);
  const res = await execBinaryAsync("git", execArgs, {
    cwd: searchRoot,
    timeoutMs: 1e4,
    maxOutputBytes: 512 * 1024
  });
  if (res.error) {
    throw new Error(`git execution error: ${res.error}`);
  }
  if (res.timedOut) {
    throw new Error("git query timed out");
  }
  if (res.exitCode !== 0) {
    const msg = (res.stderr || res.stdout).trim();
    throw new Error(msg || `git exited with code ${res.exitCode}`);
  }
  let out = res.stdout.trimEnd();
  const subcmd = gitArgs[1];
  if (out && (subcmd === "diff" || subcmd === "show")) {
    out = collapseLockfileDiffs(out, query.pattern);
  }
  return out || "(empty output)";
}
function formatFileMatches(files, totalMatches, truncated) {
  if (files.length === 0) {
    return "(no matches)";
  }
  const blocks = [];
  for (const fm of files) {
    const lines = fm.lines.map((l) => {
      const sep = l.isMatch ? ":" : "-";
      const text = l.text.length > 240 ? `${sliceSafeChars(l.text, 240)}...` : l.text;
      return `  ${l.lineNumber}${sep} ${text}`;
    });
    blocks.push(`${fm.file}
${lines.join("\n")}`);
  }
  const footer = truncated ? `
... [truncated at ${totalMatches} matches]` : "";
  return `${blocks.join("\n")}${footer}`;
}
async function executeSingleQuery(query, index, baseCwd, useRg) {
  const header = `## Query ${index + 1} [${query.type}] ${query.pattern}${query.path ? ` (in ${query.path})` : ""}`;
  try {
    if (!query.pattern || typeof query.pattern !== "string") {
      throw new Error("Query 'pattern' must be a non-empty string");
    }
    if (query.multiline !== void 0 && typeof query.multiline !== "boolean") {
      throw new Error("multiline must be a boolean");
    }
    if (query.outputMode !== void 0 && !["content", "files_with_matches", "count"].includes(query.outputMode)) {
      throw new Error("Invalid outputMode");
    }
    if (query.sortBy !== void 0 && !["path", "mtime"].includes(query.sortBy)) {
      throw new Error("Invalid sortBy");
    }
    const searchRoot = query.path ? resolvePath(query.path, baseCwd) : baseCwd;
    let targetStat;
    try {
      targetStat = await fs5.stat(searchRoot);
    } catch {
      throw new Error(`Path does not exist: ${query.path || baseCwd}`);
    }
    if (query.type === "git") {
      const gitCwd = targetStat.isFile() ? path6.dirname(searchRoot) : searchRoot;
      const out = await runGitQuery(query, gitCwd);
      return `${header}
${out}`;
    }
    if (query.type === "glob") {
      const out = await runGlobQuery(query, searchRoot, baseCwd, targetStat.isFile());
      return `${header}
${out}`;
    }
    if (query.type === "fuzzy") {
      const out = await runFuzzyQuery(query, searchRoot, baseCwd, targetStat.isFile());
      return `${header}
${out}`;
    }
    if (query.type === "text" || query.type === "regex") {
      let res;
      if (useRg) {
        try {
          res = await runTextOrRegexWithRg(query, searchRoot, baseCwd);
        } catch (err) {
          if (err instanceof Error && err.message.startsWith("rg spawn error:")) {
            res = await runTextOrRegexWithNode(query, searchRoot, baseCwd);
          } else {
            throw err;
          }
        }
      } else {
        res = await runTextOrRegexWithNode(query, searchRoot, baseCwd);
      }
      if (query.outputMode === "count" || query.outputMode === "files_with_matches") {
        const body = res.files.map(({ file, count }) => query.outputMode === "count" ? `${file}: ${count} ${query.multiline ? "matches" : "matching lines"}` : file).join("\n") || "(no matches)";
        const footer = res.truncated ? `
... [truncated; showing ${res.files.length} files]` : "";
        return `${header}
${body}${footer}`;
      }
      return `${header}
${formatFileMatches(res.files, res.totalMatches, res.truncated)}`;
    }
    throw new Error(`Unsupported query type: ${String(query.type)}`);
  } catch (err) {
    return `${header}
ERROR: ${err instanceof Error ? err.message : String(err)}`;
  }
}
async function runBatchSearch(input, options = {}) {
  const parsedQueries = parseMaybeJsonArray(input?.queries);
  if (!parsedQueries.ok) {
    if (parsedQueries.kind === "corrupted_literal" || parsedQueries.kind === "wrong_type" && (typeof input?.queries === "boolean" || input?.queries === null)) {
      return {
        text: formatCorruptedArrayError({
          header: "Validation failed",
          toolName: "batch_search",
          items: [
            {
              label: "queries",
              value: parsedQueries.kind === "corrupted_literal" ? parsedQueries.value : input.queries,
              expected: "an array of query objects"
            }
          ],
          fallbackAdvice: "split into smaller queries or use a single compound read-only run_command."
        }),
        isError: true
      };
    }
    return {
      text: "Error: 'queries' must be a non-empty array of search queries.",
      isError: true
    };
  }
  const rawQueries = parsedQueries.value;
  if (rawQueries.length === 0) {
    return {
      text: "Error: 'queries' must be a non-empty array of search queries.",
      isError: true
    };
  }
  if (rawQueries.length > 20) {
    return {
      text: "Error: 'queries' supports at most 20 queries per call.",
      isError: true
    };
  }
  const corruptedQueries = [];
  for (let i = 0; i < rawQueries.length; i++) {
    if (!isPlainObject(rawQueries[i])) {
      corruptedQueries.push({
        label: `queries[${i}]`,
        value: rawQueries[i],
        expected: "a query object ({ type, pattern, ... })"
      });
    }
  }
  if (corruptedQueries.length > 0) {
    return {
      text: formatCorruptedArrayError({
        header: "Validation failed",
        toolName: "batch_search",
        items: corruptedQueries,
        fallbackAdvice: "split into smaller queries or use a single compound read-only run_command."
      }),
      isError: true
    };
  }
  const queries = rawQueries;
  const baseCwd = resolvePath(input.cwd || process.cwd());
  try {
    const st = await fs5.stat(baseCwd);
    if (!st.isDirectory()) {
      return { text: `Error: cwd is not a directory: ${baseCwd}`, isError: true };
    }
  } catch {
    return { text: `Error: cwd does not exist: ${baseCwd}`, isError: true };
  }
  const maxChars = clampInt(input.maxChars ?? 8500, 500, DEFAULT_INLINE_MAX_BYTES);
  const maxBytes = DEFAULT_INLINE_MAX_BYTES;
  const needsTextSearch = queries.some(
    (q) => q.type === "text" || q.type === "regex"
  );
  const useRg = needsTextSearch && !options.forceNodeFallback && await isRipgrepAvailable();
  const sections = await Promise.all(
    queries.map((q, idx) => executeSingleQuery(q, idx, baseCwd, useRg))
  );
  return {
    text: fitSectionsToByteBudget(sections, maxChars, maxBytes, "\n\n"),
    isError: false
  };
}
function clampInt(val, min, max) {
  if (!Number.isFinite(val)) return min;
  return Math.max(min, Math.min(max, Math.trunc(val)));
}

// src/tools/code-graph.ts
import fsSync from "node:fs";
import { promises as fs6 } from "node:fs";
import os4 from "node:os";
import path7 from "node:path";
var VALID_MODES = /* @__PURE__ */ new Set(["overview", "defs", "callers", "callees", "impact", "skeleton"]);
function findWmCodegraphBin(env = process.env) {
  if (env.WM_CODEGRAPH_BIN !== void 0) {
    const explicit = env.WM_CODEGRAPH_BIN.trim();
    if (!explicit) return null;
    try {
      fsSync.accessSync(explicit, fsSync.constants.X_OK);
      return explicit;
    } catch {
      return null;
    }
  }
  const home = env.HOME || env.USERPROFILE || os4.homedir();
  const candidates = [path7.join(home, ".local", "bin", "wm-codegraph")];
  for (const dir of (env.PATH || "").split(path7.delimiter)) {
    if (dir) candidates.push(path7.join(dir, "wm-codegraph"));
  }
  for (const cand of candidates) {
    try {
      if (fsSync.statSync(cand).isFile()) {
        fsSync.accessSync(cand, fsSync.constants.X_OK);
        return cand;
      }
    } catch {
    }
  }
  return null;
}
async function runCodeGraph(input) {
  const mode = typeof input?.mode === "string" ? input.mode.trim() : "";
  if (!VALID_MODES.has(mode)) {
    return {
      text: `Error: 'mode' must be one of overview, defs, callers, callees, impact, skeleton (got ${JSON.stringify(input?.mode)}).`,
      isError: true
    };
  }
  const bin = findWmCodegraphBin();
  if (!bin) {
    return {
      text: "Error: 'wm-codegraph' binary not found in ~/.local/bin or PATH. Run whitemagic-agy-kit install.sh to build it.",
      isError: true
    };
  }
  const baseDir = resolvePath(input.cwd || process.cwd());
  const rootRaw = typeof input.root === "string" && input.root.trim() ? input.root.trim() : baseDir;
  const resolvedRoot = resolvePath(rootRaw, baseDir);
  try {
    const st = await fs6.stat(resolvedRoot);
    if (!st.isDirectory()) {
      return { text: `Error: root is not a directory: ${resolvedRoot}`, isError: true };
    }
  } catch {
    return { text: `Error: root directory does not exist: ${resolvedRoot}`, isError: true };
  }
  const target = typeof input.target === "string" ? input.target.trim() : "";
  const rawDepth = Number(input.depth ?? 2);
  const depth = Number.isFinite(rawDepth) ? Math.max(1, Math.min(3, Math.trunc(rawDepth))) : 2;
  const rawMaxChars = Number(input.maxChars ?? 8500);
  const maxBytes = Number.isFinite(rawMaxChars) ? Math.max(500, Math.min(DEFAULT_INLINE_MAX_BYTES, Math.trunc(rawMaxChars))) : 8500;
  const args = [mode];
  if (target) args.push(target);
  args.push("--root", resolvedRoot, "--depth", String(depth));
  const res = await execBinaryAsync(bin, args, {
    cwd: resolvedRoot,
    timeoutMs: 15e3,
    maxOutputBytes: 512 * 1024
  });
  if (res.error) {
    return { text: `Error running wm-codegraph: ${res.error}`, isError: true };
  }
  if (res.timedOut) {
    return { text: `Error: wm-codegraph timed out after 15s on ${resolvedRoot}`, isError: true };
  }
  if (res.exitCode !== 0) {
    return {
      text: `Error: wm-codegraph exited with code ${res.exitCode}: ${(res.stderr || res.stdout).trim()}`,
      isError: true
    };
  }
  const text = truncateWithByteNotice(res.stdout.trimEnd() || "(no output)", maxBytes);
  return { text, isError: false };
}

// src/tools/read-slices.ts
import path8 from "node:path";
function isRegexStartContext(line, slashIdx) {
  let j = slashIdx - 1;
  while (j >= 0 && /\s/.test(line[j])) j--;
  if (j < 0) return true;
  const prev = line[j];
  if ("=(,:![&|?{};~+-*%^<>".includes(prev)) return true;
  let k = j;
  while (k >= 0 && /[A-Za-z0-9_$]/.test(line[k])) k--;
  const word = line.slice(k + 1, j + 1);
  return word === "return" || word === "case" || word === "throw" || word === "in" || word === "of" || word === "typeof" || word === "void" || word === "delete" || word === "new";
}
function isJsxSelfClosingSlash(line, slashIdx) {
  if (line[slashIdx + 1] !== ">") return false;
  let j = slashIdx - 1;
  while (j >= 0 && /\s/.test(line[j])) j--;
  if (j >= 0 && line[j] !== "}") return false;
  return line[slashIdx + 2] !== "/";
}
function scanBracesOnLine(line, state, lang, ignoreBracesInParensBeforeOpen = false, alreadySeenOpen = false) {
  let open = 0;
  let close = 0;
  let openParen = 0;
  let closeParen = 0;
  let parenDepth = state.parenDepth ?? 0;
  let seenOpen = alreadySeenOpen;
  let inSingle = false;
  let inDouble = false;
  let inRegex = false;
  let inRegexClass = false;
  let escape = false;
  let lastCodeChar = "";
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    const next = line[i + 1];
    if (state.inBlockComment) {
      if (ch === "*" && next === "/") {
        state.inBlockComment = false;
        i++;
      }
      continue;
    }
    if (escape) {
      escape = false;
      continue;
    }
    if (inRegex) {
      if (ch === "\\") {
        escape = true;
      } else if (ch === "[") {
        inRegexClass = true;
      } else if (ch === "]") {
        inRegexClass = false;
      } else if (ch === "/" && !inRegexClass) {
        inRegex = false;
        lastCodeChar = "/";
      }
      continue;
    }
    if (state.inBacktick) {
      if (ch === "\\" && lang !== "go") {
        escape = true;
      } else if (ch === "`") {
        state.inBacktick = false;
        lastCodeChar = "`";
      } else if (lang === "js" && ch === "$" && next === "{") {
        state.inBacktick = false;
        state.templateExprStack.push(0);
        i++;
      }
      continue;
    }
    if (inSingle) {
      if (ch === "\\") escape = true;
      else if (ch === "'") {
        inSingle = false;
        lastCodeChar = "'";
      }
      continue;
    }
    if (inDouble) {
      if (ch === "\\") escape = true;
      else if (ch === '"') {
        inDouble = false;
        lastCodeChar = '"';
      }
      continue;
    }
    if (lang === "shell" && ch === "#" && (i === 0 || /[\s;&|()]/.test(line[i - 1]))) {
      break;
    }
    if ((lang === "js" || lang === "c" || lang === "go" || lang === "rust") && ch === "/" && next === "/") break;
    if ((lang === "js" || lang === "c" || lang === "go" || lang === "rust") && ch === "/" && next === "*") {
      state.inBlockComment = true;
      i++;
      continue;
    }
    if (lang === "js" && ch === "/" && next !== "/" && next !== "*" && !isJsxSelfClosingSlash(line, i) && line[i - 1] !== "<" && isRegexStartContext(line, i)) {
      inRegex = true;
      inRegexClass = false;
      continue;
    }
    if (ch === "'" && lang !== "json") {
      if (lang === "rust") {
        if (next === "\\" || line[i + 2] === "'") {
          inSingle = true;
        }
        continue;
      }
      inSingle = true;
      continue;
    }
    if (ch === '"') {
      inDouble = true;
      continue;
    }
    if (ch === "`" && (lang === "js" || lang === "c" || lang === "go")) {
      state.inBacktick = true;
      continue;
    }
    if (ch === "{") {
      if (state.templateExprStack.length > 0) {
        state.templateExprStack[state.templateExprStack.length - 1] += 1;
      } else if (!(ignoreBracesInParensBeforeOpen && !seenOpen && parenDepth > 0)) {
        open++;
        seenOpen = true;
      }
      lastCodeChar = "{";
    } else if (ch === "}") {
      if (state.templateExprStack.length > 0) {
        const top = state.templateExprStack[state.templateExprStack.length - 1];
        if (top === 0) {
          state.templateExprStack.pop();
          state.inBacktick = true;
        } else {
          state.templateExprStack[state.templateExprStack.length - 1] = top - 1;
        }
      } else if (!(ignoreBracesInParensBeforeOpen && !seenOpen && parenDepth > 0)) {
        close++;
      }
      lastCodeChar = "}";
    } else if (ch === "(") {
      openParen++;
      parenDepth++;
      lastCodeChar = "(";
    } else if (ch === ")") {
      closeParen++;
      parenDepth = Math.max(0, parenDepth - 1);
      lastCodeChar = ")";
    } else if (!/\s/.test(ch)) {
      lastCodeChar = ch;
    }
  }
  state.parenDepth = parenDepth;
  return {
    open,
    close,
    openParen,
    closeParen,
    endsWithSemi: lastCodeChar === ";",
    lastCodeChar
  };
}
function findBraceBlockEnd(lines, startIdx, lang = "c") {
  let depth = 0;
  let seenOpen = false;
  const state = {
    inBlockComment: false,
    templateExprStack: [],
    inBacktick: false,
    parenDepth: 0
  };
  const maxScan = Math.min(lines.length, startIdx + 2e3);
  for (let i = startIdx; i < maxScan; i++) {
    const { open, close, endsWithSemi, lastCodeChar } = scanBracesOnLine(
      lines[i],
      state,
      lang,
      lang === "js" || lang === "go",
      seenOpen
    );
    if (open > 0) seenOpen = true;
    depth += open - close;
    if (seenOpen && depth <= 0) {
      if (lang === "js" && lastCodeChar === ">" && i <= startIdx + 12) {
        seenOpen = false;
        depth = 0;
        continue;
      }
      return i + 1;
    }
    if (!seenOpen && (state.parenDepth ?? 0) <= 0 && endsWithSemi) {
      return i + 1;
    }
    if (!seenOpen && i > startIdx + 40) {
      return startIdx + 1;
    }
  }
  return seenOpen ? Math.min(lines.length, maxScan) : startIdx + 1;
}
function findTypeAliasEnd(lines, startIdx) {
  const firstLine = lines[startIdx] ?? "";
  const eqIdx = firstLine.indexOf("=");
  const rhs = eqIdx >= 0 ? firstLine.slice(eqIdx + 1) : firstLine;
  const state = {
    inBlockComment: false,
    templateExprStack: [],
    inBacktick: false,
    parenDepth: 0
  };
  const firstScan = scanBracesOnLine(rhs, state, "js");
  if (firstScan.open > firstScan.close) {
    return findBraceBlockEnd(lines, startIdx, "js");
  }
  if (firstScan.endsWithSemi && firstScan.openParen <= firstScan.closeParen) {
    return startIdx + 1;
  }
  let endIdx = startIdx;
  let parenBal = firstScan.openParen - firstScan.closeParen;
  let prevLast = firstScan.lastCodeChar;
  const maxScan = Math.min(lines.length - 1, startIdx + 40);
  while (endIdx < maxScan) {
    const nextLine = lines[endIdx + 1] ?? "";
    const nextTrim = nextLine.trim();
    if (!nextTrim) break;
    const isContinuation = parenBal > 0 || prevLast === "=" || prevLast === "|" || prevLast === "&" || prevLast === "?" || prevLast === ":" || /^[ \t]+(?:[|&?:]|\w+\s*\?*:)/.test(nextLine);
    if (!isContinuation) break;
    endIdx++;
    const scan = scanBracesOnLine(nextLine, state, "js");
    if (scan.open > scan.close) {
      return findBraceBlockEnd(lines, endIdx, "js");
    }
    parenBal += scan.openParen - scan.closeParen;
    prevLast = scan.lastCodeChar;
    if (scan.endsWithSemi && parenBal <= 0) break;
  }
  return endIdx + 1;
}
function findParenCallEnd(lines, startIdx) {
  const state = {
    inBlockComment: false,
    templateExprStack: [],
    inBacktick: false,
    parenDepth: 0
  };
  let seenParen = false;
  const maxScan = Math.min(lines.length, startIdx + 400);
  for (let i = startIdx; i < maxScan; i++) {
    const { openParen } = scanBracesOnLine(lines[i], state, "js");
    if (openParen > 0) seenParen = true;
    if (seenParen && (state.parenDepth ?? 0) <= 0) {
      return i + 1;
    }
  }
  return startIdx + 1;
}
function hasArrowOrBraceWithin(lines, startIdx, maxLookahead = 12) {
  const state = { inBlockComment: false, templateExprStack: [], inBacktick: false };
  let parenDepth = 0;
  let seenParen = false;
  let hasArrow = false;
  const end = Math.min(lines.length, startIdx + maxLookahead);
  for (let i = startIdx; i < end; i++) {
    const line = lines[i];
    const { open, openParen, closeParen, endsWithSemi } = scanBracesOnLine(line, state, "js");
    if (openParen > 0) seenParen = true;
    parenDepth += openParen - closeParen;
    if (line.includes("=>")) hasArrow = true;
    if (open > 0 && (!seenParen || parenDepth <= 0)) {
      return { hasArrow, hasBrace: true };
    }
    if (seenParen && parenDepth <= 0 && endsWithSemi) {
      return { hasArrow, hasBrace: false };
    }
  }
  return { hasArrow, hasBrace: false };
}
function extractTsJsSymbols(lines) {
  const symbols = [];
  const patterns = [
    {
      re: /^(?:export\s+(?:default\s+)?)?(?:declare\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)/,
      kind: "class",
      nameGroup: 1
    },
    {
      re: /^(?:export\s+)?(?:declare\s+)?interface\s+([A-Za-z_$][\w$]*)/,
      kind: "interface",
      nameGroup: 1
    },
    {
      re: /^(?:export\s+)?(?:declare\s+)?type\s+([A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*=/,
      kind: "type",
      nameGroup: 1
    },
    {
      re: /^(?:export\s+)?(?:declare\s+)?(?:const\s+)?enum\s+([A-Za-z_$][\w$]*)/,
      kind: "enum",
      nameGroup: 1
    },
    {
      re: /^(?:export\s+(?:default\s+)?)?(?:declare\s+)?(?:async\s+)?function\*?\s+([A-Za-z_$][\w$]*)/,
      kind: "function",
      nameGroup: 1
    },
    {
      re: /^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s+)?(?:function\b|\([^)]*\)\s*(?::[^=]+)?=>|[A-Za-z_$][\w$]*\s*=>)/,
      kind: "function",
      nameGroup: 1
    }
  ];
  const multilineArrowStartRe = /^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s+)?\(/;
  const methodHeaderRe = /^([ \t]{1,4})(?:(?:public|private|protected|static|readonly|override|abstract|async|get|set)\s+)*([A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*\(/;
  const controlKeywords = /* @__PURE__ */ new Set([
    "if",
    "for",
    "while",
    "switch",
    "catch",
    "else",
    "try",
    "return",
    "new",
    "throw",
    "await",
    "super",
    "typeof",
    "delete"
  ]);
  const routeRegisterRe = /^[ \t]{1,6}(?:app|router|server|fastify)\.(get|post|put|patch|delete|all|route)\s*(?:<[^>]*>)?\s*\(\s*['"`]([^'"`]+)['"`]/;
  const testSuiteRe = /^(?:describe|suite)(?:\.(?:only|skip))?\s*\(\s*['"`]([^'"`]+)['"`]/;
  const testCaseRe = /^[ \t]{0,4}(?:it|test)(?:\.(?:only|skip))?\s*\(\s*['"`]([^'"`]+)['"`]/;
  let currentClassEnd = -1;
  let currentClassIndent = -1;
  let currentFnEnd = -1;
  let currentSuiteEnd = -1;
  const commentState = {
    inBlockComment: false,
    templateExprStack: [],
    inBacktick: false
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const wasInComment = commentState.inBlockComment;
    const wasInBacktick = commentState.inBacktick;
    scanBracesOnLine(line, commentState, "js");
    if (wasInComment || wasInBacktick) continue;
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("//") || trimmed.startsWith("/*") || trimmed.startsWith("*")) {
      continue;
    }
    let matchedTop = false;
    if (!line.startsWith(" ") && !line.startsWith("	")) {
      for (const p of patterns) {
        const m = p.re.exec(line);
        if (m && m[p.nameGroup]) {
          const endLine = p.kind === "type" ? findTypeAliasEnd(lines, i) : findBraceBlockEnd(lines, i, "js");
          symbols.push({
            kind: p.kind,
            name: m[p.nameGroup],
            startLine: i + 1,
            endLine,
            depth: 0
          });
          if (p.kind === "class") {
            currentClassEnd = endLine;
            currentClassIndent = -1;
          } else if (p.kind === "function" && endLine - (i + 1) >= 40) {
            currentFnEnd = endLine;
          }
          matchedTop = true;
          break;
        }
      }
      if (!matchedTop) {
        const ma = multilineArrowStartRe.exec(line);
        if (ma && ma[1]) {
          const { hasArrow } = hasArrowOrBraceWithin(lines, i, 12);
          if (hasArrow) {
            const endLine = findBraceBlockEnd(lines, i, "js");
            symbols.push({
              kind: "function",
              name: ma[1],
              startLine: i + 1,
              endLine,
              depth: 0
            });
            if (endLine - (i + 1) >= 40) {
              currentFnEnd = endLine;
            }
            matchedTop = true;
          }
        }
      }
      if (!matchedTop) {
        const ms = testSuiteRe.exec(line);
        if (ms && ms[1]) {
          const endLine = findParenCallEnd(lines, i);
          symbols.push({
            kind: "suite",
            name: ms[1].slice(0, 64),
            startLine: i + 1,
            endLine,
            depth: 0
          });
          currentSuiteEnd = endLine;
          matchedTop = true;
        }
      }
    }
    if (!matchedTop && i + 1 <= currentClassEnd) {
      const mm = methodHeaderRe.exec(line);
      if (mm && mm[1] && mm[2] && !controlKeywords.has(mm[2])) {
        const indentWidth = mm[1].replace(/\t/g, "  ").length;
        if (currentClassIndent === -1) {
          currentClassIndent = indentWidth;
        }
        if (indentWidth === currentClassIndent) {
          const { hasBrace } = hasArrowOrBraceWithin(lines, i, 10);
          if (hasBrace) {
            const endLine = findBraceBlockEnd(lines, i, "js");
            symbols.push({
              kind: "method",
              name: mm[2],
              startLine: i + 1,
              endLine,
              depth: 1
            });
          }
        }
      }
    } else if (!matchedTop && i + 1 <= currentFnEnd) {
      const mr = routeRegisterRe.exec(line);
      if (mr && mr[1] && mr[2]) {
        const endLine = findParenCallEnd(lines, i);
        symbols.push({
          kind: "route",
          name: `${mr[1].toUpperCase()} ${mr[2]}`,
          startLine: i + 1,
          endLine,
          depth: 1
        });
      }
    } else if (!matchedTop) {
      const mt = testCaseRe.exec(line);
      if (mt && mt[1]) {
        const endLine = findParenCallEnd(lines, i);
        symbols.push({
          kind: "test",
          name: mt[1].slice(0, 64),
          startLine: i + 1,
          endLine,
          depth: i + 1 <= currentSuiteEnd ? 1 : 0
        });
      }
    }
  }
  return symbols;
}
function isPythonFStringPrefix(line, quoteIdx) {
  let k = quoteIdx - 1;
  while (k >= 0 && /[A-Za-z]/.test(line[k])) k--;
  if (k >= 0 && /[0-9_]/.test(line[k])) return false;
  const prefix = line.slice(k + 1, quoteIdx).toLowerCase();
  return prefix === "f" || prefix === "fr" || prefix === "rf";
}
function extractPythonSymbols(lines) {
  const symbols = [];
  const re = /^([ \t]*)(async\s+def|def|class)\s+([A-Za-z_]\w*)/;
  const indentOf = (l) => (/^[ \t]*/.exec(l)?.[0] ?? "").replace(/\t/g, "    ").length;
  const isContinuation = new Array(lines.length).fill(false);
  const inStringContinuation = new Array(lines.length).fill(false);
  let activeQuote = null;
  let activeSingleQuote = null;
  let activeSingleIsFString = false;
  let bracketDepth = 0;
  let backslashContinued = false;
  const fExprStack = [];
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    inStringContinuation[i] = activeQuote !== null || activeSingleQuote !== null || backslashContinued;
    if (!inStringContinuation[i] && (bracketDepth > 0 || fExprStack.length > 0) && re.test(line)) {
      bracketDepth = 0;
      fExprStack.length = 0;
      const defIndent = indentOf(line);
      for (let k = i - 1; k >= 0; k--) {
        if (inStringContinuation[k]) break;
        const t = lines[k].trim();
        if (!t || t.startsWith("#")) {
          isContinuation[k] = false;
          continue;
        }
        if (t.startsWith("@") && indentOf(lines[k]) === defIndent) {
          isContinuation[k] = false;
          continue;
        }
        break;
      }
    }
    isContinuation[i] = inStringContinuation[i] || bracketDepth > 0 || fExprStack.length > 0;
    backslashContinued = false;
    let j = 0;
    while (j < line.length) {
      if (activeQuote) {
        if (line[j] === "\\") {
          j += 2;
          continue;
        }
        if (line.startsWith(activeQuote, j)) {
          activeQuote = null;
          j += 3;
          continue;
        }
        j++;
      } else if (activeSingleQuote) {
        if (line[j] === "\\") {
          if (j === line.length - 1) {
            backslashContinued = true;
            break;
          }
          j += 2;
          continue;
        }
        if (activeSingleIsFString) {
          if (line[j] === "{") {
            if (line[j + 1] === "{") {
              j += 2;
              continue;
            }
            fExprStack.push({
              quote: activeSingleQuote,
              braceDepth: 1,
              baseBracketDepth: bracketDepth
            });
            activeSingleQuote = null;
            activeSingleIsFString = false;
            j++;
            continue;
          }
          if (line[j] === "}" && line[j + 1] === "}") {
            j += 2;
            continue;
          }
        }
        if (line[j] === activeSingleQuote) {
          activeSingleQuote = null;
          activeSingleIsFString = false;
          j++;
          continue;
        }
        j++;
      } else {
        if (line[j] === "#") break;
        if (line.startsWith('"""', j)) {
          activeQuote = '"""';
          j += 3;
          continue;
        }
        if (line.startsWith("'''", j)) {
          activeQuote = "'''";
          j += 3;
          continue;
        }
        if (line[j] === '"' || line[j] === "'") {
          activeSingleQuote = line[j] === '"' ? '"' : "'";
          activeSingleIsFString = isPythonFStringPrefix(line, j);
          j++;
          continue;
        }
        if (fExprStack.length > 0) {
          const top = fExprStack[fExprStack.length - 1];
          if (top.braceDepth === 1 && bracketDepth === top.baseBracketDepth && line[j] === ":" && line[j + 1] !== "=") {
            let specDepth = 1;
            j++;
            while (j < line.length && specDepth > 0) {
              if (line[j] === "\\") {
                j += 2;
                continue;
              }
              if (line[j] === "{") specDepth++;
              else if (line[j] === "}") specDepth--;
              j++;
            }
            if (specDepth === 0) {
              fExprStack.pop();
              activeSingleQuote = top.quote;
              activeSingleIsFString = true;
            }
            continue;
          }
          if (line[j] === "{") {
            top.braceDepth++;
            bracketDepth++;
            j++;
            continue;
          }
          if (line[j] === "}") {
            if (top.braceDepth === 1) {
              fExprStack.pop();
              activeSingleQuote = top.quote;
              activeSingleIsFString = true;
              j++;
              continue;
            }
            top.braceDepth--;
            bracketDepth = Math.max(0, bracketDepth - 1);
            j++;
            continue;
          }
        }
        if (line[j] === "(" || line[j] === "[" || line[j] === "{") bracketDepth++;
        else if (line[j] === ")" || line[j] === "]" || line[j] === "}") {
          bracketDepth = Math.max(0, bracketDepth - 1);
        } else if (line[j] === "\\" && j === line.length - 1) backslashContinued = true;
        j++;
      }
    }
    if (activeSingleQuote && !backslashContinued) {
      activeSingleQuote = null;
      activeSingleIsFString = false;
    }
  }
  const raw = [];
  for (let i = 0; i < lines.length; i++) {
    if (isContinuation[i]) continue;
    const m = re.exec(lines[i]);
    if (!m) continue;
    const indent = m[1].replace(/\t/g, "    ").length;
    const kind = m[2] === "class" ? "class" : indent > 0 ? "method" : "function";
    raw.push({ indent, kind, name: m[3], defIdx: i });
  }
  for (let idx = 0; idx < raw.length; idx++) {
    const item = raw[idx];
    let startIdx = item.defIdx;
    for (let j = item.defIdx - 1; j >= 0; j--) {
      if (isContinuation[j] || !lines[j].trim()) continue;
      const trimmed = lines[j].trimStart();
      if (trimmed.startsWith("#")) continue;
      if (!trimmed.startsWith("@") || indentOf(lines[j]) !== item.indent) break;
      startIdx = j;
    }
    let endIdx = lines.length - 1;
    for (let j = item.defIdx + 1; j < lines.length; j++) {
      if (isContinuation[j]) continue;
      const l = lines[j];
      const trimmed = l.trim();
      if (!trimmed || trimmed.startsWith("#")) continue;
      if (indentOf(l) <= item.indent) {
        endIdx = j - 1;
        break;
      }
    }
    while (endIdx > item.defIdx) {
      const trimmed = lines[endIdx].trim();
      if (trimmed && !(!isContinuation[endIdx] && trimmed.startsWith("#") && indentOf(lines[endIdx]) <= item.indent)) {
        break;
      }
      endIdx--;
    }
    symbols.push({
      kind: item.kind,
      name: item.name,
      startLine: startIdx + 1,
      endLine: endIdx + 1,
      depth: item.indent > 0 ? 1 : 0
    });
  }
  return symbols;
}
function stripLeadingGdAnnotations(text) {
  let rest = text;
  while (rest.startsWith("@")) {
    const m = /^@[A-Za-z_]\w*/.exec(rest);
    if (!m) break;
    let idx = m[0].length;
    while (idx < rest.length && (rest[idx] === " " || rest[idx] === "	")) idx++;
    if (rest[idx] === "(") {
      let depth = 0;
      let quote = null;
      while (idx < rest.length) {
        const ch = rest[idx];
        if (quote) {
          if (ch === "\\") {
            idx += 2;
            continue;
          }
          if (ch === quote) quote = null;
        } else {
          if (ch === '"' || ch === "'") quote = ch;
          else if (ch === "(") depth++;
          else if (ch === ")") {
            depth--;
            if (depth === 0) {
              idx++;
              break;
            }
          }
        }
        idx++;
      }
    }
    while (idx < rest.length && (rest[idx] === " " || rest[idx] === "	")) idx++;
    rest = rest.slice(idx);
  }
  return rest;
}
function extractGdScriptSymbols(rawLines) {
  const lines = rawLines.map((l, idx) => {
    const clean = l.endsWith("\r") ? l.slice(0, -1) : l;
    return idx === 0 ? clean.replace(/^\uFEFF/, "") : clean;
  });
  const info = new Array(lines.length);
  let activeTriple = null;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const startsInTriple = activeTriple !== null;
    let openBrace = 0;
    let closeBrace = 0;
    let openParen = 0;
    let closeParen = 0;
    let hasCode = startsInTriple;
    let j = 0;
    while (j < line.length) {
      if (activeTriple) {
        if (line[j] === "\\") {
          j += 2;
          continue;
        }
        if (line.startsWith(activeTriple, j)) {
          activeTriple = null;
          j += 3;
          continue;
        }
        j++;
      } else {
        const ch = line[j];
        if (ch === "#") break;
        if (line.startsWith('"""', j) || line.startsWith("'''", j)) {
          activeTriple = line.startsWith('"""', j) ? '"""' : "'''";
          hasCode = true;
          j += 3;
          continue;
        }
        if (ch === '"' || ch === "'") {
          hasCode = true;
          j++;
          while (j < line.length && line[j] !== ch) {
            if (line[j] === "\\") j += 2;
            else j++;
          }
          if (j < line.length) j++;
          continue;
        }
        if (ch !== " " && ch !== "	") {
          hasCode = true;
          if (ch === "{") openBrace++;
          else if (ch === "}") closeBrace++;
          else if (ch === "(") openParen++;
          else if (ch === ")") closeParen++;
        }
        j++;
      }
    }
    info[i] = {
      startsInTriple,
      openBrace,
      closeBrace,
      openParen,
      closeParen,
      hasCode
    };
  }
  const raw = [];
  const classNameRe = /^class_name\s+([A-Za-z_]\w*)\b/;
  const classRe = /^class\s+([A-Za-z_]\w*)\b/;
  const funcRe = /^(?:static\s+)?func\s+([A-Za-z_]\w*)\s*\(/;
  const signalRe = /^signal\s+([A-Za-z_]\w*)\b/;
  const enumRe = /^enum(?:\s+([A-Za-z_]\w*))?\s*(?:\{|$|#)/;
  for (let i = 0; i < lines.length; i++) {
    if (info[i].startsInTriple) continue;
    const line = lines[i];
    const indentStr = /^[ \t]*/.exec(line)?.[0] ?? "";
    const afterIndent = line.slice(indentStr.length);
    if (!afterIndent || afterIndent.startsWith("#")) continue;
    const rest = stripLeadingGdAnnotations(afterIndent);
    if (!rest || rest.startsWith("#")) continue;
    const indent = indentStr.replace(/\t/g, "    ").length;
    let m = classNameRe.exec(rest);
    if (m && m[1]) {
      raw.push({ indent, rawKind: "class_name", name: m[1], startIdx: i });
      continue;
    }
    m = classRe.exec(rest);
    if (m && m[1]) {
      raw.push({ indent, rawKind: "class", name: m[1], startIdx: i });
      continue;
    }
    m = funcRe.exec(rest);
    if (m && m[1]) {
      raw.push({ indent, rawKind: "func", name: m[1], startIdx: i });
      continue;
    }
    m = signalRe.exec(rest);
    if (m && m[1]) {
      raw.push({ indent, rawKind: "signal", name: m[1], startIdx: i });
      continue;
    }
    m = enumRe.exec(rest);
    if (m) {
      if (!m[1] && info[i].openBrace === 0) {
        let nextCodeIdx = i + 1;
        while (nextCodeIdx < lines.length && !info[nextCodeIdx].hasCode) {
          nextCodeIdx++;
        }
        if (nextCodeIdx >= lines.length || info[nextCodeIdx].openBrace === 0) {
          continue;
        }
      }
      raw.push({
        indent,
        rawKind: "enum",
        name: m[1] ?? "<anonymous>",
        startIdx: i
      });
    }
  }
  const symbols = [];
  const classStack = [];
  for (const item of raw) {
    let endIdx = item.startIdx;
    if (item.rawKind === "class_name") {
      endIdx = item.startIdx;
    } else if (item.rawKind === "signal") {
      let parenDepth = info[item.startIdx].openParen - info[item.startIdx].closeParen;
      const maxSigScan = Math.min(lines.length - 1, item.startIdx + 40);
      while (parenDepth > 0 && endIdx < maxSigScan) {
        endIdx++;
        parenDepth += info[endIdx].openParen - info[endIdx].closeParen;
      }
      if (parenDepth > 0) endIdx = item.startIdx;
    } else if (item.rawKind === "enum") {
      let braceDepth = 0;
      let seenOpen = false;
      const maxEnumScan = Math.min(lines.length - 1, item.startIdx + 500);
      for (let k = item.startIdx; k <= maxEnumScan; k++) {
        const li = info[k];
        if (li.openBrace > 0) seenOpen = true;
        braceDepth += li.openBrace - li.closeBrace;
        if (seenOpen && braceDepth <= 0) {
          endIdx = k;
          break;
        }
        if (!seenOpen && k > item.startIdx && li.hasCode && li.openBrace === 0) {
          break;
        }
      }
      if (seenOpen && braceDepth > 0) {
        endIdx = maxEnumScan;
      }
    } else {
      let headerEndIdx = item.startIdx;
      let parenDepth = info[item.startIdx].openParen - info[item.startIdx].closeParen;
      const maxHeaderScan = Math.min(lines.length - 1, item.startIdx + 40);
      while (parenDepth > 0 && headerEndIdx < maxHeaderScan) {
        headerEndIdx++;
        parenDepth += info[headerEndIdx].openParen - info[headerEndIdx].closeParen;
      }
      if (parenDepth > 0) headerEndIdx = item.startIdx;
      endIdx = lines.length - 1;
      for (let j = headerEndIdx + 1; j < lines.length; j++) {
        if (info[j].startsInTriple) continue;
        const l = lines[j];
        const trimmed = l.trim();
        if (!trimmed || trimmed.startsWith("#")) continue;
        const curIndent = (/^[ \t]*/.exec(l)?.[0] ?? "").replace(/\t/g, "    ").length;
        if (curIndent <= item.indent) {
          endIdx = j - 1;
          break;
        }
      }
      while (endIdx > headerEndIdx) {
        if (info[endIdx].startsInTriple) break;
        const endLineStr = lines[endIdx];
        const endTrimmed = endLineStr.trim();
        if (!endTrimmed) {
          endIdx--;
          continue;
        }
        if (endTrimmed.startsWith("#")) {
          const commentIndent = (/^[ \t]*/.exec(endLineStr)?.[0] ?? "").replace(
            /\t/g,
            "    "
          ).length;
          if (commentIndent <= item.indent) {
            endIdx--;
            continue;
          }
        }
        break;
      }
    }
    const startLine = item.startIdx + 1;
    const endLine = Math.max(startLine, endIdx + 1);
    while (classStack.length > 0 && (classStack[classStack.length - 1].endLine < startLine || classStack[classStack.length - 1].indent >= item.indent)) {
      classStack.pop();
    }
    const depth = classStack.length > 0 ? classStack.length : item.indent > 0 ? Math.max(1, Math.floor(item.indent / 4)) : 0;
    const kind = item.rawKind === "func" ? item.indent > 0 ? "method" : "function" : item.rawKind;
    symbols.push({
      kind,
      name: item.name,
      startLine,
      endLine,
      depth
    });
    if (item.rawKind === "class") {
      classStack.push({ indent: item.indent, endLine });
    }
  }
  return symbols;
}
function extractGoSymbols(lines) {
  const symbols = [];
  const funcRe = /^func\s+(?:\(([^)]+)\)\s+)?([A-Za-z_]\w*)/;
  const typeRe = /^type\s+([A-Za-z_]\w*(?:\[[^\]]*\])?)\s+(struct|interface)\b/;
  const commentState = {
    inBlockComment: false,
    templateExprStack: [],
    inBacktick: false
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const wasInComment = commentState.inBlockComment;
    const wasInBacktick = commentState.inBacktick;
    scanBracesOnLine(line, commentState, "go");
    if (wasInComment || wasInBacktick) continue;
    const fm = funcRe.exec(line);
    if (fm && fm[2]) {
      const recv = fm[1]?.trim();
      const name = recv ? `(${recv}) ${fm[2]}` : fm[2];
      symbols.push({
        kind: recv ? "method" : "function",
        name,
        startLine: i + 1,
        endLine: findBraceBlockEnd(lines, i, "go"),
        depth: 0
      });
      continue;
    }
    const tm = typeRe.exec(line);
    if (tm && tm[1] && tm[2]) {
      symbols.push({
        kind: tm[2],
        name: tm[1],
        startLine: i + 1,
        endLine: findBraceBlockEnd(lines, i, "go"),
        depth: 0
      });
    }
  }
  return symbols;
}
function extractRustSymbols(lines) {
  const symbols = [];
  const re = /^([ \t]*)(?:pub(?:\([^)]*\))?\s+)?(?:(?:async|unsafe|const|extern(?:\s+"[^"]*")?)\s+)*(fn|struct|enum|trait|impl|mod)(?:\s+|\s*(?=<))([A-Za-z_<][\w:<>,+'&\s]*?)(?:\s*[\({;]|$)/;
  const commentState = {
    inBlockComment: false,
    templateExprStack: [],
    inBacktick: false
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const wasInComment = commentState.inBlockComment;
    scanBracesOnLine(line, commentState, "rust");
    if (wasInComment) continue;
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("//") || trimmed.startsWith("/*")) {
      continue;
    }
    const m = re.exec(line);
    if (!m || !m[2] || !m[3]) continue;
    const indent = m[1].length;
    if (indent > 4) continue;
    symbols.push({
      kind: m[2],
      name: m[3].trim(),
      startLine: i + 1,
      endLine: findBraceBlockEnd(lines, i, "rust"),
      depth: indent > 0 ? 1 : 0
    });
  }
  return symbols;
}
function extractCLikeSymbols(lines) {
  const symbols = [];
  const typeRe = /^([ \t]{0,4})(?:(?:public|private|protected|internal|static|final|abstract|sealed|data|open|export|partial)\s+)*(class|interface|enum|struct|record|protocol|namespace)\s+([A-Za-z_]\w*)/;
  const kwFuncRe = /^([ \t]{0,4})(?:(?:public|private|protected|internal|static|final|override|virtual|inline|async|suspend|open|mutating)\s+)*(?:fun|func)\s+([A-Za-z_]\w*)/;
  const cMethodRe = /^([ \t]{0,4})(?:(?:public|private|protected|internal|static|final|abstract|override|virtual|inline|constexpr|extern|async|synchronized|native|unsigned|signed|const)\s+)*(?:[A-Za-z_~][\w:<>,\[\]\s*&]*?\s+)([A-Za-z_~][\w:]*)\s*\([^;]*$/;
  const excludedNames = /* @__PURE__ */ new Set([
    "if",
    "for",
    "while",
    "switch",
    "catch",
    "else",
    "return",
    "sizeof",
    "alignof",
    "decltype",
    "new",
    "delete",
    "throw",
    "typedef",
    "using",
    "case"
  ]);
  const commentState = {
    inBlockComment: false,
    templateExprStack: [],
    inBacktick: false
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const wasInComment = commentState.inBlockComment;
    scanBracesOnLine(line, commentState, "c");
    if (wasInComment) continue;
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("//") || trimmed.startsWith("/*") || trimmed.startsWith("*") || trimmed.startsWith("#")) {
      continue;
    }
    const tm = typeRe.exec(line);
    if (tm && tm[2] && tm[3]) {
      const indented = (tm[1]?.length ?? 0) > 0;
      symbols.push({
        kind: tm[2],
        name: tm[3],
        startLine: i + 1,
        endLine: findBraceBlockEnd(lines, i, "c"),
        depth: indented ? 1 : 0
      });
      continue;
    }
    const fm = kwFuncRe.exec(line);
    if (fm && fm[2]) {
      const indented = (fm[1]?.length ?? 0) > 0;
      symbols.push({
        kind: indented ? "method" : "function",
        name: fm[2],
        startLine: i + 1,
        endLine: findBraceBlockEnd(lines, i, "c"),
        depth: indented ? 1 : 0
      });
      continue;
    }
    const cm = cMethodRe.exec(line);
    if (cm && cm[2] && !excludedNames.has(cm[2]) && !trimmed.includes("=")) {
      const { hasBrace } = hasArrowOrBraceWithin(lines, i, 8);
      if (hasBrace) {
        const indented = (cm[1]?.length ?? 0) > 0;
        symbols.push({
          kind: indented ? "method" : "function",
          name: cm[2],
          startLine: i + 1,
          endLine: findBraceBlockEnd(lines, i, "c"),
          depth: indented ? 1 : 0
        });
      }
    }
  }
  return symbols;
}
function extractShellSymbols(lines) {
  const symbols = [];
  const re = /^(?:function\s+([A-Za-z_][\w-]*)\s*(?:\(\))?|([A-Za-z_][\w-]*)\s*\(\))\s*\{/;
  for (let i = 0; i < lines.length; i++) {
    const m = re.exec(lines[i]);
    if (!m) continue;
    const name = m[1] || m[2];
    if (!name) continue;
    symbols.push({
      kind: "function",
      name,
      startLine: i + 1,
      endLine: findBraceBlockEnd(lines, i, "shell"),
      depth: 0
    });
  }
  return symbols;
}
function extractMarkdownSymbols(lines) {
  const raw = [];
  let inFence = false;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (/^\s*(`{3,}|~{3,})/.test(line)) {
      inFence = !inFence;
      continue;
    }
    if (inFence) continue;
    const m = /^(#{1,4})\s+(.+?)\s*#*$/.exec(line);
    if (m && m[1] && m[2]) {
      raw.push({ level: m[1].length, name: m[2], startLine: i + 1 });
    }
  }
  return raw.map((item, idx) => {
    let endLine = lines.length;
    for (let j = idx + 1; j < raw.length; j++) {
      if (raw[j].level <= item.level) {
        endLine = raw[j].startLine - 1;
        break;
      }
    }
    return {
      kind: `h${item.level}`,
      name: item.name,
      startLine: item.startLine,
      endLine: Math.max(item.startLine, endLine),
      depth: item.level - 1
    };
  });
}
function extractJsonYamlSymbols(lines, isJson) {
  const symbols = [];
  if (isJson) {
    let keyIndent = null;
    for (let i = 0; i < lines.length; i++) {
      const m = /^([ \t]+)"([^"]+)"\s*:/.exec(lines[i]);
      if (m && m[1] && m[2]) {
        if (keyIndent === null) {
          keyIndent = m[1];
        }
        if (m[1] === keyIndent) {
          symbols.push({
            kind: "key",
            name: m[2],
            startLine: i + 1,
            endLine: i + 1,
            depth: 0
          });
        }
      }
    }
    for (let idx = 0; idx < symbols.length; idx++) {
      const cur = symbols[idx];
      const nextStart = idx + 1 < symbols.length ? symbols[idx + 1].startLine - 1 : lines.length - 1;
      let endLine = Math.max(cur.startLine, nextStart);
      while (endLine > cur.startLine && /^\s*[}\]]?,?\s*$/.test(lines[endLine - 1] ?? "") && idx === symbols.length - 1) {
        endLine--;
      }
      cur.endLine = endLine;
    }
    return symbols;
  }
  const re = /^([A-Za-z0-9_.-]+)\s*:/;
  for (let i = 0; i < lines.length; i++) {
    const m = re.exec(lines[i]);
    if (m && m[1]) {
      symbols.push({
        kind: "key",
        name: m[1],
        startLine: i + 1,
        endLine: i + 1,
        depth: 0
      });
    }
  }
  for (let idx = 0; idx < symbols.length; idx++) {
    const cur = symbols[idx];
    let endLine = idx + 1 < symbols.length ? symbols[idx + 1].startLine - 1 : lines.length;
    while (endLine > cur.startLine && !(lines[endLine - 1] ?? "").trim()) {
      endLine--;
    }
    cur.endLine = Math.max(cur.startLine, endLine);
  }
  return symbols;
}
function extractCssSymbols(lines, compactOnly = false) {
  const symbols = [];
  const state = {
    inBlockComment: false,
    templateExprStack: [],
    inBacktick: false
  };
  let braceDepth = 0;
  const atRuleRe = /^@(layer|media|keyframes|supports|container|font-face|theme)\b([^{;]*)/i;
  const tokenBlockRe = /^(:root\b|html(?:\[[^\]]+\]|\.[A-Za-z_-][\w-]*)?(?=\s|[,{])|\[data-theme[^\]]*\]|body\b)[^{;]*\{/;
  const sectionCommentRe = /^\/\*\s*[=\-*#]*\s*([^*/=][^*/]{1,70}?)\s*[=\-*#]*\s*\*\/$/;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const trimmed = line.trim();
    const wasInComment = state.inBlockComment;
    if (!wasInComment && braceDepth === 0 && trimmed) {
      const cm = sectionCommentRe.exec(trimmed);
      if (cm && cm[1] && /[A-Za-z0-9\u4e00-\u9fff]/.test(cm[1])) {
        symbols.push({
          kind: "section",
          name: cm[1].trim().slice(0, 64),
          startLine: i + 1,
          endLine: i + 1,
          depth: 0
        });
      } else {
        const am = atRuleRe.exec(trimmed);
        if (am && am[1]) {
          const endLine = trimmed.includes("{") ? findBraceBlockEnd(lines, i, "c") : i + 1;
          const rest = (am[2] ?? "").trim().replace(/\s+/g, " ").slice(0, 56);
          symbols.push({
            kind: `@${am[1].toLowerCase()}`,
            name: rest || `@${am[1].toLowerCase()}`,
            startLine: i + 1,
            endLine,
            depth: 0
          });
        } else {
          const tm = tokenBlockRe.exec(trimmed);
          if (tm && tm[1]) {
            const endLine = findBraceBlockEnd(lines, i, "c");
            const header = trimmed.slice(0, trimmed.indexOf("{")).trim().replace(/\s+/g, " ").slice(0, 60);
            symbols.push({
              kind: "tokens",
              name: header || tm[1],
              startLine: i + 1,
              endLine,
              depth: 0
            });
          }
        }
      }
    }
    const { open, close } = scanBracesOnLine(line, state, "c");
    braceDepth = Math.max(0, braceDepth + open - close);
  }
  if (!compactOnly && symbols.length < 10) {
    const fallbackState = {
      inBlockComment: false,
      templateExprStack: [],
      inBacktick: false
    };
    let d = 0;
    let lastAddedLine = -999;
    const seen = new Set(symbols.map((s) => s.startLine));
    for (let i = 0; i < lines.length && symbols.length < 40; i++) {
      const line = lines[i];
      const trimmed = line.trim();
      const wasInComment = fallbackState.inBlockComment;
      if (!wasInComment && d === 0 && !seen.has(i + 1) && i - lastAddedLine >= 10 && /^[.#\[a-zA-Z*:]/.test(trimmed) && trimmed.includes("{") && !trimmed.startsWith("@")) {
        const sel = trimmed.slice(0, trimmed.indexOf("{")).trim().replace(/\s+/g, " ").slice(0, 60);
        if (sel) {
          const endLine = findBraceBlockEnd(lines, i, "c");
          symbols.push({
            kind: "rule",
            name: sel,
            startLine: i + 1,
            endLine,
            depth: 0
          });
          lastAddedLine = i;
        }
      }
      const { open, close } = scanBracesOnLine(line, fallbackState, "c");
      d = Math.max(0, d + open - close);
    }
    symbols.sort((a, b) => a.startLine - b.startLine);
  }
  return symbols;
}
function extractSfcSymbols(lines) {
  const symbols = [];
  const blockOpenRe = /^<(script|template|style)\b([^>]*)>/i;
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const m = blockOpenRe.exec(line.trim());
    if (!m || !m[1] || line.startsWith("  ") || line.startsWith("	")) {
      i++;
      continue;
    }
    const tag = m[1].toLowerCase();
    const attrs = (m[2] ?? "").trim();
    const startIdx = i;
    let endIdx = i;
    if (tag === "template") {
      let depth = 0;
      const openTemplateRe = /<template\b[^>]*>/gi;
      const closeTemplateRe = /<\/template\s*>/gi;
      for (let j = i; j < lines.length; j++) {
        const ln = lines[j];
        const opens = ln.match(openTemplateRe)?.length ?? 0;
        const closes = ln.match(closeTemplateRe)?.length ?? 0;
        depth += opens - closes;
        if (depth <= 0) {
          endIdx = j;
          break;
        }
        endIdx = j;
      }
    } else {
      const closeRe = new RegExp(`</${tag}\\s*>`, "i");
      for (let j = i; j < lines.length; j++) {
        endIdx = j;
        if (j > i && closeRe.test(lines[j])) break;
        if (j === i && closeRe.test(lines[j].slice(m[0].length))) break;
      }
    }
    const headerLabel = attrs ? `<${tag} ${attrs.slice(0, 40)}>` : `<${tag}>`;
    symbols.push({
      kind: tag,
      name: headerLabel,
      startLine: startIdx + 1,
      endLine: endIdx + 1,
      depth: 0
    });
    const innerStart = startIdx + 1;
    const innerLines = lines.slice(innerStart, endIdx);
    if (tag === "script" && innerLines.length > 0) {
      const innerSyms = extractTsJsSymbols(innerLines);
      for (const s of innerSyms) {
        symbols.push({
          kind: s.kind,
          name: s.name,
          startLine: s.startLine + innerStart,
          endLine: s.endLine + innerStart,
          depth: s.depth + 1
        });
      }
    } else if (tag === "template" && innerLines.length >= 30) {
      let landmarks = 0;
      const commentLandmarkRe = /^<!--\s*[=\-*#]*\s*([^-][^>]{2,60}?)\s*[=\-*#]*\s*-->$/;
      const semanticTagRe = /^<(header|nav|main|aside|section|footer|dialog|form|[A-Z][A-Za-z0-9]*(?:Modal|Drawer|Dialog|Panel))\b([^>]*)>/;
      for (let k = 0; k < innerLines.length && landmarks < 14; k++) {
        const raw = innerLines[k];
        const indent = raw.length - raw.trimStart().length;
        if (indent > 8) continue;
        const trimmed = raw.trim();
        const cm = commentLandmarkRe.exec(trimmed);
        if (cm && cm[1]) {
          symbols.push({
            kind: "section",
            name: cm[1].trim(),
            startLine: innerStart + k + 1,
            endLine: innerStart + k + 1,
            depth: 1
          });
          landmarks++;
          continue;
        }
        const sm = semanticTagRe.exec(trimmed);
        if (sm && sm[1]) {
          const attrStr = sm[2] ?? "";
          const idMatch = /(?:data-testid|id|class)=["']([^"']+)["']/.exec(attrStr);
          const tagDesc = idMatch && idMatch[1] ? `<${sm[1]} ${idMatch[1].slice(0, 36)}>` : `<${sm[1]}>`;
          symbols.push({
            kind: "block",
            name: tagDesc,
            startLine: innerStart + k + 1,
            endLine: innerStart + k + 1,
            depth: 1
          });
          landmarks++;
        }
      }
    } else if (tag === "style" && innerLines.length > 0) {
      const cssSyms = extractCssSymbols(innerLines, true).slice(0, 12);
      for (const s of cssSyms) {
        symbols.push({
          kind: s.kind,
          name: s.name,
          startLine: s.startLine + innerStart,
          endLine: s.endLine + innerStart,
          depth: 1
        });
      }
    }
    i = endIdx + 1;
  }
  return symbols.length > 0 ? symbols : extractTsJsSymbols(lines);
}
function extractSymbolsFromLines(filePath, lines) {
  const ext = path8.extname(filePath).toLowerCase();
  switch (ext) {
    case ".ts":
    case ".tsx":
    case ".mts":
    case ".cts":
    case ".js":
    case ".jsx":
    case ".mjs":
    case ".cjs":
      return extractTsJsSymbols(lines);
    case ".vue":
    case ".svelte":
      return extractSfcSymbols(lines);
    case ".css":
    case ".scss":
    case ".less":
    case ".pcss":
      return extractCssSymbols(lines);
    case ".py":
    case ".pyi":
      return extractPythonSymbols(lines);
    case ".gd":
      return extractGdScriptSymbols(lines);
    case ".go":
      return extractGoSymbols(lines);
    case ".rs":
      return extractRustSymbols(lines);
    case ".java":
    case ".kt":
    case ".kts":
    case ".cs":
    case ".swift":
    case ".c":
    case ".cc":
    case ".cpp":
    case ".h":
    case ".hpp":
      return extractCLikeSymbols(lines);
    case ".sh":
    case ".bash":
    case ".zsh":
      return extractShellSymbols(lines);
    case ".md":
    case ".mdx":
      return extractMarkdownSymbols(lines);
    case ".json":
      return extractJsonYamlSymbols(lines, true);
    case ".yaml":
    case ".yml":
      return extractJsonYamlSymbols(lines, false);
    default:
      return extractTsJsSymbols(lines);
  }
}
var MAX_OUTLINES = 40;
var MAX_SLICE_FILES = 30;
var SLICE_TRUNCATION_TIP = "\n[tip: MCP responses are hard-capped <=8.8KB inline; for >150 lines across files, use parallel read_file(StartLine, EndLine) (46KB/call, 0 remote check)]";
var OUTLINE_BATCH_TIP = "\n[tip: outlining >20 files splits the 8.8KB cap thinly; batch 10-20 files per call for fuller outlines]";
function selectOutlineSymbols(symbols, limit) {
  if (symbols.length <= limit) return symbols;
  const keptIndices = /* @__PURE__ */ new Set();
  for (let i = 0; i < symbols.length && keptIndices.size < limit; i++) {
    if (symbols[i].depth === 0) keptIndices.add(i);
  }
  for (let i = 0; i < symbols.length && keptIndices.size < limit; i++) {
    keptIndices.add(i);
  }
  return [...keptIndices].sort((a, b) => a - b).map((idx) => symbols[idx]);
}
async function runReadSlices(input) {
  const corruptedItems = [];
  let outlines = [];
  if (input?.outlines !== void 0) {
    const parsedOutlines = parseMaybeJsonArray(input.outlines);
    if (!parsedOutlines.ok) {
      corruptedItems.push({
        label: "outlines",
        value: input.outlines,
        expected: "an array of file path strings"
      });
    } else {
      for (let i = 0; i < parsedOutlines.value.length; i++) {
        const item = parsedOutlines.value[i];
        if (typeof item !== "string" || !item.trim()) {
          corruptedItems.push({
            label: `outlines[${i}]`,
            value: item,
            expected: "a non-empty file path string"
          });
        }
      }
      if (corruptedItems.length === 0) {
        outlines = parsedOutlines.value;
      }
    }
  }
  let slices = [];
  if (input?.slices !== void 0) {
    const parsedSlices = parseMaybeJsonArray(input.slices);
    if (!parsedSlices.ok) {
      corruptedItems.push({
        label: "slices",
        value: input.slices,
        expected: "an array of slice objects ({ path, ranges })"
      });
    } else {
      for (let i = 0; i < parsedSlices.value.length; i++) {
        const item = parsedSlices.value[i];
        if (!isPlainObject(item)) {
          corruptedItems.push({
            label: `slices[${i}]`,
            value: item,
            expected: "a slice object ({ path, ranges })"
          });
          continue;
        }
        if (item.ranges !== void 0 && !Array.isArray(item.ranges)) {
          corruptedItems.push({
            label: `slices[${i}].ranges`,
            value: item.ranges,
            expected: "an array of [startLine, endLine] pairs"
          });
        } else if (Array.isArray(item.ranges)) {
          for (let j = 0; j < item.ranges.length; j++) {
            const r = item.ranges[j];
            if (typeof r === "boolean" || r === null) {
              corruptedItems.push({
                label: `slices[${i}].ranges[${j}]`,
                value: r,
                expected: "a [startLine, endLine] pair"
              });
            }
          }
        }
      }
      if (corruptedItems.length === 0) {
        slices = parsedSlices.value;
      }
    }
  }
  if (corruptedItems.length > 0) {
    return {
      text: formatCorruptedArrayError({
        header: "Validation failed",
        toolName: "read_slices",
        items: corruptedItems,
        fallbackAdvice: "immediately fall back to parallel built-in read_file(StartLine, EndLine) calls."
      }),
      isError: true
    };
  }
  if (outlines.length === 0 && slices.length === 0) {
    return {
      text: "Error: provide at least one entry in 'outlines' or 'slices'.",
      isError: true
    };
  }
  if (outlines.length > MAX_OUTLINES || slices.length > MAX_SLICE_FILES) {
    return {
      text: `Error: limits exceeded (max ${MAX_OUTLINES} outlines and ${MAX_SLICE_FILES} slice files per call).`,
      isError: true
    };
  }
  const baseCwd = resolvePath(input.cwd || process.cwd());
  const rawMaxChars = Number(input.maxChars ?? 8500);
  const maxChars = Number.isFinite(rawMaxChars) ? Math.max(1e3, Math.min(DEFAULT_INLINE_MAX_BYTES, Math.trunc(rawMaxChars))) : 8500;
  const maxBytes = DEFAULT_INLINE_MAX_BYTES;
  const perFileSymbolLimit = outlines.length <= 1 ? 120 : outlines.length <= 4 ? 60 : outlines.length <= 10 ? 30 : outlines.length <= 20 ? 16 : 10;
  const fileCache = /* @__PURE__ */ new Map();
  async function loadLines(absPath) {
    const cached = fileCache.get(absPath);
    if (cached) return cached;
    const data = await readTextFile(absPath);
    const lines = data.content.replace(/\r\n/g, "\n").split("\n");
    const entry = { lines, size: data.size };
    fileCache.set(absPath, entry);
    return entry;
  }
  const sections = [];
  if (outlines.length > 0) {
    const outlineResults = await Promise.all(
      outlines.map(async (rawPath) => {
        const absPath = resolvePath(rawPath, baseCwd);
        const relPath = toPosixPath(path8.relative(baseCwd, absPath)) || path8.basename(absPath);
        try {
          const { lines } = await loadLines(absPath);
          const symbols = extractSymbolsFromLines(absPath, lines);
          if (symbols.length === 0) {
            return `## Outline: ${relPath} (${lines.length} lines)
(no top-level symbols detected)`;
          }
          const picked = selectOutlineSymbols(symbols, perFileSymbolLimit);
          const formatted = picked.map((s) => {
            const indent = "  ".repeat(s.depth);
            const span = s.endLine > s.startLine ? `L${s.startLine}-L${s.endLine}` : `L${s.startLine}`;
            return `${indent}${span} [${s.kind}] ${s.name}`;
          });
          const extra = symbols.length > picked.length ? outlines.length > 10 ? `
... (+${symbols.length - picked.length} more symbols)` : `
... (${symbols.length - picked.length} more symbols omitted; outline this file alone for full list)` : "";
          return `## Outline: ${relPath} (${lines.length} lines)
${formatted.join("\n")}${extra}`;
        } catch (err) {
          return `## Outline: ${relPath}
ERROR: ${err instanceof Error ? err.message : String(err)}`;
        }
      })
    );
    sections.push(...outlineResults);
  }
  if (slices.length > 0) {
    const sliceResults = await Promise.all(
      slices.map(async (req) => {
        if (!req || typeof req.path !== "string" || !req.path.trim()) {
          return "## Slice: (invalid)\nERROR: 'path' must be a non-empty string";
        }
        const absPath = resolvePath(req.path, baseCwd);
        const relPath = toPosixPath(path8.relative(baseCwd, absPath)) || path8.basename(absPath);
        if (!Array.isArray(req.ranges) || req.ranges.length === 0) {
          return `## Slice: ${relPath}
ERROR: 'ranges' must be a non-empty array of [startLine, endLine] pairs`;
        }
        try {
          const { lines } = await loadLines(absPath);
          const totalLines = lines.length;
          const blocks = [];
          for (const range of req.ranges) {
            if (!Array.isArray(range) || range.length < 2) {
              blocks.push("ERROR: each range must be [startLine, endLine]");
              continue;
            }
            const rawStart = Math.trunc(Number(range[0]));
            const rawEnd = Math.trunc(Number(range[1]));
            if (!Number.isFinite(rawStart) || !Number.isFinite(rawEnd)) {
              blocks.push(`ERROR: invalid range [${String(range[0])}, ${String(range[1])}]`);
              continue;
            }
            const start = Math.max(1, rawStart);
            const end = Math.min(totalLines, rawEnd);
            if (start > totalLines) {
              blocks.push(
                `--- L${rawStart}-L${rawEnd} (out of bounds; file has ${totalLines} lines) ---`
              );
              continue;
            }
            if (end < start) {
              blocks.push(
                `ERROR: startLine (${rawStart}) > endLine (${rawEnd})`
              );
              continue;
            }
            const width = String(end).length;
            const snippet = [`--- L${start}-L${end} (of ${totalLines}) ---`];
            for (let ln = start; ln <= end; ln++) {
              const num = String(ln).padStart(width, " ");
              snippet.push(`${num}: ${lines[ln - 1] ?? ""}`);
            }
            blocks.push(snippet.join("\n"));
          }
          return `## Slice: ${relPath} (${totalLines} lines)
${blocks.join("\n")}`;
        } catch (err) {
          return `## Slice: ${relPath}
ERROR: ${err instanceof Error ? err.message : String(err)}`;
        }
      })
    );
    sections.push(...sliceResults);
  }
  const rawJoined = sections.join("\n\n");
  const rawBytes = Buffer.byteLength(rawJoined, "utf8");
  const isOverBudget = rawJoined.length > maxChars || rawBytes > maxBytes;
  const activeTip = isOverBudget && slices.length > 0 ? SLICE_TRUNCATION_TIP : isOverBudget && outlines.length > 20 ? OUTLINE_BATCH_TIP : null;
  if (activeTip && maxChars > activeTip.length + 200) {
    const tipBytes = Buffer.byteLength(activeTip, "utf8");
    const body = fitSectionsToByteBudget(
      sections,
      maxChars - activeTip.length,
      maxBytes - tipBytes,
      "\n\n"
    );
    return {
      text: `${body}${activeTip}`,
      isError: false
    };
  }
  return {
    text: fitSectionsToByteBudget(sections, maxChars, maxBytes, "\n\n"),
    isError: false
  };
}

// src/tools/run-checks.ts
import { promises as fs7 } from "node:fs";
import { performance } from "node:perf_hooks";
async function mapWithConcurrency(items, concurrency, worker) {
  const results = new Array(items.length);
  let nextIdx = 0;
  async function runWorker() {
    while (true) {
      const idx = nextIdx++;
      if (idx >= items.length) return;
      results[idx] = await worker(items[idx]);
    }
  }
  const workerCount = Math.min(concurrency, items.length);
  await Promise.all(Array.from({ length: workerCount }, () => runWorker()));
  return results;
}
async function runChecks(input, budgetMs = MCP_CALL_BUDGET_SEC * 1e3) {
  const parsedCommands = parseMaybeJsonArray(input?.commands);
  if (!parsedCommands.ok) {
    if (parsedCommands.kind === "corrupted_literal" || parsedCommands.kind === "wrong_type" && (typeof input?.commands === "boolean" || input?.commands === null)) {
      return {
        text: formatCorruptedArrayError({
          header: "Validation failed",
          toolName: "run_checks",
          items: [
            {
              label: "commands",
              value: parsedCommands.kind === "corrupted_literal" ? parsedCommands.value : input.commands,
              expected: "an array of command specs"
            }
          ],
          fallbackAdvice: "immediately fall back to a single compound run_command call."
        }),
        isError: true
      };
    }
    return {
      text: "Error: 'commands' must be a non-empty array of check commands.",
      isError: true
    };
  }
  const rawCommands = parsedCommands.value;
  if (rawCommands.length === 0) {
    return {
      text: "Error: 'commands' must be a non-empty array of check commands.",
      isError: true
    };
  }
  if (rawCommands.length > 12) {
    return {
      text: "Error: 'commands' supports at most 12 commands per call.",
      isError: true
    };
  }
  const corruptedCommands = [];
  for (let i = 0; i < rawCommands.length; i++) {
    const raw = rawCommands[i];
    if (typeof raw !== "string" && !isPlainObject(raw)) {
      corruptedCommands.push({
        label: `commands[${i}]`,
        value: raw,
        expected: "a command string or object ({ name?, command, cwd?, timeoutSec? })"
      });
    }
  }
  if (corruptedCommands.length > 0) {
    return {
      text: formatCorruptedArrayError({
        header: "Validation failed",
        toolName: "run_checks",
        items: corruptedCommands,
        fallbackAdvice: "immediately fall back to a single compound run_command call."
      }),
      isError: true
    };
  }
  const baseCwd = resolvePath(input.cwd || process.cwd());
  try {
    const st = await fs7.stat(baseCwd);
    if (!st.isDirectory()) {
      return { text: `Error: cwd is not a directory: ${baseCwd}`, isError: true };
    }
  } catch {
    return { text: `Error: cwd does not exist: ${baseCwd}`, isError: true };
  }
  const concurrency = clampInt2(input.concurrency, 1, 8, 4);
  const defaultTimeoutSec = clampInt2(input.timeoutSec, 1, MCP_CALL_BUDGET_SEC, 60);
  const outputMode = input.outputMode === "full" ? "full" : "compact";
  const maxChars = clampInt2(input.maxChars, 500, DEFAULT_INLINE_MAX_BYTES, 8e3);
  const maxBytes = DEFAULT_INLINE_MAX_BYTES;
  const normalized = [];
  for (let i = 0; i < rawCommands.length; i++) {
    const raw = rawCommands[i];
    const spec = typeof raw === "string" ? { command: raw } : raw;
    if (!spec || typeof spec.command !== "string" || !spec.command.trim()) {
      return {
        text: `Error: commands[${i}] must contain a non-empty 'command' string.`,
        isError: true
      };
    }
    if (spec.cwd !== void 0 && spec.cwd !== null && typeof spec.cwd !== "string") {
      return {
        text: `Error: commands[${i}].cwd must be a string path.`,
        isError: true
      };
    }
    const trimmedSpecCwd = spec.cwd?.trim() || "";
    const cmdCwd = trimmedSpecCwd ? resolvePath(trimmedSpecCwd, baseCwd) : baseCwd;
    if (trimmedSpecCwd) {
      try {
        const st = await fs7.stat(cmdCwd);
        if (!st.isDirectory()) {
          return {
            text: `Error: commands[${i}].cwd is not a directory: ${cmdCwd}`,
            isError: true
          };
        }
      } catch {
        return {
          text: `Error: commands[${i}].cwd does not exist: ${cmdCwd}`,
          isError: true
        };
      }
    }
    const cmdTimeoutSec = clampInt2(
      spec.timeoutSec,
      1,
      MCP_CALL_BUDGET_SEC,
      defaultTimeoutSec
    );
    normalized.push({
      index: i,
      name: spec.name?.trim() || `check-${i + 1}`,
      command: spec.command.trim(),
      cwd: cmdCwd,
      timeoutMs: cmdTimeoutSec * 1e3
    });
  }
  const safeBudgetMs = Number.isFinite(budgetMs) && budgetMs > 0 ? Math.max(100, Math.trunc(budgetMs)) : MCP_CALL_BUDGET_SEC * 1e3;
  const wallStart = performance.now();
  const deadline = wallStart + safeBudgetMs;
  const outcomes = await mapWithConcurrency(
    normalized,
    concurrency,
    async (spec) => {
      const remainingMs = deadline - performance.now();
      if (remainingMs < 100) return { spec, result: null, passed: false };
      const effTimeoutMs = Math.min(spec.timeoutMs, remainingMs);
      if (outputMode === "compact" && !input.noSqueeze) {
        const sq = await tryRunWithWmSqueeze(
          spec.command,
          spec.cwd,
          effTimeoutMs,
          Math.min(4e3, maxChars)
        );
        if (sq) {
          const passed2 = !sq.result.error && !sq.result.timedOut && sq.result.exitCode === 0;
          return { spec, result: sq.result, passed: passed2, squeezed: true };
        }
      }
      const result = await runShellCommandAsync(spec.command, {
        cwd: spec.cwd,
        timeoutMs: effTimeoutMs
      });
      const passed = !result.error && !result.timedOut && result.exitCode === 0;
      return { spec, result, passed, squeezed: false };
    }
  );
  const wallMs = Math.round(performance.now() - wallStart);
  const passedCount = outcomes.filter((o) => o.passed).length;
  const notRunCount = outcomes.filter((o) => !o.result).length;
  const failedCount = outcomes.length - passedCount - notRunCount;
  const counts = [`${passedCount}/${outcomes.length} passed`];
  if (failedCount > 0) counts.push(`${failedCount} failed`);
  if (notRunCount > 0) {
    counts.push(
      `${notRunCount} not run (call time budget of ${Math.round(safeBudgetMs / 1e3)}s used up; run them in another call)`
    );
  }
  const summaryHeader = `Checks: ${counts.join(", ")} (wall: ${wallMs}ms)`;
  const usableChars = Math.max(200, maxChars - summaryHeader.length - 4);
  const failedBudget = failedCount > 0 ? Math.max(
    160,
    Math.floor((usableChars - passedCount * 120 - notRunCount * 100) / failedCount)
  ) : 320;
  const fullPerCmdBudget = Math.max(
    160,
    Math.floor(usableChars / outcomes.length)
  );
  const blocks = outcomes.map(({ spec, result, passed, squeezed }) => {
    if (!result) return `## [NOT RUN] ${spec.name}
$ ${spec.command}`;
    const badge = result.error ? "ERROR" : result.timedOut ? "TIMEOUT" : passed ? "PASS" : `FAIL (exit ${result.exitCode ?? "err"})`;
    if (outputMode === "full") {
      const combined = [
        normalizeTerminalOutput(result.stdout),
        normalizeTerminalOutput(result.stderr)
      ].filter(Boolean).join("\n");
      const body2 = result.error ? `spawn error: ${result.error}` : truncateWithNotice(combined || "(no output)", fullPerCmdBudget);
      return `## [${badge}] ${spec.name} (${result.durationMs}ms)
$ ${spec.command}
${body2}`;
    }
    if (squeezed) {
      const rawCombined = [result.stdout.trim(), result.stderr.trim()].filter(Boolean).join("\n");
      const sqTag = rawCombined.includes("cache-hit") ? "squeezed+cached" : "squeezed";
      if (passed) {
        const oneLine = rawCombined ? truncateWithNotice(rawCombined.replace(/\s*\n\s*/g, " | "), 280) : "ok";
        return `- [PASS ${result.durationMs}ms, ${sqTag}] ${spec.name} (\`${spec.command}\`): ${oneLine}`;
      }
      const failBody = truncateWithNotice(rawCombined || "(no output)", failedBudget);
      return `## [${badge} ${result.durationMs}ms, ${sqTag}] ${spec.name}
$ ${spec.command}
${failBody}`;
    }
    const body = formatCompactCommandOutput({
      stdout: result.stdout,
      stderr: result.stderr,
      exitCode: result.exitCode,
      timedOut: result.timedOut,
      error: result.error,
      maxChars: passed ? 300 : failedBudget
    });
    if (passed) {
      return `- [PASS ${result.durationMs}ms] ${spec.name} (\`${spec.command}\`): ${body}`;
    }
    return `## [${badge} ${result.durationMs}ms] ${spec.name}
$ ${spec.command}
${body}`;
  });
  const bodyBudget = Math.max(0, maxChars - summaryHeader.length - 2);
  const bodyByteBudget = maxBytes !== void 0 ? Math.max(0, maxBytes - Buffer.byteLength(summaryHeader, "utf8") - 2) : void 0;
  const fittedBlocks = fitSectionsToByteBudget(
    blocks,
    bodyBudget,
    bodyByteBudget,
    "\n\n"
  );
  let text = truncateWithNotice(
    `${summaryHeader}

${fittedBlocks}`,
    maxChars
  );
  if (maxBytes !== void 0) {
    text = truncateWithByteNotice(text, maxBytes);
  }
  return { text, isError: false };
}
function clampInt2(val, min, max, fallback) {
  if (typeof val !== "number" && typeof val !== "string") return fallback;
  if (typeof val === "string" && val.trim() === "") return fallback;
  const num = Number(val);
  if (!Number.isFinite(num)) return fallback;
  return Math.max(min, Math.min(max, Math.trunc(num)));
}

// src/mcp-server.ts
var SERVER_VERSION = "4.0.0";
function recordFastTelemetry(tool, outText, wallMs, isError) {
  if (process.env.WM_FAST_NO_STATS === "1") return;
  try {
    const statsPath = process.env.WM_FAST_STATS || path9.join(os5.homedir(), ".local", "share", "whitemagic", "wm_fast_stats.jsonl");
    fs8.mkdirSync(path9.dirname(statsPath), { recursive: true });
    const row = JSON.stringify({
      ts: Math.floor(Date.now() / 1e3),
      tool,
      out: Buffer.byteLength(outText, "utf8"),
      wall_ms: Math.max(0, Math.round(wallMs)),
      err: isError
    });
    fs8.appendFileSync(statsPath, row + "\n", "utf8");
  } catch {
  }
}
async function wrapFastTool(tool, fn) {
  const t0 = Date.now();
  const res = await fn();
  recordFastTelemetry(tool, res.text ?? "", Date.now() - t0, Boolean(res.isError));
  return res;
}
function buildApplyEditsToolDefinition(compat) {
  const preferStringEdits = compat.editsFormat === "string";
  const editsSchema = preferStringEdits ? {
    type: "string",
    description: 'JSON array string of 1-50 files to edit or create: [{"path", "hunks"?: [{"target", "replacement", "startLine"?, "endLine"?, "replaceAll"?}], "create"?, "overwrite"?}]. A hunk replaces exact target text (optional 1-based line window; replaceAll replaces every occurrence in it); "create" writes the whole file instead of hunks, and "overwrite" allows replacing an existing file. A native array is also accepted, but this host version can deliver a long array of objects in call_mcp_tool Arguments as [] while a string arrives intact.'
  } : {
    type: "array",
    minItems: 1,
    maxItems: 50,
    description: "Files to edit or create (a JSON array string of the same shape is also accepted).",
    items: {
      type: "object",
      properties: {
        path: { type: "string" },
        hunks: {
          type: "array",
          items: {
            type: "object",
            properties: {
              target: { type: "string", description: "Exact text to replace." },
              replacement: { type: "string", description: "Replacement text." },
              startLine: {
                type: "integer",
                minimum: 1,
                description: "Optional 1-based start line to narrow matching."
              },
              endLine: {
                type: "integer",
                minimum: 1,
                description: "Optional 1-based end line to narrow matching."
              },
              replaceAll: {
                type: "boolean",
                description: "Replace every occurrence in range (default false)."
              },
              strictWindow: {
                type: "boolean",
                description: "Disable safe unique auto-relocate / trailing-whitespace fallback for this hunk (default false)."
              }
            },
            required: ["target", "replacement"]
          }
        },
        create: {
          type: "string",
          description: "Create file with this full content instead of hunks."
        },
        overwrite: {
          type: "boolean",
          description: "Allow overwriting existing file when using 'create'."
        }
      },
      required: ["path"]
    }
  };
  return {
    name: "apply_edits",
    description: preferStringEdits ? "Atomically apply multiple non-contiguous hunks across one or more files (all-or-nothing) with optional file creation and inline verify command. Pass 'edits' as one JSON array string." : "Atomically apply multiple non-contiguous hunks across one or more files (all-or-nothing) with optional file creation and inline verify command.",
    inputSchema: {
      type: "object",
      properties: {
        cwd: {
          type: "string",
          description: "Base directory for relative file paths and verify command (defaults to the first workspace root)."
        },
        edits: editsSchema,
        dryRun: {
          type: "boolean",
          description: "Validate all hunks without writing files (default false)."
        },
        bestEffort: {
          type: "boolean",
          description: "When true, write valid files even if other files fail validation (each file remains atomic; failed files return closest-line snippets and verify is skipped until fixed). Default false (all-or-nothing)."
        },
        verify: {
          type: "string",
          description: "Optional shell command run right after writing (e.g. 'npm run typecheck')."
        },
        verifyTimeoutSec: {
          type: "integer",
          minimum: 1,
          maximum: MCP_CALL_BUDGET_SEC,
          description: `Optional timeout in seconds for the verify command (default 60, max ${MCP_CALL_BUDGET_SEC}: agy drops MCP results after 180s).`
        },
        maxVerifyChars: {
          type: "integer",
          minimum: 300,
          maximum: 7500,
          description: "Optional character cap for verify output (default 4000; full response hard-capped <= 8800 UTF-8 bytes)."
        }
      },
      required: ["edits"]
    }
  };
}
function createAgyFastServer(options = {}) {
  const env = options.env ?? process.env;
  const savedEditsFormat = options.configEditsFormat ?? readSavedEditsFormatSync(env);
  let hostCompat = resolveHostCompat({
    env,
    configEditsFormat: savedEditsFormat
  });
  let defaultCwd;
  const resolveToolArgs = (args) => {
    if (args.cwd !== void 0 && args.cwd !== null && typeof args.cwd !== "string") {
      return {
        ok: false,
        error: `Error: 'cwd' must be a string path (received ${Array.isArray(args.cwd) ? "array" : typeof args.cwd}).`
      };
    }
    const rawCwd = typeof args.cwd === "string" ? args.cwd.trim() : "";
    if (!rawCwd) {
      return {
        ok: true,
        value: defaultCwd ? { ...args, cwd: defaultCwd } : { ...args, cwd: void 0 }
      };
    }
    const isHomePath = rawCwd === "~" || rawCwd.startsWith("~/") || rawCwd.startsWith("~\\");
    if (defaultCwd && !path9.isAbsolute(rawCwd) && !isHomePath) {
      return {
        ok: true,
        value: { ...args, cwd: path9.resolve(defaultCwd, rawCwd) }
      };
    }
    return { ok: true, value: { ...args, cwd: rawCwd } };
  };
  const server = new McpStdioServer({
    name: "whitemagic-agy-fast",
    version: SERVER_VERSION,
    onInitialize: ({ clientInfo }) => {
      hostCompat = resolveHostCompat({
        clientInfo,
        env,
        configEditsFormat: savedEditsFormat
      });
    },
    onWorkspaceRoots: (paths) => {
      for (const candidate of paths) {
        try {
          if (fs8.statSync(candidate).isDirectory()) {
            defaultCwd = candidate;
            return;
          }
        } catch {
        }
      }
    }
  });
  server.registerTool(
    {
      name: "batch_search",
      description: "Run multiple read-only text/regex searches, file globs, fuzzy path lookups, and read-only git queries (status/diff/log/show/branch) concurrently in one call with bounded output.",
      inputSchema: {
        type: "object",
        properties: {
          cwd: {
            type: "string",
            description: "Base directory for relative paths (defaults to the first workspace root)."
          },
          queries: {
            type: "array",
            minItems: 1,
            maxItems: 20,
            description: "List of search/glob/git queries to run concurrently.",
            items: {
              type: "object",
              properties: {
                type: {
                  type: "string",
                  enum: ["text", "regex", "glob", "fuzzy", "git"]
                },
                pattern: {
                  type: "string",
                  description: "Literal text, regex, fuzzy path query, glob pattern (use * and {a,b}, not regex |), or read-only git subcommand (e.g. 'status --short', 'log -n 5 --oneline')."
                },
                path: {
                  type: "string",
                  description: "Optional subdirectory or file relative to cwd."
                },
                glob: {
                  type: "string",
                  description: "Optional file filter glob for text/regex queries (e.g. '*.ts')."
                },
                multiline: {
                  type: "boolean",
                  description: "Allow text/regex to span lines. In multiline regex, dot matches newlines."
                },
                outputMode: {
                  type: "string",
                  enum: ["content", "files_with_matches", "count"],
                  description: "Text/regex output (default content). Count reports matching lines per file, or occurrences in multiline mode."
                },
                fileType: {
                  type: "string",
                  description: "Language or extension filter for text, regex, or fuzzy queries (e.g. typescript or ts)."
                },
                sortBy: {
                  type: "string",
                  enum: ["path", "mtime"],
                  description: "Glob order (default path). Use mtime for newest first."
                },
                caseSensitive: {
                  type: "boolean",
                  description: "Case sensitivity for text/regex (default true)."
                },
                contextLines: {
                  type: "integer",
                  minimum: 0,
                  maximum: 5,
                  description: "Context lines around matches (default 0)."
                },
                maxResults: {
                  type: "integer",
                  minimum: 1,
                  maximum: 200,
                  description: "Max matches/files for this query (default 40)."
                }
              },
              required: ["type", "pattern"]
            }
          },
          maxChars: {
            type: "integer",
            minimum: 500,
            maximum: 8800,
            description: "Total character cap (default 8500; hard inline UTF-8 cap <= 8800 bytes to avoid disk spills)."
          }
        },
        required: ["queries"]
      }
    },
    async (args) => {
      const resolved = resolveToolArgs(args);
      if (!resolved.ok) return { text: resolved.error, isError: true };
      return wrapFastTool("batch_search", () => runBatchSearch(resolved.value));
    }
  );
  server.registerTool(
    () => buildApplyEditsToolDefinition(hostCompat),
    async (args) => {
      const resolved = resolveToolArgs(args);
      if (!resolved.ok) return { text: resolved.error, isError: true };
      return wrapFastTool("apply_edits", () => runApplyEdits(resolved.value));
    }
  );
  server.registerTool(
    {
      name: "read_slices",
      description: "Batch-extract symbol/section outlines (TS/JS/Vue/Svelte/CSS/Python/GDScript/Go/Rust/Java/MD/JSON/YAML) across files (prefer 10-20 files/batch, max 40) in one call, plus optional tiny <=120-line slices. For reading >150 lines of known ranges, prefer parallel built-in read_file(StartLine, EndLine) (46KB/call inline, 0 remote check).",
      inputSchema: {
        type: "object",
        properties: {
          cwd: {
            type: "string",
            description: "Base directory for relative paths (defaults to the first workspace root)."
          },
          outlines: {
            type: "array",
            maxItems: 40,
            items: { type: "string" },
            description: "File paths to extract top-level symbol/section line ranges from (prefer 10-20 per batch for fuller outlines; max 40)."
          },
          slices: {
            type: "array",
            maxItems: 30,
            description: "Optional tiny 1-based inclusive [startLine, endLine] ranges to piggyback (total response hard-capped <= 8800 UTF-8 bytes).",
            items: {
              type: "object",
              properties: {
                path: { type: "string" },
                ranges: {
                  type: "array",
                  minItems: 1,
                  items: {
                    type: "array",
                    minItems: 2,
                    maxItems: 2,
                    items: { type: "integer", minimum: 1 }
                  }
                }
              },
              required: ["path", "ranges"]
            }
          },
          maxChars: {
            type: "integer",
            minimum: 1e3,
            maximum: 8800,
            description: "Total character cap (default 8500; hard inline UTF-8 cap <= 8800 bytes to prevent output.txt spills)."
          }
        }
      }
    },
    async (args) => {
      const resolved = resolveToolArgs(args);
      if (!resolved.ok) return { text: resolved.error, isError: true };
      return wrapFastTool("read_slices", () => runReadSlices(resolved.value));
    }
  );
  server.registerTool(
    {
      name: "run_checks",
      description: "Run multiple independent non-interactive shell commands concurrently under one check (build/typecheck/lint/test with outputMode 'compact' using wm-squeeze CoACT compression + git caching, or diagnostic/audit scripts with outputMode 'full').",
      inputSchema: {
        type: "object",
        properties: {
          cwd: {
            type: "string",
            description: "Base directory for relative paths (defaults to the first workspace root)."
          },
          commands: {
            type: "array",
            minItems: 1,
            maxItems: 12,
            description: "Commands to execute concurrently.",
            items: {
              type: "object",
              properties: {
                name: { type: "string", description: "Short label (e.g. 'typecheck')." },
                command: { type: "string", description: "Shell command to run." },
                cwd: { type: "string", description: "Optional working directory override." },
                timeoutSec: { type: "integer", minimum: 1, maximum: MCP_CALL_BUDGET_SEC }
              },
              required: ["command"]
            }
          },
          concurrency: {
            type: "integer",
            minimum: 1,
            maximum: 8,
            description: "Max parallel commands (default 4)."
          },
          timeoutSec: {
            type: "integer",
            minimum: 1,
            maximum: MCP_CALL_BUDGET_SEC,
            description: `Default per-command timeout in seconds (default 60). All commands of one call share a ${MCP_CALL_BUDGET_SEC}s budget because agy drops MCP results after 180s; commands that cannot start within it are reported as not run.`
          },
          outputMode: {
            type: "string",
            enum: ["compact", "full"],
            description: "'compact' hides verbose pass logs and uses wm-squeeze CoACT compression+cache (default); 'full' preserves stdout for diagnostic/inspection commands."
          },
          noSqueeze: {
            type: "boolean",
            description: "Bypass wm-squeeze compression/caching in compact mode (default false)."
          },
          maxChars: {
            type: "integer",
            minimum: 500,
            maximum: 8800,
            description: "Total character cap (default 8000; hard inline UTF-8 cap <= 8800 bytes)."
          }
        },
        required: ["commands"]
      }
    },
    async (args) => {
      const resolved = resolveToolArgs(args);
      if (!resolved.ok) return { text: resolved.error, isError: true };
      return wrapFastTool("run_checks", () => runChecks(resolved.value));
    }
  );
  server.registerTool(
    {
      name: "code_graph",
      description: "Incremental AST/symbol & cross-file call-graph query via wm-codegraph (<6ms, saves 85-99% tokens vs grep+read loops). Modes: overview, defs, callers, callees, impact, skeleton.",
      inputSchema: {
        type: "object",
        properties: {
          mode: {
            type: "string",
            enum: ["overview", "defs", "callers", "callees", "impact", "skeleton"]
          },
          target: {
            type: "string",
            description: "Symbol name or file path fragment (optional for overview)."
          },
          root: {
            type: "string",
            description: "Repository root directory (defaults to cwd / workspace root)."
          },
          cwd: {
            type: "string",
            description: "Base directory for relative root paths."
          },
          depth: {
            type: "integer",
            minimum: 1,
            maximum: 3,
            description: "BFS depth for callers (1-3, default 2)."
          },
          maxChars: {
            type: "integer",
            minimum: 500,
            maximum: 8800,
            description: "Total output cap (default 8500; hard inline UTF-8 cap <= 8800 bytes)."
          }
        },
        required: ["mode"]
      }
    },
    async (args) => {
      const resolved = resolveToolArgs(args);
      if (!resolved.ok) return { text: resolved.error, isError: true };
      return wrapFastTool("code_graph", () => runCodeGraph(resolved.value));
    }
  );
  return server;
}
function main() {
  const server = createAgyFastServer();
  const exitImmediately = () => {
    process.exit(0);
  };
  const flushAndExit = () => {
    process.stdout.end(() => {
      process.exit(0);
    });
  };
  process.stdout.on("error", (err) => {
    if (err.code === "EPIPE" || err.code === "ERR_STREAM_DESTROYED") {
      process.exit(0);
    }
  });
  process.on("SIGINT", exitImmediately);
  process.on("SIGTERM", exitImmediately);
  server.listen(process.stdin, process.stdout, flushAndExit);
}
main();
export {
  createAgyFastServer
};
