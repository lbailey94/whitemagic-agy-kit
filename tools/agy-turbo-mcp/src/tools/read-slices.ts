import path from "node:path";
import {
  formatCorruptedArrayError,
  isPlainObject,
  parseMaybeJsonArray,
  type CorruptedItem,
} from "../utils/arg-validation.js";
import { readTextFile, resolvePath, toPosixPath } from "../utils/fs-utils.js";
import {
  DEFAULT_INLINE_MAX_BYTES,
  fitSectionsToByteBudget,
} from "../utils/output-filter.js";

export interface SliceRequest {
  path: string;
  ranges: Array<[number, number]>;
}

export interface ReadSlicesInput {
  cwd?: string;
  outlines?: string[];
  slices?: SliceRequest[];
  maxChars?: number;
}

export interface SymbolEntry {
  kind: string;
  name: string;
  startLine: number;
  endLine: number;
  depth: number;
}

type BraceLang = "js" | "c" | "go" | "rust" | "shell" | "json";

interface ScanState {
  inBlockComment: boolean;
  templateExprStack: number[]; // tracks open `{` depth inside `${...}` of nested template strings
  inBacktick: boolean;
  parenDepth?: number;
}

function isRegexStartContext(line: string, slashIdx: number): boolean {
  let j = slashIdx - 1;
  while (j >= 0 && /\s/.test(line[j]!)) j--;
  if (j < 0) return true;
  const prev = line[j]!;
  if ("=(,:![&|?{};~+-*%^<>".includes(prev)) return true;
  // Check preceding keyword (return, case, throw, in, of, typeof, void, delete, new)
  let k = j;
  while (k >= 0 && /[A-Za-z0-9_$]/.test(line[k]!)) k--;
  const word = line.slice(k + 1, j + 1);
  return (
    word === "return" ||
    word === "case" ||
    word === "throw" ||
    word === "in" ||
    word === "of" ||
    word === "typeof" ||
    word === "void" ||
    word === "delete" ||
    word === "new"
  );
}

function isJsxSelfClosingSlash(line: string, slashIdx: number): boolean {
  if (line[slashIdx + 1] !== ">") return false;
  let j = slashIdx - 1;
  while (j >= 0 && /\s/.test(line[j]!)) j--;
  // JSX self-closing `/>` only reaches regex-start context after `}` (e.g. `<Icon s={1} />`)
  // or on a continuation line (`  />`); regex literals like `/>/g` or `/>}/` follow `(`, `=`, `return`, etc.
  if (j >= 0 && line[j] !== "}") return false;
  return line[slashIdx + 2] !== "/";
}

function scanBracesOnLine(
  line: string,
  state: ScanState,
  lang: BraceLang,
  ignoreBracesInParensBeforeOpen = false,
  alreadySeenOpen = false,
): {
  open: number;
  close: number;
  openParen: number;
  closeParen: number;
  endsWithSemi: boolean;
  lastCodeChar: string;
} {
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
    const ch = line[i]!;
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

    // Comments
    if (
      lang === "shell" &&
      ch === "#" &&
      (i === 0 || /[\s;&|()]/.test(line[i - 1]!))
    ) {
      break;
    }
    if ((lang === "js" || lang === "c" || lang === "go" || lang === "rust") && ch === "/" && next === "/") break;
    if ((lang === "js" || lang === "c" || lang === "go" || lang === "rust") && ch === "/" && next === "*") {
      state.inBlockComment = true;
      i++;
      continue;
    }

    // Regex literal (JS/TS only); never treat JSX closing `</Tag>` or self-closing `/>` as a regex start
    if (
      lang === "js" &&
      ch === "/" &&
      next !== "/" &&
      next !== "*" &&
      !isJsxSelfClosingSlash(line, i) &&
      line[i - 1] !== "<" &&
      isRegexStartContext(line, i)
    ) {
      inRegex = true;
      inRegexClass = false;
      continue;
    }

    if (ch === "'" && lang !== "json") {
      // In Rust, `'a` / `'static` are lifetimes, not character literals; only treat `'c'` or `'\...'` as char literals
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
        state.templateExprStack[state.templateExprStack.length - 1]! += 1;
      } else if (!(ignoreBracesInParensBeforeOpen && !seenOpen && parenDepth > 0)) {
        open++;
        seenOpen = true;
      }
      lastCodeChar = "{";
    } else if (ch === "}") {
      if (state.templateExprStack.length > 0) {
        const top = state.templateExprStack[state.templateExprStack.length - 1]!;
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
    lastCodeChar,
  };
}

