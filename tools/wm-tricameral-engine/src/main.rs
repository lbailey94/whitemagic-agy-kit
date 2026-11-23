//! Muninn-1 Standalone Safe-Rust Holographic CPU Engine (`TM7B\x03`).
//!
//! Runs `Muninn-1` natively on CPU (`#![forbid(unsafe_code)]`, `pulp` AVX2 FMA,
//! `rayon` multi-core dispatch, `wm_gen4_lut::tmac_gemv_32rows`):
//!   - 3:1 Hybrid Recurrent Core:
//!       * 3 Phase-Bound `Gated DeltaNet-2` Holographic Memory blocks (`O(1)` state `S_t`,
//!         decoupled per-head erase `b_t` + channel-wise write `w_t`, L2-cache resident!)
//!       * 1 Full `RoPE` Softmax Attention block
//!   - Fixed Asymmetric 4-Stream `mHC` Sinkhorn-Knopp Routing
//!   - Guna Triadic Multi-Shell Nonary Weights (`Shell 1` Tamas 1.58b / `Shell 2` Rajas 3.17b / `Shell 3` Sattva 4.75b)
//!   - Native 6D Galactic MAC Coordinate Port (`[Logos/Pathos, Observer/Observed, Time, Ethos, Vitality, Catuskoti]`)
//!   - Clean `[EOS]` termination + Exact PyTorch-to-Rust Top-1 & AVX2-vs-Scalar Parity Verification

#![forbid(unsafe_code)]

use pulp::{Arch, Simd, WithSimd};
use rayon::prelude::*;
use serde_json::Value;
use std::collections::HashMap;
use std::env;
use std::fs::{self, File};
use std::io::{BufRead, BufReader, Read, Seek, SeekFrom, Write};
use std::os::unix::net::UnixListener;
use std::time::Instant;
use wm_tricameral_engine::{
    pack_ternary_2pairs_per_byte, tmac_gemv_32rows_packed4, tmac_gemv_32rows_packed4_scalar,
};

const TM7B_MAGIC_V3: &[u8; 8] = b"TM7B\x03\x00\x00\x00";
const TM7B_MAGIC_V4: &[u8; 8] = b"TM7B\x04\x00\x00\x00";
const TM7B_MAGIC_V5: &[u8; 8] = b"TM7B\x05\x00\x00\x00";
const TM7B_MAGIC_V6: &[u8; 8] = b"TM7B\x06\x00\x00\x00";
const HMS_MAGIC_V1: &[u8; 8] = b"HMS1\x01\x00\x00\x00";
const NS3S_MAGIC_V1: &[u8; 8] = b"NS3S\x01\x00\x00\x00";
const CSPD_MAGIC_V1: &[u8; 8] = b"CSPD\x01\x00\x00\x00";
const N_STREAMS: usize = 4;

struct ModelConfig {
    bundle_version: u8,
    vocab_size: usize,
    d_model: usize,
    n_heads: usize,
    head_dim: usize,
    d_ff: usize,
    n_core_blocks: usize,
    n_shells: usize,
    engram_slots: usize,
    engram_dim: usize,
    mac_dim: usize,
    core_delta_mask: u32,
}

// ---------------------------------------------------------------------------
// SIMD Dot Products & Ternary T-MAC Packing
// ---------------------------------------------------------------------------

struct SimdDot<'a> {
    a: &'a [f32],
    b: &'a [f32],
}

impl WithSimd for SimdDot<'_> {
    type Output = f32;

    #[inline(always)]
    fn with_simd<S: Simd>(self, simd: S) -> Self::Output {
        let (a_head, a_tail) = S::as_simd_f32s(self.a);
        let (b_head, b_tail) = S::as_simd_f32s(self.b);
        let mut acc = simd.splat_f32s(0.0);
        for (&va, &vb) in a_head.iter().zip(b_head.iter()) {
            acc = simd.mul_add_f32s(va, vb, acc);
        }
        let mut sum = simd.reduce_sum_f32s(acc);
        for (&ta, &tb) in a_tail.iter().zip(b_tail.iter()) {
            sum += ta * tb;
        }
        sum
    }
}

#[inline]
fn dot_f32(arch: Arch, a: &[f32], b: &[f32]) -> f32 {
    arch.dispatch(SimdDot { a, b })
}

struct SimdDeltaRowUpdate<'a> {
    row: &'a mut [f32],
    k_hat: &'a [f32],
    q_hat: &'a [f32],
    alpha: f32,
    r_val: f32,
}

impl WithSimd for SimdDeltaRowUpdate<'_> {
    type Output = f32;

    #[inline(always)]
    fn with_simd<S: Simd>(self, simd: S) -> Self::Output {
        let (r_head, r_tail) = S::as_mut_simd_f32s(self.row);
        let (k_head, k_tail) = S::as_simd_f32s(self.k_hat);
        let (q_head, q_tail) = S::as_simd_f32s(self.q_hat);
        let v_alpha = simd.splat_f32s(self.alpha);
        let v_rval = simd.splat_f32s(self.r_val);
        let mut acc = simd.splat_f32s(0.0);
        for ((rv, &kv), &qv) in r_head.iter_mut().zip(k_head.iter()).zip(q_head.iter()) {
            let updated = simd.mul_add_f32s(v_rval, kv, simd.mul_f32s(v_alpha, *rv));
            *rv = updated;
            acc = simd.mul_add_f32s(updated, qv, acc);
        }
        let mut sum = simd.reduce_sum_f32s(acc);
        for ((rt, &kt), &qt) in r_tail.iter_mut().zip(k_tail.iter()).zip(q_tail.iter()) {
            let updated = self.alpha * *rt + self.r_val * kt;
            *rt = updated;
            sum += updated * qt;
        }
        sum
    }
}

#[inline]
fn delta_row_update_and_dot(
    arch: Arch,
    row: &mut [f32],
    k_hat: &[f32],
    q_hat: &[f32],
    alpha: f32,
    r_val: f32,
) -> f32 {
    arch.dispatch(SimdDeltaRowUpdate {
        row,
        k_hat,
        q_hat,
        alpha,
        r_val,
    })
}

fn read_u32_le(buf: &[u8], pos: &mut usize) -> u32 {
    let b: [u8; 4] = buf[*pos..*pos + 4].try_into().expect("u32 slice");
    *pos += 4;
    u32::from_le_bytes(b)
}

fn read_f32_le(buf: &[u8], pos: &mut usize) -> f32 {
    let b: [u8; 4] = buf[*pos..*pos + 4].try_into().expect("f32 slice");
    *pos += 4;
    f32::from_le_bytes(b)
}

fn stream_u32_le<R: Read>(r: &mut R) -> u32 {
    let mut b = [0u8; 4];
    r.read_exact(&mut b).expect("stream u32");
    u32::from_le_bytes(b)
}

fn stream_f32_le<R: Read>(r: &mut R) -> f32 {
    let mut b = [0u8; 4];
    r.read_exact(&mut b).expect("stream f32");
    f32::from_le_bytes(b)
}

