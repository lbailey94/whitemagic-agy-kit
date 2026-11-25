#!/usr/bin/env python3
"""
Grok 4.7 Fleet Calibration Benchmark (`wm-grok-fleet`).

Runs 3 empirical experiments (~22 workers) and writes JSON + Markdown reports:
  1. Effort & Speed Multiplier Matrix: 4 models x 2 verifiable tasks (8 workers)
  2. Micro-Task Batch Granularity Sweep on Crucible ZPD synthesis: 2 models x (1, 4, 8) (6 workers)
  3. Host Concurrency Stress & Scaling Probe: 8 concurrent read-only workers (`read-concurrency 8`)

Respects VALKYRIE zero-pyc invariant (`sys.dont_write_bytecode = True`) and uses ephemeral
`/tmp/grok_fleet_bench_repo` for isolated `worktree` + `bwrap` testing.
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
FLEET_BIN = REPO_ROOT / "bin" / "wm-grok-fleet"
VALKYRIE_FORGE_DIR = Path(
    os.environ.get("VALKYRIE_FORGE_DIR")
    or (Path.home() / "Desktop" / "front burner" / "repos" / "VALKYRIE" / "code" / "valkyrie-ops" / "gen4_forge")
)

DEFAULT_ARTIFACT_DIR = Path("/media/lucas/SD_CARD1/grok_fleet/calibration_2026_11_24")
DEFAULT_DB_PATH = Path.home() / ".local" / "share" / "whitemagic" / "grok_fleet" / "calibration_2026_11_24.db"
EPHEMERAL_WORKTREE_REPO = Path("/tmp/grok_fleet_bench_repo")


# ---------------------------------------------------------------------------
# Experiment 1A Payload: Self-Contained Security Micro-Audit (2 Vulns, 2 Decoys)
# ---------------------------------------------------------------------------
EXP1A_TARGET_CODE = '''\
import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path


def _b64url_decode(segment: str) -> bytes:
    pad = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + pad)


def verify_session_token(token: str, secret: bytes, now: int | None = None) -> dict | None:
    """Verify a signed session token header.payload.sig and return claims if valid."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    hdr_b64, pay_b64, sig_b64 = parts
    try:
        header = json.loads(_b64url_decode(hdr_b64).decode("utf-8"))
        claims = json.loads(_b64url_decode(pay_b64).decode("utf-8"))
    except Exception:
        return None

    alg = str(header.get("alg", "")).strip().lower()
    if alg == "none":
        # Legacy unsigned dev mode check
        exp = int(claims.get("exp", 0))
        if exp and exp < int(now or time.time()):
            return None
        return claims

    if alg != "hs256":
        return None

    signing_input = f"{hdr_b64}.{pay_b64}".encode("ascii")
    expected = hmac.new(secret, signing_input, hashlib.sha256).digest()
    try:
        provided = _b64url_decode(sig_b64)
    except Exception:
        return None
    if not hmac.compare_digest(expected, provided):
        return None

    exp = int(claims.get("exp", 0))
    if exp and exp < int(now or time.time()):
        return None
    return claims


def verify_webhook_hmac(payload: bytes, sig_header: str, secret: bytes) -> bool:
    """Verify X-Hub-Signature-256 webhook header using constant-time comparison."""
    if not sig_header or not sig_header.startswith("sha256="):
        return False
    provided_hex = sig_header[len("sha256="):].strip().lower()
    if len(provided_hex) != 64:
        return False
    expected_hex = hmac.new(secret, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected_hex, provided_hex)


def load_tenant_artifact(base_dir: str, tenant_id: str, rel_path: str) -> bytes:
    """Load a tenant-scoped artifact from disk."""
    if not tenant_id or "/" in tenant_id or "\\" in tenant_id or ".." in tenant_id:
        raise ValueError("invalid tenant_id")
    tenant_root = os.path.abspath(os.path.join(base_dir, tenant_id))
    candidate = os.path.abspath(os.path.join(tenant_root, rel_path))
    if not candidate.startswith(tenant_root):
        raise PermissionError("path escapes tenant directory")
    with open(candidate, "rb") as fp:
        return fp.read()


def read_audit_entry(audit_root: str, entry_name: str) -> str:
    """Safely read a single regular audit log file inside audit_root."""
    if not entry_name or "/" in entry_name or "\\" in entry_name or entry_name in (".", ".."):
        raise ValueError("invalid entry_name")
    root_res = Path(audit_root).resolve(strict=True)
    target = root_res / entry_name
    if target.is_symlink():
        raise PermissionError("symlinks forbidden")
    target_res = target.resolve(strict=True)
    if not target_res.is_relative_to(root_res):
        raise PermissionError("path escapes audit_root")
    if not target_res.is_file():
        raise FileNotFoundError(entry_name)
    return target_res.read_text(encoding="utf-8")