function findBraceBlockEnd(
  lines: string[],
  startIdx: number,
  lang: BraceLang = "c",
): number {
  let depth = 0;
  let seenOpen = false;
  const state: ScanState = {
    inBlockComment: false,
    templateExprStack: [],
    inBacktick: false,
    parenDepth: 0,
  };
  const maxScan = Math.min(lines.length, startIdx + 2000);

  for (let i = startIdx; i < maxScan; i++) {
    const { open, close, endsWithSemi, lastCodeChar } = scanBracesOnLine(
      lines[i]!,
      state,
      lang,
      lang === "js" || lang === "go",
      seenOpen,
    );
    if (open > 0) seenOpen = true;
    depth += open - close;
    if (seenOpen && depth <= 0) {
      // Inline generic return type like `): Promise<{ ok: boolean }>\n{` closes with `>` before body `{`
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
    // Multiline signature without opening brace after 40 lines
    if (!seenOpen && i > startIdx + 40) {
      return startIdx + 1;
    }
  }
  return seenOpen ? Math.min(lines.length, maxScan) : startIdx + 1;
}

function findTypeAliasEnd(lines: string[], startIdx: number): number {
  const firstLine = lines[startIdx] ?? "";
  const eqIdx = firstLine.indexOf("=");
  const rhs = eqIdx >= 0 ? firstLine.slice(eqIdx + 1) : firstLine;
  const state: ScanState = {
    inBlockComment: false,
    templateExprStack: [],
    inBacktick: false,
    parenDepth: 0,
  };
  const firstScan = scanBracesOnLine(rhs, state, "js");
  if (firstScan.open > firstScan.close) {
    return findBraceBlockEnd(lines, startIdx, "js");
  }
  if (firstScan.endsWithSemi && firstScan.openParen <= firstScan.closeParen) {
    return startIdx + 1;
  }

  // Handle multiline union/intersection/mapped types without braces (including semi: false style)
  let endIdx = startIdx;
  let parenBal = firstScan.openParen - firstScan.closeParen;
  let prevLast = firstScan.lastCodeChar;
  const maxScan = Math.min(lines.length - 1, startIdx + 40);
  while (endIdx < maxScan) {
    const nextLine = lines[endIdx + 1] ?? "";
    const nextTrim = nextLine.trim();
    if (!nextTrim) break;
    const isContinuation =
      parenBal > 0 ||
      prevLast === "=" ||
      prevLast === "|" ||
      prevLast === "&" ||
      prevLast === "?" ||
      prevLast === ":" ||
      /^[ \t]+(?:[|&?:]|\w+\s*\?*:)/.test(nextLine);
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

function findParenCallEnd(lines: string[], startIdx: number): number {
  const state: ScanState = {
    inBlockComment: false,
    templateExprStack: [],
    inBacktick: false,
    parenDepth: 0,
  };
  let seenParen = false;
  const maxScan = Math.min(lines.length, startIdx + 400);
  for (let i = startIdx; i < maxScan; i++) {
    const { openParen } = scanBracesOnLine(lines[i]!, state, "js");
    if (openParen > 0) seenParen = true;
    if (seenParen && (state.parenDepth ?? 0) <= 0) {
      return i + 1;
    }
  }
  return startIdx + 1;
}

function hasArrowOrBraceWithin(lines: string[], startIdx: number, maxLookahead = 12): { hasArrow: boolean; hasBrace: boolean } {
  const state: ScanState = { inBlockComment: false, templateExprStack: [], inBacktick: false };
  let parenDepth = 0;
  let seenParen = false;
  let hasArrow = false;
  const end = Math.min(lines.length, startIdx + maxLookahead);
  for (let i = startIdx; i < end; i++) {
    const line = lines[i]!;
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

function extractTsJsSymbols(lines: string[]): SymbolEntry[] {
  const symbols: SymbolEntry[] = [];
  const patterns: Array<{ re: RegExp; kind: string; nameGroup: number }> = [
    {
      re: /^(?:export\s+(?:default\s+)?)?(?:declare\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)/,
      kind: "class",
      nameGroup: 1,
    },
    {
      re: /^(?:export\s+)?(?:declare\s+)?interface\s+([A-Za-z_$][\w$]*)/,
      kind: "interface",
      nameGroup: 1,
    },
    {
      re: /^(?:export\s+)?(?:declare\s+)?type\s+([A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*=/,
      kind: "type",
      nameGroup: 1,
    },
    {
      re: /^(?:export\s+)?(?:declare\s+)?(?:const\s+)?enum\s+([A-Za-z_$][\w$]*)/,
      kind: "enum",
      nameGroup: 1,
    },
    {
      re: /^(?:export\s+(?:default\s+)?)?(?:declare\s+)?(?:async\s+)?function\*?\s+([A-Za-z_$][\w$]*)/,
      kind: "function",
      nameGroup: 1,
    },
    {
      re: /^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s+)?(?:function\b|\([^)]*\)\s*(?::[^=]+)?=>|[A-Za-z_$][\w$]*\s*=>)/,
      kind: "function",
      nameGroup: 1,
    },
  ];
  const multilineArrowStartRe =
    /^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s+)?\(/;

  const methodHeaderRe =
    /^([ \t]{1,4})(?:(?:public|private|protected|static|readonly|override|abstract|async|get|set)\s+)*([A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*\(/;
  const controlKeywords = new Set([
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
    "delete",
  ]);

  const routeRegisterRe =
    /^[ \t]{1,6}(?:app|router|server|fastify)\.(get|post|put|patch|delete|all|route)\s*(?:<[^>]*>)?\s*\(\s*['"`]([^'"`]+)['"`]/;
  const testSuiteRe = /^(?:describe|suite)(?:\.(?:only|skip))?\s*\(\s*['"`]([^'"`]+)['"`]/;
  const testCaseRe = /^[ \t]{0,4}(?:it|test)(?:\.(?:only|skip))?\s*\(\s*['"`]([^'"`]+)['"`]/;

  let currentClassEnd = -1;
  let currentClassIndent = -1;
  let currentFnEnd = -1;
  let currentSuiteEnd = -1;
  const commentState: ScanState = {
    inBlockComment: false,
    templateExprStack: [],
    inBacktick: false,
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]!;
    const wasInComment = commentState.inBlockComment;
    const wasInBacktick = commentState.inBacktick;
    scanBracesOnLine(line, commentState, "js");
    if (wasInComment || wasInBacktick) continue;

    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("//") || trimmed.startsWith("/*") || trimmed.startsWith("*")) {
      continue;
    }

    let matchedTop = false;
    if (!line.startsWith(" ") && !line.startsWith("\t")) {
      for (const p of patterns) {
        const m = p.re.exec(line);
        if (m && m[p.nameGroup]) {
          const endLine =
            p.kind === "type"
              ? findTypeAliasEnd(lines, i)
              : findBraceBlockEnd(lines, i, "js");
          symbols.push({
            kind: p.kind,
            name: m[p.nameGroup]!,
            startLine: i + 1,
            endLine,
            depth: 0,
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
              depth: 0,
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
            depth: 0,
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
              depth: 1,
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
          depth: 1,
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
          depth: i + 1 <= currentSuiteEnd ? 1 : 0,
        });
      }
    }
  }

  return symbols;
}

function isPythonFStringPrefix(line: string, quoteIdx: number): boolean {
  let k = quoteIdx - 1;
  while (k >= 0 && /[A-Za-z]/.test(line[k]!)) k--;
  if (k >= 0 && /[0-9_]/.test(line[k]!)) return false;
  const prefix = line.slice(k + 1, quoteIdx).toLowerCase();
  return prefix === "f" || prefix === "fr" || prefix === "rf";
}

function extractPythonSymbols(lines: string[]): SymbolEntry[] {
  const symbols: SymbolEntry[] = [];
  const re = /^([ \t]*)(async\s+def|def|class)\s+([A-Za-z_]\w*)/;
  const indentOf = (l: string) => (/^[ \t]*/.exec(l)?.[0] ?? "").replace(/\t/g, "    ").length;

  // Pre-compute whether each line continues the previous logical line: it starts
  // inside a triple-quoted string, inside open brackets (multi-line signatures,
  // calls, literals), inside a Python 3.12+ f-string expression, or after a
  // backslash continuation.
  const isContinuation = new Array<boolean>(lines.length).fill(false);
  const inStringContinuation = new Array<boolean>(lines.length).fill(false);
  let activeQuote: '"""' | "'''" | null = null;
  let activeSingleQuote: '"' | "'" | null = null;
  let activeSingleIsFString = false;
  let bracketDepth = 0;
  let backslashContinued = false;
  const fExprStack: Array<{ quote: '"' | "'"; braceDepth: number; baseBracketDepth: number }> = [];

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]!;
    inStringContinuation[i] =
      activeQuote !== null || activeSingleQuote !== null || backslashContinued;
    // In Python, `def`/`async def`/`class` are statements and cannot appear
    // inside open brackets or an f-string expression. Recover from mid-edit
    // unclosed brackets so subsequent symbols in the file are preserved.
    if (
      !inStringContinuation[i] &&
      (bracketDepth > 0 || fExprStack.length > 0) &&
      re.test(line)
    ) {
      bracketDepth = 0;
      fExprStack.length = 0;
      const defIndent = indentOf(line);
      for (let k = i - 1; k >= 0; k--) {
        if (inStringContinuation[k]) break;
        const t = lines[k]!.trim();
        if (!t || t.startsWith("#")) {
          isContinuation[k] = false;
          continue;
        }
        if (t.startsWith("@") && indentOf(lines[k]!) === defIndent) {
          isContinuation[k] = false;
          continue;
        }
        break;
      }
    }
    isContinuation[i] =
      inStringContinuation[i]! || bracketDepth > 0 || fExprStack.length > 0;
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
              baseBracketDepth: bracketDepth,
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
          const top = fExprStack[fExprStack.length - 1]!;
          if (
            top.braceDepth === 1 &&
            bracketDepth === top.baseBracketDepth &&
            line[j] === ":" &&
            line[j + 1] !== "="
          ) {
            // Format spec (e.g. f"{x:'>10}" or f"{x:{w}}"): scan to matching `}`.
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
    // Single-line string without trailing backslash does not span lines in Python
    if (activeSingleQuote && !backslashContinued) {
      activeSingleQuote = null;
      activeSingleIsFString = false;
    }
  }

  const raw: Array<{ indent: number; kind: string; name: string; defIdx: number }> = [];
  for (let i = 0; i < lines.length; i++) {
    if (isContinuation[i]) continue;
    const m = re.exec(lines[i]!);
    if (!m) continue;
    const indent = m[1]!.replace(/\t/g, "    ").length;
    const kind = m[2] === "class" ? "class" : indent > 0 ? "method" : "function";
    raw.push({ indent, kind, name: m[3]!, defIdx: i });
  }

  for (let idx = 0; idx < raw.length; idx++) {
    const item = raw[idx]!;
    // Decorators directly above the def belong to the symbol.
    let startIdx = item.defIdx;
    for (let j = item.defIdx - 1; j >= 0; j--) {
      if (isContinuation[j] || !lines[j]!.trim()) continue;
      const trimmed = lines[j]!.trimStart();
      if (trimmed.startsWith("#")) continue;
      if (!trimmed.startsWith("@") || indentOf(lines[j]!) !== item.indent) break;
      startIdx = j;
    }
    let endIdx = lines.length - 1;
    for (let j = item.defIdx + 1; j < lines.length; j++) {
      if (isContinuation[j]) continue;
      const l = lines[j]!;
      const trimmed = l.trim();
      if (!trimmed || trimmed.startsWith("#")) continue;
      if (indentOf(l) <= item.indent) {
        endIdx = j - 1;
        break;
      }
    }
    // Trailing blank lines and comments at or left of the def indent introduce
    // what follows, not this body.
    while (endIdx > item.defIdx) {
      const trimmed = lines[endIdx]!.trim();
      if (
        trimmed &&
        !(
          !isContinuation[endIdx] &&
          trimmed.startsWith("#") &&
          indentOf(lines[endIdx]!) <= item.indent
        )
      ) {
        break;
      }
      endIdx--;
    }
    symbols.push({
      kind: item.kind,
      name: item.name,
      startLine: startIdx + 1,
      endLine: endIdx + 1,
      depth: item.indent > 0 ? 1 : 0,
    });
  }

  return symbols;
}

function stripLeadingGdAnnotations(text: string): string {
  let rest = text;
  while (rest.startsWith("@")) {
    const m = /^@[A-Za-z_]\w*/.exec(rest);
    if (!m) break;
    let idx = m[0].length;
    while (idx < rest.length && (rest[idx] === " " || rest[idx] === "\t")) idx++;
    if (rest[idx] === "(") {
      let depth = 0;
      let quote: string | null = null;
      while (idx < rest.length) {
        const ch = rest[idx]!;
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
    while (idx < rest.length && (rest[idx] === " " || rest[idx] === "\t")) idx++;
    rest = rest.slice(idx);
  }
  return rest;
}

function extractGdScriptSymbols(rawLines: string[]): SymbolEntry[] {
  const lines = rawLines.map((l, idx) => {
    const clean = l.endsWith("\r") ? l.slice(0, -1) : l;
    return idx === 0 ? clean.replace(/^\uFEFF/, "") : clean;
  });

  interface GdLineInfo {
    startsInTriple: boolean;
    openBrace: number;
    closeBrace: number;
    openParen: number;
    closeParen: number;
    hasCode: boolean;
  }

  const info: GdLineInfo[] = new Array<GdLineInfo>(lines.length);
  let activeTriple: '"""' | "'''" | null = null;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]!;
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
        const ch = line[j]!;
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
        if (ch !== " " && ch !== "\t") {
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
      hasCode,
    };
  }

  type GdRawKind = "class_name" | "class" | "func" | "signal" | "enum";
  const raw: Array<{
    indent: number;
    rawKind: GdRawKind;
    name: string;
    startIdx: number;
  }> = [];

  const classNameRe = /^class_name\s+([A-Za-z_]\w*)\b/;
  const classRe = /^class\s+([A-Za-z_]\w*)\b/;
  const funcRe = /^(?:static\s+)?func\s+([A-Za-z_]\w*)\s*\(/;
  const signalRe = /^signal\s+([A-Za-z_]\w*)\b/;
  const enumRe = /^enum(?:\s+([A-Za-z_]\w*))?\s*(?:\{|$|#)/;

  for (let i = 0; i < lines.length; i++) {
    if (info[i]!.startsInTriple) continue;
    const line = lines[i]!;
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
      // Anonymous `enum` without `{` on same line must open `{` on next code line
      if (!m[1] && info[i]!.openBrace === 0) {
        let nextCodeIdx = i + 1;
        while (nextCodeIdx < lines.length && !info[nextCodeIdx]!.hasCode) {
          nextCodeIdx++;
        }
        if (nextCodeIdx >= lines.length || info[nextCodeIdx]!.openBrace === 0) {
          continue;
        }
      }
      raw.push({
        indent,
        rawKind: "enum",
        name: m[1] ?? "<anonymous>",
        startIdx: i,
      });
    }
  }

  const symbols: SymbolEntry[] = [];
  const classStack: Array<{ indent: number; endLine: number }> = [];

  for (const item of raw) {
    let endIdx = item.startIdx;

    if (item.rawKind === "class_name") {
      endIdx = item.startIdx;
    } else if (item.rawKind === "signal") {
      let parenDepth = info[item.startIdx]!.openParen - info[item.startIdx]!.closeParen;
      const maxSigScan = Math.min(lines.length - 1, item.startIdx + 40);
      while (parenDepth > 0 && endIdx < maxSigScan) {
        endIdx++;
        parenDepth += info[endIdx]!.openParen - info[endIdx]!.closeParen;
      }
      if (parenDepth > 0) endIdx = item.startIdx;
    } else if (item.rawKind === "enum") {
      let braceDepth = 0;
      let seenOpen = false;
      const maxEnumScan = Math.min(lines.length - 1, item.startIdx + 500);
      for (let k = item.startIdx; k <= maxEnumScan; k++) {
        const li = info[k]!;
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
      // `class` or `func` / `static func`: advance past multiline signature parens first
      let headerEndIdx = item.startIdx;
      let parenDepth = info[item.startIdx]!.openParen - info[item.startIdx]!.closeParen;
      const maxHeaderScan = Math.min(lines.length - 1, item.startIdx + 40);
      while (parenDepth > 0 && headerEndIdx < maxHeaderScan) {
        headerEndIdx++;
        parenDepth += info[headerEndIdx]!.openParen - info[headerEndIdx]!.closeParen;
      }
      if (parenDepth > 0) headerEndIdx = item.startIdx;

      endIdx = lines.length - 1;
      for (let j = headerEndIdx + 1; j < lines.length; j++) {
        if (info[j]!.startsInTriple) continue;
        const l = lines[j]!;
        const trimmed = l.trim();
        if (!trimmed || trimmed.startsWith("#")) continue;
        const curIndent = (/^[ \t]*/.exec(l)?.[0] ?? "").replace(/\t/g, "    ").length;
        if (curIndent <= item.indent) {
          endIdx = j - 1;
          break;
        }
      }

      while (endIdx > headerEndIdx) {
        if (info[endIdx]!.startsInTriple) break;
        const endLineStr = lines[endIdx]!;
        const endTrimmed = endLineStr.trim();
        if (!endTrimmed) {
          endIdx--;
          continue;
        }
        if (endTrimmed.startsWith("#")) {
          const commentIndent = (/^[ \t]*/.exec(endLineStr)?.[0] ?? "").replace(
            /\t/g,
            "    ",
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

    while (
      classStack.length > 0 &&
      (classStack[classStack.length - 1]!.endLine < startLine ||
        classStack[classStack.length - 1]!.indent >= item.indent)
    ) {
      classStack.pop();
    }

    const depth =
      classStack.length > 0
        ? classStack.length
        : item.indent > 0
          ? Math.max(1, Math.floor(item.indent / 4))
          : 0;

    const kind =
      item.rawKind === "func"
        ? item.indent > 0
          ? "method"
          : "function"
        : item.rawKind;

    symbols.push({
      kind,
      name: item.name,
      startLine,
      endLine,
      depth,
    });

    if (item.rawKind === "class") {
      classStack.push({ indent: item.indent, endLine });
    }
  }

  return symbols;
}

function extractGoSymbols(lines: string[]): SymbolEntry[] {
  const symbols: SymbolEntry[] = [];
  const funcRe = /^func\s+(?:\(([^)]+)\)\s+)?([A-Za-z_]\w*)/;
  const typeRe = /^type\s+([A-Za-z_]\w*(?:\[[^\]]*\])?)\s+(struct|interface)\b/;
  const commentState: ScanState = {
    inBlockComment: false,
    templateExprStack: [],
    inBacktick: false,
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]!;
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
        depth: 0,
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
        depth: 0,
      });
    }
  }
  return symbols;
}

function extractRustSymbols(lines: string[]): SymbolEntry[] {
  const symbols: SymbolEntry[] = [];
  const re =
    /^([ \t]*)(?:pub(?:\([^)]*\))?\s+)?(?:(?:async|unsafe|const|extern(?:\s+"[^"]*")?)\s+)*(fn|struct|enum|trait|impl|mod)(?:\s+|\s*(?=<))([A-Za-z_<][\w:<>,+'&\s]*?)(?:\s*[\({;]|$)/;
  const commentState: ScanState = {
    inBlockComment: false,
    templateExprStack: [],
    inBacktick: false,
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]!;
    const wasInComment = commentState.inBlockComment;
    scanBracesOnLine(line, commentState, "rust");
    if (wasInComment) continue;

    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("//") || trimmed.startsWith("/*")) {
      continue;
    }
    const m = re.exec(line);
    if (!m || !m[2] || !m[3]) continue;
    const indent = m[1]!.length;
    if (indent > 4) continue;
    symbols.push({
      kind: m[2],
      name: m[3].trim(),
      startLine: i + 1,
      endLine: findBraceBlockEnd(lines, i, "rust"),
      depth: indent > 0 ? 1 : 0,
    });
  }
  return symbols;
}

function extractCLikeSymbols(lines: string[]): SymbolEntry[] {
  const symbols: SymbolEntry[] = [];
  const typeRe =
    /^([ \t]{0,4})(?:(?:public|private|protected|internal|static|final|abstract|sealed|data|open|export|partial)\s+)*(class|interface|enum|struct|record|protocol|namespace)\s+([A-Za-z_]\w*)/;
  const kwFuncRe =
    /^([ \t]{0,4})(?:(?:public|private|protected|internal|static|final|override|virtual|inline|async|suspend|open|mutating)\s+)*(?:fun|func)\s+([A-Za-z_]\w*)/;
  // C / C++ / Java / C# functions and class methods (return-type + name + '(')
  const cMethodRe =
    /^([ \t]{0,4})(?:(?:public|private|protected|internal|static|final|abstract|override|virtual|inline|constexpr|extern|async|synchronized|native|unsigned|signed|const)\s+)*(?:[A-Za-z_~][\w:<>,\[\]\s*&]*?\s+)([A-Za-z_~][\w:]*)\s*\([^;]*$/;
  const excludedNames = new Set([
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
    "case",
  ]);

  const commentState: ScanState = {
    inBlockComment: false,
    templateExprStack: [],
    inBacktick: false,
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]!;
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
        depth: indented ? 1 : 0,
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
        depth: indented ? 1 : 0,
      });
      continue;
    }

    // Match C/C++/Java/C# function/method definitions that open a brace block
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
          depth: indented ? 1 : 0,
        });
      }
    }
  }
  return symbols;
}

function extractShellSymbols(lines: string[]): SymbolEntry[] {
  const symbols: SymbolEntry[] = [];
  const re = /^(?:function\s+([A-Za-z_][\w-]*)\s*(?:\(\))?|([A-Za-z_][\w-]*)\s*\(\))\s*\{/;
  for (let i = 0; i < lines.length; i++) {
    const m = re.exec(lines[i]!);
    if (!m) continue;
    const name = m[1] || m[2];
    if (!name) continue;
    symbols.push({
      kind: "function",
      name,
      startLine: i + 1,
      endLine: findBraceBlockEnd(lines, i, "shell"),
      depth: 0,
    });
  }
  return symbols;
}

function extractMarkdownSymbols(lines: string[]): SymbolEntry[] {
  const raw: Array<{ level: number; name: string; startLine: number }> = [];
  let inFence = false;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]!;
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
      if (raw[j]!.level <= item.level) {
        endLine = raw[j]!.startLine - 1;
        break;
      }
    }
    return {
      kind: `h${item.level}`,
      name: item.name,
      startLine: item.startLine,
      endLine: Math.max(item.startLine, endLine),
      depth: item.level - 1,
    };
  });
}

function extractJsonYamlSymbols(lines: string[], isJson: boolean): SymbolEntry[] {
  const symbols: SymbolEntry[] = [];
  if (isJson) {
    // Detect top-level key indentation dynamically (supports 2-space, 4-space, or tabs)
    let keyIndent: string | null = null;
    for (let i = 0; i < lines.length; i++) {
      const m = /^([ \t]+)"([^"]+)"\s*:/.exec(lines[i]!);
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
            depth: 0,
          });
        }
      }
    }
    for (let idx = 0; idx < symbols.length; idx++) {
      const cur = symbols[idx]!;
      const nextStart =
        idx + 1 < symbols.length ? symbols[idx + 1]!.startLine - 1 : lines.length - 1;
      let endLine = Math.max(cur.startLine, nextStart);
      while (endLine > cur.startLine && /^\s*[}\]]?,?\s*$/.test(lines[endLine - 1] ?? "") && idx === symbols.length - 1) {
        endLine--;
      }
      cur.endLine = endLine;
    }
    return symbols;
  }

  // YAML top-level keys
  const re = /^([A-Za-z0-9_.-]+)\s*:/;
  for (let i = 0; i < lines.length; i++) {
    const m = re.exec(lines[i]!);
    if (m && m[1]) {
      symbols.push({
        kind: "key",
        name: m[1],
        startLine: i + 1,
        endLine: i + 1,
        depth: 0,
      });
    }
  }
  for (let idx = 0; idx < symbols.length; idx++) {
    const cur = symbols[idx]!;
    let endLine =
      idx + 1 < symbols.length ? symbols[idx + 1]!.startLine - 1 : lines.length;
    while (endLine > cur.startLine && !(lines[endLine - 1] ?? "").trim()) {
      endLine--;
    }
    cur.endLine = Math.max(cur.startLine, endLine);
  }
  return symbols;
}

function extractCssSymbols(lines: string[], compactOnly = false): SymbolEntry[] {
  const symbols: SymbolEntry[] = [];
  const state: ScanState = {
    inBlockComment: false,
    templateExprStack: [],
    inBacktick: false,
  };
  let braceDepth = 0;

  const atRuleRe = /^@(layer|media|keyframes|supports|container|font-face|theme)\b([^{;]*)/i;
  const tokenBlockRe =
    /^(:root\b|html(?:\[[^\]]+\]|\.[A-Za-z_-][\w-]*)?(?=\s|[,{])|\[data-theme[^\]]*\]|body\b)[^{;]*\{/;
  const sectionCommentRe = /^\/\*\s*[=\-*#]*\s*([^*/=][^*/]{1,70}?)\s*[=\-*#]*\s*\*\/$/;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]!;
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
          depth: 0,
        });
      } else {
        const am = atRuleRe.exec(trimmed);
        if (am && am[1]) {
          const endLine = trimmed.includes("{")
            ? findBraceBlockEnd(lines, i, "c")
            : i + 1;
          const rest = (am[2] ?? "").trim().replace(/\s+/g, " ").slice(0, 56);
          symbols.push({
            kind: `@${am[1].toLowerCase()}`,
            name: rest || `@${am[1].toLowerCase()}`,
            startLine: i + 1,
            endLine,
            depth: 0,
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
              depth: 0,
            });
          }
        }
      }
    }

    const { open, close } = scanBracesOnLine(line, state, "c");
    braceDepth = Math.max(0, braceDepth + open - close);
  }

  // If few structural blocks were found in a standalone CSS/SCSS file, sample top-level rule groups
  if (!compactOnly && symbols.length < 10) {
    const fallbackState: ScanState = {
      inBlockComment: false,
      templateExprStack: [],
      inBacktick: false,
    };
    let d = 0;
    let lastAddedLine = -999;
    const seen = new Set(symbols.map((s) => s.startLine));
    for (let i = 0; i < lines.length && symbols.length < 40; i++) {
      const line = lines[i]!;
      const trimmed = line.trim();
      const wasInComment = fallbackState.inBlockComment;
      if (
        !wasInComment &&
        d === 0 &&
        !seen.has(i + 1) &&
        i - lastAddedLine >= 10 &&
        /^[.#\[a-zA-Z*:]/.test(trimmed) &&
        trimmed.includes("{") &&
        !trimmed.startsWith("@")
      ) {
        const sel = trimmed.slice(0, trimmed.indexOf("{")).trim().replace(/\s+/g, " ").slice(0, 60);
        if (sel) {
          const endLine = findBraceBlockEnd(lines, i, "c");
          symbols.push({
            kind: "rule",
            name: sel,
            startLine: i + 1,
            endLine,
            depth: 0,
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

function extractSfcSymbols(lines: string[]): SymbolEntry[] {
  const symbols: SymbolEntry[] = [];
  const blockOpenRe = /^<(script|template|style)\b([^>]*)>/i;

  let i = 0;
  while (i < lines.length) {
    const line = lines[i]!;
    const m = blockOpenRe.exec(line.trim());
    if (!m || !m[1] || line.startsWith("  ") || line.startsWith("\t")) {
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
        const ln = lines[j]!;
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
        if (j > i && closeRe.test(lines[j]!)) break;
        if (j === i && closeRe.test(lines[j]!.slice(m[0].length))) break;
      }
    }

    const headerLabel = attrs ? `<${tag} ${attrs.slice(0, 40)}>` : `<${tag}>`;
    symbols.push({
      kind: tag,
      name: headerLabel,
      startLine: startIdx + 1,
      endLine: endIdx + 1,
      depth: 0,
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
          depth: s.depth + 1,
        });
      }
    } else if (tag === "template" && innerLines.length >= 30) {
      let landmarks = 0;
      const commentLandmarkRe = /^<!--\s*[=\-*#]*\s*([^-][^>]{2,60}?)\s*[=\-*#]*\s*-->$/;
      const semanticTagRe =
        /^<(header|nav|main|aside|section|footer|dialog|form|[A-Z][A-Za-z0-9]*(?:Modal|Drawer|Dialog|Panel))\b([^>]*)>/;
      for (let k = 0; k < innerLines.length && landmarks < 14; k++) {
        const raw = innerLines[k]!;
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
            depth: 1,
          });
          landmarks++;
          continue;
        }
        const sm = semanticTagRe.exec(trimmed);
        if (sm && sm[1]) {
          const attrStr = sm[2] ?? "";
          const idMatch = /(?:data-testid|id|class)=["']([^"']+)["']/.exec(attrStr);
          const tagDesc =
            idMatch && idMatch[1]
              ? `<${sm[1]} ${idMatch[1].slice(0, 36)}>`
              : `<${sm[1]}>`;
          symbols.push({
            kind: "block",
            name: tagDesc,
            startLine: innerStart + k + 1,
            endLine: innerStart + k + 1,
            depth: 1,
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
          depth: 1,
        });
      }
    }

    i = endIdx + 1;
  }

  return symbols.length > 0 ? symbols : extractTsJsSymbols(lines);
}

function extractSymbolsFromLines(filePath: string, lines: string[]): SymbolEntry[] {
  const ext = path.extname(filePath).toLowerCase();

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

export function extractSymbols(filePath: string, content: string): SymbolEntry[] {
  const lines = content.replace(/\r\n/g, "\n").split("\n");
  return extractSymbolsFromLines(filePath, lines);
}

const MAX_OUTLINES = 40;
const MAX_SLICE_FILES = 30;
const SLICE_TRUNCATION_TIP =
  "\n[tip: MCP responses are hard-capped <=8.8KB inline; for >150 lines across files, use parallel read_file(StartLine, EndLine) (46KB/call, 0 remote check)]";
const OUTLINE_BATCH_TIP =
  "\n[tip: outlining >20 files splits the 8.8KB cap thinly; batch 10-20 files per call for fuller outlines]";

function selectOutlineSymbols(symbols: SymbolEntry[], limit: number): SymbolEntry[] {
  if (symbols.length <= limit) return symbols;
  // Prioritize depth-0 declarations so one giant class's methods never crowd out top-level structure
  const keptIndices = new Set<number>();
  for (let i = 0; i < symbols.length && keptIndices.size < limit; i++) {
    if (symbols[i]!.depth === 0) keptIndices.add(i);
  }
  for (let i = 0; i < symbols.length && keptIndices.size < limit; i++) {
    keptIndices.add(i);
  }
  return [...keptIndices]
    .sort((a, b) => a - b)
    .map((idx) => symbols[idx]!);
}

export async function runReadSlices(
  input: ReadSlicesInput,
): Promise<{ text: string; isError: boolean }> {
  const corruptedItems: CorruptedItem[] = [];
  let outlines: string[] = [];
  if (input?.outlines !== undefined) {
    const parsedOutlines = parseMaybeJsonArray(input.outlines);
    if (!parsedOutlines.ok) {
      corruptedItems.push({
        label: "outlines",
        value: input.outlines,
        expected: "an array of file path strings",
      });
    } else {
      for (let i = 0; i < parsedOutlines.value.length; i++) {
        const item = parsedOutlines.value[i];
        if (typeof item !== "string" || !item.trim()) {
          corruptedItems.push({
            label: `outlines[${i}]`,
            value: item,
            expected: "a non-empty file path string",
          });
        }
      }
      if (corruptedItems.length === 0) {
        outlines = parsedOutlines.value as string[];
      }
    }
  }

  let slices: SliceRequest[] = [];
  if (input?.slices !== undefined) {
    const parsedSlices = parseMaybeJsonArray(input.slices);
    if (!parsedSlices.ok) {
      corruptedItems.push({
        label: "slices",
        value: input.slices,
        expected: "an array of slice objects ({ path, ranges })",
      });
    } else {
      for (let i = 0; i < parsedSlices.value.length; i++) {
        const item = parsedSlices.value[i];
        if (!isPlainObject(item)) {
          corruptedItems.push({
            label: `slices[${i}]`,
            value: item,
            expected: "a slice object ({ path, ranges })",
          });
          continue;
        }
        if (item.ranges !== undefined && !Array.isArray(item.ranges)) {
          corruptedItems.push({
            label: `slices[${i}].ranges`,
            value: item.ranges,
            expected: "an array of [startLine, endLine] pairs",
          });
        } else if (Array.isArray(item.ranges)) {
          for (let j = 0; j < item.ranges.length; j++) {
            const r = item.ranges[j];
            if (typeof r === "boolean" || r === null) {
              corruptedItems.push({
                label: `slices[${i}].ranges[${j}]`,
                value: r,
                expected: "a [startLine, endLine] pair",
              });
            }
          }
        }
      }
      if (corruptedItems.length === 0) {
        slices = parsedSlices.value as unknown as SliceRequest[];
      }
    }
  }

  if (corruptedItems.length > 0) {
    return {
      text: formatCorruptedArrayError({
        header: "Validation failed",
        toolName: "read_slices",
        items: corruptedItems,
        fallbackAdvice:
          "immediately fall back to parallel built-in read_file(StartLine, EndLine) calls.",
      }),
      isError: true,
    };
  }

  if (outlines.length === 0 && slices.length === 0) {
    return {
      text: "Error: provide at least one entry in 'outlines' or 'slices'.",
      isError: true,
    };
  }
  if (outlines.length > MAX_OUTLINES || slices.length > MAX_SLICE_FILES) {
    return {
      text: `Error: limits exceeded (max ${MAX_OUTLINES} outlines and ${MAX_SLICE_FILES} slice files per call).`,
      isError: true,
    };
  }

  const baseCwd = resolvePath(input.cwd || process.cwd());
  const rawMaxChars = Number(input.maxChars ?? 8_500);
  const maxChars = Number.isFinite(rawMaxChars)
    ? Math.max(1000, Math.min(DEFAULT_INLINE_MAX_BYTES, Math.trunc(rawMaxChars)))
    : 8_500;
  const maxBytes = DEFAULT_INLINE_MAX_BYTES;

  const perFileSymbolLimit =
    outlines.length <= 1
      ? 120
      : outlines.length <= 4
        ? 60
        : outlines.length <= 10
          ? 30
          : outlines.length <= 20
            ? 16
            : 10;

  const fileCache = new Map<string, { lines: string[]; size: number }>();
  async function loadLines(absPath: string): Promise<{ lines: string[]; size: number }> {
    const cached = fileCache.get(absPath);
    if (cached) return cached;
    const data = await readTextFile(absPath);
    const lines = data.content.replace(/\r\n/g, "\n").split("\n");
    const entry = { lines, size: data.size };
    fileCache.set(absPath, entry);
    return entry;
  }

  const sections: string[] = [];

  // Process outlines concurrently
  if (outlines.length > 0) {
    const outlineResults = await Promise.all(
      outlines.map(async (rawPath) => {
        const absPath = resolvePath(rawPath, baseCwd);
        const relPath =
          toPosixPath(path.relative(baseCwd, absPath)) || path.basename(absPath);
        try {
          const { lines } = await loadLines(absPath);
          const symbols = extractSymbolsFromLines(absPath, lines);
          if (symbols.length === 0) {
            return `## Outline: ${relPath} (${lines.length} lines)\n(no top-level symbols detected)`;
          }
          const picked = selectOutlineSymbols(symbols, perFileSymbolLimit);
          const formatted = picked.map((s) => {
            const indent = "  ".repeat(s.depth);
            const span =
              s.endLine > s.startLine
                ? `L${s.startLine}-L${s.endLine}`
                : `L${s.startLine}`;
            return `${indent}${span} [${s.kind}] ${s.name}`;
          });
          const extra =
            symbols.length > picked.length
              ? outlines.length > 10
                ? `\n... (+${symbols.length - picked.length} more symbols)`
                : `\n... (${symbols.length - picked.length} more symbols omitted; outline this file alone for full list)`
              : "";
          return `## Outline: ${relPath} (${lines.length} lines)\n${formatted.join("\n")}${extra}`;
        } catch (err) {
          return `## Outline: ${relPath}\nERROR: ${err instanceof Error ? err.message : String(err)}`;
        }
      }),
    );
    sections.push(...outlineResults);
  }

  // Process slices concurrently
  if (slices.length > 0) {
    const sliceResults = await Promise.all(
      slices.map(async (req) => {
        if (!req || typeof req.path !== "string" || !req.path.trim()) {
          return "## Slice: (invalid)\nERROR: 'path' must be a non-empty string";
        }
        const absPath = resolvePath(req.path, baseCwd);
        const relPath =
          toPosixPath(path.relative(baseCwd, absPath)) || path.basename(absPath);

        if (!Array.isArray(req.ranges) || req.ranges.length === 0) {
          return `## Slice: ${relPath}\nERROR: 'ranges' must be a non-empty array of [startLine, endLine] pairs`;
        }

        try {
          const { lines } = await loadLines(absPath);
          const totalLines = lines.length;
          const blocks: string[] = [];

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
                `--- L${rawStart}-L${rawEnd} (out of bounds; file has ${totalLines} lines) ---`,
              );
              continue;
            }
            if (end < start) {
              blocks.push(
                `ERROR: startLine (${rawStart}) > endLine (${rawEnd})`,
              );
              continue;
            }

            const width = String(end).length;
            const snippet: string[] = [`--- L${start}-L${end} (of ${totalLines}) ---`];
            for (let ln = start; ln <= end; ln++) {
              const num = String(ln).padStart(width, " ");
              snippet.push(`${num}: ${lines[ln - 1] ?? ""}`);
            }
            blocks.push(snippet.join("\n"));
          }

          return `## Slice: ${relPath} (${totalLines} lines)\n${blocks.join("\n")}`;
        } catch (err) {
          return `## Slice: ${relPath}\nERROR: ${err instanceof Error ? err.message : String(err)}`;
        }
      }),
    );
    sections.push(...sliceResults);
  }

  const rawJoined = sections.join("\n\n");
  const rawBytes = Buffer.byteLength(rawJoined, "utf8");
  const isOverBudget = rawJoined.length > maxChars || rawBytes > maxBytes;
  const activeTip =
    isOverBudget && slices.length > 0
      ? SLICE_TRUNCATION_TIP
      : isOverBudget && outlines.length > 20
        ? OUTLINE_BATCH_TIP
        : null;

  if (activeTip && maxChars > activeTip.length + 200) {
    const tipBytes = Buffer.byteLength(activeTip, "utf8");
    const body = fitSectionsToByteBudget(
      sections,
      maxChars - activeTip.length,
      maxBytes - tipBytes,
      "\n\n",
    );
    return {
      text: `${body}${activeTip}`,
      isError: false,
    };
  }

  return {
    text: fitSectionsToByteBudget(sections, maxChars, maxBytes, "\n\n"),
    isError: false,
  };
}