fn stream_f32_vec<R: Read>(r: &mut R, count: usize) -> Vec<f32> {
    let mut out = vec![0.0_f32; count];
    let mut chunk_buf = [0u8; 16_384];
    let mut filled = 0usize;
    while filled < count {
        let take_floats = (count - filled).min(chunk_buf.len() / 4);
        let byte_len = take_floats * 4;
        r.read_exact(&mut chunk_buf[..byte_len])
            .expect("stream f32 vec");
        for i in 0..take_floats {
            let off = i * 4;
            out[filled + i] = f32::from_le_bytes([
                chunk_buf[off],
                chunk_buf[off + 1],
                chunk_buf[off + 2],
                chunk_buf[off + 3],
            ]);
        }
        filled += take_floats;
    }
    out
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum ScaleAxis {
    Scalar,
    PerOc,
    PerIc,
}

struct TernaryShell {
    /// len 1 (`Scalar`), `rows` (`PerOc`), or `cols` (`PerIc`)
    scales: Vec<f32>,
    packed_col_major: Vec<[u8; 32]>,
}

struct TernaryLayer {
    rows: usize,
    cols: usize,
    scale_axis: ScaleAxis,
    shells: Vec<TernaryShell>,
    /// Optional physical-fault float override used ONLY during `--fault-bench` (zero overhead otherwise).
    override_f32: Option<Vec<f32>>,
}

/// Ultra-fast 3-shell `4.75-bit RoleQ-T Ratio-3 Tied` (`3*S1 + S2 + S3`) T-MAC mirror
/// (`0.75 bytes/weight` = `5.3x` smaller than FP32, `>0.999` cosine similarity!) of FP32 layers
/// (`tok_emb`, `prelude.0`, `coda.1`, `w_write`) used ONLY by the Matryoshka self-speculative
/// drafter (`--spec-k > 0`). Target verification always uses exact FP32 + T-MAC.
fn quantize_f32_to_tmac_layer(w: &[f32], rows: usize, cols: usize) -> TernaryLayer {
    let mut s1_scales = vec![1e-6_f32; rows];
    let mut s2_scales = vec![1e-6_f32; rows];
    let mut s3_scales = vec![1e-6_f32; rows];
    let mut q1 = vec![0i8; rows * cols];
    let mut q2 = vec![0i8; rows * cols];
    let mut q3 = vec![0i8; rows * cols];

    q1.par_chunks_mut(cols)
        .zip(q2.par_chunks_mut(cols))
        .zip(q3.par_chunks_mut(cols))
        .zip(s1_scales.par_iter_mut())
        .zip(s2_scales.par_iter_mut())
        .zip(s3_scales.par_iter_mut())
        .enumerate()
        .for_each(|(r, (((((q1_r, q2_r), q3_r), sc1), sc2), sc3))| {
            let src = &w[r * cols..(r + 1) * cols];
            let mut sum_abs = 0.0_f32;
            for &v in src {
                sum_abs += v.abs();
            }
            let s1 = (sum_abs / (cols as f32)).max(1e-6);
            let s2 = s1 / 3.0;
            *sc1 = s1;
            *sc2 = s2;
            let mut res_abs = 0.0_f32;
            for c in 0..cols {
                let q9 = (src[c] / s2).round().clamp(-4.0, 4.0);
                let v1 = q9.signum() * ((q9.abs() + 1.0) / 3.0).floor();
                let v2 = q9 - 3.0 * v1;
                q1_r[c] = v1 as i8;
                q2_r[c] = v2 as i8;
                let r2 = src[c] - (v1 * s1 + v2 * s2);
                res_abs += r2.abs();
            }
            let s3 = (res_abs / (cols as f32)).max(1e-6);
            *sc3 = s3;
            for c in 0..cols {
                let r2 = src[c] - (f32::from(q1_r[c]) * s1 + f32::from(q2_r[c]) * s2);
                q3_r[c] = (r2 / s3).round().clamp(-1.0, 1.0) as i8;
            }
        });

    TernaryLayer {
        rows,
        cols,
        scale_axis: ScaleAxis::PerOc,
        shells: vec![
            TernaryShell {
                scales: s1_scales,
                packed_col_major: pack_ternary_2pairs_per_byte(rows, cols, &q1),
            },
            TernaryShell {
                scales: s2_scales,
                packed_col_major: pack_ternary_2pairs_per_byte(rows, cols, &q2),
            },
            TernaryShell {
                scales: s3_scales,
                packed_col_major: pack_ternary_2pairs_per_byte(rows, cols, &q3),
            },
        ],
        override_f32: None,
    }
}

/// Bit-exact `K`-token batched FP32 matrix-multiply: reads each weight row from DRAM ONCE
/// and reuses it hot in L1d across all `K` tokens (64-row cacheline-aligned blocks = 0 false sharing!).
fn linear_mat_f32_k(
    arch: Arch,
    w: &[f32],
    xs: &[Vec<f32>],
    out_f: usize,
    in_f: usize,
) -> Vec<Vec<f32>> {
    let k_len = xs.len();
    if k_len == 1 {
        return vec![linear_vec(arch, w, &xs[0], out_f, in_f)];
    }
    let mut flat = vec![0.0_f32; out_f * k_len];
    if out_f >= 256 {
        flat.par_chunks_mut(64 * k_len)
            .enumerate()
            .for_each(|(blk_i, dst_blk)| {
                let r_start = blk_i * 64;
                let n_rows = dst_blk.len() / k_len;
                for lr in 0..n_rows {
                    let r = r_start + lr;
                    let row = &w[r * in_f..(r + 1) * in_f];
                    let dst = &mut dst_blk[lr * k_len..(lr + 1) * k_len];
                    for (t, x) in xs.iter().enumerate() {
                        dst[t] = dot_f32(arch, row, x);
                    }
                }
            });
    } else {
        for r in 0..out_f {
            let row = &w[r * in_f..(r + 1) * in_f];
            let base = r * k_len;
            for (t, x) in xs.iter().enumerate() {
                flat[base + t] = dot_f32(arch, row, x);
            }
        }
    }
    let mut outs = vec![vec![0.0_f32; out_f]; k_len];
    for r in 0..out_f {
        let base = r * k_len;
        for t in 0..k_len {
            outs[t][r] = flat[base + t];
        }
    }
    outs
}

fn linear_mat_f32_bias_k(
    arch: Arch,
    w: &[f32],
    b: &[f32],
    xs: &[Vec<f32>],
    out_f: usize,
    in_f: usize,
) -> Vec<Vec<f32>> {
    let mut outs = linear_mat_f32_k(arch, w, xs, out_f, in_f);
    for o in &mut outs {
        for i in 0..out_f {
            o[i] += b[i];
        }
    }
    outs
}

/// Exact float32 GEMV directly from dense 2-pair/byte (`2.0 bpw`) packed T-MAC nibbles (`0` extra f32 RAM!).
/// Used ONLY during reference parity verification (`--verify-parity`); normal generation uses
/// quantized integer `tmac_gemv_32rows_packed4`.
fn eval_packed_exact_f32_gemv(layer: &TernaryLayer, x: &[f32], use_shells: usize) -> Vec<f32> {
    let col_pairs = layer.cols / 2;
    let packed_pairs = col_pairs.div_ceil(2);
    let active = &layer.shells[..use_shells];
    let axis = layer.scale_axis;
    let mut out = vec![0.0_f32; layer.rows];
    out.par_chunks_mut(32).enumerate().for_each(|(rb, dst)| {
        for sh in active {
            let slice = &sh.packed_col_major[rb * packed_pairs..(rb + 1) * packed_pairs];
            let mut acc = [0.0_f32; 32];
            for (pp, blk) in slice.iter().enumerate() {
                let cp_lo = 2 * pp;
                let (x0, x1) = if axis == ScaleAxis::PerIc {
                    (
                        x[2 * cp_lo] * sh.scales[2 * cp_lo],
                        x[2 * cp_lo + 1] * sh.scales[2 * cp_lo + 1],
                    )
                } else {
                    (x[2 * cp_lo], x[2 * cp_lo + 1])
                };
                let lut_lo: [f32; 9] =
                    [-x0 - x1, -x1, x0 - x1, -x0, 0.0, x0, -x0 + x1, x1, x0 + x1];
                let cp_hi = cp_lo + 1;
                if cp_hi < col_pairs {
                    let (x2, x3) = if axis == ScaleAxis::PerIc {
                        (
                            x[2 * cp_hi] * sh.scales[2 * cp_hi],
                            x[2 * cp_hi + 1] * sh.scales[2 * cp_hi + 1],
                        )
                    } else {
                        (x[2 * cp_hi], x[2 * cp_hi + 1])
                    };
                    let lut_hi: [f32; 9] =
                        [-x2 - x3, -x3, x2 - x3, -x2, 0.0, x2, -x2 + x3, x3, x2 + x3];
                    for r in 0..32 {
                        let b = blk[r];
                        acc[r] += lut_lo[(b & 0x0F) as usize] + lut_hi[(b >> 4) as usize];
                    }
                } else {
                    for r in 0..32 {
                        acc[r] += lut_lo[(blk[r] & 0x0F) as usize];
                    }
                }
            }
            match axis {
                ScaleAxis::Scalar => {
                    let sc = sh.scales[0];
                    for r in 0..32 {
                        dst[r] += acc[r] * sc;
                    }
                }
                ScaleAxis::PerOc => {
                    let base = rb * 32;
                    for r in 0..32 {
                        dst[r] += acc[r] * sh.scales[base + r];
                    }
                }
                ScaleAxis::PerIc => {
                    for r in 0..32 {
                        dst[r] += acc[r];
                    }
                }
            }
        }
    });
    out
}

fn pack_ternary_for_tmac(rows: usize, cols: usize, q: &[i8]) -> Vec<[u8; 32]> {
    pack_ternary_2pairs_per_byte(rows, cols, q)
}

fn build_group_luts_i8(x: &[f32]) -> (f32, Vec<[i8; 16]>) {
    let mut x_max = 1e-6_f32;
    for &v in x {
        let a = v.abs();
        if a > x_max {
            x_max = a;
        }
    }
    let act_scale = x_max / 31.0;
    let x_i8: Vec<i8> = x
        .iter()
        .map(|&v| (v / act_scale).round().clamp(-31.0, 31.0) as i8)
        .collect();
    let col_pairs = x.len() / 2;
    let mut group_luts = Vec::with_capacity(col_pairs);
    for cp in 0..col_pairs {
        let a0 = i16::from(x_i8[2 * cp]);
        let a1 = i16::from(x_i8[2 * cp + 1]);
        let mut lut = [0i8; 16];
        for w0 in 0..=2i16 {
            for w1 in 0..=2i16 {
                lut[(w0 + 3 * w1) as usize] = ((w0 - 1) * a0 + (w1 - 1) * a1) as i8;
            }
        }
        group_luts.push(lut);
    }
    (act_scale, group_luts)
}

fn eval_tmac_ternary_gemv(
    arch: Arch,
    layer: &TernaryLayer,
    x: &[f32],
    active_shells: usize,
    use_tmac_int: bool,
    verify_avx2_parity: bool,
    max_int_diff: &mut i32,
) -> Vec<f32> {
    if let Some(ref wf) = layer.override_f32 {
        return linear_vec(arch, wf, x, layer.rows, layer.cols);
    }
    let use_shells = active_shells.clamp(1, layer.shells.len());
    if use_tmac_int || verify_avx2_parity {
        let row_blocks = layer.rows / 32;
        let col_pairs = layer.cols / 2;
        let packed_pairs = col_pairs.div_ceil(2);
        let active = &layer.shells[..use_shells];
        let axis = layer.scale_axis;

        // For PerIc (`RoleQ-T` Writers: `wo`, `w_down`), fold per-IC scale into x per shell (`0` inner-loop overhead!).
        let shell_luts: Vec<(f32, Vec<[i8; 16]>)> = if axis == ScaleAxis::PerIc {
            active
                .iter()
                .map(|sh| {
                    let x_eff: Vec<f32> = x
                        .iter()
                        .zip(sh.scales.iter())
                        .map(|(&xv, &sv)| xv * sv)
                        .collect();
                    build_group_luts_i8(&x_eff)
                })
                .collect()
        } else {
            vec![build_group_luts_i8(x)]
        };

        let mut out_int = if use_tmac_int {
            vec![0.0_f32; layer.rows]
        } else {
            Vec::new()
        };
        if verify_avx2_parity {
            for (s_idx, sh) in active.iter().enumerate() {
                let (act_scale, group_luts) = if axis == ScaleAxis::PerIc {
                    (shell_luts[s_idx].0, &shell_luts[s_idx].1)
                } else {
                    (shell_luts[0].0, &shell_luts[0].1)
                };
                for rb in 0..row_blocks {
                    let slice = &sh.packed_col_major[rb * packed_pairs..(rb + 1) * packed_pairs];
                    let mut acc_simd = [0i32; 32];
                    let mut acc_scal = [0i32; 32];
                    tmac_gemv_32rows_packed4(slice, group_luts, &mut acc_simd);
                    tmac_gemv_32rows_packed4_scalar(slice, group_luts, &mut acc_scal);
                    for r in 0..32 {
                        let d = (acc_simd[r] - acc_scal[r]).abs();
                        if d > *max_int_diff {
                            *max_int_diff = d;
                        }
                        if use_tmac_int {
                            let mul = match axis {
                                ScaleAxis::Scalar => act_scale * sh.scales[0],
                                ScaleAxis::PerOc => act_scale * sh.scales[rb * 32 + r],
                                ScaleAxis::PerIc => act_scale,
                            };
                            out_int[rb * 32 + r] += (acc_simd[r] as f32) * mul;
                        }
                    }
                }
            }
        } else if use_tmac_int {
            out_int
                .par_chunks_mut(32)
                .enumerate()
                .for_each(|(rb, dst_chunk)| {
                    for (s_idx, sh) in active.iter().enumerate() {
                        let (act_scale, group_luts) = if axis == ScaleAxis::PerIc {
                            (shell_luts[s_idx].0, &shell_luts[s_idx].1)
                        } else {
                            (shell_luts[0].0, &shell_luts[0].1)
                        };
                        let slice =
                            &sh.packed_col_major[rb * packed_pairs..(rb + 1) * packed_pairs];
                        let mut acc = [0i32; 32];
                        tmac_gemv_32rows_packed4(slice, group_luts, &mut acc);
                        match axis {
                            ScaleAxis::Scalar => {
                                let mul = act_scale * sh.scales[0];
                                for r in 0..32 {
                                    dst_chunk[r] += (acc[r] as f32) * mul;
                                }
                            }
                            ScaleAxis::PerOc => {
                                let base = rb * 32;
                                for r in 0..32 {
                                    dst_chunk[r] += (acc[r] as f32) * act_scale * sh.scales[base + r];
                                }
                            }
                            ScaleAxis::PerIc => {
                                for r in 0..32 {
                                    dst_chunk[r] += (acc[r] as f32) * act_scale;
                                }
                            }
                        }
                    }
                });
        }
        if use_tmac_int {
            return out_int;
        }
    }
    let _ = arch;
    eval_packed_exact_f32_gemv(layer, x, use_shells)
}

/// Bit-exact `K`-token batched T-MAC ternary GEMM: loads each 32-row packed weight block
/// (`12-36 KB`, L1d-resident!) from DRAM ONCE and evaluates all `K` tokens with exact `tmac_gemv_32rows_packed4`.
fn eval_tmac_ternary_gemm_k(
    arch: Arch,
    layer: &TernaryLayer,
    xs: &[Vec<f32>],
    active_shells: usize,
    use_tmac_int: bool,
) -> Vec<Vec<f32>> {
    let k_len = xs.len();
    if k_len == 1 {
        let mut dummy = 0i32;
        return vec![eval_tmac_ternary_gemv(
            arch,
            layer,
            &xs[0],
            active_shells,
            use_tmac_int,
            false,
            &mut dummy,
        )];
    }
    if let Some(ref wf) = layer.override_f32 {
        return linear_mat_f32_k(arch, wf, xs, layer.rows, layer.cols);
    }
    let use_shells = active_shells.clamp(1, layer.shells.len());
    if !use_tmac_int {
        return xs
            .iter()
            .map(|x| eval_packed_exact_f32_gemv(layer, x, use_shells))
            .collect();
    }
    let row_blocks = layer.rows / 32;
    let col_pairs = layer.cols / 2;
    let packed_pairs = col_pairs.div_ceil(2);
    let active = &layer.shells[..use_shells];
    let axis = layer.scale_axis;

    // Precompute tiny activation LUTs for each of the K tokens
    let all_luts: Vec<Vec<(f32, Vec<[i8; 16]>)>> = xs
        .iter()
        .map(|x| {
            if axis == ScaleAxis::PerIc {
                active
                    .iter()
                    .map(|sh| {
                        let x_eff: Vec<f32> = x
                            .iter()
                            .zip(sh.scales.iter())
                            .map(|(&xv, &sv)| xv * sv)
                            .collect();
                        build_group_luts_i8(&x_eff)
                    })
                    .collect()
            } else {
                vec![build_group_luts_i8(x)]
            }
        })
        .collect();

    // Flat buffer `[row_blocks, k_len, 32]` so each Rayon task writes disjoint memory
    let mut flat = vec![0.0_f32; row_blocks * k_len * 32];
    flat.par_chunks_mut(k_len * 32)
        .enumerate()
        .for_each(|(rb, dst_rb)| {
            for (s_idx, sh) in active.iter().enumerate() {
                let slice = &sh.packed_col_major[rb * packed_pairs..(rb + 1) * packed_pairs];
                for (t, tok_luts) in all_luts.iter().enumerate() {
                    let (act_scale, group_luts) = if axis == ScaleAxis::PerIc {
                        (tok_luts[s_idx].0, &tok_luts[s_idx].1)
                    } else {
                        (tok_luts[0].0, &tok_luts[0].1)
                    };
                    let mut acc = [0i32; 32];
                    tmac_gemv_32rows_packed4(slice, group_luts, &mut acc);
                    let dst = &mut dst_rb[t * 32..(t + 1) * 32];
                    match axis {
                        ScaleAxis::Scalar => {
                            let mul = act_scale * sh.scales[0];
                            for r in 0..32 {
                                dst[r] += (acc[r] as f32) * mul;
                            }
                        }
                        ScaleAxis::PerOc => {
                            let base = rb * 32;
                            for r in 0..32 {
                                dst[r] += (acc[r] as f32) * act_scale * sh.scales[base + r];
                            }
                        }
                        ScaleAxis::PerIc => {
                            for r in 0..32 {
                                dst[r] += (acc[r] as f32) * act_scale;
                            }
                        }
                    }
                }
            }
        });

    let mut outs = vec![vec![0.0_f32; layer.rows]; k_len];
    for rb in 0..row_blocks {
        let rb_off = rb * k_len * 32;
        for t in 0..k_len {
            let src = &flat[rb_off + t * 32..rb_off + (t + 1) * 32];
            outs[t][rb * 32..(rb + 1) * 32].copy_from_slice(src);
        }
    }
    outs
}

// ---------------------------------------------------------------------------
// Standalone Byte-Level BPE + Sutra Logoglyph Tokenizer
// ---------------------------------------------------------------------------

struct BpeTokenizer {
    vocab: HashMap<String, u32>,
    id_to_piece: Vec<String>,
    special_tokens: Vec<(String, u32)>,
    merges: HashMap<(String, String), usize>,
    byte_to_unicode: [char; 256],
    unicode_to_byte: HashMap<char, u8>,
}

impl BpeTokenizer {
    fn from_json_bytes(raw: &[u8]) -> Self {
        let val: Value = serde_json::from_slice(raw).expect("parse tokenizer json");
        let mut bs: Vec<u8> = Vec::new();
        for b in b'!'..=b'~' {
            bs.push(b);
        }
        for b in 0xA1u8..=0xACu8 {
            bs.push(b);
        }
        for b in 0xAEu8..=0xFFu8 {
            bs.push(b);
        }
        let mut cs: Vec<u32> = bs.iter().map(|&b| u32::from(b)).collect();
        let mut n = 0u32;
        for b in 0u8..=255u8 {
            if !bs.contains(&b) {
                bs.push(b);
                cs.push(256 + n);
                n += 1;
            }
        }
        let mut b2u = ['\0'; 256];
        let mut u2b = HashMap::with_capacity(256);
        for (&b, &c) in bs.iter().zip(cs.iter()) {
            let ch = char::from_u32(c).unwrap_or('?');
            b2u[b as usize] = ch;
            u2b.insert(ch, b);
        }

        let mut vocab = HashMap::new();
        let mut max_id = 0usize;
        if let Some(obj) = val
            .get("model")
            .and_then(|m| m.get("vocab"))
            .and_then(Value::as_object)
        {
            for (k, v) in obj {
                if let Some(id) = v.as_u64() {
                    vocab.insert(k.clone(), id as u32);
                    max_id = max_id.max(id as usize);
                }
            }
        }
        let mut special_tokens = Vec::new();
        if let Some(arr) = val.get("added_tokens").and_then(Value::as_array) {
            for item in arr {
                if let (Some(content), Some(id)) = (
                    item.get("content").and_then(Value::as_str),
                    item.get("id").and_then(Value::as_u64),
                ) {
                    vocab.insert(content.to_string(), id as u32);
                    special_tokens.push((content.to_string(), id as u32));
                    max_id = max_id.max(id as usize);
                }
            }
        }
        special_tokens.sort_by(|a, b| b.0.len().cmp(&a.0.len()));

        let mut id_to_piece = vec![String::new(); max_id + 1];
        for (k, &id) in &vocab {
            if (id as usize) < id_to_piece.len() {
                id_to_piece[id as usize] = k.clone();
            }
        }

        let mut merges = HashMap::new();
        if let Some(arr) = val
            .get("model")
            .and_then(|m| m.get("merges"))
            .and_then(Value::as_array)
        {
            for (rank, m) in arr.iter().enumerate() {
                if let Some(s) = m.as_str()
                    && let Some((a, b)) = s.split_once(' ')
                {
                    merges.insert((a.to_string(), b.to_string()), rank);
                } else if let Some(pair) = m.as_array()
                    && pair.len() == 2
                    && let (Some(a), Some(b)) = (pair[0].as_str(), pair[1].as_str())
                {
                    merges.insert((a.to_string(), b.to_string()), rank);
                }
            }
        }

        Self {
            vocab,
            id_to_piece,
            special_tokens,
            merges,
            byte_to_unicode: b2u,
            unicode_to_byte: u2b,
        }
    }

    fn bpe_encode_chunk(&self, text: &str, out: &mut Vec<u32>) {
        let bytes = text.as_bytes();
        let mut i = 0;
        let unk = *self.vocab.get("[UNK]").unwrap_or(&3);
        while i < bytes.len() {
            let start = i;
            if bytes[i] == b' ' {
                i += 1;
            }
            if i < bytes.len() {
                let first_is_alnum = bytes[i].is_ascii_alphanumeric();
                while i < bytes.len()
                    && bytes[i] != b' '
                    && (bytes[i].is_ascii_alphanumeric() == first_is_alnum)
                {
                    i += 1;
                }
            }
            let mut pieces: Vec<String> = bytes[start..i]
                .iter()
                .map(|&b| self.byte_to_unicode[b as usize].to_string())
                .collect();
            while pieces.len() > 1 {
                let mut best_rank = usize::MAX;
                let mut best_idx = None;
                for p in 0..(pieces.len() - 1) {
                    if let Some(&r) = self.merges.get(&(pieces[p].clone(), pieces[p + 1].clone()))
                        && r < best_rank
                    {
                        best_rank = r;
                        best_idx = Some(p);
                    }
                }
                let Some(bi) = best_idx else { break };
                let merged = format!("{}{}", pieces[bi], pieces[bi + 1]);
                pieces.splice(bi..=bi + 1, std::iter::once(merged));
            }
            for pc in pieces {
                out.push(*self.vocab.get(&pc).unwrap_or(&unk));
            }
        }
    }

    fn encode(&self, text: &str) -> Vec<u32> {
        let mut out = Vec::new();
        let mut rem = text;
        while !rem.is_empty() {
            let mut earliest: Option<(usize, &str, u32)> = None;
            for (sp, id) in &self.special_tokens {
                if let Some(pos) = rem.find(sp.as_str())
                    && earliest.is_none_or(|e| pos < e.0)
                {
                    earliest = Some((pos, sp.as_str(), *id));
                }
            }
            match earliest {
                Some((pos, sp, id)) => {
                    if pos > 0 {
                        self.bpe_encode_chunk(&rem[..pos], &mut out);
                    }
                    out.push(id);
                    rem = &rem[pos + sp.len()..];
                }
                None => {
                    self.bpe_encode_chunk(rem, &mut out);
                    break;
                }
            }
        }
        out
    }

    fn decode(&self, ids: &[u32]) -> String {
        let mut raw_bytes = Vec::new();
        let mut out = String::new();
        for &id in ids {
            let piece = self
                .id_to_piece
                .get(id as usize)
                .map(String::as_str)
                .unwrap_or("");
            if self.special_tokens.iter().any(|(s, _)| s == piece) {
                if !raw_bytes.is_empty() {
                    out.push_str(&String::from_utf8_lossy(&raw_bytes));
                    raw_bytes.clear();
                }
                out.push_str(piece);
            } else {
                for ch in piece.chars() {
                    if let Some(&b) = self.unicode_to_byte.get(&ch) {
                        raw_bytes.push(b);
                    } else {
                        if !raw_bytes.is_empty() {
                            out.push_str(&String::from_utf8_lossy(&raw_bytes));
                            raw_bytes.clear();
                        }
                        out.push(ch);
                    }
                }
            }
        }
        if !raw_bytes.is_empty() {
            out.push_str(&String::from_utf8_lossy(&raw_bytes));
        }
        out
    }
}

// ---------------------------------------------------------------------------
// TM7B\x03 Loader
// ---------------------------------------------------------------------------

struct RefCheckV3 {
    pids: Vec<u32>,
    mac_6d: Vec<f32>,
    ref_logits: Vec<f32>,
}

struct MhcCorePrecomputed {
    h_mix: [f32; 16],
    a_in: [f32; 4],
    b_out: [f32; 4],
    /// Optional TM7B\x04 per-stream depthwise basis gates: `[N_STREAMS * d_model]`
    stream_in_gate: Option<Vec<f32>>,
    stream_out_gate: Option<Vec<f32>>,
}

struct StiefelSidecar {
    d_out: usize,
    d_in: usize,
    rank_total: usize,
    s_fp32: Vec<f32>,
    u_scale: Vec<f32>,
    v_scale: Vec<f32>,
    u_i8: Vec<i8>, // [rank_total, d_out] col-major contiguous
    v_i8: Vec<i8>, // [rank_total, d_in] col-major contiguous
}

fn load_ns3s_sidecar(path: &str) -> (usize, HashMap<String, StiefelSidecar>) {
    let f = File::open(path).expect("open ns3s sidecar");
    let mut r = BufReader::new(f);
    let mut magic = [0u8; 8];
    r.read_exact(&mut magic).expect("read ns3s magic");
    assert_eq!(&magic, NS3S_MAGIC_V1, "unsupported NS3S sidecar magic");
    let n_vaults = stream_u32_le(&mut r) as usize;
    let n_layers = stream_u32_le(&mut r) as usize;
    let mut map = HashMap::with_capacity(n_layers);
    for _ in 0..n_layers {
        let nlen = stream_u32_le(&mut r) as usize;
        let mut nb = vec![0u8; nlen];
        r.read_exact(&mut nb).expect("read sidecar layer name");
        let name = String::from_utf8(nb).expect("sidecar name utf8");
        let d_out = stream_u32_le(&mut r) as usize;
        let d_in = stream_u32_le(&mut r) as usize;
        let rank_total = stream_u32_le(&mut r) as usize;
        let s_fp32 = stream_f32_vec(&mut r, rank_total);
        let u_scale = stream_f32_vec(&mut r, rank_total);
        let v_scale = stream_f32_vec(&mut r, rank_total);
        let mut u_raw = vec![0u8; rank_total * d_out];
        r.read_exact(&mut u_raw).expect("read sidecar u_i8");
        let u_i8: Vec<i8> = u_raw.into_iter().map(|b| b as i8).collect();
        let mut v_raw = vec![0u8; rank_total * d_in];
        r.read_exact(&mut v_raw).expect("read sidecar v_i8");
        let v_i8: Vec<i8> = v_raw.into_iter().map(|b| b as i8).collect();
        map.insert(
            name,
            StiefelSidecar {
                d_out,
                d_in,
                rank_total,
                s_fp32,
                u_scale,
                v_scale,
                u_i8,
                v_i8,
            },
        );
    }
    (n_vaults, map)
}

#[inline]
fn apply_stiefel_sidecar(sc: &StiefelSidecar, x: &[f32], out: &mut [f32]) {
    debug_assert_eq!(x.len(), sc.d_in);
    debug_assert_eq!(out.len(), sc.d_out);
    for k in 0..sc.rank_total {
        let v_row = &sc.v_i8[k * sc.d_in..(k + 1) * sc.d_in];
        let mut dot = 0.0_f32;
        for (&xv, &vi) in x.iter().zip(v_row.iter()) {
            dot += xv * f32::from(vi);
        }
        let coeff = dot * sc.v_scale[k] * sc.s_fp32[k] * sc.u_scale[k];
        if coeff.abs() > 1e-12 {
            let u_row = &sc.u_i8[k * sc.d_out..(k + 1) * sc.d_out];
            for (o, &ui) in out.iter_mut().zip(u_row.iter()) {
                *o += coeff * f32::from(ui);
            }
        }
    }
}

/// Load and stack one or more Composable Soft-Prompt Distillation (`.cspd`) cartridges
/// (`CSPD\x01\x00\x00\x00` + `n_tokens: u32` + `d_model: u32` + `n_tokens * d_model` le f32).
fn load_cspd_soft_prompts(paths_csv: &str, expected_d_model: usize) -> Vec<Vec<f32>> {
    let mut all_vecs = Vec::new();
    for raw_p in paths_csv.split(',') {
        let p = raw_p.trim();
        if p.is_empty() {
            continue;
        }
        let raw = fs::read(p).expect("read .cspd soft-prompt cartridge");
        assert!(
            raw.len() >= 16 && &raw[0..8] == CSPD_MAGIC_V1,
            "invalid CSPD\\x01 magic in {p}"
        );
        let mut pos = 8usize;
        let n_tok = read_u32_le(&raw, &mut pos) as usize;
        let d_mod = read_u32_le(&raw, &mut pos) as usize;
        assert_eq!(
            d_mod, expected_d_model,
            "CSPD d_model ({d_mod}) != bundle d_model ({expected_d_model})"
        );
        assert!(
            raw.len() >= 16 + n_tok * d_mod * 4,
            "truncated CSPD cartridge {p}"
        );
        for _ in 0..n_tok {
            let mut v = Vec::with_capacity(d_mod);
            for _ in 0..d_mod {
                v.push(read_f32_le(&raw, &mut pos));
            }
            all_vecs.push(v);
        }
    }
    all_vecs
}

/// Tier-0 Zero-Cost Prompt/History N-Gram Lookahead Drafter (`< 2 us`, 0 neural FLOPs!).
fn draft_ngram_lookahead(
    history: &[u32],
    t0_tok: u32,
    max_k: usize,
    stop_on_eos: bool,
    eos_id: Option<u32>,
) -> Vec<u32> {
    let n = history.len();
    if n < 3 || max_k == 0 {
        return Vec::new();
    }
    let a2 = history[n - 2];
    let a1 = history[n - 1];
    for idx in (2..n).rev() {
        if history[idx - 2] == a2 && history[idx - 1] == a1 && history[idx] == t0_tok {
            let mut out = Vec::with_capacity(max_k);
            for k in 1..=max_k {
                let src_i = idx + k;
                let tok = if src_i < n {
                    history[src_i]
                } else if src_i == n {
                    t0_tok
                } else {
                    break;
                };
                out.push(tok);
                if stop_on_eos && eos_id == Some(tok) {
                    break;
                }
            }
            if !out.is_empty() {
                return out;
            }
        }
    }
    Vec::new()
}

struct MuninnBundle {
    cfg: ModelConfig,
    tok: BpeTokenizer,
    floats: HashMap<String, Vec<f32>>,
    floats_tmac: HashMap<String, TernaryLayer>,
    ternary: HashMap<String, TernaryLayer>,
    engram_table_i8: Vec<i8>,
    sidecars: HashMap<String, StiefelSidecar>,
    refs: Vec<RefCheckV3>,
    mhc_core: Vec<MhcCorePrecomputed>,
}

fn build_draft_tmac_mirrors(b: &mut MuninnBundle) {
    if !b.floats_tmac.is_empty() {
        return;
    }
    let d = b.cfg.d_model;
    let dff = b.cfg.d_ff;
    let mut specs: Vec<(String, usize, usize)> = vec![
        ("tok_emb.weight".to_string(), b.cfg.vocab_size, d),
        ("prelude.0.wq.weight".to_string(), d, d),
        ("prelude.0.wk.weight".to_string(), d, d),
        ("prelude.0.wv.weight".to_string(), d, d),
        ("prelude.0.wo.weight".to_string(), d, d),
        ("prelude.0.w_up.weight".to_string(), dff, d),
        ("prelude.0.w_down.weight".to_string(), d, dff),
        ("coda.1.wq.weight".to_string(), d, d),
        ("coda.1.wk.weight".to_string(), d, d),
        ("coda.1.wv.weight".to_string(), d, d),
        ("coda.1.wo.weight".to_string(), d, d),
        ("coda.1.w_up.weight".to_string(), dff, d),
        ("coda.1.w_down.weight".to_string(), d, dff),
    ];
    for i in 0..b.cfg.n_core_blocks {
        specs.push((format!("core.{i}.w_write.weight"), d, d));
    }
    for (name, rows, cols) in specs {
        if let Some(w) = b.floats.get(&name)
            && w.len() == rows * cols
            && rows % 32 == 0
            && cols % 2 == 0
        {
            b.floats_tmac
                .insert(name, quantize_f32_to_tmac_layer(w, rows, cols));
        }
    }
}

/// Streaming, lazy per-shell `TM7B\x03 / TM7B\x04 / TM7B\x05` loader (`#![forbid(unsafe_code)]`).
/// Avoids buffering the entire `.tm7b` file in RAM (`fs::read` eliminated) and skips unused
/// shells (`s >= max_shells_opt`) via `SeekFrom::Current` with zero disk read or packing cost.
fn load_tm7b_v3(path: &str, max_shells_opt: Option<usize>) -> MuninnBundle {
    let file = File::open(path).expect("open TM7B v3/v4/v5 bundle");
    let mut reader = BufReader::with_capacity(1 << 20, file);
    let mut magic = [0u8; 8];
    reader.read_exact(&mut magic).expect("read TM7B magic");
    let bundle_version = if &magic == TM7B_MAGIC_V6 {
        6u8
    } else if &magic == TM7B_MAGIC_V5 {
        5u8
    } else if &magic == TM7B_MAGIC_V4 {
        4u8
    } else if &magic == TM7B_MAGIC_V3 {
        3u8
    } else {
        panic!("invalid TM7B\\x03, TM7B\\x04, TM7B\\x05, or TM7B\\x06 magic");
    };
    let vocab_size = stream_u32_le(&mut reader) as usize;
    let d_model = stream_u32_le(&mut reader) as usize;
    let n_heads = stream_u32_le(&mut reader) as usize;
    let d_ff = stream_u32_le(&mut reader) as usize;
    let n_core_blocks = stream_u32_le(&mut reader) as usize;
    let n_shells = stream_u32_le(&mut reader) as usize;
    let engram_slots = stream_u32_le(&mut reader) as usize;
    let engram_dim = stream_u32_le(&mut reader) as usize;
    let mac_dim = stream_u32_le(&mut reader) as usize;
    let core_delta_mask = stream_u32_le(&mut reader);
    let n_floats = stream_u32_le(&mut reader) as usize;
    let n_ternary = stream_u32_le(&mut reader) as usize;

    let tok_len = stream_u32_le(&mut reader) as usize;
    let mut tok_bytes = vec![0u8; tok_len];
    reader.read_exact(&mut tok_bytes).expect("read tokenizer");
    let tok = BpeTokenizer::from_json_bytes(&tok_bytes);
    drop(tok_bytes);

    let eng_len = engram_slots * engram_dim;
    let mut eng_raw = vec![0u8; eng_len];
    reader.read_exact(&mut eng_raw).expect("read engram table");
    let engram_table_i8: Vec<i8> = eng_raw.into_iter().map(|u| u as i8).collect();

    let mut floats = HashMap::with_capacity(n_floats);
    for _ in 0..n_floats {
        let nlen = stream_u32_le(&mut reader) as usize;
        let mut nbuf = vec![0u8; nlen];
        reader.read_exact(&mut nbuf).expect("read float name");
        let name = String::from_utf8(nbuf).expect("utf8 float name");
        let ndim = stream_u32_le(&mut reader) as usize;
        let mut total = 1usize;
        for _ in 0..ndim {
            total *= stream_u32_le(&mut reader) as usize;
        }
        let vec = stream_f32_vec(&mut reader, total);
        floats.insert(name, vec);
    }

    let mut ternary = HashMap::with_capacity(n_ternary);
    let mut raw_u8_scratch: Vec<u8> = Vec::new();
    let mut q_i8_scratch: Vec<i8> = Vec::new();
    for _ in 0..n_ternary {
        let nlen = stream_u32_le(&mut reader) as usize;
        let mut nbuf = vec![0u8; nlen];
        reader.read_exact(&mut nbuf).expect("read ternary name");
        let name = String::from_utf8(nbuf).expect("utf8 ternary name");
        let rows = stream_u32_le(&mut reader) as usize;
        let cols = stream_u32_le(&mut reader) as usize;
        let sh_cnt = stream_u32_le(&mut reader) as usize;
        let scale_axis = if bundle_version >= 5 {
            match stream_u32_le(&mut reader) {
                1 => ScaleAxis::PerOc,
                2 => ScaleAxis::PerIc,
                _ => ScaleAxis::Scalar,
            }
        } else {
            ScaleAxis::Scalar
        };
        let keep_shells = max_shells_opt.map(|m| m.clamp(1, sh_cnt)).unwrap_or(sh_cnt);
        let n_elems = rows * cols;
        if raw_u8_scratch.len() < n_elems {
            raw_u8_scratch.resize(n_elems, 0);
            q_i8_scratch.resize(n_elems, 0);
        }
        let mut shells = Vec::with_capacity(keep_shells);
        for s_idx in 0..sh_cnt {
            if bundle_version >= 5 {
                let sc_len = stream_u32_le(&mut reader) as usize;
                if s_idx < keep_shells {
                    let scales = stream_f32_vec(&mut reader, sc_len);
                    reader
                        .read_exact(&mut raw_u8_scratch[..n_elems])
                        .expect("read ternary shell v5");
                    for (dst, &u) in q_i8_scratch[..n_elems]
                        .iter_mut()
                        .zip(raw_u8_scratch[..n_elems].iter())
                    {
                        *dst = u as i8;
                    }
                    let packed = pack_ternary_for_tmac(rows, cols, &q_i8_scratch[..n_elems]);
                    shells.push(TernaryShell {
                        scales,
                        packed_col_major: packed,
                    });
                } else {
                    reader
                        .seek(SeekFrom::Current((sc_len * 4 + n_elems) as i64))
                        .expect("seek past inactive ternary shell v5");
                }
            } else if s_idx < keep_shells {
                let scale = stream_f32_le(&mut reader);
                reader
                    .read_exact(&mut raw_u8_scratch[..n_elems])
                    .expect("read ternary shell");
                for (dst, &u) in q_i8_scratch[..n_elems]
                    .iter_mut()
                    .zip(raw_u8_scratch[..n_elems].iter())
                {
                    *dst = u as i8;
                }
                let packed = pack_ternary_for_tmac(rows, cols, &q_i8_scratch[..n_elems]);
                shells.push(TernaryShell {
                    scales: vec![scale],
                    packed_col_major: packed,
                });
            } else {
                // Lazy per-shell skip: zero disk read, zero RAM allocation, zero packing!
                reader
                    .seek(SeekFrom::Current((4 + n_elems) as i64))
                    .expect("seek past inactive ternary shell");
            }
        }
        ternary.insert(
            name,
            TernaryLayer {
                rows,
                cols,
                scale_axis,
                shells,
                override_f32: None,
            },
        );
    }
    drop(raw_u8_scratch);
    drop(q_i8_scratch);

    let n_refs = stream_u32_le(&mut reader) as usize;
    let mut refs = Vec::with_capacity(n_refs);
    for _ in 0..n_refs {
        let np = stream_u32_le(&mut reader) as usize;
        let mut pids = Vec::with_capacity(np);
        for _ in 0..np {
            pids.push(stream_u32_le(&mut reader));
        }
        let mac_6d = stream_f32_vec(&mut reader, mac_dim);
        let nl = stream_u32_le(&mut reader) as usize;
        let ref_logits = stream_f32_vec(&mut reader, nl);
        refs.push(RefCheckV3 {
            pids,
            mac_6d,
            ref_logits,
        });
    }
    drop(reader);

    // Precompute mHC Sinkhorn matrices & optional TM7B\x04 per-stream depthwise basis gates once at load time
    let mut mhc_core = Vec::with_capacity(n_core_blocks);
    for b_idx in 0..n_core_blocks {
        let h_mix = sinkhorn_4x4(&floats[&format!("mhc_logits.{b_idx}")]);
        let a_in = softmax_4(&floats[&format!("h_pre.{b_idx}")]);
        let mut b_out = softmax_4(&floats[&format!("h_post.{b_idx}")]);
        for bo in &mut b_out {
            *bo *= N_STREAMS as f32;
        }
        let stream_in_gate = floats.get(&format!("stream_in_gate.{b_idx}")).cloned();
        let stream_out_gate = floats.get(&format!("stream_out_gate.{b_idx}")).cloned();
        mhc_core.push(MhcCorePrecomputed {
            h_mix,
            a_in,
            b_out,
            stream_in_gate,
            stream_out_gate,
        });
    }

    MuninnBundle {
        cfg: ModelConfig {
            bundle_version,
            vocab_size,
            d_model,
            n_heads,
            head_dim: d_model / n_heads,
            d_ff,
            n_core_blocks,
            n_shells,
            engram_slots,
            engram_dim,
            mac_dim,
            core_delta_mask,
        },
        tok,
        floats,
        floats_tmac: HashMap::new(),
        ternary,
        engram_table_i8,
        sidecars: HashMap::new(),
        refs,
        mhc_core,
    }
}

// ---------------------------------------------------------------------------
// Math Primitives & O(1) Phase-Bound Gated DeltaNet-2 Safe-Rust State
// ---------------------------------------------------------------------------

#[inline]
fn sigmoid(x: f32) -> f32 {
    1.0 / (1.0 + (-x).exp())
}

fn rms_norm(x: &[f32], w: &[f32]) -> Vec<f32> {
    let sq: f32 = x.iter().map(|&v| v * v).sum::<f32>() / (x.len() as f32);
    let inv = (sq + 1e-6).sqrt().recip();
    x.iter().zip(w.iter()).map(|(&a, &b)| a * inv * b).collect()
}

fn linear_vec(arch: Arch, w: &[f32], x: &[f32], out_f: usize, in_f: usize) -> Vec<f32> {
    let mut out = vec![0.0_f32; out_f];
    if out_f >= 1024 {
        out.par_iter_mut().enumerate().for_each(|(r, dst)| {
            *dst = dot_f32(arch, &w[r * in_f..(r + 1) * in_f], x);
        });
    } else {
        for r in 0..out_f {
            out[r] = dot_f32(arch, &w[r * in_f..(r + 1) * in_f], x);
        }
    }
    out
}

fn linear_vec_bias(
    arch: Arch,
    w: &[f32],
    b: &[f32],
    x: &[f32],
    out_f: usize,
    in_f: usize,
) -> Vec<f32> {
    let mut out = linear_vec(arch, w, x, out_f, in_f);
    for i in 0..out_f {
        out[i] += b[i];
    }
    out
}

fn apply_rope_pos(v: &mut [f32], pos: usize, n_heads: usize, head_dim: usize) {
    let p = pos as f32;
    for h in 0..n_heads {
        let base = h * head_dim;
        for i in (0..head_dim).step_by(2) {
            let inv_freq = 1.0_f32 / (1_000_000.0_f32.powf((i as f32) / (head_dim as f32)));
            let ang = p * inv_freq;
            let (s, c) = ang.sin_cos();
            let x0 = v[base + i];
            let x1 = v[base + i + 1];
            v[base + i] = x0 * c - x1 * s;
            v[base + i + 1] = x0 * s + x1 * c;
        }
    }
}

fn sinkhorn_4x4(logits: &[f32]) -> [f32; 16] {
    let mut m = [0.0_f32; 16];
    for r in 0..4 {
        let mut mx = f32::NEG_INFINITY;
        for c in 0..4 {
            mx = mx.max(logits[r * 4 + c]);
        }
        for c in 0..4 {
            m[r * 4 + c] = (logits[r * 4 + c] - mx).exp();
        }
    }
    for _ in 0..12 {
        for r in 0..4 {
            let s = (m[r * 4] + m[r * 4 + 1] + m[r * 4 + 2] + m[r * 4 + 3]).max(1e-6);
            for c in 0..4 {
                m[r * 4 + c] /= s;
            }
        }
        for c in 0..4 {
            let s = (m[c] + m[4 + c] + m[8 + c] + m[12 + c]).max(1e-6);
            for r in 0..4 {
                m[r * 4 + c] /= s;
            }
        }
    }
    m
}

fn softmax_4(v: &[f32]) -> [f32; 4] {
    let mx = v[0].max(v[1]).max(v[2]).max(v[3]);
    let mut e = [
        (v[0] - mx).exp(),
        (v[1] - mx).exp(),
        (v[2] - mx).exp(),
        (v[3] - mx).exp(),
    ];
    let s = (e[0] + e[1] + e[2] + e[3]).max(1e-6);
    for x in &mut e {
        *x /= s;
    }
    e
}

#[inline]
fn apply_clifford_wedge(dst: &mut [f32], fn_in: &[f32], ffn_out: &[f32], c_alpha: f32) {
    let half = dst.len() / 2;
    for i in 0..half {
        let u1 = fn_in[2 * i];
        let u2 = fn_in[2 * i + 1];
        let v1 = ffn_out[2 * i];
        let v2 = ffn_out[2 * i + 1];
        let w_pos = u1 * v2 - u2 * v1;
        dst[i] += ffn_out[i] + c_alpha * w_pos;
        dst[half + i] += ffn_out[half + i] - c_alpha * w_pos;
    }
}

#[inline]
fn cosine_sim(a: &[f32], b: &[f32]) -> f64 {
    let (mut dot, mut na, mut nb) = (0.0_f64, 0.0_f64, 0.0_f64);
    for (&x, &y) in a.iter().zip(b.iter()) {
        let xf = f64::from(x);
        let yf = f64::from(y);
        dot += xf * yf;
        na += xf * xf;
        nb += yf * yf;
    }
    dot / (na.sqrt() * nb.sqrt()).max(1e-12)
}

#[derive(Clone)]
enum BlockState {
    AttnKv {
        k: Vec<f32>,
        v: Vec<f32>,
    },
    /// O(1) Holographic Associative Memory State `S_t` of shape `[n_heads, head_dim, head_dim]`
    /// plus `TM7B\x06` causal depthwise `short_conv1d(k=4)` history `[3, d_model]` for q, k, v.
    DeltaHologram {
        s_mat: Vec<f32>,
        conv_q: Vec<f32>,
        conv_k: Vec<f32>,
        conv_v: Vec<f32>,
    },
}

impl BlockState {
    fn new_attn(d_model: usize, max_len: usize) -> Self {
        Self::AttnKv {
            k: Vec::with_capacity(max_len * d_model),
            v: Vec::with_capacity(max_len * d_model),
        }
    }
    fn new_delta(n_heads: usize, head_dim: usize) -> Self {
        let d_model = n_heads * head_dim;
        Self::DeltaHologram {
            s_mat: vec![0.0_f32; n_heads * head_dim * head_dim],
            conv_q: vec![0.0_f32; 3 * d_model],
            conv_k: vec![0.0_f32; 3 * d_model],
            conv_v: vec![0.0_f32; 3 * d_model],
        }
    }
}

#[derive(Clone)]
struct MuninnRuntimeState {
    prelude: [BlockState; 2],
    core: Vec<BlockState>,
    coda: [BlockState; 2],
    r_loops: usize,
    pos: usize,
    energy_halt_tau: Option<f32>,
    energy_halt_active: bool,
    halted_r2_tokens: usize,
    full_r_tokens: usize,
    energy_sum: f64,
}

struct PrefixCacheEntry {
    r_loops: usize,
    shells: usize,
    mac_bits: Vec<u32>,
    prefix_toks: Vec<u32>,
    rt_snap: MuninnRuntimeState,
}

struct UnifiedRadixCache {
    entries: Vec<PrefixCacheEntry>,
    hits: usize,
    misses: usize,
    saved_prefill_tokens: usize,
}

impl UnifiedRadixCache {
    fn new() -> Self {
        Self {
            entries: Vec::with_capacity(24),
            hits: 0,
            misses: 0,
            saved_prefill_tokens: 0,
        }
    }

    fn lookup(
        &mut self,
        r_loops: usize,
        shells: usize,
        mac_bits: &[u32],
        gen_ids: &[u32],
    ) -> Option<(usize, MuninnRuntimeState)> {
        let prompt_len = gen_ids.len();
        let mut best_idx: Option<usize> = None;
        let mut best_len = 0usize;
        for (idx, ent) in self.entries.iter().enumerate() {
            let plen = ent.prefix_toks.len();
            if ent.r_loops == r_loops
                && ent.shells == shells
                && ent.mac_bits == mac_bits
                && plen > best_len
                && prompt_len > plen
                && gen_ids.starts_with(&ent.prefix_toks)
            {
                best_len = plen;
                best_idx = Some(idx);
            }
        }
        if let Some(i) = best_idx {
            self.hits += 1;
            self.saved_prefill_tokens += best_len;
            Some((best_len, self.entries[i].rt_snap.clone()))
        } else {
            self.misses += 1;
            None
        }
    }

    fn insert(
        &mut self,
        r_loops: usize,
        shells: usize,
        mac_bits: &[u32],
        prefix_toks: &[u32],
        rt_snap: &MuninnRuntimeState,
    ) {
        if self.entries.iter().any(|e| {
            e.r_loops == r_loops
                && e.shells == shells
                && e.mac_bits == mac_bits
                && e.prefix_toks == prefix_toks
        }) {
            return;
        }
        if self.entries.len() >= 24 {
            self.entries.remove(0);
        }
        self.entries.push(PrefixCacheEntry {
            r_loops,
            shells,
            mac_bits: mac_bits.to_vec(),
            prefix_toks: prefix_toks.to_vec(),
            rt_snap: rt_snap.clone(),
        });
    }
}

impl MuninnRuntimeState {
    fn new(cfg: &ModelConfig, r_loops: usize, max_len: usize) -> Self {
        let mut core = Vec::with_capacity(r_loops * cfg.n_core_blocks);
        for _ in 0..r_loops {
            for b_idx in 0..cfg.n_core_blocks {
                let is_delta = ((cfg.core_delta_mask >> b_idx) & 1) != 0;
                if is_delta {
                    core.push(BlockState::new_delta(cfg.n_heads, cfg.head_dim));
                } else {
                    core.push(BlockState::new_attn(cfg.d_model, max_len));
                }
            }
        }
        Self {
            prelude: [
                BlockState::new_attn(cfg.d_model, max_len),
                BlockState::new_attn(cfg.d_model, max_len),
            ],
            core,
            coda: [
                BlockState::new_attn(cfg.d_model, max_len),
                BlockState::new_attn(cfg.d_model, max_len),
            ],
            r_loops,
            pos: 0,
            energy_halt_tau: None,
            energy_halt_active: false,
            halted_r2_tokens: 0,
            full_r_tokens: 0,
            energy_sum: 0.0,
        }
    }

    /// Persist all `O(1)` Gated DeltaNet-2 holographic working-memory matrices `S_t` to disk (`.hms`).
    fn save_hologram(&self, path: &str, cfg: &ModelConfig) -> usize {
        let delta_mats: Vec<&[f32]> = self
            .core
            .iter()
            .filter_map(|st| match st {
                BlockState::DeltaHologram { s_mat, .. } => Some(s_mat.as_slice()),
                BlockState::AttnKv { .. } => None,
            })
            .collect();
        let n_delta = delta_mats.len();
        let elems_per = cfg.n_heads * cfg.head_dim * cfg.head_dim;
        let mut out = Vec::with_capacity(24 + n_delta * elems_per * 4);
        out.extend_from_slice(HMS_MAGIC_V1);
        out.extend_from_slice(&(self.r_loops as u32).to_le_bytes());
        out.extend_from_slice(&(n_delta as u32).to_le_bytes());
        out.extend_from_slice(&(cfg.n_heads as u32).to_le_bytes());
        out.extend_from_slice(&(cfg.head_dim as u32).to_le_bytes());
        for mat in delta_mats {
            for &v in mat {
                out.extend_from_slice(&v.to_le_bytes());
            }
        }
        fs::write(path, &out).expect("write .hms hologram state");
        out.len()
    }

    /// Restore persisted `Gated DeltaNet-2` working-memory hologram matrices `S_t` from `.hms`.
    fn load_hologram(&mut self, path: &str, cfg: &ModelConfig) -> usize {
        let raw = fs::read(path).expect("read .hms hologram state");
        assert!(
            raw.len() >= 24 && &raw[0..8] == HMS_MAGIC_V1,
            "invalid HMS1 hologram magic"
        );
        let mut pos = 8usize;
        let r_loops = read_u32_le(&raw, &mut pos) as usize;
        let n_delta = read_u32_le(&raw, &mut pos) as usize;
        let n_heads = read_u32_le(&raw, &mut pos) as usize;
        let head_dim = read_u32_le(&raw, &mut pos) as usize;
        assert_eq!(r_loops, self.r_loops, "HMS r_loops mismatch");
        assert_eq!(n_heads, cfg.n_heads, "HMS n_heads mismatch");
        assert_eq!(head_dim, cfg.head_dim, "HMS head_dim mismatch");
        let elems_per = n_heads * head_dim * head_dim;
        let mut loaded = 0usize;
        for st in &mut self.core {
            if let BlockState::DeltaHologram {
                s_mat,
                conv_q,
                conv_k,
                conv_v,
            } = st
            {
                assert_eq!(s_mat.len(), elems_per);
                for dst in s_mat.iter_mut() {
                    *dst = read_f32_le(&raw, &mut pos);
                }
                conv_q.fill(0.0);
                conv_k.fill(0.0);
                conv_v.fill(0.0);
                loaded += 1;
            }
        }
        assert_eq!(loaded, n_delta, "HMS delta layer count mismatch");
        raw.len()
    }

    /// Snapshot only the first `max_slots` `O(1)` DeltaNet-2 recurrent matrices + `k=4` conv ring buffers.
    fn snapshot_deltas_slots(&self, max_slots: usize) -> DeltaSnapshot {
        let mut core_deltas = Vec::with_capacity(max_slots.min(self.core.len()));
        for (slot, st) in self.core.iter().enumerate().take(max_slots) {
            if let BlockState::DeltaHologram {
                s_mat,
                conv_q,
                conv_k,
                conv_v,
            } = st
            {
                core_deltas.push((
                    slot,
                    s_mat.clone(),
                    conv_q.clone(),
                    conv_k.clone(),
                    conv_v.clone(),
                ));
            }
        }
        DeltaSnapshot { core_deltas }
    }

    /// Roll back runtime state cleanly to `target_pos` (`O(1)` KV truncate + exact DeltaNet-2/conv restore).
    fn restore_to_pos(&mut self, target_pos: usize, snap: &DeltaSnapshot, d_model: usize) {
        let kv_elems = target_pos * d_model;
        for st in self
            .prelude
            .iter_mut()
            .chain(self.core.iter_mut())
            .chain(self.coda.iter_mut())
        {
            if let BlockState::AttnKv { k, v } = st {
                k.truncate(kv_elems);
                v.truncate(kv_elems);
            }
        }
        for (slot, s_src, cq_src, ck_src, cv_src) in &snap.core_deltas {
            if let BlockState::DeltaHologram {
                s_mat,
                conv_q,
                conv_k,
                conv_v,
            } = &mut self.core[*slot]
            {
                s_mat.copy_from_slice(s_src);
                conv_q.copy_from_slice(cq_src);
                conv_k.copy_from_slice(ck_src);
                conv_v.copy_from_slice(cv_src);
            }
        }
        self.pos = target_pos;
    }
}

#[derive(Clone)]
struct DeltaSnapshot {
    core_deltas: Vec<(usize, Vec<f32>, Vec<f32>, Vec<f32>, Vec<f32>)>,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum LogitsMode {
    None,
    LastOnly,
    All,
}

/// Bit-exact `K`-token batched forward pass (`#![forbid(unsafe_code)]`):
/// Reads every ternary & FP32 weight matrix from DRAM ONCE across all `K` tokens, while executing
/// `short_conv1d(k=4)`, `Gated DeltaNet-2` $S_t$, and causal `AttnKv` sequentially per token
/// for 100% bit-exact parity with `K` serial `forward_step` calls.
/// When `record_snaps` is true, returns per-token `DeltaSnapshot` after each step `0..K` for
/// lossless Matryoshka self-speculative rollback.
#[allow(clippy::too_many_arguments)]
fn forward_batch_k(
    arch: Arch,
    b: &MuninnBundle,
    token_ids: &[u32],
    prev_ids: &[u32],
    mac_6d: Option<&[f32]>,
    state: &mut MuninnRuntimeState,
    active_shells: usize,
    use_tmac_int: bool,
    use_i8_fp32: bool,
    record_snaps: bool,
    logits_mode: LogitsMode,
) -> (Vec<Option<Vec<f32>>>, Vec<DeltaSnapshot>) {
    forward_batch_k_ext(
        arch,
        b,
        token_ids,
        prev_ids,
        None,
        mac_6d,
        state,
        active_shells,
        use_tmac_int,
        use_i8_fp32,
        record_snaps,
        logits_mode,
    )
}

#[allow(clippy::too_many_arguments)]
fn forward_batch_k_ext(
    arch: Arch,
    b: &MuninnBundle,
    token_ids: &[u32],
    prev_ids: &[u32],
    custom_xs: Option<&[Vec<f32>]>,
    mac_6d: Option<&[f32]>,
    state: &mut MuninnRuntimeState,
    active_shells: usize,
    use_tmac_int: bool,
    use_i8_fp32: bool,
    record_snaps: bool,
    logits_mode: LogitsMode,
) -> (Vec<Option<Vec<f32>>>, Vec<DeltaSnapshot>) {
    let k_len = custom_xs.map(|c| c.len()).unwrap_or(token_ids.len());
    if custom_xs.is_none() {
        assert_eq!(k_len, prev_ids.len());
    }
    if k_len == 0 {
        return (Vec::new(), Vec::new());
    }
    let base_pos = state.pos;
    let d_model = b.cfg.d_model;
    let n_heads = b.cfg.n_heads;
    let head_dim = b.cfg.head_dim;
    let d_ff = b.cfg.d_ff;
    let n_core = b.cfg.n_core_blocks;
    let tok_emb = &b.floats["tok_emb.weight"];
    let eng_dim = b.cfg.engram_dim;
    let eng_gate_w = &b.floats["engram_gate.weight"];
    let eng_gate_b = b.floats["engram_gate.bias"][0];
    let eng_proj_w = &b.floats["engram_proj.weight"];

    let mut xs: Vec<Vec<f32>> = if let Some(cxs) = custom_xs {
        cxs.to_vec()
    } else {
        let mut built = Vec::with_capacity(k_len);
        for t in 0..k_len {
            let tid = (token_ids[t] as usize).min(b.cfg.vocab_size - 1);
            let mut x = tok_emb[tid * d_model..(tid + 1) * d_model].to_vec();
            if base_pos + t == 0
                && let Some(mac) = mac_6d
            {
                let mac_w = &b.floats["mac_proj.weight"];
                let mac_g = b.floats["mac_gate"][0].tanh();
                let proj = linear_vec(arch, mac_w, mac, d_model, b.cfg.mac_dim);
                for d in 0..d_model {
                    x[d] += mac_g * proj[d];
                }
            }
            let h_slot = ((u64::from(prev_ids[t]).wrapping_mul(1315423911))
                .wrapping_add(u64::from(token_ids[t]).wrapping_mul(2654435761))
                % (b.cfg.engram_slots as u64)) as usize;
            let alpha_eng = sigmoid(dot_f32(arch, eng_gate_w, &x) + eng_gate_b);
            let eng_slice = &b.engram_table_i8[h_slot * eng_dim..(h_slot + 1) * eng_dim];
            let eng_f: Vec<f32> = eng_slice.iter().map(|&v| f32::from(v) / 64.0).collect();
            let ep = linear_vec(arch, eng_proj_w, &eng_f, d_model, eng_dim);
            for d in 0..d_model {
                x[d] += alpha_eng * ep[d];
            }
            built.push(x);
        }
        built
    };

    let mut snaps: Vec<DeltaSnapshot> = if record_snaps {
        vec![DeltaSnapshot { core_deltas: Vec::new() }; k_len]
    } else {
        Vec::new()
    };

    let mut run_block_batch = |step_xs: &mut Vec<Vec<f32>>,
                               blk_state: &mut BlockState,
                               core_slot_opt: Option<usize>,
                               prefix: &str,
                               use_rope: bool,
                               is_ternary: bool,
                               trop_temp: f32| {
        let n1 = &b.floats[&format!("{prefix}.norm1.weight")];
        let n2 = &b.floats[&format!("{prefix}.norm2.weight")];
        let c_alpha = b.floats[&format!("{prefix}.clifford_alpha")][0];
        let hs: Vec<Vec<f32>> = step_xs.iter().map(|x| rms_norm(x, n1)).collect();

        let proj_batch = |pname: &str, inps: &[Vec<f32>], out_f: usize, in_f: usize| -> Vec<Vec<f32>> {
            if is_ternary {
                eval_tmac_ternary_gemm_k(
                    arch,
                    &b.ternary[&format!("{prefix}.{pname}")],
                    inps,
                    active_shells,
                    use_tmac_int,
                )
            } else {
                let wkey = format!("{prefix}.{pname}.weight");
                if use_i8_fp32
                    && let Some(tl) = b.floats_tmac.get(&wkey)
                {
                    eval_tmac_ternary_gemm_k(arch, tl, inps, 3, true)
                } else {
                    linear_mat_f32_k(arch, &b.floats[&wkey], inps, out_f, in_f)
                }
            }
        };

        let (mut qs, mut ks, mut vs) = if is_ternary && use_tmac_int {
            let lq = &b.ternary[&format!("{prefix}.wq")];
            let lk = &b.ternary[&format!("{prefix}.wk")];
            let lv = &b.ternary[&format!("{prefix}.wv")];
            if lq.override_f32.is_none()
                && lk.override_f32.is_none()
                && lv.override_f32.is_none()
                && lq.scale_axis == ScaleAxis::PerOc
                && lk.scale_axis == ScaleAxis::PerOc
                && lv.scale_axis == ScaleAxis::PerOc
            {
                let tok_luts: Vec<(f32, Vec<[i8; 16]>)> =
                    hs.iter().map(|h| build_group_luts_i8(h)).collect();
                let rb_single = lq.rows / 32;
                let packed_pairs = (lq.cols / 2).div_ceil(2);
                let mut flat = vec![0.0_f32; 3 * rb_single * k_len * 32];
                flat.par_chunks_mut(k_len * 32)
                    .enumerate()
                    .for_each(|(blk_idx, dst_rb)| {
                        let (layer, rb) = if blk_idx < rb_single {
                            (lq, blk_idx)
                        } else if blk_idx < 2 * rb_single {
                            (lk, blk_idx - rb_single)
                        } else {
                            (lv, blk_idx - 2 * rb_single)
                        };
                        let use_sh = active_shells.clamp(1, layer.shells.len());
                        let base = rb * 32;
                        for sh in &layer.shells[..use_sh] {
                            let slice =
                                &sh.packed_col_major[rb * packed_pairs..(rb + 1) * packed_pairs];
                            for (t, (act_scale, group_luts)) in tok_luts.iter().enumerate() {
                                let mut acc = [0i32; 32];
                                tmac_gemv_32rows_packed4(slice, group_luts, &mut acc);
                                let dst = &mut dst_rb[t * 32..(t + 1) * 32];
                                for r in 0..32 {
                                    dst[r] += (acc[r] as f32) * *act_scale * sh.scales[base + r];
                                }
                            }
                        }
                    });
                let mut q_out = vec![vec![0.0_f32; lq.rows]; k_len];
                let mut k_out = vec![vec![0.0_f32; lq.rows]; k_len];
                let mut v_out = vec![vec![0.0_f32; lq.rows]; k_len];
                for rb in 0..rb_single {
                    for t in 0..k_len {
                        let q_off = (rb * k_len + t) * 32;
                        let k_off = ((rb_single + rb) * k_len + t) * 32;
                        let v_off = ((2 * rb_single + rb) * k_len + t) * 32;
                        q_out[t][rb * 32..(rb + 1) * 32].copy_from_slice(&flat[q_off..q_off + 32]);
                        k_out[t][rb * 32..(rb + 1) * 32].copy_from_slice(&flat[k_off..k_off + 32]);
                        v_out[t][rb * 32..(rb + 1) * 32].copy_from_slice(&flat[v_off..v_off + 32]);
                    }
                }
                (q_out, k_out, v_out)
            } else {
                (
                    proj_batch("wq", &hs, d_model, d_model),
                    proj_batch("wk", &hs, d_model, d_model),
                    proj_batch("wv", &hs, d_model, d_model),
                )
            }
        } else {
            (
                proj_batch("wq", &hs, d_model, d_model),
                proj_batch("wk", &hs, d_model, d_model),
                proj_batch("wv", &hs, d_model, d_model),
            )
        };
        let is_delta_v6 = b.cfg.bundle_version >= 6
            && matches!(blk_state, BlockState::DeltaHologram { .. });
        if use_rope && !is_delta_v6 {
            for t in 0..k_len {
                apply_rope_pos(&mut qs[t], base_pos + t, n_heads, head_dim);
                apply_rope_pos(&mut ks[t], base_pos + t, n_heads, head_dim);
            }
        }

        let ctxs: Vec<Vec<f32>> = match blk_state {
            BlockState::DeltaHologram {
                s_mat,
                conv_q,
                conv_k,
                conv_v,
            } => {
                let abs = linear_mat_f32_bias_k(
                    arch,
                    &b.floats[&format!("{prefix}.w_ab.weight")],
                    &b.floats[&format!("{prefix}.w_ab.bias")],
                    &hs,
                    2 * n_heads,
                    d_model,
                );
                let w_write_key = format!("{prefix}.w_write.weight");
                let w_write_b = &b.floats[&format!("{prefix}.w_write.bias")];
                let mut w_raws = if use_i8_fp32
                    && let Some(tl) = b.floats_tmac.get(&w_write_key)
                {
                    let mut tmp = eval_tmac_ternary_gemm_k(arch, tl, &hs, 3, true);
                    for o in &mut tmp {
                        for i in 0..d_model {
                            o[i] += w_write_b[i];
                        }
                    }
                    tmp
                } else {
                    linear_mat_f32_bias_k(arch, &b.floats[&w_write_key], w_write_b, &hs, d_model, d_model)
                };
                let _ = &mut w_raws;

                // L1d-pinned K-token head-parallel Gated DeltaNet-2 update (1 Rayon dispatch for all K tokens!)
                // When `record_snaps` is true, also capture per-token `s_mat` + `conv` snapshots for O(1) 0.2ms rollback!
                let mut conv_snaps: Vec<(Vec<f32>, Vec<f32>, Vec<f32>)> = Vec::new();
                if b.cfg.bundle_version >= 6 {
                    for t in 0..k_len {
                        let apply_short_conv =
                            |vec_cur: &mut [f32], hist: &mut [f32], cname: &str| {
                                if let Some(cw) = b.floats.get(&format!("{prefix}.{cname}.weight")) {
                                    let cb = b.floats.get(&format!("{prefix}.{cname}.bias"));
                                    for d in 0..d_model {
                                        let x0 = hist[d];
                                        let x1 = hist[d_model + d];
                                        let x2 = hist[2 * d_model + d];
                                        let x3 = vec_cur[d];
                                        hist[d] = x1;
                                        hist[d_model + d] = x2;
                                        hist[2 * d_model + d] = x3;
                                        let w0 = cw[d * 4];
                                        let w1 = cw[d * 4 + 1];
                                        let w2 = cw[d * 4 + 2];
                                        let w3 = cw[d * 4 + 3];
                                        let bias = cb.map(|bv| bv[d]).unwrap_or(0.0);
                                        let y = w0 * x0 + w1 * x1 + w2 * x2 + w3 * x3 + bias;
                                        vec_cur[d] = y * sigmoid(y);
                                    }
                                }
                            };
                        apply_short_conv(&mut qs[t], conv_q, "conv_q");
                        apply_short_conv(&mut ks[t], conv_k, "conv_k");
                        apply_short_conv(&mut vs[t], conv_v, "conv_v");
                        if record_snaps {
                            conv_snaps.push((conv_q.clone(), conv_k.clone(), conv_v.clone()));
                        }
                    }
                } else if record_snaps {
                    for _ in 0..k_len {
                        conv_snaps.push((conv_q.clone(), conv_k.clone(), conv_v.clone()));
                    }
                }

                let h_sq = head_dim * head_dim;
                let snap_steps = if record_snaps { k_len.saturating_sub(1) } else { 0 };
                let mut flat_h_out = vec![0.0_f32; n_heads * k_len * head_dim];
                let mut flat_s_snaps = if snap_steps > 0 {
                    vec![0.0_f32; n_heads * snap_steps * h_sq]
                } else {
                    Vec::new()
                };
                if snap_steps > 0 {
                    s_mat
                        .par_chunks_mut(h_sq)
                        .zip(flat_h_out.par_chunks_mut(k_len * head_dim))
                        .zip(flat_s_snaps.par_chunks_mut(snap_steps * h_sq))
                        .enumerate()
                        .for_each(|(hi, ((s_head, out_hk), snap_hk))| {
                            let off = hi * head_dim;
                            let mut q_hat = [0.0_f32; 128];
                            let mut k_hat = [0.0_f32; 128];
                            for t in 0..k_len {
                                let q = &qs[t];
                                let k = &ks[t];
                                let v = &vs[t];
                                let ab = &abs[t];
                                let w_raw = &w_raws[t];
                                let qn = dot_f32(arch, &q[off..off + head_dim], &q[off..off + head_dim]).sqrt().max(1e-5);
                                let kn = dot_f32(arch, &k[off..off + head_dim], &k[off..off + head_dim]).sqrt().max(1e-5);
                                for d in 0..head_dim {
                                    q_hat[d] = q[off + d] / qn;
                                    k_hat[d] = k[off + d] / kn;
                                }
                                let q_h = &q_hat[..head_dim];
                                let k_h = &k_hat[..head_dim];
                                let alpha = (0.50 + 0.499 * sigmoid(ab[hi])).clamp(1e-4, 0.9999);
                                let beta = sigmoid(ab[n_heads + hi]);
                                let out_h = &mut out_hk[t * head_dim..(t + 1) * head_dim];
                                for r in 0..head_dim {
                                    let row = &mut s_head[r * head_dim..(r + 1) * head_dim];
                                    let s_k = alpha * dot_f32(arch, row, k_h);
                                    let u_r = sigmoid(w_raw[off + r]) * v[off + r];
                                    let r_val = u_r - beta * s_k;
                                    out_h[r] = delta_row_update_and_dot(arch, row, k_h, q_h, alpha, r_val);
                                }
                                if t < snap_steps {
                                    snap_hk[t * h_sq..(t + 1) * h_sq].copy_from_slice(s_head);
                                }
                            }
                        });
                    if let Some(c_slot) = core_slot_opt {
                        for t in 0..snap_steps {
                            let mut s_t = vec![0.0_f32; n_heads * h_sq];
                            for hi in 0..n_heads {
                                let src = &flat_s_snaps[(hi * snap_steps + t) * h_sq..(hi * snap_steps + t + 1) * h_sq];
                                s_t[hi * h_sq..(hi + 1) * h_sq].copy_from_slice(src);
                            }
                            let (ref cq, ref ck, ref cv) = conv_snaps[t];
                            snaps[t].core_deltas.push((c_slot, s_t, cq.clone(), ck.clone(), cv.clone()));
                        }
                    }
                } else {
                    s_mat
                        .par_chunks_mut(h_sq)
                        .zip(flat_h_out.par_chunks_mut(k_len * head_dim))
                        .enumerate()
                        .for_each(|(hi, (s_head, out_hk))| {
                            let off = hi * head_dim;
                            let mut q_hat = [0.0_f32; 128];
                            let mut k_hat = [0.0_f32; 128];
                            for t in 0..k_len {
                                let q = &qs[t];
                                let k = &ks[t];
                                let v = &vs[t];
                                let ab = &abs[t];
                                let w_raw = &w_raws[t];
                                let qn = dot_f32(arch, &q[off..off + head_dim], &q[off..off + head_dim]).sqrt().max(1e-5);
                                let kn = dot_f32(arch, &k[off..off + head_dim], &k[off..off + head_dim]).sqrt().max(1e-5);
                                for d in 0..head_dim {
                                    q_hat[d] = q[off + d] / qn;
                                    k_hat[d] = k[off + d] / kn;
                                }
                                let q_h = &q_hat[..head_dim];
                                let k_h = &k_hat[..head_dim];
                                let alpha = (0.50 + 0.499 * sigmoid(ab[hi])).clamp(1e-4, 0.9999);
                                let beta = sigmoid(ab[n_heads + hi]);
                                let out_h = &mut out_hk[t * head_dim..(t + 1) * head_dim];
                                for r in 0..head_dim {
                                    let row = &mut s_head[r * head_dim..(r + 1) * head_dim];
                                    let s_k = alpha * dot_f32(arch, row, k_h);
                                    let u_r = sigmoid(w_raw[off + r]) * v[off + r];
                                    let r_val = u_r - beta * s_k;
                                    out_h[r] = delta_row_update_and_dot(arch, row, k_h, q_h, alpha, r_val);
                                }
                            }
                        });
                }
                let mut outs = vec![vec![0.0_f32; d_model]; k_len];
                for hi in 0..n_heads {
                    let h_off = hi * k_len * head_dim;
                    for t in 0..k_len {
                        let src = &flat_h_out[h_off + t * head_dim..h_off + (t + 1) * head_dim];
                        outs[t][hi * head_dim..(hi + 1) * head_dim].copy_from_slice(src);
                    }
                }
                outs
            }
            BlockState::AttnKv { k: ck, v: cv } => {
                let attn_scale = trop_temp / (head_dim as f32).sqrt();
                let mut outs = Vec::with_capacity(k_len);
                for t in 0..k_len {
                    ck.extend_from_slice(&ks[t]);
                    cv.extend_from_slice(&vs[t]);
                    let kv_len = ck.len() / d_model;
                    let q = &qs[t];
                    let mut out_vec = vec![0.0_f32; d_model];
                    for hi in 0..n_heads {
                        let off = hi * head_dim;
                        let q_vec = &q[off..off + head_dim];
                        let mut scores = Vec::with_capacity(kv_len);
                        let mut max_s = f32::NEG_INFINITY;
                        for s_t in 0..kv_len {
                            let k_vec = &ck[s_t * d_model + off..s_t * d_model + off + head_dim];
                            let sc = dot_f32(arch, q_vec, k_vec) * attn_scale;
                            max_s = max_s.max(sc);
                            scores.push(sc);
                        }
                        let mut sum_e = 0.0_f32;
                        for s in &mut scores {
                            *s = (*s - max_s).exp();
                            sum_e += *s;
                        }
                        let inv_s = sum_e.max(1e-6).recip();
                        for (s_t, &wt) in scores.iter().enumerate() {
                            let p_wt = wt * inv_s;
                            let v_vec = &cv[s_t * d_model + off..s_t * d_model + off + head_dim];
                            for d in 0..head_dim {
                                out_vec[off + d] += p_wt * v_vec[d];
                            }
                        }
                    }
                    outs.push(out_vec);
                }
                outs
            }
        };

        let wo_ins: Vec<Vec<f32>> =
            if let Some(sw) = b.floats.get(&format!("{prefix}.sub_norm_attn.weight")) {
                ctxs.iter().map(|c| rms_norm(c, sw)).collect()
            } else {
                ctxs
            };
        let attn_projs = proj_batch("wo", &wo_ins, d_model, d_model);
        for t in 0..k_len {
            for d in 0..d_model {
                step_xs[t][d] += attn_projs[t][d];
            }
        }

        let fn_ins: Vec<Vec<f32>> = step_xs.iter().map(|x| rms_norm(x, n2)).collect();
        let mut ups = proj_batch("w_up", &fn_ins, d_ff, d_model);
        for up in &mut ups {
            for v in up.iter_mut() {
                let r = v.max(0.0);
                *v = r * r;
            }
        }
        let wd_ins: Vec<Vec<f32>> =
            if let Some(sw) = b.floats.get(&format!("{prefix}.sub_norm_ffn.weight")) {
                ups.iter().map(|u| rms_norm(u, sw)).collect()
            } else {
                ups
            };
        let mut ffn_outs = proj_batch("w_down", &wd_ins, d_model, d_ff);
        if active_shells >= 3
            && let Some(sc) = b.sidecars.get(&format!("{prefix}.w_down"))
        {
            for t in 0..k_len {
                apply_stiefel_sidecar(sc, &wd_ins[t], &mut ffn_outs[t]);
            }
        }
        for t in 0..k_len {
            apply_clifford_wedge(&mut step_xs[t], &fn_ins[t], &ffn_outs[t], c_alpha);
        }
    };

    run_block_batch(&mut xs, &mut state.prelude[0], None, "prelude.0", true, false, 1.0);
    run_block_batch(&mut xs, &mut state.prelude[1], None, "prelude.1", false, true, 1.0);
    let xs_prelude = xs.clone();

    let inj = b.floats["gamma_inj"][0].tanh() * 0.25;
    let mut streams: Vec<[Vec<f32>; N_STREAMS]> = xs
        .into_iter()
        .map(|x| [x.clone(), x.clone(), x.clone(), x])
        .collect();

    for loop_idx in 0..state.r_loops {
        let trop_temp = 1.0 + 0.15 * (loop_idx as f32);
        for b_idx in 0..n_core {
            let mhc = &b.mhc_core[b_idx];
            let h_mix = mhc.h_mix;
            let a_in = mhc.a_in;
            let b_out = mhc.b_out;
            let mut aggs: Vec<Vec<f32>> = Vec::with_capacity(k_len);
            for t in 0..k_len {
                let mut agg = vec![0.0_f32; d_model];
                if let Some(ref ig) = mhc.stream_in_gate {
                    for d in 0..d_model {
                        agg[d] = a_in[0] * ig[d] * streams[t][0][d]
                            + a_in[1] * ig[d_model + d] * streams[t][1][d]
                            + a_in[2] * ig[2 * d_model + d] * streams[t][2][d]
                            + a_in[3] * ig[3 * d_model + d] * streams[t][3][d]
                            + inj * xs_prelude[t][d];
                    }
                } else {
                    for d in 0..d_model {
                        agg[d] = a_in[0] * streams[t][0][d]
                            + a_in[1] * streams[t][1][d]
                            + a_in[2] * streams[t][2][d]
                            + a_in[3] * streams[t][3][d]
                            + inj * xs_prelude[t][d];
                    }
                }
                aggs.push(agg);
            }
            let mut afters = aggs.clone();
            let c_slot = loop_idx * n_core + b_idx;
            let pref = format!("core.{b_idx}");
            run_block_batch(
                &mut afters,
                &mut state.core[c_slot],
                Some(c_slot),
                &pref,
                true,
                true,
                trop_temp,
            );
            for t in 0..k_len {
                let mut next_streams = [
                    vec![0.0_f32; d_model],
                    vec![0.0_f32; d_model],
                    vec![0.0_f32; d_model],
                    vec![0.0_f32; d_model],
                ];
                if let Some(ref og) = mhc.stream_out_gate {
                    for s_i in 0..N_STREAMS {
                        let og_off = s_i * d_model;
                        for d in 0..d_model {
                            let delta = afters[t][d] - aggs[t][d];
                            let mixed = h_mix[s_i * 4] * streams[t][0][d]
                                + h_mix[s_i * 4 + 1] * streams[t][1][d]
                                + h_mix[s_i * 4 + 2] * streams[t][2][d]
                                + h_mix[s_i * 4 + 3] * streams[t][3][d];
                            next_streams[s_i][d] = mixed + b_out[s_i] * og[og_off + d] * delta;
                        }
                    }
                } else {
                    for s_i in 0..N_STREAMS {
                        for d in 0..d_model {
                            let delta = afters[t][d] - aggs[t][d];
                            let mixed = h_mix[s_i * 4] * streams[t][0][d]
                                + h_mix[s_i * 4 + 1] * streams[t][1][d]
                                + h_mix[s_i * 4 + 2] * streams[t][2][d]
                                + h_mix[s_i * 4 + 3] * streams[t][3][d];
                            next_streams[s_i][d] = mixed + b_out[s_i] * delta;
                        }
                    }
                }
                streams[t] = next_streams;
            }
        }
    }

    let mut mergeds: Vec<Vec<f32>> = streams
        .into_iter()
        .map(|st| {
            let mut m = vec![0.0_f32; d_model];
            for d in 0..d_model {
                m[d] = 0.25 * (st[0][d] + st[1][d] + st[2][d] + st[3][d]);
            }
            m
        })
        .collect();

    run_block_batch(&mut mergeds, &mut state.coda[0], None, "coda.0", true, true, 1.0);
    run_block_batch(&mut mergeds, &mut state.coda[1], None, "coda.1", false, false, 1.0);
    state.pos += k_len;

    let fn_w = &b.floats["final_norm.weight"];
    let logits_out = match logits_mode {
        LogitsMode::None => vec![None; k_len],
        LogitsMode::LastOnly => {
            let mut res = vec![None; k_len];
            let last_h = rms_norm(&mergeds[k_len - 1], fn_w);
            let lg = if use_i8_fp32
                && let Some(tl) = b.floats_tmac.get("tok_emb.weight")
            {
                let mut dummy = 0i32;
                eval_tmac_ternary_gemv(arch, tl, &last_h, 3, true, false, &mut dummy)
            } else {
                linear_vec(arch, tok_emb, &last_h, b.cfg.vocab_size, d_model)
            };
            res[k_len - 1] = Some(lg);
            res
        }
        LogitsMode::All => {
            let normed: Vec<Vec<f32>> = mergeds.iter().map(|m| rms_norm(m, fn_w)).collect();
            let all_lg = if use_i8_fp32
                && let Some(tl) = b.floats_tmac.get("tok_emb.weight")
            {
                eval_tmac_ternary_gemm_k(arch, tl, &normed, 3, true)
            } else {
                linear_mat_f32_k(arch, tok_emb, &normed, b.cfg.vocab_size, d_model)
            };
            all_lg.into_iter().map(Some).collect()
        }
    };
    (logits_out, snaps)
}

#[allow(clippy::too_many_arguments)]
fn forward_step(
    arch: Arch,
    b: &MuninnBundle,
    token_id: u32,
    prev_token_id: u32,
    mac_6d: Option<&[f32]>,
    state: &mut MuninnRuntimeState,
    active_shells: usize,
    use_tmac_int: bool,
    verify_avx2: bool,
    max_int_diff: &mut i32,
    compute_logits: bool,
) -> Option<Vec<f32>> {
    let pos = state.pos;
    let d_model = b.cfg.d_model;
    let n_heads = b.cfg.n_heads;
    let head_dim = b.cfg.head_dim;
    let d_ff = b.cfg.d_ff;
    let n_core = b.cfg.n_core_blocks;
    let tok_emb = &b.floats["tok_emb.weight"];

    let tid = (token_id as usize).min(b.cfg.vocab_size - 1);
    let mut x = tok_emb[tid * d_model..(tid + 1) * d_model].to_vec();

    // Native 6D Galactic MAC Coordinate Port injected cleanly at pos == 0
    if pos == 0
        && let Some(mac) = mac_6d
    {
        let mac_w = &b.floats["mac_proj.weight"];
        let mac_g = b.floats["mac_gate"][0].tanh();
        let proj = linear_vec(arch, mac_w, mac, d_model, b.cfg.mac_dim);
        for d in 0..d_model {
            x[d] += mac_g * proj[d];
        }
    }

    // O(1) Sparse Engram lookup + gate
    let eng_dim = b.cfg.engram_dim;
    let h_slot = ((u64::from(prev_token_id).wrapping_mul(1315423911))
        .wrapping_add(u64::from(token_id).wrapping_mul(2654435761))
        % (b.cfg.engram_slots as u64)) as usize;
    let g_eng =
        dot_f32(arch, &b.floats["engram_gate.weight"], &x) + b.floats["engram_gate.bias"][0];
    let alpha_eng = sigmoid(g_eng);
    let eng_slice = &b.engram_table_i8[h_slot * eng_dim..(h_slot + 1) * eng_dim];
    let eng_f: Vec<f32> = eng_slice.iter().map(|&v| f32::from(v) / 64.0).collect();
    let ep = linear_vec(
        arch,
        &b.floats["engram_proj.weight"],
        &eng_f,
        d_model,
        eng_dim,
    );
    for d in 0..d_model {
        x[d] += alpha_eng * ep[d];
    }

    let run_block_step = |step_x: &mut Vec<f32>,
                          blk_state: &mut BlockState,
                          prefix: &str,
                          use_rope: bool,
                          is_ternary: bool,
                          trop_temp: f32,
                          v_avx2: bool,
                          m_diff: &mut i32| {
        let n1 = &b.floats[&format!("{prefix}.norm1.weight")];
        let n2 = &b.floats[&format!("{prefix}.norm2.weight")];
        let c_alpha = b.floats[&format!("{prefix}.clifford_alpha")][0];
        let h = rms_norm(step_x, n1);

        let mut proj_lin =
            |pname: &str, inp: &[f32], out_f: usize, in_f: usize, check_avx: bool| -> Vec<f32> {
                if is_ternary {
                    eval_tmac_ternary_gemv(
                        arch,
                        &b.ternary[&format!("{prefix}.{pname}")],
                        inp,
                        active_shells,
                        use_tmac_int,
                        check_avx,
                        m_diff,
                    )
                } else {
                    linear_vec(
                        arch,
                        &b.floats[&format!("{prefix}.{pname}.weight")],
                        inp,
                        out_f,
                        in_f,
                    )
                }
            };

        let mut q = Vec::new();
        let mut k = Vec::new();
        let mut v = Vec::new();
        if is_ternary && !v_avx2 {
            let lq = &b.ternary[&format!("{prefix}.wq")];
            let lk = &b.ternary[&format!("{prefix}.wk")];
            let lv = &b.ternary[&format!("{prefix}.wv")];
            if use_tmac_int
                && lq.override_f32.is_none()
                && lk.override_f32.is_none()
                && lv.override_f32.is_none()
                && lq.scale_axis == ScaleAxis::PerOc
                && lk.scale_axis == ScaleAxis::PerOc
                && lv.scale_axis == ScaleAxis::PerOc
            {
                let (act_scale, group_luts) = build_group_luts_i8(&h);
                let rb_single = lq.rows / 32;
                let packed_pairs = (lq.cols / 2).div_ceil(2);
                let mut qkv_out = vec![0.0_f32; lq.rows * 3];
                qkv_out
                    .par_chunks_mut(32)
                    .enumerate()
                    .for_each(|(blk_idx, dst_chunk)| {
                        let (layer, rb) = if blk_idx < rb_single {
                            (lq, blk_idx)
                        } else if blk_idx < 2 * rb_single {
                            (lk, blk_idx - rb_single)
                        } else {
                            (lv, blk_idx - 2 * rb_single)
                        };
                        let use_sh = active_shells.clamp(1, layer.shells.len());
                        let base = rb * 32;
                        for sh in &layer.shells[..use_sh] {
                            let slice =
                                &sh.packed_col_major[rb * packed_pairs..(rb + 1) * packed_pairs];
                            let mut acc = [0i32; 32];
                            tmac_gemv_32rows_packed4(slice, &group_luts, &mut acc);
                            for r in 0..32 {
                                dst_chunk[r] += (acc[r] as f32) * act_scale * sh.scales[base + r];
                            }
                        }
                    });
                v = qkv_out.split_off(2 * lq.rows);
                k = qkv_out.split_off(lq.rows);
                q = qkv_out;
            }
        }
        if q.is_empty() {
            q = proj_lin("wq", &h, d_model, d_model, v_avx2);
            k = proj_lin("wk", &h, d_model, d_model, false);
            v = proj_lin("wv", &h, d_model, d_model, false);
        }
        let is_delta_v6 = b.cfg.bundle_version >= 6
            && matches!(blk_state, BlockState::DeltaHologram { .. });
        if use_rope && !is_delta_v6 {
            apply_rope_pos(&mut q, pos, n_heads, head_dim);
            apply_rope_pos(&mut k, pos, n_heads, head_dim);
        }

        let ctx = match blk_state {
            BlockState::DeltaHologram {
                s_mat,
                conv_q,
                conv_k,
                conv_v,
            } => {
                // TM7B\x06: Causal depthwise short_conv1d(k=4) + SiLU on NoPE DeltaNet-2 q, k, v!
                if b.cfg.bundle_version >= 6 {
                    let apply_short_conv =
                        |vec_cur: &mut [f32], hist: &mut [f32], cname: &str| {
                            if let Some(cw) = b.floats.get(&format!("{prefix}.{cname}.weight")) {
                                let cb = b.floats.get(&format!("{prefix}.{cname}.bias"));
                                for d in 0..d_model {
                                    let x0 = hist[d];
                                    let x1 = hist[d_model + d];
                                    let x2 = hist[2 * d_model + d];
                                    let x3 = vec_cur[d];
                                    hist[d] = x1;
                                    hist[d_model + d] = x2;
                                    hist[2 * d_model + d] = x3;
                                    let w0 = cw[d * 4];
                                    let w1 = cw[d * 4 + 1];
                                    let w2 = cw[d * 4 + 2];
                                    let w3 = cw[d * 4 + 3];
                                    let bias = cb.map(|bv| bv[d]).unwrap_or(0.0);
                                    let y = w0 * x0 + w1 * x1 + w2 * x2 + w3 * x3 + bias;
                                    vec_cur[d] = y * sigmoid(y);
                                }
                            }
                        };
                    apply_short_conv(&mut q, conv_q, "conv_q");
                    apply_short_conv(&mut k, conv_k, "conv_k");
                    apply_short_conv(&mut v, conv_v, "conv_v");
                }
                let ab = linear_vec_bias(
                    arch,
                    &b.floats[&format!("{prefix}.w_ab.weight")],
                    &b.floats[&format!("{prefix}.w_ab.bias")],
                    &h,
                    2 * n_heads,
                    d_model,
                );
                let w_raw = linear_vec_bias(
                    arch,
                    &b.floats[&format!("{prefix}.w_write.weight")],
                    &b.floats[&format!("{prefix}.w_write.bias")],
                    &h,
                    d_model,
                    d_model,
                );
                let mut out_vec = vec![0.0_f32; d_model];
                s_mat
                    .par_chunks_mut(head_dim * head_dim)
                    .zip(out_vec.par_chunks_mut(head_dim))
                    .enumerate()
                    .for_each(|(hi, (s_head, out_h))| {
                        let off = hi * head_dim;
                        let qn = dot_f32(arch, &q[off..off + head_dim], &q[off..off + head_dim])
                            .sqrt()
                            .max(1e-5);
                        let kn = dot_f32(arch, &k[off..off + head_dim], &k[off..off + head_dim])
                            .sqrt()
                            .max(1e-5);
                        let mut q_hat = [0.0_f32; 128];
                        let mut k_hat = [0.0_f32; 128];
                        for d in 0..head_dim {
                            q_hat[d] = q[off + d] / qn;
                            k_hat[d] = k[off + d] / kn;
                        }
                        let q_h = &q_hat[..head_dim];
                        let k_h = &k_hat[..head_dim];
                        let alpha = (0.50 + 0.499 * sigmoid(ab[hi])).clamp(1e-4, 0.9999);
                        let beta = sigmoid(ab[n_heads + hi]);
                        for r in 0..head_dim {
                            let row = &mut s_head[r * head_dim..(r + 1) * head_dim];
                            let s_k = alpha * dot_f32(arch, row, k_h);
                            let u_r = sigmoid(w_raw[off + r]) * v[off + r];
                            let r_val = u_r - beta * s_k;
                            out_h[r] = delta_row_update_and_dot(arch, row, k_h, q_h, alpha, r_val);
                        }
                    });
                out_vec
            }
            BlockState::AttnKv { k: ck, v: cv } => {
                ck.extend_from_slice(&k);
                cv.extend_from_slice(&v);
                let kv_len = ck.len() / d_model;
                let attn_scale = trop_temp / (head_dim as f32).sqrt();
                let mut out_vec = vec![0.0_f32; d_model];
                for hi in 0..n_heads {
                    let off = hi * head_dim;
                    let q_vec = &q[off..off + head_dim];
                    let mut scores = Vec::with_capacity(kv_len);
                    let mut max_s = f32::NEG_INFINITY;
                    for s_t in 0..kv_len {
                        let k_vec = &ck[s_t * d_model + off..s_t * d_model + off + head_dim];
                        let sc = dot_f32(arch, q_vec, k_vec) * attn_scale;
                        max_s = max_s.max(sc);
                        scores.push(sc);
                    }
                    let mut sum_e = 0.0_f32;
                    for s in &mut scores {
                        *s = (*s - max_s).exp();
                        sum_e += *s;
                    }
                    let inv_s = sum_e.max(1e-6).recip();
                    for (s_t, &wt) in scores.iter().enumerate() {
                        let p_wt = wt * inv_s;
                        let v_vec = &cv[s_t * d_model + off..s_t * d_model + off + head_dim];
                        for d in 0..head_dim {
                            out_vec[off + d] += p_wt * v_vec[d];
                        }
                    }
                }
                out_vec
            }
        };

        let wo_in = if let Some(sw) = b.floats.get(&format!("{prefix}.sub_norm_attn.weight")) {
            rms_norm(&ctx, sw)
        } else {
            ctx
        };
        let attn_proj = proj_lin("wo", &wo_in, d_model, d_model, false);
        for d in 0..d_model {
            step_x[d] += attn_proj[d];
        }

        let fn_in = rms_norm(step_x, n2);
        let mut up = proj_lin("w_up", &fn_in, d_ff, d_model, false);
        for v in &mut up {
            let r = v.max(0.0);
            *v = r * r;
        }
        let wd_in = if let Some(sw) = b.floats.get(&format!("{prefix}.sub_norm_ffn.weight")) {
            rms_norm(&up, sw)
        } else {
            up
        };
        let mut ffn_out = proj_lin("w_down", &wd_in, d_model, d_ff, false);
        if active_shells >= 3
            && let Some(sc) = b.sidecars.get(&format!("{prefix}.w_down"))
        {
            apply_stiefel_sidecar(sc, &wd_in, &mut ffn_out);
        }
        apply_clifford_wedge(step_x, &fn_in, &ffn_out, c_alpha);
    };

    run_block_step(
        &mut x,
        &mut state.prelude[0],
        "prelude.0",
        true,
        false,
        1.0,
        false,
        max_int_diff,
    );
    run_block_step(
        &mut x,
        &mut state.prelude[1],
        "prelude.1",
        false,
        true,
        1.0,
        verify_avx2,
        max_int_diff,
    );
    let x_prelude = x.clone();

    let inj = b.floats["gamma_inj"][0].tanh() * 0.25;
    let mut streams = [x.clone(), x.clone(), x.clone(), x];
    for loop_idx in 0..state.r_loops {
        let trop_temp = 1.0 + 0.15 * (loop_idx as f32);
        for b_idx in 0..n_core {
            let mhc = &b.mhc_core[b_idx];
            let h_mix = mhc.h_mix;
            let a_in = mhc.a_in;
            let b_out = mhc.b_out;
            let mut agg = vec![0.0_f32; d_model];
            if let Some(ref ig) = mhc.stream_in_gate {
                for d in 0..d_model {
                    agg[d] = a_in[0] * ig[d] * streams[0][d]
                        + a_in[1] * ig[d_model + d] * streams[1][d]
                        + a_in[2] * ig[2 * d_model + d] * streams[2][d]
                        + a_in[3] * ig[3 * d_model + d] * streams[3][d]
                        + inj * x_prelude[d];
                }
            } else {
                for d in 0..d_model {
                    agg[d] = a_in[0] * streams[0][d]
                        + a_in[1] * streams[1][d]
                        + a_in[2] * streams[2][d]
                        + a_in[3] * streams[3][d]
                        + inj * x_prelude[d];
                }
            }
            let mut after = agg.clone();
            let c_slot = loop_idx * n_core + b_idx;
            let pref = format!("core.{b_idx}");
            run_block_step(
                &mut after,
                &mut state.core[c_slot],
                &pref,
                true,
                true,
                trop_temp,
                verify_avx2 && loop_idx == 0 && b_idx == 0,
                max_int_diff,
            );
            let mut next_streams = [
                vec![0.0_f32; d_model],
                vec![0.0_f32; d_model],
                vec![0.0_f32; d_model],
                vec![0.0_f32; d_model],
            ];
            if let Some(ref og) = mhc.stream_out_gate {
                for s_i in 0..N_STREAMS {
                    let og_off = s_i * d_model;
                    for d in 0..d_model {
                        let delta = after[d] - agg[d];
                        let mixed = h_mix[s_i * 4] * streams[0][d]
                            + h_mix[s_i * 4 + 1] * streams[1][d]
                            + h_mix[s_i * 4 + 2] * streams[2][d]
                            + h_mix[s_i * 4 + 3] * streams[3][d];
                        next_streams[s_i][d] = mixed + b_out[s_i] * og[og_off + d] * delta;
                    }
                }
            } else {
                for s_i in 0..N_STREAMS {
                    for d in 0..d_model {
                        let delta = after[d] - agg[d];
                        let mixed = h_mix[s_i * 4] * streams[0][d]
                            + h_mix[s_i * 4 + 1] * streams[1][d]
                            + h_mix[s_i * 4 + 2] * streams[2][d]
                            + h_mix[s_i * 4 + 3] * streams[3][d];
                        next_streams[s_i][d] = mixed + b_out[s_i] * delta;
                    }
                }
            }
            streams = next_streams;
        }
        // Energy-Guided Dynamic Loop Halting (`arXiv:2611.08977`) after R=2 on decode tokens
        if loop_idx == 1
            && state.energy_halt_active
            && state.r_loops > 2
            && let (Some(tau), Some(ew), Some(eb)) = (
                state.energy_halt_tau,
                b.floats.get("energy_head.weight"),
                b.floats.get("energy_head.bias"),
            )
        {
            let mut h_r2 = vec![0.0_f32; d_model];
            for d in 0..d_model {
                h_r2[d] = 0.25 * (streams[0][d] + streams[1][d] + streams[2][d] + streams[3][d]);
            }
            let raw_e = dot_f32(arch, ew, &h_r2) + eb[0];
            let energy = if raw_e > 20.0 {
                raw_e
            } else {
                (1.0_f32 + raw_e.exp()).ln()
            };
            state.energy_sum += f64::from(energy);
            if energy <= tau {
                state.halted_r2_tokens += 1;
                break;
            } else {
                state.full_r_tokens += 1;
            }
        }
    }

    let mut merged = vec![0.0_f32; d_model];
    for d in 0..d_model {
        merged[d] = 0.25 * (streams[0][d] + streams[1][d] + streams[2][d] + streams[3][d]);
    }
    run_block_step(
        &mut merged,
        &mut state.coda[0],
        "coda.0",
        true,
        true,
        1.0,
        false,
        max_int_diff,
    );
    run_block_step(
        &mut merged,
        &mut state.coda[1],
        "coda.1",
        false,
        false,
        1.0,
        false,
        max_int_diff,
    );
    state.pos += 1;

    if compute_logits {
        let last_h = rms_norm(&merged, &b.floats["final_norm.weight"]);
        Some(linear_vec(
            arch,
            tok_emb,
            &last_h,
            b.cfg.vocab_size,
            d_model,
        ))
    } else {
        None
    }
}

fn read_vm_hwm_mb() -> f64 {
    #[cfg(target_os = "linux")]
    if let Ok(st) = fs::read_to_string("/proc/self/status") {
        for line in st.lines() {
            if let Some(rest) = line.strip_prefix("VmHWM:")
                && let Some(kb_str) = rest.split_whitespace().next()
                && let Ok(kb) = kb_str.parse::<f64>()
            {
                return kb / 1024.0;
            }
        }
    }
    0.0
}

fn unpack_packed4_to_i8(rows: usize, cols: usize, packed: &[[u8; 32]]) -> Vec<i8> {
    let col_pairs = cols / 2;
    let packed_pairs = col_pairs.div_ceil(2);
    let row_blocks = rows / 32;
    let mut out = vec![0i8; rows * cols];
    for rb in 0..row_blocks {
        for pp in 0..packed_pairs {
            let blk = &packed[rb * packed_pairs + pp];
            let cp_lo = 2 * pp;
            let cp_hi = cp_lo + 1;
            for r in 0..32 {
                let row_idx = rb * 32 + r;
                let base = row_idx * cols;
                let b = blk[r];
                let nlo = b & 0x0F;
                out[base + 2 * cp_lo] = ((nlo % 3) as i8) - 1;
                out[base + 2 * cp_lo + 1] = ((nlo / 3) as i8) - 1;
                if cp_hi < col_pairs {
                    let nhi = b >> 4;
                    out[base + 2 * cp_hi] = ((nhi % 3) as i8) - 1;
                    out[base + 2 * cp_hi + 1] = ((nhi / 3) as i8) - 1;
                }
            }
        }
    }
    out
}

struct SplitMix64(u64);
impl SplitMix64 {
    fn next_u64(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }
    fn next_f64(&mut self) -> f64 {
        ((self.next_u64() >> 11) as f64) * (1.0 / ((1u64 << 53) as f64))
    }
    fn next_normal(&mut self) -> f64 {
        let u1 = self.next_f64().max(1e-12);
        let u2 = self.next_f64();
        (-2.0 * u1.ln()).sqrt() * (std::f64::consts::TAU * u2).cos()
    }
}

/// Pure Safe-Rust top-p nucleus sampler (`temp <= 1e-5` -> bit-exact greedy argmax).
fn sample_token_top_p(logits: &[f32], temp: f32, top_p: f32, rng: &mut SplitMix64) -> usize {
    if logits.is_empty() {
        return 2;
    }
    if temp <= 1e-5 {
        return logits
            .iter()
            .enumerate()
            .max_by(|a, b| a.1.total_cmp(b.1))
            .map(|(i, _)| i)
            .unwrap_or(2);
    }
    let inv_t = 1.0_f32 / temp.max(1e-4);
    let max_l = logits.iter().copied().fold(f32::NEG_INFINITY, f32::max);
    // Keep top-64 candidates before nucleus truncation for O(V) zero-overhead CPU sampling
    let mut top: Vec<(usize, f32)> = logits
        .iter()
        .enumerate()
        .filter(|(_, l)| (**l - max_l) * inv_t > -16.0)
        .map(|(i, &l)| (i, ((l - max_l) * inv_t).exp()))
        .collect();
    if top.is_empty() {
        return 2;
    }
    top.sort_unstable_by(|a, b| b.1.total_cmp(&a.1));
    if top.len() > 64 {
        top.truncate(64);
    }
    let sum_all: f32 = top.iter().map(|&(_, p)| p).sum::<f32>().max(1e-12);
    let cut_p = top_p.clamp(0.05, 1.0) * sum_all;
    let mut cum = 0.0_f32;
    let mut keep = 0usize;
    for &(_, p) in &top {
        cum += p;
        keep += 1;
        if cum >= cut_p {
            break;
        }
    }
    top.truncate(keep.max(1));
    let norm_s: f32 = top.iter().map(|&(_, p)| p).sum::<f32>().max(1e-12);
    let u = (rng.next_f64() as f32) * norm_s;
    let mut acc = 0.0_f32;
    for &(idx, p) in &top {
        acc += p;
        if acc >= u {
            return idx;
        }
    }
    top[0].0
}

fn eval_bundle_refs_accuracy(
    arch: Arch,
    bundle: &MuninnBundle,
    active_shells: usize,
) -> (f64, f64, f64) {
    let mut cos_sum = 0.0f64;
    let mut top1_hits = 0usize;
    let mut top5_hits = 0usize;
    let mut dummy_diff = 0i32;
    for r in &bundle.refs {
        let mut st = MuninnRuntimeState::new(&bundle.cfg, 4, r.pids.len() + 2);
        let mut logits = Vec::new();
        for (ti, &tid) in r.pids.iter().enumerate() {
            let prev = if ti > 0 { r.pids[ti - 1] } else { 0 };
            if let Some(lg) = forward_step(
                arch,
                bundle,
                tid,
                prev,
                Some(&r.mac_6d),
                &mut st,
                active_shells,
                false,
                false,
                &mut dummy_diff,
                ti + 1 == r.pids.len(),
            ) {
                logits = lg;
            }
        }
        let mut dot = 0.0f64;
        let mut na = 0.0f64;
        let mut nb = 0.0f64;
        for (&a, &b) in logits.iter().zip(r.ref_logits.iter()) {
            dot += f64::from(a) * f64::from(b);
            na += f64::from(a) * f64::from(a);
            nb += f64::from(b) * f64::from(b);
        }
        cos_sum += dot / (na.sqrt() * nb.sqrt()).max(1e-12);

        let argmax_py = r
            .ref_logits
            .iter()
            .enumerate()
            .max_by(|a, b| a.1.total_cmp(b.1))
            .map(|(i, _)| i)
            .unwrap_or(0);
        let mut idx_vals: Vec<(usize, f32)> = logits.iter().copied().enumerate().collect();
        idx_vals.sort_by(|a, b| b.1.total_cmp(&a.1));
        if idx_vals.first().map(|(i, _)| *i) == Some(argmax_py) {
            top1_hits += 1;
        }
        if idx_vals.iter().take(5).any(|(i, _)| *i == argmax_py) {
            top5_hits += 1;
        }
    }
    let n = bundle.refs.len().max(1) as f64;
    (cos_sum / n, (top1_hits as f64) / n, (top5_hits as f64) / n)
}

fn run_real_muninn_fault_bench(
    arch: Arch,
    bundle: &mut MuninnBundle,
    out_path: &str,
    weights_path: &str,
) {
    // Pre-unpack all ternary layers once into compact i8 arrays for fast deterministic fault sweeps
    let layer_names: Vec<String> = {
        let mut ks: Vec<String> = bundle.ternary.keys().cloned().collect();
        ks.sort();
        ks
    };
    let mut unpacked: HashMap<String, (Vec<Vec<i8>>, Vec<f32>, Vec<f32>, f32)> = HashMap::new();
    for name in &layer_names {
        let l = &bundle.ternary[name];
        let mut qs = Vec::with_capacity(l.shells.len());
        let mut scs = Vec::with_capacity(l.shells.len());
        let mut w_full = vec![0.0f32; l.rows * l.cols];
        for sh in &l.shells {
            let q = unpack_packed4_to_i8(l.rows, l.cols, &sh.packed_col_major);
            let sc0 = sh.scales[0];
            for (idx, (dst, &qi)) in w_full.iter_mut().zip(q.iter()).enumerate() {
                let sc = match l.scale_axis {
                    ScaleAxis::Scalar => sc0,
                    ScaleAxis::PerOc => sh.scales[idx / l.cols],
                    ScaleAxis::PerIc => sh.scales[idx % l.cols],
                };
                *dst += f32::from(qi) * sc;
            }
            qs.push(q);
            scs.push(sc0);
        }
        let max_abs = w_full.iter().fold(1e-6f32, |m, &v| m.max(v.abs()));
        unpacked.insert(name.clone(), (qs, scs, w_full, max_abs));
    }

    let (clean_cos, clean_t1, clean_t5) =
        eval_bundle_refs_accuracy(arch, bundle, bundle.cfg.n_shells);
    println!(
        "Real Muninn-1 clean 3-shell baseline ({} ternary layers, {} refs): cos={:.6} top1={:.1}% top5={:.1}%",
        layer_names.len(),
        bundle.refs.len(),
        clean_cos,
        clean_t1 * 100.0,
        clean_t5 * 100.0
    );

    // 1. Photonic MZI + PCM Drift Sweep (Sb2S3 nu=0.02 & GST nu=0.09)
    let photonic_points = [
        ("Sb2S3 (nu=0.02)", 0.02f64, 0.02f64, 1.0f64),
        ("Sb2S3 (nu=0.02)", 0.02, 0.05, 1e4),
        ("Sb2S3 (nu=0.02)", 0.02, 0.10, 1e6),
        ("Sb2S3 (nu=0.02)", 0.02, 0.20, 1e6),
        ("GST (nu=0.09)", 0.09, 0.05, 1e2),
        ("GST (nu=0.09)", 0.09, 0.10, 1e4),
    ];
    let mut photonic_rows = Vec::new();
    for (p_idx, &(mat, nu, sigma, elapsed)) in photonic_points.iter().enumerate() {
        let drift = elapsed.max(1.0).powf(-nu);
        // Mode A: 8-bit Analog PCM
        let mut rng_a = SplitMix64(0xAA00_0001u64.wrapping_add(p_idx as u64 * 17));
        for name in &layer_names {
            let (_, _, w_full, max_abs) = &unpacked[name];
            let mut wf = vec![0.0f32; w_full.len()];
            for (dst, &w) in wf.iter_mut().zip(w_full.iter()) {
                let norm = (w / *max_abs).clamp(-1.0, 1.0);
                let q8 = (norm * 127.0).round() / 127.0;
                let theta = f64::from(q8).acos();
                let theta_eff =
                    (theta * drift + rng_a.next_normal() * sigma).clamp(0.0, std::f64::consts::PI);
                *dst = (theta_eff.cos() as f32) * *max_abs;
            }
            bundle.ternary.get_mut(name).unwrap().override_f32 = Some(wf);
        }
        let (a_cos, a_t1, a_t5) = eval_bundle_refs_accuracy(arch, bundle, 3);

        // Mode B: Discrete Phase-Keyed 3-Shell Ternary
        let mut rng_t = SplitMix64(0xBB00_0001u64.wrapping_add(p_idx as u64 * 17));
        for name in &layer_names {
            let (qs, scs, w_full, _) = &unpacked[name];
            let mut wf = vec![0.0f32; w_full.len()];
            for (s, q_s) in qs.iter().enumerate() {
                let sc = scs[s];
                for (dst, &qi) in wf.iter_mut().zip(q_s.iter()) {
                    let eff = match qi {
                        0 => (rng_t.next_normal() * 0.003) as f32,
                        1 => (rng_t.next_normal() * sigma).cos() as f32,
                        -1 => (std::f64::consts::PI * drift + rng_t.next_normal() * sigma).cos()
                            as f32,
                        _ => 0.0,
                    };
                    *dst += eff * sc;
                }
            }
            bundle.ternary.get_mut(name).unwrap().override_f32 = Some(wf);
        }
        let (t3_cos, t3_t1, t3_t5) = eval_bundle_refs_accuracy(arch, bundle, 3);

        // Mode C: Balanced Differential Dual-Rail MZI 3-Shell Ternary
        // Both +1 and -1 traverse matched symmetric PCM arms (canceling asymmetric DC drift before RMSNorm!)
        let mut rng_b = SplitMix64(0xBF00_0001u64.wrapping_add(p_idx as u64 * 17));
        let sym_gain = (std::f64::consts::PI * drift).cos().abs(); // symmetric attenuation on both +/- arms
        for name in &layer_names {
            let (qs, scs, w_full, _) = &unpacked[name];
            let mut wf = vec![0.0f32; w_full.len()];
            for (s, q_s) in qs.iter().enumerate() {
                let sc = scs[s];
                for (dst, &qi) in wf.iter_mut().zip(q_s.iter()) {
                    let eff = match qi {
                        0 => (rng_b.next_normal() * 0.003) as f32,
                        1 => {
                            let dtheta = rng_b.next_normal() * sigma;
                            (sym_gain * dtheta.cos()) as f32
                        }
                        -1 => {
                            let dtheta = rng_b.next_normal() * sigma;
                            (-(sym_gain * dtheta.cos())) as f32
                        }
                        _ => 0.0,
                    };
                    *dst += eff * sc;
                }
            }
            bundle.ternary.get_mut(name).unwrap().override_f32 = Some(wf);
        }
        let (tb_cos, tb_t1, tb_t5) = eval_bundle_refs_accuracy(arch, bundle, 3);

        println!(
            "Photonic {mat:15} t={elapsed:7.0}s sig={sigma:.2}rad | 8b-Analog={a_cos:.4} ({:.0}%) | 1-End 3S={t3_cos:.4} ({:.0}%) | Bal-Diff MZI 3S={tb_cos:.4} ({:.0}%)",
            a_t1 * 100.0,
            t3_t1 * 100.0,
            tb_t1 * 100.0,
        );
        photonic_rows.push(serde_json::json!({
            "material": mat,
            "phase_noise_rad": sigma,
            "elapsed_secs": elapsed,
            "analog_8bit_logit_cosine": a_cos,
            "analog_8bit_top1_acc": a_t1,
            "analog_8bit_top5_acc": a_t5,
            "ternary_3shell_single_ended_logit_cosine": t3_cos,
            "ternary_3shell_single_ended_top1_acc": t3_t1,
            "ternary_3shell_single_ended_top5_acc": t3_t5,
            "ternary_3shell_balanced_diff_mzi_logit_cosine": tb_cos,
            "ternary_3shell_balanced_diff_mzi_top1_acc": tb_t1,
            "ternary_3shell_balanced_diff_mzi_top5_acc": tb_t5,
            "balanced_diff_mzi_cosine_advantage_vs_analog": tb_cos - a_cos,
        }));
    }

    // 2. Memristor Crossbar Stuck-At Fault Sweep (0%, 5%, 10%, 15%, 20% + 5% jitter)
    let fault_rates = [0.0f64, 0.05, 0.10, 0.15, 0.20];
    let jitter = 0.05f64;
    let mut mem_rows = Vec::new();
    for (f_idx, &fr) in fault_rates.iter().enumerate() {
        // 2a: 8-bit Analog Memristor
        let mut rng_a = SplitMix64(0xCC00_0001u64.wrapping_add(f_idx as u64 * 31));
        for name in &layer_names {
            let (_, _, w_full, max_abs) = &unpacked[name];
            let mut wf = vec![0.0f32; w_full.len()];
            for (dst, &w) in wf.iter_mut().zip(w_full.iter()) {
                let u = rng_a.next_f64();
                let eff = if u < fr * 0.5 {
                    *max_abs * if rng_a.next_f64() < 0.5 { 1.0 } else { -1.0 }
                } else if u < fr {
                    0.0
                } else {
                    w * (1.0 + (rng_a.next_normal() * jitter) as f32)
                };
                *dst = eff;
            }
            bundle.ternary.get_mut(name).unwrap().override_f32 = Some(wf);
        }
        let (a_cos, a_t1, a_t5) = eval_bundle_refs_accuracy(arch, bundle, 3);

        // 2b: All-Memristor 3-Shell Ternary
        let mut rng_m = SplitMix64(0xDD00_0001u64.wrapping_add(f_idx as u64 * 31));
        for name in &layer_names {
            let (qs, scs, w_full, _) = &unpacked[name];
            let mut wf = vec![0.0f32; w_full.len()];
            for (s, q_s) in qs.iter().enumerate() {
                let sc = scs[s];
                for (dst, &qi) in wf.iter_mut().zip(q_s.iter()) {
                    let u = rng_m.next_f64();
                    let eff = if u < fr * 0.5 {
                        if rng_m.next_f64() < 0.5 { 1.0 } else { -1.0 }
                    } else if u < fr {
                        0.0
                    } else {
                        f32::from(qi) * (1.0 + (rng_m.next_normal() * jitter) as f32)
                    };
                    *dst += eff * sc;
                }
            }
            bundle.ternary.get_mut(name).unwrap().override_f32 = Some(wf);
        }
        let (m_cos, m_t1, m_t5) = eval_bundle_refs_accuracy(arch, bundle, 3);

        // 2c: Matryoshka Hybrid (Digital Hardwired ROM S1+S2 + Analog Memristor S3)
        let mut rng_h = SplitMix64(0xEE00_0001u64.wrapping_add(f_idx as u64 * 31));
        for name in &layer_names {
            let (qs, scs, w_full, _) = &unpacked[name];
            let mut wf = vec![0.0f32; w_full.len()];
            for (s, q_s) in qs.iter().enumerate() {
                let sc = scs[s];
                if s < 2 {
                    for (dst, &qi) in wf.iter_mut().zip(q_s.iter()) {
                        *dst += f32::from(qi) * sc;
                    }
                } else {
                    for (dst, &qi) in wf.iter_mut().zip(q_s.iter()) {
                        let u = rng_h.next_f64();
                        let eff = if u < fr * 0.5 {
                            if rng_h.next_f64() < 0.5 { 1.0 } else { -1.0 }
                        } else if u < fr {
                            0.0
                        } else {
                            f32::from(qi) * (1.0 + (rng_h.next_normal() * jitter) as f32)
                        };
                        *dst += eff * sc;
                    }
                }
            }
            bundle.ternary.get_mut(name).unwrap().override_f32 = Some(wf);
        }
        let (h_cos, h_t1, h_t5) = eval_bundle_refs_accuracy(arch, bundle, 3);

        println!(
            "Memristor fault={:4.0}% | 8b-Analog cos={a_cos:.4} top1={:.0}% | All-Mem 3S cos={m_cos:.4} top1={:.0}% | Matryoshka ROM_S12+Mem_S3 cos={h_cos:.4} top1={:.0}%",
            fr * 100.0,
            a_t1 * 100.0,
            m_t1 * 100.0,
            h_t1 * 100.0
        );
        mem_rows.push(serde_json::json!({
            "fault_rate_pct": fr * 100.0,
            "conductance_jitter": jitter,
            "analog_8bit_logit_cosine": a_cos,
            "analog_8bit_top1_acc": a_t1,
            "analog_8bit_top5_acc": a_t5,
            "all_memristor_3shell_logit_cosine": m_cos,
            "all_memristor_3shell_top1_acc": m_t1,
            "all_memristor_3shell_top5_acc": m_t5,
            "matryoshka_rom_s12_memristor_s3_logit_cosine": h_cos,
            "matryoshka_rom_s12_memristor_s3_top1_acc": h_t1,
            "matryoshka_rom_s12_memristor_s3_top5_acc": h_t5,
        }));
    }

    for name in &layer_names {
        bundle.ternary.get_mut(name).unwrap().override_f32 = None;
    }

    let report = serde_json::json!({
        "schema": "wm.gen4.physir.muninn1-real-photonic-memristor/1.0",
        "weights_source": weights_path,
        "ternary_layers_perturbed": layer_names.len(),
        "vocab_size": bundle.cfg.vocab_size,
        "refs_evaluated": bundle.refs.len(),
        "clean_3shell_baseline": {
            "logit_cosine": clean_cos,
            "top1_acc": clean_t1,
            "top5_acc": clean_t5,
        },
        "photonic_pcm_sweep": photonic_rows,
        "memristor_fault_sweep": mem_rows,
    });
    fs::write(out_path, serde_json::to_string_pretty(&report).unwrap())
        .expect("write fault receipt");
    println!("Wrote real Muninn-1 full-model photonic/memristor fault receipt to {out_path}");
}

fn find_arg<'a>(args: &'a [String], flag: &str) -> Option<&'a str> {
    args.windows(2)
        .find(|w| w[0] == flag)
        .map(|w| w[1].as_str())
}