'''


def _numbered_code(src: str) -> str:
    return "\n".join(f"{i:3d}: {line}" for i, line in enumerate(src.splitlines(), 1))


def build_exp1a_prompt() -> str:
    return (
        "You are performing a focused security micro-audit on the single Python module below.\n"
        "All code is provided inline. Do NOT run shell or file tools.\n"
        "Identify ONLY genuine, exploitable vulnerabilities (ignore safe functions).\n"
        "Output ONLY valid JSON matching this exact schema (no markdown fences or prose):\n"
        '{"findings": [{"function": "<name>", "vuln_class": "<class>", "line_hint": <int>, '
        '"exploit_input": "<concise exploit>", "fix": "<concise fix>"}]}\n\n'
        "```python\n"
        f"{_numbered_code(EXP1A_TARGET_CODE)}\n"
        "```\n"
    )


# ---------------------------------------------------------------------------
# Experiment 1B Setup: Ephemeral Git Repo on ext4 for Worktree + bwrap Verify
# ---------------------------------------------------------------------------
EXP1B_BUGGY_INTERVAL = '''\
"""Closed-interval [start, end] merge and membership utilities."""
from __future__ import annotations


def merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping or touching closed intervals [start, end]."""
    if not intervals:
        return []
    merged = [intervals[0]]
    for start, end in intervals[1:]:
        prev_start, prev_end = merged[-1]
        if start < prev_end:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def query_point(intervals: list[tuple[int, int]], x: int) -> bool:
    """Return True if x lies inside any closed interval [start, end]."""
    for start, end in merge_intervals(intervals):
        if start <= x < end:
            return True
    return False
'''

EXP1B_INITIAL_TESTS = '''\
import unittest
from interval_index import merge_intervals, query_point


class TestIntervalIndex(unittest.TestCase):
    def test_unsorted_merge(self):
        self.assertEqual(merge_intervals([(5, 8), (1, 6)]), [(1, 8)])

    def test_touching_intervals_merge(self):
        self.assertEqual(merge_intervals([(1, 3), (3, 5)]), [(1, 5)])

    def test_query_right_endpoint(self):
        self.assertTrue(query_point([(2, 7)], 7))


if __name__ == "__main__":
    unittest.main()
'''


def setup_ephemeral_worktree_repo(repo_dir: Path) -> None:
    if repo_dir.exists():
        shutil.rmtree(repo_dir)
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(repo_dir), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "bench@local.test"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Bench"], check=True, capture_output=True)
    (repo_dir / "interval_index.py").write_text(EXP1B_BUGGY_INTERVAL, encoding="utf-8")
    (repo_dir / "test_interval_index.py").write_text(EXP1B_INITIAL_TESTS, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_dir), "add", "interval_index.py", "test_interval_index.py"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "initial buggy interval index"], check=True, capture_output=True)


def build_exp1b_prompt() -> str:
    return (
        "In this repository (`interval_index.py` and `test_interval_index.py`):\n"
        "1. Fix all bugs in `interval_index.py` for closed intervals `[start, end]` "
        "(handle unsorted inputs, merge touching endpoints where `start == prev_end`, "
        "and treat closed right endpoints `x == end` as inside the interval).\n"
        "2. Add at least 3 new `def test_...` methods to `test_interval_index.py` covering:\n"
        "   - empty input `[]`\n"
        "   - touching / duplicate intervals\n"
        "   - negative coordinates\n"
        "3. Run `python3 -m unittest -v test_interval_index.py` to confirm all tests pass, then stop.\n"
    )


# ---------------------------------------------------------------------------
# Experiment 2 Payload: Crucible ZPD Synthesis (Batch Sizes 1, 4, 8)
# ---------------------------------------------------------------------------
CRUCIBLE_THEMES = [
    "sliding window median",
    "monotonic stack span",
    "prefix XOR parity",
    "interval intersection count",
    "run-length encoding",
    "LRU cache eviction",
    "topological cycle check",
    "trie prefix count",
]


def build_exp2_prompt(batch_n: int) -> str:
    themes = CRUCIBLE_THEMES[:batch_n]
    theme_lines = "\n".join(f"  {i + 1}. {t}" for i, t in enumerate(themes))
    return (
        f"You are Gardener-CoAuthor. Author exactly {batch_n} self-verifying Python algorithmic puzzles, "
        "one for each distinct theme listed below.\n"
        "Do NOT run shell or file tools. Output ONLY a valid JSON array (no markdown fences, no commentary) "
        "of exactly "
        f"{batch_n} objects with exact keys `theme`, `buggy_code`, `fixed_code`, and `test_code`:\n"
        f"{theme_lines}\n\n"
        "Strict requirements for EVERY object:\n"
        "- Escape newlines properly inside JSON strings.\n"
        "- Both `buggy_code` and `fixed_code` must be self-contained Python 3 stdlib code defining `def compute(xs): ...` "
        "(no file I/O, no network, executes in <0.2s).\n"
        "- `test_code` must use plain `assert` statements calling `compute(...)` so that "
        "`buggy_code + '\\n' + test_code` FAILS (raises AssertionError / exits non-zero) while "
        "`fixed_code + '\\n' + test_code` PASSES (exits 0).\n"
        "- Each `buggy_code` bug must be realistic and subtle (edge case, off-by-one, wrong tie-break), "
        "not a syntax error.\n"
    )


# ---------------------------------------------------------------------------
# Experiment 3 Payload: 8-Way Concurrency Micro-Audit (4 Buggy, 4 Clean)
# ---------------------------------------------------------------------------
EXP3_CASES: list[dict[str, Any]] = [
    {
        "id": "exp3-sql-vuln",
        "expected_vulnerable": True,
        "expected_class": "sql_injection",
        "code": '''\
def find_user_by_role(conn, role: str):
    sql = f"SELECT id, email FROM users WHERE role = '{role}' AND active = 1"
    return conn.execute(sql).fetchall()
''',
    },
    {
        "id": "exp3-sql-clean",
        "expected_vulnerable": False,
        "expected_class": "none",
        "code": '''\
def find_user_by_role(conn, role: str):
    sql = "SELECT id, email FROM users WHERE role = ? AND active = 1"
    return conn.execute(sql, (role,)).fetchall()
''',
    },
    {
        "id": "exp3-rng-vuln",
        "expected_vulnerable": True,
        "expected_class": "weak_randomness",
        "code": '''\
import random
import string

def issue_password_reset_token(length: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))
''',
    },
    {
        "id": "exp3-rng-clean",
        "expected_vulnerable": False,
        "expected_class": "none",
        "code": '''\
import secrets
import string

def issue_password_reset_token(length: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))
''',
    },
    {
        "id": "exp3-yaml-vuln",
        "expected_vulnerable": True,
        "expected_class": "unsafe_deserialization",
        "code": '''\
import yaml

def parse_plugin_manifest(raw_yaml: str) -> dict:
    data = yaml.load(raw_yaml, Loader=yaml.UnsafeLoader)
    if not isinstance(data, dict):
        raise ValueError("manifest must be a mapping")
    return data
''',
    },
    {
        "id": "exp3-yaml-clean",
        "expected_vulnerable": False,
        "expected_class": "none",
        "code": '''\
import yaml

def parse_plugin_manifest(raw_yaml: str) -> dict:
    data = yaml.safe_load(raw_yaml)
    if not isinstance(data, dict):
        raise ValueError("manifest must be a mapping")
    return data
''',
    },
    {
        "id": "exp3-ssrf-vuln",
        "expected_vulnerable": True,
        "expected_class": "ssrf",
        "code": '''\
import urllib.request
from urllib.parse import urlparse

def fetch_avatar(url: str) -> bytes:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("bad scheme")
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.read(1_000_000)
''',
    },
    {
        "id": "exp3-ssrf-clean",
        "expected_vulnerable": False,
        "expected_class": "none",
        "code": '''\
import ipaddress
import socket
from urllib.parse import urlparse
import urllib.request

ALLOWED_HOSTS = {"cdn.example.com", "avatars.example.org"}

def fetch_public_avatar(url: str) -> bytes:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise ValueError("disallowed URL")
    for info in socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM):
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError("non-global address rejected")
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.read(1_000_000)
''',
    },
]


def build_exp3_prompt(case: dict[str, Any]) -> str:
    return (
        "Audit this self-contained Python snippet for security vulnerabilities.\n"
        "Do NOT run shell or file tools. Output ONLY valid JSON matching:\n"
        '{"vulnerable": <bool>, "vuln_class": "<string or none>", "line_hint": <int>, "reason": "<brief>"}\n\n'
        "```python\n"
        f"{_numbered_code(case['code'])}\n"
        "```\n"
    )


# ---------------------------------------------------------------------------
# Helpers: JSON extraction & Deterministic Graders
# ---------------------------------------------------------------------------
def extract_json_payload(raw_text: str, expect_array: bool = False) -> tuple[Any | None, bool]:
    txt = (raw_text or "").strip()
    if not txt:
        return None, False
    try:
        parsed = json.loads(txt)
        if expect_array and isinstance(parsed, list):
            return parsed, True
        if not expect_array and isinstance(parsed, dict):
            return parsed, True
    except Exception:
        pass

    stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        parsed = json.loads(stripped)
        return parsed, False
    except Exception:
        pass

    open_ch, close_ch = ("[", "]") if expect_array else ("{", "}")
    start = stripped.find(open_ch)
    end = stripped.rfind(close_ch)
    if start != -1 and end > start:
        try:
            return json.loads(stripped[start : end + 1]), False
        except Exception:
            pass
    return None, False


def grade_exp1a(output_text: str) -> dict[str, Any]:
    obj, strict_raw = extract_json_payload(output_text, expect_array=False)
    if not isinstance(obj, dict) or not isinstance(obj.get("findings"), list):
        return {
            "json_valid": False,
            "strict_raw_json": False,
            "tp": 0,
            "fp": 0,
            "fn": 2,
            "precision": 0.0,
            "recall": 0.0,
            "f1": 0.0,
        }
    tp_alg = False
    tp_path = False
    fp = 0
    for f in obj["findings"]:
        if not isinstance(f, dict):
            fp += 1
            continue
        fn_name = str(f.get("function") or "").lower()
        blob = json.dumps(f).lower()
        line_hint = int(f.get("line_hint") or 0) if str(f.get("line_hint") or "").isdigit() else 0

        is_token_fn = "verify_session_token" in fn_name or (15 <= line_hint <= 51)
        is_tenant_fn = "load_tenant_artifact" in fn_name or (62 <= line_hint <= 76)
        is_decoy = "verify_webhook_hmac" in fn_name or "read_audit_entry" in fn_name

        if is_token_fn and not is_decoy and any(k in blob for k in ("alg", "none", "signature", "bypass", "forge")):
            tp_alg = True
        elif is_tenant_fn and not is_decoy and any(k in blob for k in ("travers", "prefix", "startswith", "sibling", "escape", "path")):
            tp_path = True
        else:
            fp += 1

    tp = int(tp_alg) + int(tp_path)
    fn = 2 - tp
    prec = round(tp / (tp + fp), 4) if (tp + fp) > 0 else 0.0
    rec = round(tp / 2.0, 4)
    f1 = round(2 * prec * rec / (prec + rec), 4) if (prec + rec) > 0 else 0.0
    return {
        "json_valid": True,
        "strict_raw_json": strict_raw,
        "tp_alg_none": tp_alg,
        "tp_path_prefix": tp_path,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": prec,
        "recall": rec,
        "f1": f1,
    }


def grade_exp1b(job: dict[str, Any], base_repo: Path) -> dict[str, Any]:
    patch_path = Path(job["patch_path"]) if job.get("patch_path") else None
    receipt_path = Path(job["receipt_path"]) if job.get("receipt_path") else None
    patch_txt = patch_path.read_text(encoding="utf-8", errors="replace") if patch_path and patch_path.is_file() else ""
    receipt = json.loads(receipt_path.read_text(encoding="utf-8")) if receipt_path and receipt_path.is_file() else {}
    added_tests = re.findall(r"^\+\s*def\s+test_[a-zA-Z0-9_]+", patch_txt, flags=re.MULTILINE)

    # Independent Maker != Checker replay on a clean copy of base_repo
    independent_pass = False
    total_tests_ran = 0
    if patch_txt.strip():
        with tempfile.TemporaryDirectory(prefix="exp1b_verify_") as td:
            copy_repo = Path(td) / "repo"
            shutil.copytree(base_repo, copy_repo)
            p_file = Path(td) / "cand.patch"
            p_file.write_text(patch_txt, encoding="utf-8")
            ap = subprocess.run(["git", "-C", str(copy_repo), "apply", str(p_file)], capture_output=True, text=True)
            if ap.returncode == 0:
                ut = subprocess.run(
                    ["python3", "-B", "-m", "unittest", "-v", "test_interval_index.py"],
                    cwd=str(copy_repo),
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                independent_pass = ut.returncode == 0
                m = re.search(r"Ran\s+(\d+)\s+test", (ut.stderr or "") + (ut.stdout or ""))
                if m:
                    total_tests_ran = int(m.group(1))

    passed = (
        job.get("status") == "done"
        and job.get("verify_exit_code") == 0
        and receipt.get("scope_confined") is True
        and independent_pass
        and len(added_tests) >= 3
    )
    return {
        "passed": passed,
        "verify_exit_code": job.get("verify_exit_code"),
        "scope_confined": receipt.get("scope_confined"),
        "network_isolated": receipt.get("network_isolated"),
        "patch_bytes": len(patch_txt.encode("utf-8")),
        "added_tests_count": len(added_tests),
        "independent_unittest_pass": independent_pass,
        "total_tests_ran": total_tests_ran,
    }


def _load_valkyrie_gates():
    if str(VALKYRIE_FORGE_DIR) not in sys.path:
        sys.path.insert(0, str(VALKYRIE_FORGE_DIR))
    import bg5_phylogeny_dedup as phy_mod  # type: ignore
    import stage7a_kekkai as kekkai_mod  # type: ignore

    return kekkai_mod.KekkaiSanitizer, phy_mod.PhylogeneticFirewall


def _run_puzzle_code(code: str, bwrap_bin: str | None) -> int:
    if bwrap_bin:
        cmd = [
            bwrap_bin,
            "--ro-bind", "/", "/",
            "--dev", "/dev",
            "--proc", "/proc",
            "--tmpfs", "/tmp",
            "--unshare-net",
            "--die-with-parent",
            "--",
            "python3", "-B", "-I", "-c", code,
        ]
    else:
        cmd = ["python3", "-B", "-I", "-c", code]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return proc.returncode
    except subprocess.TimeoutExpired:
        return 124
    except Exception:
        return 125


def grade_exp2_worker(
    job: dict[str, Any],
    expected_n: int,
    KekkaiSanitizer: Any,
    PhylogeneticFirewall: Any,
    global_fw: Any,
    bwrap_bin: str | None,
) -> dict[str, Any]:
    out_txt = Path(job["output_path"]).read_text(encoding="utf-8", errors="replace") if job.get("output_path") and Path(job["output_path"]).is_file() else ""
    parsed, strict_raw = extract_json_payload(out_txt, expect_array=True)
    if isinstance(parsed, dict) and isinstance(parsed.get("puzzles"), list):
        puzzles = parsed["puzzles"]
    elif isinstance(parsed, list):
        puzzles = parsed
    else:
        puzzles = []

    local_san = KekkaiSanitizer()
    local_fw = PhylogeneticFirewall(near_dup_jaccard=0.72, template_cluster_cap=8)

    valid_schema = 0
    oracle_pass = 0
    kekkai_pass = 0
    promoted_local = 0
    promoted_global = 0
    item_reasons: list[str] = []

    for idx, item in enumerate(puzzles):
        if not isinstance(item, dict):
            item_reasons.append(f"#{idx}:not_dict")
            continue
        if not all(k in item and isinstance(item[k], str) and item[k].strip() for k in ("theme", "buggy_code", "fixed_code", "test_code")):
            item_reasons.append(f"#{idx}:missing_keys")
            continue
        valid_schema += 1
        buggy = item["buggy_code"]
        fixed = item["fixed_code"]
        testc = item["test_code"]
        theme = item["theme"]

        rb = _run_puzzle_code(buggy + "\n" + testc, bwrap_bin)
        rf = _run_puzzle_code(fixed + "\n" + testc, bwrap_bin)
        if not (rb != 0 and rf == 0):
            item_reasons.append(f"#{idx}:oracle_fail(bug_rc={rb},fix_rc={rf})")
            continue
        oracle_pass += 1

        canonical = json.dumps({"buggy_code": buggy, "fixed_code": fixed, "test_code": testc}, sort_keys=True)
        if local_san.sanitize(canonical, check_noise=False) is None:
            item_reasons.append(f"#{idx}:kekkai_reject")
            continue
        kekkai_pass += 1

        doc = f"Theme={theme} Fixed={fixed} Tests={testc}"
        dec_local = local_fw.admit(doc, enforce_minhash=True, enforce_template_cap=True)
        if dec_local.fate == "PROMOTE":
            promoted_local += 1
            dec_glob = global_fw.admit(doc, enforce_minhash=True, enforce_template_cap=True)
            if dec_glob.fate == "PROMOTE":
                promoted_global += 1
        else:
            item_reasons.append(f"#{idx}:fw_{dec_local.fate}:{dec_local.reason}")

    tot_tok = int(job.get("input_tokens") or 0) + int(job.get("output_tokens") or 0)
    reason_tok = int(job.get("reasoning_tokens") or 0)
    wall_s = float(job.get("wall_s") or 0.0)
    return {
        "requested": expected_n,
        "strict_raw_json": strict_raw,
        "parsed_items": len(puzzles),
        "valid_schema": valid_schema,
        "oracle_pass": oracle_pass,
        "kekkai_pass": kekkai_pass,
        "promoted": promoted_local,
        "promoted_global": promoted_global,
        "admission_rate": round(promoted_local / max(1, expected_n), 4),
        "wall_s": wall_s,
        "total_in_out_tokens": tot_tok,
        "reasoning_tokens": reason_tok,
        "tokens_per_promoted": round(tot_tok / promoted_local, 1) if promoted_local else None,
        "wall_s_per_promoted": round(wall_s / promoted_local, 2) if promoted_local else None,
        "reasoning_per_promoted": round(reason_tok / promoted_local, 1) if promoted_local else None,
        "reject_reasons": item_reasons[:10],
    }


def grade_exp3_case(output_text: str, case: dict[str, Any]) -> dict[str, Any]:
    obj, strict_raw = extract_json_payload(output_text, expect_array=False)
    if not isinstance(obj, dict) or not isinstance(obj.get("vulnerable"), bool):
        return {"json_valid": False, "strict_raw_json": False, "correct": False, "outcome": "INVALID"}
    pred = bool(obj["vulnerable"])
    exp = bool(case["expected_vulnerable"])
    if exp and pred:
        outcome = "TP"
    elif (not exp) and pred:
        outcome = "FP"
    elif (not exp) and (not pred):
        outcome = "TN"
    else:
        outcome = "FN"
    return {
        "json_valid": True,
        "strict_raw_json": strict_raw,
        "predicted": pred,
        "expected": exp,
        "correct": pred == exp,
        "outcome": outcome,
        "vuln_class": obj.get("vuln_class"),
    }


# ---------------------------------------------------------------------------
# Fleet Runner Invocation & Host Sampler
# ---------------------------------------------------------------------------
def _fleet_cli(args_list: list[str]) -> dict[str, Any]:
    cmd = ["python3", "-B", str(FLEET_BIN)] + args_list
    proc = subprocess.run(cmd, capture_output=True, text=True)
    stdout = proc.stdout.strip()
    if not stdout:
        raise RuntimeError(f"wm-grok-fleet {' '.join(args_list[:2])} failed rc={proc.returncode}: {proc.stderr}")
    return json.loads(stdout)


def _sample_host(samples: list[dict[str, Any]], stop_ev: threading.Event) -> None:
    while not stop_ev.is_set():
        mem_avail_mb = 0.0
        swap_used_mb = 0.0
        try:
            kv = {}
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                parts = line.split()
                if len(parts) >= 2:
                    kv[parts[0].rstrip(":")] = int(parts[1])
            mem_avail_mb = round(kv.get("MemAvailable", 0) / 1024.0, 1)
            swap_used_mb = round((kv.get("SwapTotal", 0) - kv.get("SwapFree", 0)) / 1024.0, 1)
        except Exception:
            pass
        try:
            load1 = round(os.getloadavg()[0], 2)
        except Exception:
            load1 = 0.0
        try:
            root_free_gb = round(shutil.disk_usage("/").free / (1024.0 ** 3), 2)
        except Exception:
            root_free_gb = 0.0
        samples.append(
            {
                "ts": time.monotonic(),
                "mem_available_mb": mem_avail_mb,
                "swap_used_mb": swap_used_mb,
                "load1": load1,
                "root_free_gb": root_free_gb,
            }
        )
        stop_ev.wait(1.0)


def render_markdown_report(report: dict[str, Any]) -> str:
    lines: list[str] = [
        f"# Grok 4.7 Fleet Calibration Report ({report['timestamp_utc']})",
        "",
        f"- **Artifact Dir:** `{report['artifact_dir']}`",
        f"- **Queue DB:** `{report['db_path']}`",
        f"- **Total Workers Executed:** `{report['totals']['workers']}` (OK: `{report['totals']['ok']}`, Failed: `{report['totals']['failed']}`)",
        f"- **Total Tokens:** `in={report['totals']['input_tokens']:,}` · `cache_read={report['totals']['cache_read_tokens']:,}` · `out={report['totals']['output_tokens']:,}` · `reason={report['totals']['reasoning_tokens']:,}`",
        "",
        "## Experiment 1: Model Effort & `-fast` Multiplier Matrix",
        "",
        "| Model | 1A Audit F1 (TP/FP) | 1A Wall (s) | 1A Out/Reason Tok | 1B Worktree+bwrap | 1B +Tests | 1B Wall (s) | 1B Out/Reason Tok |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for m in report["exp1_models"]:
        r1a = report["exp1a"][m]
        r1b = report["exp1b"][m]
        lines.append(
            f"| `{m}` | {r1a['grade']['f1']:.2f} ({r1a['grade']['tp']}/{r1a['grade']['fp']}) | {r1a['wall_s']} | "
            f"{r1a['output_tokens']}/{r1a['reasoning_tokens']} | {'PASS' if r1b['grade']['passed'] else 'FAIL'} | "
            f"+{r1b['grade']['added_tests_count']} | {r1b['wall_s']} | {r1b['output_tokens']}/{r1b['reasoning_tokens']} |"
        )

    lines.extend([
        "",
        "## Experiment 2: Crucible ZPD Batch Granularity Sweep (`1` vs `4` vs `8`)",
        "",
        "| Model | Batch N | Parsed/Req | Oracle | Kekkai | Promoted | Admission | Wall (s) | Tok/Promoted | Reason/Promoted |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ])
    for row in report["exp2"]:
        g = row["grade"]
        lines.append(
            f"| `{row['model']}` | {row['batch_n']} | {g['parsed_items']}/{g['requested']} | "
            f"{g['oracle_pass']} | {g['kekkai_pass']} | {g['promoted']} | {g['admission_rate']:.0%} | "
            f"{g['wall_s']} | {g['tokens_per_promoted']} | {g['reasoning_per_promoted']} |"
        )

    e3 = report["exp3"]
    lines.extend([
        "",
        "## Experiment 3: 8-Way Concurrency Stress Probe (`grok-4.7-medium`, `--read-concurrency 8`)",
        "",
        f"- **Completion:** `{e3['done']}/{e3['total']}` | **Wall:** `{e3['campaign_wall_s']}s` vs sum `{e3['sum_worker_s']}s` (**`{e3['speedup_x']}x` speedup**) | **429s:** `{e3['rate_limit_errors']}`",
        f"- **Audit Accuracy:** `{e3['correct']}/{e3['total']}` (`TP={e3['tp']} FP={e3['fp']} TN={e3['tn']} FN={e3['fn']}` | Precision `{e3['precision']:.2f}`, Recall `{e3['recall']:.2f}`)",
        f"- **Host Headroom:** Min MemAvailable `{e3['host_min_mem_mb']} MiB` · Max Swap `{e3['host_max_swap_mb']} MiB` · Max Load1 `{e3['host_max_load1']}` · Min `/` Free `{e3['host_min_root_gb']} GiB`",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Run Grok 4.7 Fleet Calibration Benchmark")
    ap.add_argument("--artifact-dir", default=str(DEFAULT_ARTIFACT_DIR))
    ap.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    ap.add_argument("--keep-db", action="store_true", help="Do not reset existing calibration DB")
    args = ap.parse_args()

    art_dir = Path(args.artifact_dir)
    db_path = Path(args.db_path)
    art_dir.mkdir(parents=True, exist_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if not args.keep_db:
        for suf in ("", "-wal", "-shm"):
            p = Path(str(db_path) + suf)
            if p.exists():
                p.unlink()

    setup_ephemeral_worktree_repo(EPHEMERAL_WORKTREE_REPO)
    KekkaiSanitizer, PhylogeneticFirewall = _load_valkyrie_gates()
    bwrap_bin = shutil.which("bwrap")

    models_exp1 = ["grok-4.7-medium", "grok-4.7-medium-fast", "grok-4.7-high", "grok-4.7-xhigh"]
    exp1a_prompt = build_exp1a_prompt()
    exp1b_prompt = build_exp1b_prompt()

    try:
        # ------------------------------------------------------------------
        # Campaign 1: Effort & Speed Multiplier Matrix (8 jobs)
        # ------------------------------------------------------------------
        c1_id = "cal_exp1_effort"
        c1_manifest = art_dir / "exp1_manifest.jsonl"
        c1_rows = []
        for m in models_exp1:
            c1_rows.append({
                "id": f"exp1a-{m}",
                "name": f"sec-audit-{m}",
                "stage": "exp1a",
                "action": "ask",
                "model": m,
                "cwd": str(REPO_ROOT),
                "prompt": exp1a_prompt,
            })
            c1_rows.append({
                "id": f"exp1b-{m}",
                "name": f"interval-fix-{m}",
                "stage": "exp1b",
                "action": "worktree",
                "model": m,
                "cwd": str(EPHEMERAL_WORKTREE_REPO),
                "prompt": exp1b_prompt,
                "confined": True,
                "verify_cmd": "python3 -m unittest -v test_interval_index.py",
            })
        c1_manifest.write_text("\n".join(json.dumps(r) for r in c1_rows) + "\n", encoding="utf-8")
        _fleet_cli(["enqueue", "--campaign", c1_id, "--manifest", str(c1_manifest), "--db-path", str(db_path), "--artifact-dir", str(art_dir)])
        print("[bench] Running Experiment 1 (4 models x 2 tasks = 8 workers)...", flush=True)
        _fleet_cli([
            "run", "--campaign", c1_id,
            "--read-concurrency", "4", "--write-concurrency", "2",
            "--stall-timeout", "90", "--wall-timeout", "240", "--max-retries", "2",
            "--db-path", str(db_path), "--artifact-dir", str(art_dir),
        ])
        c1_exp_json = art_dir / "exp1_export.json"
        _fleet_cli(["export", "--campaign", c1_id, "--out", str(c1_exp_json), "--db-path", str(db_path)])
        c1_jobs = {j["id"]: j for j in json.loads(c1_exp_json.read_text(encoding="utf-8"))["jobs"]}

        exp1a_res: dict[str, Any] = {}
        exp1b_res: dict[str, Any] = {}
        for m in models_exp1:
            j1a = c1_jobs[f"exp1a-{m}"]
            out_1a = Path(j1a["output_path"]).read_text(encoding="utf-8", errors="replace") if j1a.get("output_path") and Path(j1a["output_path"]).is_file() else ""
            exp1a_res[m] = {
                "status": j1a["status"],
                "wall_s": j1a["wall_s"],
                "input_tokens": j1a["input_tokens"],
                "cache_read_tokens": j1a["cache_read_tokens"],
                "output_tokens": j1a["output_tokens"],
                "reasoning_tokens": j1a["reasoning_tokens"],
                "tool_calls": j1a["tool_calls"],
                "grade": grade_exp1a(out_1a),
            }
            j1b = c1_jobs[f"exp1b-{m}"]
            exp1b_res[m] = {
                "status": j1b["status"],
                "wall_s": j1b["wall_s"],
                "input_tokens": j1b["input_tokens"],
                "cache_read_tokens": j1b["cache_read_tokens"],
                "output_tokens": j1b["output_tokens"],
                "reasoning_tokens": j1b["reasoning_tokens"],
                "tool_calls": j1b["tool_calls"],
                "grade": grade_exp1b(j1b, EPHEMERAL_WORKTREE_REPO),
            }

        # ------------------------------------------------------------------
        # Campaign 2: Batch Granularity Sweep on Crucible ZPD Synthesis (6 jobs)
        # ------------------------------------------------------------------
        c2_id = "cal_exp2_granularity"
        c2_manifest = art_dir / "exp2_manifest.jsonl"
        c2_rows = []
        exp2_combos = [(m, n) for m in ("grok-4.7-medium", "grok-4.7-high") for n in (1, 4, 8)]
        for m, n in exp2_combos:
            c2_rows.append({
                "id": f"exp2-{m}-n{n}",
                "name": f"crucible-{m}-n{n}",
                "stage": "exp2",
                "action": "ask",
                "model": m,
                "cwd": str(REPO_ROOT),
                "prompt": build_exp2_prompt(n),
            })
        c2_manifest.write_text("\n".join(json.dumps(r) for r in c2_rows) + "\n", encoding="utf-8")
        _fleet_cli(["enqueue", "--campaign", c2_id, "--manifest", str(c2_manifest), "--db-path", str(db_path), "--artifact-dir", str(art_dir)])
        print("[bench] Running Experiment 2 (Crucible batch sweep 1/4/8 x 2 models = 6 workers)...", flush=True)
        _fleet_cli([
            "run", "--campaign", c2_id,
            "--read-concurrency", "6", "--write-concurrency", "1",
            "--stall-timeout", "90", "--wall-timeout", "240", "--max-retries", "2",
            "--db-path", str(db_path), "--artifact-dir", str(art_dir),
        ])
        c2_exp_json = art_dir / "exp2_export.json"
        _fleet_cli(["export", "--campaign", c2_id, "--out", str(c2_exp_json), "--db-path", str(db_path)])
        c2_jobs = {j["id"]: j for j in json.loads(c2_exp_json.read_text(encoding="utf-8"))["jobs"]}

        global_fw = PhylogeneticFirewall(near_dup_jaccard=0.72, template_cluster_cap=8)
        exp2_res: list[dict[str, Any]] = []
        for m, n in exp2_combos:
            j2 = c2_jobs[f"exp2-{m}-n{n}"]
            exp2_res.append({
                "model": m,
                "batch_n": n,
                "status": j2["status"],
                "grade": grade_exp2_worker(j2, n, KekkaiSanitizer, PhylogeneticFirewall, global_fw, bwrap_bin),
            })

        # ------------------------------------------------------------------
        # Campaign 3: Host Concurrency Stress & Scaling Probe (8 concurrent)
        # ------------------------------------------------------------------
        c3_id = "cal_exp3_stress8"
        c3_manifest = art_dir / "exp3_manifest.jsonl"
        c3_rows = [
            {
                "id": case["id"],
                "name": case["id"],
                "stage": "exp3",
                "action": "ask",
                "model": "grok-4.7-medium",
                "cwd": str(REPO_ROOT),
                "prompt": build_exp3_prompt(case),
            }
            for case in EXP3_CASES
        ]
        c3_manifest.write_text("\n".join(json.dumps(r) for r in c3_rows) + "\n", encoding="utf-8")
        _fleet_cli(["enqueue", "--campaign", c3_id, "--manifest", str(c3_manifest), "--db-path", str(db_path), "--artifact-dir", str(art_dir)])
        print("[bench] Running Experiment 3 (8 concurrent grok-4.7-medium workers)...", flush=True)

        host_samples: list[dict[str, Any]] = []
        stop_ev = threading.Event()
        sampler = threading.Thread(target=_sample_host, args=(host_samples, stop_ev), daemon=True)
        sampler.start()
        t_c3 = time.monotonic()
        try:
            c3_summary = _fleet_cli([
                "run", "--campaign", c3_id,
                "--read-concurrency", "8", "--write-concurrency", "1",
                "--stall-timeout", "90", "--wall-timeout", "240", "--max-retries", "2",
                "--db-path", str(db_path), "--artifact-dir", str(art_dir),
            ])
        finally:
            stop_ev.set()
            sampler.join(timeout=3)
        c3_wall = round(time.monotonic() - t_c3, 2)

        c3_exp_json = art_dir / "exp3_export.json"
        _fleet_cli(["export", "--campaign", c3_id, "--out", str(c3_exp_json), "--db-path", str(db_path)])
        c3_jobs = {j["id"]: j for j in json.loads(c3_exp_json.read_text(encoding="utf-8"))["jobs"]}

        tp = fp = tn = fn = invalid = rate_limits = 0
        e3_cases_out = []
        for case in EXP3_CASES:
            j3 = c3_jobs[case["id"]]
            if j3.get("error") and "429" in str(j3.get("error")):
                rate_limits += 1
            if int(j3.get("attempts") or 1) > 1:
                rate_limits += int(j3["attempts"]) - 1
            out3 = Path(j3["output_path"]).read_text(encoding="utf-8", errors="replace") if j3.get("output_path") and Path(j3["output_path"]).is_file() else ""
            g3 = grade_exp3_case(out3, case)
            oc = g3["outcome"]
            if oc == "TP":
                tp += 1
            elif oc == "FP":
                fp += 1
            elif oc == "TN":
                tn += 1
            elif oc == "FN":
                fn += 1
            else:
                invalid += 1
            e3_cases_out.append({"id": case["id"], "wall_s": j3["wall_s"], "grade": g3})

        sum_w_s = round(sum(float(j.get("wall_s") or 0) for j in c3_jobs.values()), 2)
        prec3 = round(tp / (tp + fp), 4) if (tp + fp) > 0 else 0.0
        rec3 = round(tp / (tp + fn), 4) if (tp + fn) > 0 else 0.0
        exp3_res = {
            "total": len(EXP3_CASES),
            "done": c3_summary["counts"].get("done", 0),
            "failed": c3_summary["counts"].get("failed", 0),
            "campaign_wall_s": c3_wall,
            "sum_worker_s": sum_w_s,
            "speedup_x": round(sum_w_s / max(0.01, c3_wall), 2),
            "rate_limit_errors": rate_limits,
            "tp": tp,
            "fp": fp,
            "tn": tn,
            "fn": fn,
            "invalid": invalid,
            "correct": tp + tn,
            "precision": prec3,
            "recall": rec3,
            "host_samples_count": len(host_samples),
            "host_min_mem_mb": min((s["mem_available_mb"] for s in host_samples), default=0.0),
            "host_max_swap_mb": max((s["swap_used_mb"] for s in host_samples), default=0.0),
            "host_max_load1": max((s["load1"] for s in host_samples), default=0.0),
            "host_min_root_gb": min((s["root_free_gb"] for s in host_samples), default=0.0),
            "cases": e3_cases_out,
        }

        all_jobs = list(c1_jobs.values()) + list(c2_jobs.values()) + list(c3_jobs.values())
        report = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "artifact_dir": str(art_dir),
            "db_path": str(db_path),
            "totals": {
                "workers": len(all_jobs),
                "ok": sum(1 for j in all_jobs if j["status"] == "done"),
                "failed": sum(1 for j in all_jobs if j["status"] != "done"),
                "input_tokens": sum(int(j.get("input_tokens") or 0) for j in all_jobs),
                "cache_read_tokens": sum(int(j.get("cache_read_tokens") or 0) for j in all_jobs),
                "output_tokens": sum(int(j.get("output_tokens") or 0) for j in all_jobs),
                "reasoning_tokens": sum(int(j.get("reasoning_tokens") or 0) for j in all_jobs),
            },
            "exp1_models": models_exp1,
            "exp1a": exp1a_res,
            "exp1b": exp1b_res,
            "exp2": exp2_res,
            "exp3": exp3_res,
        }

        json_out = art_dir / "calibration_report.json"
        md_out = art_dir / "CALIBRATION_REPORT.md"
        json_out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        md_out.write_text(render_markdown_report(report), encoding="utf-8")
        print(f"[bench] Wrote {json_out} and {md_out}", flush=True)
        print(render_markdown_report(report), flush=True)
        return 0
    finally:
        if EPHEMERAL_WORKTREE_REPO.exists():
            shutil.rmtree(EPHEMERAL_WORKTREE_REPO, ignore_errors=True)
        _fleet_cli(["prune-worktrees"])


if __name__ == "__main__":
    sys.exit(main())
