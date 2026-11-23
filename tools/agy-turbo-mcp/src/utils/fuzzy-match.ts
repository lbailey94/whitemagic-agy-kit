/**
 * Lightweight case-insensitive subsequence path matcher and ranker.
 */

export interface FuzzyMatchResult { path: string; score: number }

function findContiguousIndex(haystack: string[], needle: string[]): number {
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

function isAscii(text: string): boolean {
  for (let i = 0; i < text.length; i++) {
    if (text.charCodeAt(i) > 0x7f) return false;
  }
  return true;
}

/** Compute subsequence span cost (smaller is better); operates on Unicode code points. */
export function subsequenceCost(needle: string, haystack: string): number | undefined {
  if (!needle) return 2147483647;

  // Zero-allocation ASCII fast path (covers ~99.9% of source file paths)
  if (isAscii(needle) && isAscii(haystack)) {
    const query = needle.toLowerCase();
    const lowered = haystack.toLowerCase();
    const contiguousAt = lowered.indexOf(query);
    if (contiguousAt >= 0) {
      return contiguousAt === 0 ? -100 : -60 + Math.min(25, contiguousAt);
    }
    let cursor = 0;
    let first = -1;
    let last = -1;
    const hLen = lowered.length;
    for (let i = 0; i < query.length; i++) {
      const targetCode = query.charCodeAt(i);
      while (cursor < hLen && lowered.charCodeAt(cursor) !== targetCode) cursor++;
      if (cursor === hLen) return undefined;
      if (first < 0) first = cursor;
      last = cursor++;
    }
    return Math.max(0, last - first + 1 - query.length) - (first === 0 ? 20 : 0);
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
    if (cursor === lowered.length) return undefined;
    if (first < 0) first = cursor;
    last = cursor++;
  }
  return Math.max(0, last - first + 1 - query.length) - (first === 0 ? 20 : 0);
}

// Higher-is-better path ranking score, with -1 as the non-match sentinel.
function scoreSubsequence(query: string, target: string): number {
  const cost = subsequenceCost(query, target);
  return cost === undefined ? -1 : Math.max(1, 1000 - cost);
}

export function fuzzyScorePath(query: string, candidatePosix: string): number {
  const trimmed = query.trim().replace(/\\/g, "/");
  if (!trimmed) return -1;

  // Support space-separated multi-token fuzzy queries (all tokens must match)
  const tokens = trimmed.split(/\s+/).filter(Boolean);
  const slashIdx = candidatePosix.lastIndexOf("/");
  const basename = slashIdx === -1 ? candidatePosix : candidatePosix.slice(slashIdx + 1);

  let totalScore = 0;
  for (const token of tokens) {
    const fullScore = scoreSubsequence(token, candidatePosix);
    if (fullScore < 0) return -1;
    const baseScore = !token.includes("/") ? scoreSubsequence(token, basename) : -1;
    // Matching the filename itself gets a strong boost over matching parent dirs only
    totalScore += baseScore >= 0 ? Math.max(fullScore, baseScore + 45) : fullScore;
    if (basename.toLowerCase() === token.toLowerCase()) totalScore += 60;
  }

  return totalScore;
}

export function rankFuzzyPaths(
  query: string,
  candidates: string[],
  maxResults: number,
): { matches: FuzzyMatchResult[]; totalMatched: number } {
  const scored: FuzzyMatchResult[] = [];
  for (const candidate of candidates) {
    const score = fuzzyScorePath(query, candidate);
    if (score >= 0) scored.push({ path: candidate, score });
  }
  scored.sort((a, b) => b.score - a.score || a.path.length - b.path.length || (a.path < b.path ? -1 : a.path > b.path ? 1 : 0));
  return {
    matches: scored.slice(0, maxResults),
    totalMatched: scored.length,
  };
}