fn main() {
    let args: Vec<String> = env::args().collect();
    let weights_path =
        find_arg(&args, "--weights").unwrap_or("/tmp/muninn1_smoke/muninn1_pilot.tm7b");
    let receipt_path =
        find_arg(&args, "--receipt").unwrap_or("/tmp/muninn1_smoke/muninn1_rust_e2e_receipt.json");
    let prompt = find_arg(&args, "--prompt")
        .unwrap_or("[USR] Run memory lookup in Gana Wall threshold 0.85 limit 8 cleanly.\n[AST]");
    let n_gen: usize = find_arg(&args, "--max-new")
        .and_then(|s| s.parse().ok())
        .unwrap_or(32);
    let r_loops_gen: usize = find_arg(&args, "--r-loops")
        .and_then(|s| s.parse().ok())
        .unwrap_or(2);
    let rep_penalty: f32 = find_arg(&args, "--rep-penalty")
        .and_then(|s| s.parse().ok())
        .unwrap_or(1.12);
    let stop_on_eos = !args.iter().any(|a| a == "--no-stop-on-eos");
    let skip_parity = args.iter().any(|a| a == "--skip-parity");
    let batch_jsonl = find_arg(&args, "--batch-jsonl");
    let batch_out = find_arg(&args, "--batch-out");
    let daemon_sock = find_arg(&args, "--daemon");
    let score_ppl_in = find_arg(&args, "--score-ppl");
    let score_ppl_out = find_arg(&args, "--score-out");
    let fault_bench_out = find_arg(&args, "--fault-bench");
    let load_state_path = find_arg(&args, "--load-state");
    let save_state_path = find_arg(&args, "--save-state");
    let continue_state = args.iter().any(|a| a == "--continue-state");
    let sidecar_path = find_arg(&args, "--sidecar");
    let energy_halt_tau: Option<f32> = find_arg(&args, "--energy-halt").and_then(|s| s.parse().ok());
    let temperature: f32 = find_arg(&args, "--temp")
        .and_then(|s| s.parse().ok())
        .unwrap_or(0.0);
    let top_p: f32 = find_arg(&args, "--top-p")
        .and_then(|s| s.parse().ok())
        .unwrap_or(0.92);
    let rng_seed: u64 = find_arg(&args, "--seed")
        .and_then(|s| s.parse().ok())
        .unwrap_or(20261120);
    let spec_k: usize = find_arg(&args, "--spec-k")
        .and_then(|s| s.parse().ok())
        .unwrap_or(0);
    let draft_weights_path = find_arg(&args, "--draft-weights");
    let spec_draft_shells_opt: Option<usize> = find_arg(&args, "--spec-draft-shells")
        .and_then(|s| s.parse().ok());
    let spec_draft_r: usize = find_arg(&args, "--spec-draft-r")
        .and_then(|s| s.parse().ok())
        .unwrap_or(2);
    let spec_margin_thresh: f32 = find_arg(&args, "--spec-margin-thresh")
        .and_then(|s| s.parse().ok())
        .unwrap_or(if draft_weights_path.is_some() { 1.2 } else { 2.5 });
    let prefill_chunk: usize = find_arg(&args, "--prefill-chunk")
        .and_then(|s| s.parse().ok())
        .unwrap_or(1)
        .max(1);
    let soft_prompt_paths = find_arg(&args, "--soft-prompt");
    let use_spec_ngram = args.iter().any(|a| a == "--spec-ngram");
    let profile_breakdown = args.iter().any(|a| a == "--profile-breakdown");
    let mac_opt: Option<Vec<f32>> = find_arg(&args, "--mac").map(|s| {
        s.split(',')
            .filter_map(|p| p.trim().parse::<f32>().ok())
            .collect()
    });

    let requested_shells: Option<usize> = find_arg(&args, "--shells").and_then(|s| s.parse().ok());
    let lazy_load_shells = if fault_bench_out.is_none()
        && (skip_parity || batch_jsonl.is_some() || score_ppl_in.is_some() || daemon_sock.is_some())
    {
        requested_shells
    } else {
        None
    };

    let arch = Arch::new();
    let t_load = Instant::now();
    let mut bundle = load_tm7b_v3(weights_path, lazy_load_shells);
    let _sidecar_vaults = if let Some(sp) = sidecar_path {
        let (nv, map) = load_ns3s_sidecar(sp);
        bundle.sidecars = map;
        nv
    } else {
        0
    };
    let load_ms = t_load.elapsed().as_secs_f64() * 1000.0;
    if let Some(fb_out) = fault_bench_out {
        run_real_muninn_fault_bench(arch, &mut bundle, fb_out, weights_path);
        return;
    }
    if profile_breakdown {
        build_draft_tmac_mirrors(&mut bundle);
        let d = bundle.cfg.d_model;
        let dff = bundle.cfg.d_ff;
        let v = bundle.cfg.vocab_size;
        let x1 = vec![0.1_f32; d];
        let xs5 = vec![vec![0.1_f32; d]; 5];
        let xff1 = vec![0.1_f32; dff];
        let xsff5 = vec![vec![0.1_f32; dff]; 5];
        let mut dummy = 0i32;

        let t = Instant::now();
        let _ = linear_vec(arch, &bundle.floats["tok_emb.weight"], &x1, v, d);
        let lm_f32_1 = t.elapsed().as_secs_f64() * 1000.0;

        let t = Instant::now();
        let _ = linear_mat_f32_k(arch, &bundle.floats["tok_emb.weight"], &xs5, v, d);
        let lm_f32_5 = t.elapsed().as_secs_f64() * 1000.0;

        let t = Instant::now();
        let _ = eval_tmac_ternary_gemv(arch, &bundle.floats_tmac["tok_emb.weight"], &x1, 1, true, false, &mut dummy);
        let lm_tmac_1 = t.elapsed().as_secs_f64() * 1000.0;

        let t = Instant::now();
        let _ = linear_vec(arch, &bundle.floats["prelude.0.w_up.weight"], &x1, dff, d);
        let _ = linear_vec(arch, &bundle.floats["prelude.0.w_down.weight"], &xff1, d, dff);
        let pre_f32_1 = t.elapsed().as_secs_f64() * 1000.0;

        let t = Instant::now();
        let _ = linear_mat_f32_k(arch, &bundle.floats["prelude.0.w_up.weight"], &xs5, dff, d);
        let _ = linear_mat_f32_k(arch, &bundle.floats["prelude.0.w_down.weight"], &xsff5, d, dff);
        let pre_f32_5 = t.elapsed().as_secs_f64() * 1000.0;

        let t = Instant::now();
        let _ = eval_tmac_ternary_gemv(arch, &bundle.floats_tmac["prelude.0.w_up.weight"], &x1, 1, true, false, &mut dummy);
        let _ = eval_tmac_ternary_gemv(arch, &bundle.floats_tmac["prelude.0.w_down.weight"], &xff1, 1, true, false, &mut dummy);
        let pre_tmac_1 = t.elapsed().as_secs_f64() * 1000.0;

        let l_up = &bundle.ternary["core.0.w_up"];
        let l_dn = &bundle.ternary["core.0.w_down"];
        let t = Instant::now();
        let _ = eval_tmac_ternary_gemv(arch, l_up, &x1, 3, true, false, &mut dummy);
        let _ = eval_tmac_ternary_gemv(arch, l_dn, &xff1, 3, true, false, &mut dummy);
        let tmac_s3_1 = t.elapsed().as_secs_f64() * 1000.0;

        let t = Instant::now();
        let _ = eval_tmac_ternary_gemm_k(arch, l_up, &xs5, 3, true);
        let _ = eval_tmac_ternary_gemm_k(arch, l_dn, &xsff5, 3, true);
        let tmac_s3_5 = t.elapsed().as_secs_f64() * 1000.0;

        let t = Instant::now();
        let _ = eval_tmac_ternary_gemv(arch, l_up, &x1, 1, true, false, &mut dummy);
        let _ = eval_tmac_ternary_gemv(arch, l_dn, &xff1, 1, true, false, &mut dummy);
        let tmac_s1_1 = t.elapsed().as_secs_f64() * 1000.0;

        let mut st_s3 = MuninnRuntimeState::new(&bundle.cfg, 4, 32);
        let t = Instant::now();
        let _ = forward_step(arch, &bundle, 10, 1, None, &mut st_s3, 3, true, false, &mut dummy, true);
        let full_s3r4_1 = t.elapsed().as_secs_f64() * 1000.0;

        let mut st_s3_5 = MuninnRuntimeState::new(&bundle.cfg, 4, 32);
        let t = Instant::now();
        let _ = forward_batch_k(arch, &bundle, &[10, 11, 12, 13, 14], &[1, 10, 11, 12, 13], None, &mut st_s3_5, 3, true, false, false, LogitsMode::All);
        let full_s3r4_5 = t.elapsed().as_secs_f64() * 1000.0;

        let mut st_s1_r2 = MuninnRuntimeState::new(&bundle.cfg, 2, 32);
        let t = Instant::now();
        let _ = forward_batch_k(arch, &bundle, &[10], &[1], None, &mut st_s1_r2, 1, true, true, false, LogitsMode::LastOnly);
        let full_s1r2_tmac_1 = t.elapsed().as_secs_f64() * 1000.0;

        let mut st_s1_r1 = MuninnRuntimeState::new(&bundle.cfg, 1, 32);
        let t = Instant::now();
        let _ = forward_batch_k(arch, &bundle, &[10], &[1], None, &mut st_s1_r1, 1, true, true, false, LogitsMode::LastOnly);
        let full_s1r1_tmac_1 = t.elapsed().as_secs_f64() * 1000.0;

        println!(
            "{}",
            serde_json::to_string_pretty(&serde_json::json!({
                "lm_f32_k1_ms": (lm_f32_1 * 100.0).round() / 100.0,
                "lm_f32_k5_ms": (lm_f32_5 * 100.0).round() / 100.0,
                "lm_tmac_k1_ms": (lm_tmac_1 * 100.0).round() / 100.0,
                "pre_ffn_f32_k1_ms": (pre_f32_1 * 100.0).round() / 100.0,
                "pre_ffn_f32_k5_ms": (pre_f32_5 * 100.0).round() / 100.0,
                "pre_ffn_tmac_k1_ms": (pre_tmac_1 * 100.0).round() / 100.0,
                "tmac_ffn_s3_k1_ms": (tmac_s3_1 * 100.0).round() / 100.0,
                "tmac_ffn_s3_k5_ms": (tmac_s3_5 * 100.0).round() / 100.0,
                "tmac_ffn_s1_k1_ms": (tmac_s1_1 * 100.0).round() / 100.0,
                "full_s3r4_f32_k1_ms": (full_s3r4_1 * 100.0).round() / 100.0,
                "full_s3r4_f32_k5_ms": (full_s3r4_5 * 100.0).round() / 100.0,
                "full_s1r2_tmac_k1_ms": (full_s1r2_tmac_1 * 100.0).round() / 100.0,
                "full_s1r1_tmac_k1_ms": (full_s1r1_tmac_1 * 100.0).round() / 100.0,
            }))
            .unwrap()
        );
        return;
    }
    let active_shells: usize = requested_shells
        .unwrap_or(bundle.cfg.n_shells)
        .clamp(1, bundle.cfg.n_shells);
    let draft_bundle_opt: Option<MuninnBundle> = if spec_k > 0 {
        draft_weights_path.map(|dp| {
            let db = load_tm7b_v3(dp, spec_draft_shells_opt);
            assert_eq!(
                db.cfg.vocab_size, bundle.cfg.vocab_size,
                "Cross-organ --draft-weights vocab_size mismatch ({} vs {})",
                db.cfg.vocab_size, bundle.cfg.vocab_size
            );
            assert_eq!(
                db.tok.id_to_piece, bundle.tok.id_to_piece,
                "Cross-organ --draft-weights tokenizer mismatch! Both organs must share the exact same tokenizer."
            );
            db
        })
    } else {
        None
    };
    let i8_prep_ms = if spec_k > 0 && draft_bundle_opt.is_none() {
        let t_i8 = Instant::now();
        build_draft_tmac_mirrors(&mut bundle);
        t_i8.elapsed().as_secs_f64() * 1000.0
    } else {
        0.0
    };
    let soft_prompt_vecs: Vec<Vec<f32>> = soft_prompt_paths
        .map(|csv| load_cspd_soft_prompts(csv, bundle.cfg.d_model))
        .unwrap_or_default();

    // Optional Fast Perplexity / Entropy Curator Mode (`--score-ppl` — 0-CU Black Garden v3 Quality Gate!)
    if let Some(in_path) = score_ppl_in {
        let bos_id = *bundle.tok.vocab.get("[BOS]").unwrap_or(&1);
        let raw = fs::read_to_string(in_path).expect("read score-ppl jsonl");
        let mut out_rows = Vec::new();
        for line in raw.lines() {
            let trimmed = line.trim();
            if trimmed.is_empty() {
                continue;
            }
            let item: Value = serde_json::from_str(trimmed).expect("parse score-ppl item");
            let id = item["id"].as_str().unwrap_or("").to_string();
            let txt = item["text"].as_str().unwrap_or("");
            let max_toks = item["max_tokens"]
                .as_u64()
                .map(|v| v as usize)
                .unwrap_or(192);
            let mut ids = vec![bos_id];
            ids.extend(bundle.tok.encode(txt).into_iter().take(max_toks));
            if ids.len() < 3 {
                continue;
            }
            let mut rt = MuninnRuntimeState::new(&bundle.cfg, r_loops_gen, ids.len() + 2);
            let mut dummy_diff = 0i32;
            let mut nll_sum = 0.0_f64;
            let mut scored_n = 0usize;
            let t0 = Instant::now();
            for ti in 0..(ids.len() - 1) {
                let prev = if ti > 0 { ids[ti - 1] } else { 0 };
                if let Some(lg) = forward_step(
                    arch,
                    &bundle,
                    ids[ti],
                    prev,
                    None,
                    &mut rt,
                    active_shells,
                    true,
                    false,
                    &mut dummy_diff,
                    true,
                ) {
                    let tgt = ids[ti + 1] as usize;
                    if tgt < lg.len() {
                        let mx = lg.iter().copied().fold(f32::NEG_INFINITY, f32::max);
                        let mut sum_e = 0.0_f64;
                        for &v in &lg {
                            sum_e += f64::from((v - mx).exp());
                        }
                        let lse = f64::from(mx) + sum_e.max(1e-12).ln();
                        nll_sum += lse - f64::from(lg[tgt]);
                        scored_n += 1;
                    }
                }
            }
            let mean_nll = nll_sum / (scored_n.max(1) as f64);
            let ppl = mean_nll.min(20.0).exp();
            let decision = if ppl < 1.8 {
                "PRUNE_BOILERPLATE"
            } else if ppl > 350.0 {
                "PRUNE_GARBAGE"
            } else {
                "GOLDILOCKS_KEEP"
            };
            let elapsed_ms = t0.elapsed().as_secs_f64() * 1000.0;
            out_rows.push(serde_json::json!({
                "id": id,
                "tokens_scored": scored_n,
                "mean_nll": (mean_nll * 10000.0).round() / 10000.0,
                "ppl": (ppl * 100.0).round() / 100.0,
                "decision": decision,
                "active_shells": active_shells,
                "elapsed_ms": (elapsed_ms * 100.0).round() / 100.0,
            }));
        }
        let out_str = out_rows
            .iter()
            .map(|r| serde_json::to_string(r).unwrap())
            .collect::<Vec<_>>()
            .join("\n")
            + "\n";
        if let Some(op) = score_ppl_out {
            fs::write(op, &out_str).expect("write score-ppl out");
        }
        println!(
            "{}",
            serde_json::json!({
                "scored_docs": out_rows.len(),
                "active_shells": active_shells,
                "load_ms": (load_ms * 100.0).round() / 100.0,
                "peak_rss_mb": (read_vm_hwm_mb() * 100.0).round() / 100.0,
                "results": out_rows,
            })
        );
        return;
    }

    // Optional Fast Batch JSONL Mode & Zero-Cold-Load Resident UNIX-Socket Daemon (`--daemon <sock>`)
    // Includes 24-entry Unified Radix Cache (URC) for Hybrid DeltaNet-2 + RoPE prefix state snapshots!
    if batch_jsonl.is_some() || daemon_sock.is_some() {
        let bos_id = *bundle.tok.vocab.get("[BOS]").unwrap_or(&1);
        let eos_id = bundle.tok.vocab.get("[EOS]").copied();
        let mut urc = UnifiedRadixCache::new();
        let b_chunk_default = if prefill_chunk > 1 { prefill_chunk } else { 16usize };

        let run_one_item = |item: &Value, urc: &mut UnifiedRadixCache| -> Value {
            let id = item["id"].as_str().unwrap_or("").to_string();
            let p_str = item["prompt"].as_str().unwrap_or("");
            let m_new = item["max_new"]
                .as_u64()
                .map(|v| v as usize)
                .unwrap_or(n_gen);
            let rp = item["rep_penalty"]
                .as_f64()
                .map(|v| v as f32)
                .unwrap_or(rep_penalty);
            let r_l = item["r_loops"]
                .as_u64()
                .map(|v| v as usize)
                .unwrap_or(r_loops_gen);
            let item_shells = item["shells"]
                .as_u64()
                .map(|v| (v as usize).clamp(1, bundle.cfg.n_shells))
                .unwrap_or(active_shells);
            let item_temp = item["temp"]
                .as_f64()
                .map(|v| v as f32)
                .unwrap_or(temperature);
            let item_top_p = item["top_p"]
                .as_f64()
                .map(|v| v as f32)
                .unwrap_or(top_p);
            let item_seed = item["seed"].as_u64().unwrap_or(rng_seed);
            let mut item_rng = SplitMix64(item_seed);
            let mac_v: Option<Vec<f32>> = item["mac_6d"].as_array().map(|arr| {
                arr.iter()
                    .filter_map(|v| v.as_f64().map(|x| x as f32))
                    .collect()
            });
            let mac_bits: Vec<u32> = mac_v
                .as_ref()
                .map(|v| v.iter().map(|x| x.to_bits()).collect())
                .unwrap_or_default();
            let item_sp = item["soft_prompt"]
                .as_str()
                .filter(|s| !s.is_empty())
                .map(|s| load_cspd_soft_prompts(s, bundle.cfg.d_model));
            let sp_vecs = item_sp.as_ref().unwrap_or(&soft_prompt_vecs);
            let item_load_hms = item["load_state"].as_str().filter(|s| !s.is_empty());
            let item_save_hms = item["save_state"].as_str().filter(|s| !s.is_empty());

            let mut gen_ids = vec![bos_id];
            gen_ids.extend(bundle.tok.encode(p_str));
            let prompt_len = gen_ids.len();
            let mut rt = MuninnRuntimeState::new(
                &bundle.cfg,
                r_l,
                prompt_len + sp_vecs.len() + m_new + 4,
            );
            rt.energy_halt_tau = energy_halt_tau;
            let loaded_hms_b = item_load_hms
                .map(|p| rt.load_hologram(p, &bundle.cfg))
                .unwrap_or(0);
            let mut dummy_diff = 0i32;
            let mut last_logits = Vec::new();
            let mut urc_hit_toks = 0usize;
            let t0 = Instant::now();
            if !sp_vecs.is_empty() {
                let _ = forward_batch_k(
                    arch, &bundle, &[bos_id], &[0], mac_v.as_deref(),
                    &mut rt, item_shells, true, false, false, LogitsMode::None,
                );
                let mut sp_i = 0usize;
                while sp_i < sp_vecs.len() {
                    let sp_end = (sp_i + b_chunk_default).min(sp_vecs.len());
                    let _ = forward_batch_k_ext(
                        arch, &bundle, &[], &[], Some(&sp_vecs[sp_i..sp_end]),
                        None, &mut rt, item_shells, true, false, false, LogitsMode::None,
                    );
                    sp_i = sp_end;
                }
                let mut b_i = 1usize;
                while b_i < prompt_len {
                    let b_end = (b_i + b_chunk_default).min(prompt_len);
                    let chunk_toks = &gen_ids[b_i..b_end];
                    let chunk_prev: Vec<u32> = (b_i..b_end).map(|idx| gen_ids[idx - 1]).collect();
                    let lm = if b_end == prompt_len { LogitsMode::LastOnly } else { LogitsMode::None };
                    let (mut lgs, _) = forward_batch_k(
                        arch, &bundle, chunk_toks, &chunk_prev, None,
                        &mut rt, item_shells, true, false, false, lm,
                    );
                    if b_end == prompt_len {
                        last_logits = lgs.pop().flatten().unwrap_or_default();
                    }
                    b_i = b_end;
                }
            } else {
                let mut b_i = 0usize;
                if item_load_hms.is_none() && prompt_len > 16 {
                    if let Some((cached_len, snap_rt)) =
                        urc.lookup(r_l, item_shells, &mac_bits, &gen_ids)
                    {
                        rt = snap_rt;
                        rt.energy_halt_tau = energy_halt_tau;
                        b_i = cached_len;
                        urc_hit_toks = cached_len;
                    }
                }
                while b_i < prompt_len {
                    let next_ckpt = if b_i < 16 && prompt_len > 16 {
                        16
                    } else if b_i < 32 && prompt_len > 32 {
                        32
                    } else {
                        b_i + b_chunk_default
                    };
                    let b_end = next_ckpt.min(b_i + b_chunk_default).min(prompt_len);
                    let chunk_toks = &gen_ids[b_i..b_end];
                    let chunk_prev: Vec<u32> = (b_i..b_end)
                        .map(|idx| if idx > 0 { gen_ids[idx - 1] } else { 0 })
                        .collect();
                    let lm = if b_end == prompt_len {
                        LogitsMode::LastOnly
                    } else {
                        LogitsMode::None
                    };
                    let mac_arg = if b_i == 0 { mac_v.as_deref() } else { None };
                    let (mut lgs, _) = forward_batch_k(
                        arch,
                        &bundle,
                        chunk_toks,
                        &chunk_prev,
                        mac_arg,
                        &mut rt,
                        item_shells,
                        true,
                        false,
                        false,
                        lm,
                    );
                    if item_load_hms.is_none() && (b_end == 16 || b_end == 32) && b_end < prompt_len {
                        urc.insert(r_l, item_shells, &mac_bits, &gen_ids[0..b_end], &rt);
                    }
                    if b_end == prompt_len {
                        last_logits = lgs.pop().flatten().unwrap_or_default();
                    }
                    b_i = b_end;
                }
            }
            rt.energy_halt_active = energy_halt_tau.is_some();
            let mut first_margin = 0.0_f32;
            let mut max_margin = 0.0_f32;
            let mut digit_margins: Vec<f32> = Vec::new();
            for step_i in 0..m_new {
                if rp > 1.0 && !last_logits.is_empty() {
                    let mut seen = gen_ids.clone();
                    seen.sort_unstable();
                    seen.dedup();
                    for tid in seen {
                        let idx = tid as usize;
                        if idx < last_logits.len() {
                            if last_logits[idx] > 0.0 {
                                last_logits[idx] /= rp;
                            } else {
                                last_logits[idx] *= rp;
                            }
                        }
                    }
                }
                let mut best_i = 0usize;
                let mut best_v = f32::NEG_INFINITY;
                let mut second_v = f32::NEG_INFINITY;
                for (idx, &lv) in last_logits.iter().enumerate() {
                    if lv > best_v {
                        second_v = best_v;
                        best_v = lv;
                        best_i = idx;
                    } else if lv > second_v {
                        second_v = lv;
                    }
                }
                let margin = (best_v - second_v).max(0.0);
                if step_i == 0 {
                    first_margin = margin;
                }
                if margin > max_margin {
                    max_margin = margin;
                }
                let piece = bundle
                    .tok
                    .id_to_piece
                    .get(best_i)
                    .map(String::as_str)
                    .unwrap_or("");
                if piece.contains('1')
                    || piece.contains('2')
                    || piece.contains('3')
                    || piece.contains('4')
                    || piece.contains('5')
                {
                    digit_margins.push(margin);
                }
                let chosen_i = if item_temp > 1e-5 {
                    sample_token_top_p(&last_logits, item_temp, item_top_p, &mut item_rng)
                } else {
                    best_i
                };
                let next_tok = chosen_i as u32;
                let prev = *gen_ids.last().unwrap_or(&0);
                gen_ids.push(next_tok);
                if stop_on_eos && eos_id == Some(next_tok) {
                    break;
                }
                if step_i + 1 >= m_new {
                    break;
                }
                last_logits = forward_step(
                    arch,
                    &bundle,
                    next_tok,
                    prev,
                    None,
                    &mut rt,
                    item_shells,
                    true,
                    false,
                    &mut dummy_diff,
                    true,
                )
                .unwrap_or_default();
            }
            let saved_hms_b = item_save_hms
                .map(|p| rt.save_hologram(p, &bundle.cfg))
                .unwrap_or(0);
            let elapsed_ms = t0.elapsed().as_secs_f64() * 1000.0;
            let gen_only = bundle.tok.decode(&gen_ids[prompt_len..]);
            let tot_dec = rt.halted_r2_tokens + rt.full_r_tokens;
            serde_json::json!({
                "id": id,
                "gen_text": gen_only,
                "first_margin": (f64::from(first_margin) * 1000.0).round() / 1000.0,
                "pick_margin": (f64::from(digit_margins.first().copied().unwrap_or(first_margin)) * 1000.0).round() / 1000.0,
                "max_margin": (f64::from(max_margin) * 1000.0).round() / 1000.0,
                "elapsed_ms": (elapsed_ms * 100.0).round() / 100.0,
                "urc_hit_tokens": urc_hit_toks,
                "hologram_loaded_bytes": loaded_hms_b,
                "hologram_saved_bytes": saved_hms_b,
                "energy_halted_r2": rt.halted_r2_tokens,
                "energy_full_r": rt.full_r_tokens,
                "mean_energy": if tot_dec > 0 { (rt.energy_sum / (tot_dec as f64) * 1000.0).round() / 1000.0 } else { 0.0 },
            })
        };

        if let Some(in_path) = batch_jsonl {
            let raw = fs::read_to_string(in_path).expect("read batch jsonl");
            let mut results = Vec::new();
            for line in raw.lines() {
                let trimmed = line.trim();
                if trimmed.is_empty() {
                    continue;
                }
                let item: Value = serde_json::from_str(trimmed).expect("parse batch item");
                results.push(run_one_item(&item, &mut urc));
            }
            let rss_mb = read_vm_hwm_mb();
            let out_str = results
                .iter()
                .map(|r| serde_json::to_string(r).unwrap())
                .collect::<Vec<_>>()
                .join("\n")
                + "\n";
            if let Some(op) = batch_out {
                fs::write(op, &out_str).expect("write batch out");
            }
            println!(
                "{}",
                serde_json::json!({
                    "batch_count": results.len(),
                    "load_ms": (load_ms * 100.0).round() / 100.0,
                    "peak_rss_mb": (rss_mb * 100.0).round() / 100.0,
                    "bundle_version": bundle.cfg.bundle_version,
                    "urc_hits": urc.hits,
                    "urc_misses": urc.misses,
                    "urc_saved_prefill_tokens": urc.saved_prefill_tokens,
                })
            );
            return;
        }

        if let Some(sock_path) = daemon_sock {
            let _ = fs::remove_file(sock_path);
            let listener = UnixListener::bind(sock_path).expect("bind unix daemon socket");
            println!(
                "{}",
                serde_json::json!({
                    "status": "daemon_ready",
                    "socket": sock_path,
                    "weights": weights_path,
                    "load_ms": (load_ms * 100.0).round() / 100.0,
                    "peak_rss_mb": (read_vm_hwm_mb() * 100.0).round() / 100.0,
                })
            );
            for stream_res in listener.incoming() {
                if let Ok(mut stream) = stream_res {
                    let mut line = String::new();
                    let mut reader = BufReader::new(&stream);
                    if reader.read_line(&mut line).is_ok() && !line.trim().is_empty() {
                        if let Ok(req) = serde_json::from_str::<Value>(line.trim()) {
                            let cmd_str = req["cmd"].as_str().unwrap_or("");
                            if cmd_str == "ping" {
                                let resp = serde_json::json!({
                                    "status": "ok",
                                    "weights": weights_path,
                                    "urc_hits": urc.hits,
                                    "urc_misses": urc.misses,
                                    "urc_saved_prefill_tokens": urc.saved_prefill_tokens,
                                    "peak_rss_mb": (read_vm_hwm_mb() * 100.0).round() / 100.0,
                                });
                                let _ = writeln!(stream, "{}", resp);
                                continue;
                            } else if cmd_str == "shutdown" {
                                let _ = writeln!(stream, "{}", serde_json::json!({"status": "shutting_down"}));
                                let _ = fs::remove_file(sock_path);
                                return;
                            } else if let Some(arr) = req["batch"].as_array() {
                                let outs: Vec<Value> = arr.iter().map(|it| run_one_item(it, &mut urc)).collect();
                                let resp = serde_json::json!({
                                    "status": "ok",
                                    "batch_count": outs.len(),
                                    "urc_hits": urc.hits,
                                    "urc_misses": urc.misses,
                                    "urc_saved_prefill_tokens": urc.saved_prefill_tokens,
                                    "peak_rss_mb": (read_vm_hwm_mb() * 100.0).round() / 100.0,
                                    "results": outs,
                                });
                                let _ = writeln!(stream, "{}", resp);
                                continue;
                            } else {
                                let resp = run_one_item(&req, &mut urc);
                                let _ = writeln!(stream, "{}", resp);
                                continue;
                            }
                        }
                    }
                }
            }
            return;
        }
    }

    // 1. Exact PyTorch-vs-Rust Reference Logit Parity (via O(1) DeltaNet-2 step engine!) + AVX2 Parity
    let mut max_int_diff = 0i32;
    let mut max_logit_diff = 0.0_f32;
    let mut top1_matches = 0usize;
    let mut ref0_logits = Vec::new();
    if !skip_parity {
        for (ri, r) in bundle.refs.iter().enumerate() {
            let mut st = MuninnRuntimeState::new(&bundle.cfg, 4, r.pids.len() + 2);
            let mut logits = Vec::new();
            for (ti, &tid) in r.pids.iter().enumerate() {
                let prev = if ti > 0 { r.pids[ti - 1] } else { 0 };
                if let Some(lg) = forward_step(
                    arch,
                    &bundle,
                    tid,
                    prev,
                    Some(&r.mac_6d),
                    &mut st,
                    active_shells,
                    false,
                    ri == 0 && ti == 0,
                    &mut max_int_diff,
                    ti + 1 == r.pids.len(),
                ) {
                    logits = lg;
                }
            }
            if ri == 0 {
                ref0_logits = logits.clone();
            }
            let argmax_rust = logits
                .iter()
                .enumerate()
                .max_by(|a, b| a.1.total_cmp(b.1))
                .map(|(i, _)| i)
                .unwrap_or(0);
            let argmax_py = r
                .ref_logits
                .iter()
                .enumerate()
                .max_by(|a, b| a.1.total_cmp(b.1))
                .map(|(i, _)| i)
                .unwrap_or(0);
            if argmax_rust == argmax_py {
                top1_matches += 1;
            }
            for (&a, &b) in logits.iter().zip(r.ref_logits.iter()) {
                max_logit_diff = max_logit_diff.max((a - b).abs());
            }
        }
    }

    // 2. Guna Triadic Multi-Shell Probe (Shell 1 Tamas / Shell 2 Rajas / Shell 3 Sattva)
    let mut guna_probes = Vec::new();
    if !skip_parity && let Some(r0) = bundle.refs.first() {
        let guna_names = ["Tamas_1.58b", "Rajas_3.17b", "Sattva_4.75b"];
        let argmax_py = r0
            .ref_logits
            .iter()
            .enumerate()
            .max_by(|a, b| a.1.total_cmp(b.1))
            .map(|(i, _)| i)
            .unwrap_or(0);
        for sh in 1..=bundle.cfg.n_shells {
            let mut dummy = 0i32;
            let mut st = MuninnRuntimeState::new(&bundle.cfg, 4, r0.pids.len() + 2);
            let mut lg = Vec::new();
            for (ti, &tid) in r0.pids.iter().enumerate() {
                let prev = if ti > 0 { r0.pids[ti - 1] } else { 0 };
                if let Some(l) = forward_step(
                    arch,
                    &bundle,
                    tid,
                    prev,
                    Some(&r0.mac_6d),
                    &mut st,
                    sh,
                    false,
                    false,
                    &mut dummy,
                    ti + 1 == r0.pids.len(),
                ) {
                    lg = l;
                }
            }
            let am = lg
                .iter()
                .enumerate()
                .max_by(|a, b| a.1.total_cmp(b.1))
                .map(|(i, _)| i)
                .unwrap_or(0);
            let cos = cosine_sim(&lg, &r0.ref_logits);
            let md = lg
                .iter()
                .zip(r0.ref_logits.iter())
                .map(|(a, b)| (a - b).abs())
                .fold(0.0_f32, f32::max);
            guna_probes.push(serde_json::json!({
                "shells": sh,
                "guna": guna_names.get(sh - 1).copied().unwrap_or("Extra"),
                "bits_per_weight": ((sh as f64) * 1.58496 * 100.0).round() / 100.0,
                "ref0_cosine_vs_pytorch_full": (cos * 1_000_000.0).round() / 1_000_000.0,
                "ref0_max_logit_diff": md,
                "ref0_top1_match_full": am == argmax_py,
            }));
        }
        let _ = ref0_logits.len();
    }

    // 3. Real Pure-CPU Autoregressive Generation Benchmark (O(1) DeltaNet-2 + T-MAC + Clean EOS)
    let bos_id = *bundle.tok.vocab.get("[BOS]").unwrap_or(&1);
    let eos_id = bundle.tok.vocab.get("[EOS]").copied();
    let mut gen_ids = if continue_state && load_state_path.is_some() {
        Vec::new()
    } else {
        vec![bos_id]
    };
    gen_ids.extend(bundle.tok.encode(prompt));
    let mut rt = MuninnRuntimeState::new(
        &bundle.cfg,
        r_loops_gen,
        gen_ids.len() + soft_prompt_vecs.len() + n_gen + 4,
    );
    let loaded_hms_bytes = load_state_path
        .map(|p| rt.load_hologram(p, &bundle.cfg))
        .unwrap_or(0);
    let mut dummy_diff = 0i32;
    let mut last_logits = Vec::new();
    let use_spec = spec_k > 0 && temperature <= 0.0 && rep_penalty <= 1.0 && n_gen > 0;
    let draft_bundle_ref: &MuninnBundle = draft_bundle_opt.as_ref().unwrap_or(&bundle);
    let use_draft_mirror = draft_bundle_opt.is_none();
    let draft_shells = spec_draft_shells_opt
        .unwrap_or(if draft_bundle_opt.is_some() { draft_bundle_ref.cfg.n_shells } else { 1 })
        .clamp(1, draft_bundle_ref.cfg.n_shells);
    let draft_r = if draft_bundle_opt.is_some() {
        spec_draft_r.clamp(1, 8)
    } else {
        spec_draft_r.clamp(1, r_loops_gen)
    };
    let draft_core_slots = draft_r * draft_bundle_ref.cfg.n_core_blocks;
    let mut rt_draft_opt = if use_spec {
        let mut rtd = MuninnRuntimeState::new(
            &draft_bundle_ref.cfg,
            draft_r,
            gen_ids.len() + soft_prompt_vecs.len() + n_gen + spec_k + 4,
        );
        if let Some(p) = load_state_path {
            if draft_bundle_opt.is_none() && draft_r == r_loops_gen {
                let _ = rtd.load_hologram(p, &draft_bundle_ref.cfg);
            }
        }
        Some(rtd)
    } else {
        None
    };

    let t_prefill = Instant::now();
    if !soft_prompt_vecs.is_empty() && !gen_ids.is_empty() {
        let _ = forward_batch_k(
            arch, &bundle, &[gen_ids[0]], &[0], mac_opt.as_deref(),
            &mut rt, active_shells, true, false, false, LogitsMode::None,
        );
        if let Some(ref mut rtd) = rt_draft_opt {
            let _ = forward_batch_k(
                arch, draft_bundle_ref, &[gen_ids[0]], &[0], mac_opt.as_deref(),
                rtd, draft_shells, true, use_draft_mirror, false, LogitsMode::None,
            );
        }
        let mut sp_i = 0usize;
        while sp_i < soft_prompt_vecs.len() {
            let sp_end = (sp_i + 16).min(soft_prompt_vecs.len());
            let _ = forward_batch_k_ext(
                arch, &bundle, &[], &[], Some(&soft_prompt_vecs[sp_i..sp_end]),
                None, &mut rt, active_shells, true, false, false, LogitsMode::None,
            );
            if let Some(ref mut rtd) = rt_draft_opt {
                if draft_bundle_ref.cfg.d_model == bundle.cfg.d_model {
                    let _ = forward_batch_k_ext(
                        arch, draft_bundle_ref, &[], &[], Some(&soft_prompt_vecs[sp_i..sp_end]),
                        None, rtd, draft_shells, true, use_draft_mirror, false, LogitsMode::None,
                    );
                }
            }
            sp_i = sp_end;
        }
        let total = gen_ids.len();
        let step_c = prefill_chunk.max(8);
        let mut i = 1usize;
        while i < total {
            let end = (i + step_c).min(total);
            let chunk_toks = &gen_ids[i..end];
            let chunk_prev: Vec<u32> = (i..end).map(|idx| gen_ids[idx - 1]).collect();
            let lm = if end == total { LogitsMode::LastOnly } else { LogitsMode::None };
            let (mut lgs, _) = forward_batch_k(
                arch, &bundle, chunk_toks, &chunk_prev, None,
                &mut rt, active_shells, true, false, false, lm,
            );
            if end == total {
                last_logits = lgs.pop().flatten().unwrap_or_default();
            }
            i = end;
        }
        if let Some(ref mut rtd) = rt_draft_opt {
            if total > 1 {
                let all_prev: Vec<u32> = (1..total).map(|idx| gen_ids[idx - 1]).collect();
                let _ = forward_batch_k(
                    arch, draft_bundle_ref, &gen_ids[1..total], &all_prev, None,
                    rtd, draft_shells, true, use_draft_mirror, false, LogitsMode::None,
                );
            }
        }
    } else if prefill_chunk > 1 {
        let total = gen_ids.len();
        let mut i = 0usize;
        while i < total {
            let end = (i + prefill_chunk).min(total);
            let chunk_toks = &gen_ids[i..end];
            let chunk_prev: Vec<u32> = (i..end).map(|idx| if idx > 0 { gen_ids[idx - 1] } else { 0 }).collect();
            let lm = if end == total { LogitsMode::LastOnly } else { LogitsMode::None };
            let (mut lgs, _) = forward_batch_k(
                arch,
                &bundle,
                chunk_toks,
                &chunk_prev,
                mac_opt.as_deref(),
                &mut rt,
                active_shells,
                true,
                false,
                false,
                lm,
            );
            if end == total {
                last_logits = lgs.pop().flatten().unwrap_or_default();
            }
            i = end;
        }
        if let Some(ref mut rtd) = rt_draft_opt {
            if total > 0 {
                let all_prev: Vec<u32> = (0..total).map(|idx| if idx > 0 { gen_ids[idx - 1] } else { 0 }).collect();
                let _ = forward_batch_k(
                    arch,
                    draft_bundle_ref,
                    &gen_ids,
                    &all_prev,
                    mac_opt.as_deref(),
                    rtd,
                    draft_shells,
                    true,
                    use_draft_mirror,
                    false,
                    LogitsMode::None,
                );
            }
        }
    } else {
        for (ti, &tid) in gen_ids.iter().enumerate() {
            let prev = if ti > 0 { gen_ids[ti - 1] } else { 0 };
            if let Some(lg) = forward_step(
                arch,
                &bundle,
                tid,
                prev,
                mac_opt.as_deref(),
                &mut rt,
                active_shells,
                true,
                false,
                &mut dummy_diff,
                ti + 1 == gen_ids.len(),
            ) {
                last_logits = lg;
            }
        }
        if let Some(ref mut rtd) = rt_draft_opt {
            let total = gen_ids.len();
            if total > 0 {
                let all_prev: Vec<u32> = (0..total).map(|idx| if idx > 0 { gen_ids[idx - 1] } else { 0 }).collect();
                let _ = forward_batch_k(
                    arch,
                    draft_bundle_ref,
                    &gen_ids,
                    &all_prev,
                    mac_opt.as_deref(),
                    rtd,
                    draft_shells,
                    true,
                    use_draft_mirror,
                    false,
                    LogitsMode::None,
                );
            }
        }
    }
    let ttft_ms = t_prefill.elapsed().as_secs_f64() * 1000.0;
    rt.energy_halt_tau = energy_halt_tau;
    rt.energy_halt_active = energy_halt_tau.is_some();

    let t_gen = Instant::now();
    let mut actual_gen = 0usize;
    let mut stopped_on_eos = false;
    let mut single_rng = SplitMix64(rng_seed);
    let mut spec_rounds = 0usize;
    let mut spec_drafted = 0usize;
    let mut spec_accepted = 0usize;

    if use_spec {
        let rtd = rt_draft_opt.as_mut().expect("draft state");
        let argmax_and_margin = |lg: &[f32]| -> (u32, f32) {
            let mut best_i = 0u32;
            let mut top1 = f32::NEG_INFINITY;
            let mut top2 = f32::NEG_INFINITY;
            for (i, &v) in lg.iter().enumerate() {
                if v > top1 {
                    top2 = top1;
                    top1 = v;
                    best_i = i as u32;
                } else if v > top2 {
                    top2 = v;
                }
            }
            (best_i, top1 - top2)
        };
        let argmax_id = |lg: &[f32]| -> u32 { argmax_and_margin(lg).0 };
        let mut pending_draft_tok: Option<(u32, u32)> = None;
        while actual_gen < n_gen {
            let t0_tok = argmax_id(&last_logits);
            let prev_before_t0 = *gen_ids.last().unwrap_or(&0);
            if stop_on_eos && eos_id == Some(t0_tok) {
                gen_ids.push(t0_tok);
                actual_gen += 1;
                stopped_on_eos = true;
                break;
            }
            let remaining_after_t0 = n_gen.saturating_sub(actual_gen + 1);
            let cur_k = spec_k.min(remaining_after_t0);
            if cur_k == 0 {
                gen_ids.push(t0_tok);
                actual_gen += 1;
                let _ = forward_step(
                    arch, &bundle, t0_tok, prev_before_t0, None, &mut rt,
                    active_shells, true, false, &mut dummy_diff, false,
                );
                break;
            }

            if let Some((ptok, pprev)) = pending_draft_tok.take() {
                let _ = forward_batch_k(
                    arch, draft_bundle_ref, &[ptok], &[pprev], None, rtd,
                    draft_shells, true, use_draft_mirror, false, LogitsMode::None,
                );
            }

            // 1. Draft up to `cur_k` speculative tokens on `rtd` with Margin-Gated Early Stop
            let base_pos = rt.pos;
            let draft_base_pos = rtd.pos;
            let mut verify_toks: Vec<u32> = Vec::with_capacity(cur_k + 1);
            let mut verify_prevs: Vec<u32> = Vec::with_capacity(cur_k + 1);
            verify_toks.push(t0_tok);
            verify_prevs.push(prev_before_t0);

            let mut draft_snaps: Vec<DeltaSnapshot> = Vec::with_capacity(cur_k);
            let ngram_drafts = if use_spec_ngram {
                draft_ngram_lookahead(&gen_ids, t0_tok, cur_k, stop_on_eos, eos_id)
            } else {
                Vec::new()
            };
            let used_ngram = !ngram_drafts.is_empty();
            if used_ngram {
                let mut cur_prev = t0_tok;
                for &d_next in &ngram_drafts {
                    verify_toks.push(d_next);
                    verify_prevs.push(cur_prev);
                    cur_prev = d_next;
                }
            } else {
                let mut cur_tok = t0_tok;
                let mut cur_prev = prev_before_t0;
                for _ in 0..cur_k {
                    let (mut dlgs, _) = forward_batch_k(
                        arch, draft_bundle_ref, &[cur_tok], &[cur_prev], None, rtd,
                        draft_shells, true, use_draft_mirror, false, LogitsMode::LastOnly,
                    );
                    draft_snaps.push(rtd.snapshot_deltas_slots(draft_core_slots));
                    let (d_next, d_margin) = argmax_and_margin(&dlgs.pop().flatten().unwrap_or_default());
                    verify_toks.push(d_next);
                    verify_prevs.push(cur_tok);
                    cur_prev = cur_tok;
                    cur_tok = d_next;
                    if (stop_on_eos && eos_id == Some(d_next)) || d_margin < spec_margin_thresh {
                        break;
                    }
                }
            }
            let n_drafted = verify_toks.len() - 1;
            spec_rounds += 1;
            spec_drafted += n_drafted;

            // 2. Single-pass `K`-batched EXACT verification in full `(active_shells, r_loops_gen)` with L1d-pinned per-step snapshots
            let (target_lgs, target_snaps) = forward_batch_k(
                arch,
                &bundle,
                &verify_toks,
                &verify_prevs,
                None,
                &mut rt,
                active_shells,
                true,
                false,
                true,
                LogitsMode::All,
            );

            // 3. Exact greedy verification & O(1) 0.2ms `DeltaNet-2 + AttnKv` rollback (ZERO replay matmuls!)
            let mut accepted_drafts = 0usize;
            let mut next_target_logits = target_lgs[0].clone().unwrap_or_default();
            for j in 0..n_drafted {
                let tgt_pred = argmax_id(target_lgs[j].as_ref().unwrap());
                if tgt_pred == verify_toks[j + 1] {
                    accepted_drafts += 1;
                    next_target_logits = target_lgs[j + 1].clone().unwrap_or_default();
                } else {
                    next_target_logits = target_lgs[j].clone().unwrap_or_default();
                    break;
                }
            }
            spec_accepted += accepted_drafts;

            let emit_count = 1 + accepted_drafts;
            let mut hit_eos_at: Option<usize> = None;
            for j in 0..emit_count {
                let tok_out = verify_toks[j];
                gen_ids.push(tok_out);
                actual_gen += 1;
                if stop_on_eos && eos_id == Some(tok_out) {
                    stopped_on_eos = true;
                    hit_eos_at = Some(j);
                    break;
                }
                if actual_gen >= n_gen {
                    hit_eos_at = Some(j);
                    break;
                }
            }

            let keep_steps = hit_eos_at.map(|j| j + 1).unwrap_or(emit_count);
            if keep_steps < verify_toks.len() {
                rt.restore_to_pos(
                    base_pos + keep_steps,
                    &target_snaps[keep_steps - 1],
                    bundle.cfg.d_model,
                );
            }
            if hit_eos_at.is_some() {
                break;
            }
            if used_ngram {
                let _ = forward_batch_k(
                    arch,
                    draft_bundle_ref,
                    &verify_toks[..emit_count],
                    &verify_prevs[..emit_count],
                    None,
                    rtd,
                    draft_shells,
                    true,
                    use_draft_mirror,
                    false,
                    LogitsMode::None,
                );
            } else if accepted_drafts < n_drafted {
                rtd.restore_to_pos(
                    draft_base_pos + emit_count,
                    &draft_snaps[emit_count - 1],
                    draft_bundle_ref.cfg.d_model,
                );
            } else {
                let last_idx = n_drafted;
                pending_draft_tok = Some((verify_toks[last_idx], verify_prevs[last_idx]));
            }
            last_logits = next_target_logits;
        }
    } else {
        for _ in 0..n_gen {
            if rep_penalty > 1.0 && !last_logits.is_empty() {
                let mut seen = gen_ids.clone();
                seen.sort_unstable();
                seen.dedup();
                for tid in seen {
                    let idx = tid as usize;
                    if idx < last_logits.len() {
                        if last_logits[idx] > 0.0 {
                            last_logits[idx] /= rep_penalty;
                        } else {
                            last_logits[idx] *= rep_penalty;
                        }
                    }
                }
            }
            let next_tok = sample_token_top_p(&last_logits, temperature, top_p, &mut single_rng) as u32;
            let prev = *gen_ids.last().unwrap_or(&0);
            gen_ids.push(next_tok);
            actual_gen += 1;
            if stop_on_eos && eos_id == Some(next_tok) {
                stopped_on_eos = true;
                break;
            }
            last_logits = forward_step(
                arch,
                &bundle,
                next_tok,
                prev,
                None,
                &mut rt,
                active_shells,
                true,
                false,
                &mut dummy_diff,
                true,
            )
            .unwrap_or_default();
        }
    }
    let gen_s = t_gen.elapsed().as_secs_f64();
    let saved_hms_bytes = save_state_path
        .map(|p| rt.save_hologram(p, &bundle.cfg))
        .unwrap_or(0);
    let tok_per_sec = (actual_gen.max(1) as f64) / gen_s.max(1e-6);
    let decoded = bundle.tok.decode(&gen_ids);
    let rss_mb = read_vm_hwm_mb();
    let spec_acc_pct = if spec_drafted > 0 {
        (100.0 * (spec_accepted as f64) / (spec_drafted as f64) * 100.0).round() / 100.0
    } else {
        0.0
    };
    let spec_mean_emit = if spec_rounds > 0 {
        ((actual_gen as f64) / (spec_rounds as f64) * 100.0).round() / 100.0
    } else {
        0.0
    };

    let report = serde_json::json!({
        "schema": "wm.gen4.muninn1-rust-e2e/1.0",
        "bundle_format": format!("TM7B\\x{:02x}", bundle.cfg.bundle_version),
        "weights": weights_path,
        "hologram_state_loaded_bytes": loaded_hms_bytes,
        "hologram_state_saved_bytes": saved_hms_bytes,
        "architecture": {
            "d_model": bundle.cfg.d_model,
            "n_heads": bundle.cfg.n_heads,
            "d_ff": bundle.cfg.d_ff,
            "n_core_blocks": bundle.cfg.n_core_blocks,
            "core_delta_mask": bundle.cfg.core_delta_mask,
            "n_shells": bundle.cfg.n_shells,
            "active_shells": active_shells,
            "mac_dim": bundle.cfg.mac_dim,
            "vocab_size": bundle.cfg.vocab_size,
            "engram_slots": bundle.cfg.engram_slots,
            "engram_dim": bundle.cfg.engram_dim,
        },
        "load_ms": (load_ms * 100.0).round() / 100.0,
        "i8_prep_ms": (i8_prep_ms * 100.0).round() / 100.0,
        "ttft_ms": (ttft_ms * 100.0).round() / 100.0,
        "decode_ms": (gen_s * 1000.0 * 100.0).round() / 100.0,
        "refs_checked": bundle.refs.len(),
        "pytorch_top1_agreement_pct": 100.0 * (top1_matches as f64) / (bundle.refs.len().max(1) as f64),
        "max_logit_diff": max_logit_diff,
        "max_avx2_vs_scalar_int32_diff": max_int_diff,
        "guna_triadic_shell_probe": guna_probes,
        "r_loops_gen": r_loops_gen,
        "prefill_chunk": prefill_chunk,
        "soft_prompt_tokens": soft_prompt_vecs.len(),
        "spec_k": if use_spec { spec_k } else { 0 },
        "draft_weights": draft_weights_path.unwrap_or(""),
        "spec_draft_shells": draft_shells,
        "spec_draft_r": draft_r,
        "spec_rounds": spec_rounds,
        "spec_drafted": spec_drafted,
        "spec_accepted": spec_accepted,
        "spec_accept_rate_pct": spec_acc_pct,
        "spec_mean_emit_per_round": spec_mean_emit,
        "gen_tokens": actual_gen,
        "gen_max_new": n_gen,
        "stopped_on_eos": stopped_on_eos,
        "gen_tok_per_sec_r2": (tok_per_sec * 100.0).round() / 100.0,
        "decode_tok_per_sec": (tok_per_sec * 100.0).round() / 100.0,
        "energy_halted_r2": rt.halted_r2_tokens,
        "energy_full_r": rt.full_r_tokens,
        "decoded_sample": decoded,
        "peak_rss_mb": (rss_mb * 100.0).round() / 100.0,
    });
    if let Ok(mut f) = fs::File::create(receipt_path) {
        let _ = writeln!(f, "{}", serde_json::to_string_pretty(&report).unwrap());
    }
    println!("{}", serde_json::to_string_pretty(&report).unwrap());
}
