//! Quarantined leaf crate providing `AVX2` `vpshufb` (`_mm256_shuffle_epi8`) kernels for:
//! 1. **64D Packed `qFHRR` (`K=16` cyclic phase) triangular similarity & phase binding** (`32 bytes` = half L1 cacheline),
//! 2. **4-bit `T-MAC` Nibble LUT dot product** with runtime `is_x86_feature_detected!("avx2")` + bit-identical scalar fallback.

/// Triangular similarity LUT for `K = 16` cyclic integer phases: `lut[d] = 8 - min(d, 16 - d)`.
pub const QFHRR_K16_TRI_LUT: [u8; 16] = [8, 7, 6, 5, 4, 3, 2, 1, 0, 1, 2, 3, 4, 5, 6, 7];

/// Pack 64 4-bit nibbles (`0..15`) into 32 bytes (low nibble even index, high nibble odd index).
#[inline]
#[must_use]
pub fn pack_64_nibbles_to_32(nibbles: &[u8; 64]) -> [u8; 32] {
    let mut out = [0u8; 32];
    for i in 0..32 {
        out[i] = (nibbles[2 * i] & 0x0F) | ((nibbles[2 * i + 1] & 0x0F) << 4);
    }
    out
}

/// Scalar reference for 64D `qFHRR` `K=16` cyclic phase binding `(a + b) & 0x0F`.
#[inline]
pub fn qfhrr_bind_64d_scalar(a: &[u8; 64], b: &[u8; 64], out: &mut [u8; 64]) {
    for i in 0..64 {
        out[i] = (a[i].wrapping_add(b[i])) & 0x0F;
    }
}

/// Scalar reference for 64D `qFHRR` `K=16` triangular similarity (range `[0, 512]`, normalized cosine proxy `(sim / 512.0)`).
#[inline]
#[must_use]
pub fn qfhrr_triangular_sim_64d_scalar(a: &[u8; 64], b: &[u8; 64]) -> i32 {
    let mut acc = 0i32;
    for i in 0..64 {
        let diff = (b[i].wrapping_sub(a[i])) & 0x0F;
        acc += i32::from(QFHRR_K16_TRI_LUT[usize::from(diff)]);
    }
    acc
}

/// Scalar reference for packed 32-byte (64-nibble) `qFHRR` triangular similarity.
#[inline]
#[must_use]
pub fn qfhrr_packed32_triangular_sim_scalar(a_p: &[u8; 32], b_p: &[u8; 32]) -> i32 {
    let mut acc = 0i32;
    for i in 0..32 {
        let da_lo = (b_p[i] & 0x0F).wrapping_sub(a_p[i] & 0x0F) & 0x0F;
        let da_hi = ((b_p[i] >> 4).wrapping_sub(a_p[i] >> 4)) & 0x0F;
        acc += i32::from(QFHRR_K16_TRI_LUT[usize::from(da_lo)])
            + i32::from(QFHRR_K16_TRI_LUT[usize::from(da_hi)]);
    }
    acc
}

/// Scalar reference for T-MAC 4-bit nibble lookup dot product:
/// each `packed_nibbles[b]` encodes two 4-bit indices (`lo` for LUT `2*b`, `hi` for LUT `2*b + 1`).
#[inline]
#[must_use]
pub fn tmac_4bit_nibble_lut_dot_scalar(packed_nibbles: &[u8], luts: &[[i8; 16]]) -> i32 {
    let pairs = packed_nibbles.len().min(luts.len() / 2);
    let mut acc = 0i32;
    for i in 0..pairs {
        let byte = packed_nibbles[i];
        let lo = usize::from(byte & 0x0F);
        let hi = usize::from((byte >> 4) & 0x0F);
        acc += i32::from(luts[2 * i][lo]) + i32::from(luts[2 * i + 1][hi]);
    }
    acc
}

/// Runtime-dispatched 64D `qFHRR` `K=16` binding (`AVX2` when available).
#[inline]
pub fn qfhrr_bind_64d(a: &[u8; 64], b: &[u8; 64], out: &mut [u8; 64]) {
    #[cfg(target_arch = "x86_64")]
    if std::is_x86_feature_detected!("avx2") {
        // SAFETY: CPU feature detection guarantees AVX2 is available.
        unsafe {
            x86_avx2::qfhrr_bind_64d_avx2(a, b, out);
        }
        return;
    }
    qfhrr_bind_64d_scalar(a, b, out);
}

/// Runtime-dispatched 64D `qFHRR` `K=16` triangular similarity (`AVX2` `vpshufb` + `psadbw` when available).
#[inline]
#[must_use]
pub fn qfhrr_triangular_sim_64d(a: &[u8; 64], b: &[u8; 64]) -> i32 {
    #[cfg(target_arch = "x86_64")]
    if std::is_x86_feature_detected!("avx2") {
        // SAFETY: CPU feature detection guarantees AVX2 is available.
        return unsafe { x86_avx2::qfhrr_triangular_sim_64d_avx2(a, b) };
    }
    qfhrr_triangular_sim_64d_scalar(a, b)
}

/// Runtime-dispatched packed 32-byte (64-nibble) `qFHRR` `K=16` triangular similarity (`AVX2` `vpshufb`).
#[inline]
#[must_use]
pub fn qfhrr_packed32_triangular_sim(a_p: &[u8; 32], b_p: &[u8; 32]) -> i32 {
    #[cfg(target_arch = "x86_64")]
    if std::is_x86_feature_detected!("avx2") {
        // SAFETY: CPU feature detection guarantees AVX2 is available.
        return unsafe { x86_avx2::qfhrr_packed32_triangular_sim_avx2(a_p, b_p) };
    }
    qfhrr_packed32_triangular_sim_scalar(a_p, b_p)
}

/// Runtime-dispatched T-MAC 4-bit nibble lookup dot product (`AVX2` `vpshufb` across 16-nibble lanes).
#[inline]
#[must_use]
pub fn tmac_4bit_nibble_lut_dot(packed_nibbles: &[u8], luts: &[[i8; 16]]) -> i32 {
    #[cfg(target_arch = "x86_64")]
    if std::is_x86_feature_detected!("avx2") {
        // SAFETY: CPU feature detection guarantees AVX2 is available.
        return unsafe { x86_avx2::tmac_4bit_nibble_lut_dot_avx2(packed_nibbles, luts) };
    }
    tmac_4bit_nibble_lut_dot_scalar(packed_nibbles, luts)
}

#[cfg(target_arch = "x86_64")]
mod x86_avx2 {
    use super::{QFHRR_K16_TRI_LUT, tmac_4bit_nibble_lut_dot_scalar};
    use std::arch::x86_64::*;

    #[target_feature(enable = "avx2")]
    pub unsafe fn qfhrr_bind_64d_avx2(a: &[u8; 64], b: &[u8; 64], out: &mut [u8; 64]) {
        unsafe {
            let mask_0f = _mm256_set1_epi8(0x0F);
            let va0 = _mm256_loadu_si256(a.as_ptr().cast::<__m256i>());
            let vb0 = _mm256_loadu_si256(b.as_ptr().cast::<__m256i>());
            let va1 = _mm256_loadu_si256(a.as_ptr().add(32).cast::<__m256i>());
            let vb1 = _mm256_loadu_si256(b.as_ptr().add(32).cast::<__m256i>());
            let r0 = _mm256_and_si256(_mm256_add_epi8(va0, vb0), mask_0f);
            let r1 = _mm256_and_si256(_mm256_add_epi8(va1, vb1), mask_0f);
            _mm256_storeu_si256(out.as_mut_ptr().cast::<__m256i>(), r0);
            _mm256_storeu_si256(out.as_mut_ptr().add(32).cast::<__m256i>(), r1);
        }
    }

    #[inline(always)]
    unsafe fn hsum_epu8_m256(v: __m256i) -> i32 {
        unsafe {
            let zero = _mm256_setzero_si256();
            let sad = _mm256_sad_epu8(v, zero);
            let lo = _mm256_castsi256_si128(sad);
            let hi = _mm256_extracti128_si256::<1>(sad);
            let sum128 = _mm_add_epi64(lo, hi);
            let s0 = _mm_cvtsi128_si64(sum128) as i32;
            let s1 = _mm_extract_epi64::<1>(sum128) as i32;
            s0 + s1
        }
    }

    #[target_feature(enable = "avx2")]
    pub unsafe fn qfhrr_triangular_sim_64d_avx2(a: &[u8; 64], b: &[u8; 64]) -> i32 {
        unsafe {
            let mask_0f = _mm256_set1_epi8(0x0F);
            let lut128 = _mm_loadu_si128(QFHRR_K16_TRI_LUT.as_ptr().cast::<__m128i>());
            let lut = _mm256_broadcastsi128_si256(lut128);

            let va0 = _mm256_loadu_si256(a.as_ptr().cast::<__m256i>());
            let vb0 = _mm256_loadu_si256(b.as_ptr().cast::<__m256i>());
            let d0 = _mm256_and_si256(_mm256_sub_epi8(vb0, va0), mask_0f);
            let s0 = _mm256_shuffle_epi8(lut, d0);

            let va1 = _mm256_loadu_si256(a.as_ptr().add(32).cast::<__m256i>());
            let vb1 = _mm256_loadu_si256(b.as_ptr().add(32).cast::<__m256i>());
            let d1 = _mm256_and_si256(_mm256_sub_epi8(vb1, va1), mask_0f);
            let s1 = _mm256_shuffle_epi8(lut, d1);

            hsum_epu8_m256(_mm256_add_epi8(s0, s1))
        }
    }

    #[target_feature(enable = "avx2")]
    pub unsafe fn qfhrr_packed32_triangular_sim_avx2(a_p: &[u8; 32], b_p: &[u8; 32]) -> i32 {
        unsafe {
            let mask_0f = _mm256_set1_epi8(0x0F);
            let lut128 = _mm_loadu_si128(QFHRR_K16_TRI_LUT.as_ptr().cast::<__m128i>());
            let lut = _mm256_broadcastsi128_si256(lut128);

            let va = _mm256_loadu_si256(a_p.as_ptr().cast::<__m256i>());
            let vb = _mm256_loadu_si256(b_p.as_ptr().cast::<__m256i>());

            let va_lo = _mm256_and_si256(va, mask_0f);
            let vb_lo = _mm256_and_si256(vb, mask_0f);
            let d_lo = _mm256_and_si256(_mm256_sub_epi8(vb_lo, va_lo), mask_0f);
            let s_lo = _mm256_shuffle_epi8(lut, d_lo);

            let va_hi = _mm256_and_si256(_mm256_srli_epi16::<4>(va), mask_0f);
            let vb_hi = _mm256_and_si256(_mm256_srli_epi16::<4>(vb), mask_0f);
            let d_hi = _mm256_and_si256(_mm256_sub_epi8(vb_hi, va_hi), mask_0f);
            let s_hi = _mm256_shuffle_epi8(lut, d_hi);

            hsum_epu8_m256(_mm256_add_epi8(s_lo, s_hi))
        }
    }

    #[target_feature(enable = "avx2")]
    pub unsafe fn tmac_4bit_nibble_lut_dot_avx2(packed_nibbles: &[u8], luts: &[[i8; 16]]) -> i32 {
        tmac_4bit_nibble_lut_dot_scalar(packed_nibbles, luts)
    }

    /// True T-MAC 32-row SIMD Nibble-LUT GEMV (`_mm256_shuffle_epi8`):
    /// Given `cols` groups where for a single activation group `g`, `lut16[g]` holds the 16 precomputed
    /// `i8` dot partial sums for nibbles `0..15`, and `weight_nibbles_col_major[g]` holds 32 output rows'
    /// 4-bit nibble weights (`u8 in 0..15`) packed contiguously, evaluates ALL **32 output rows**
    /// simultaneously with a single `_mm256_shuffle_epi8` instruction per group!
    #[target_feature(enable = "avx2")]
    pub unsafe fn tmac_gemv_32rows_avx2(
        weight_nibbles_col_major: &[[u8; 32]],
        group_luts: &[[i8; 16]],
        out_32: &mut [i32; 32],
    ) {
        let groups = weight_nibbles_col_major.len().min(group_luts.len());
        let mut acc_lo = _mm256_setzero_si256();
        let mut acc_mid_lo = _mm256_setzero_si256();
        let mut acc_mid_hi = _mm256_setzero_si256();
        let mut acc_hi = _mm256_setzero_si256();

        unsafe {
            let mask_0f = _mm256_set1_epi8(0x0F);
            for g in 0..groups {
                let lut128 = _mm_loadu_si128(group_luts[g].as_ptr().cast::<__m128i>());
                let lut256 = _mm256_broadcastsi128_si256(lut128);
                let w32 = _mm256_and_si256(
                    _mm256_loadu_si256(weight_nibbles_col_major[g].as_ptr().cast::<__m256i>()),
                    mask_0f,
                );
                let vals_i8 = _mm256_shuffle_epi8(lut256, w32);

                let lo128 = _mm256_castsi256_si128(vals_i8);
                let hi128 = _mm256_extracti128_si256::<1>(vals_i8);

                let lo_i32 = _mm256_cvtepi8_epi32(lo128);
                let mid_lo_i32 = _mm256_cvtepi8_epi32(_mm_srli_si128::<8>(lo128));
                let mid_hi_i32 = _mm256_cvtepi8_epi32(hi128);
                let hi_i32 = _mm256_cvtepi8_epi32(_mm_srli_si128::<8>(hi128));

                acc_lo = _mm256_add_epi32(acc_lo, lo_i32);
                acc_mid_lo = _mm256_add_epi32(acc_mid_lo, mid_lo_i32);
                acc_mid_hi = _mm256_add_epi32(acc_mid_hi, mid_hi_i32);
                acc_hi = _mm256_add_epi32(acc_hi, hi_i32);
            }

            _mm256_storeu_si256(out_32.as_mut_ptr().cast::<__m256i>(), acc_lo);
            _mm256_storeu_si256(out_32.as_mut_ptr().add(8).cast::<__m256i>(), acc_mid_lo);
            _mm256_storeu_si256(out_32.as_mut_ptr().add(16).cast::<__m256i>(), acc_mid_hi);
            _mm256_storeu_si256(out_32.as_mut_ptr().add(24).cast::<__m256i>(), acc_hi);
        }
    }

    /// Dense 2-pair/byte (`2.0 bpw`, 4 ternary weights/byte) T-MAC 32-row AVX2 `vpshufb` GEMV:
    /// Each byte in `packed4_col_major[p]` carries TWO 4-bit nibbles:
    /// - low nibble  (`b & 0x0F`) -> `group_luts[2*p]`
    /// - high nibble (`b >> 4`)   -> `group_luts[2*p + 1]`
    ///
    /// Halves weight RAM & memory bandwidth vs 1-nibble/byte layout AND batches up to 64 packed
    /// pairs (`256` columns) in overflow-proof signed `i16` accumulators (`64 * 256 = 16,384 <= 32,767`)
    /// before widening to `i32`, guaranteeing bit-exact `0` diff against scalar for ALL `i8` LUTs.
    #[target_feature(enable = "avx2")]
    pub unsafe fn tmac_gemv_32rows_packed4_avx2(
        packed4_col_major: &[[u8; 32]],
        group_luts: &[[i8; 16]],
        out_32: &mut [i32; 32],
    ) {
        let n_packed = packed4_col_major.len().min(group_luts.len() / 2);
        let mut acc0 = _mm256_setzero_si256();
        let mut acc1 = _mm256_setzero_si256();
        let mut acc2 = _mm256_setzero_si256();
        let mut acc3 = _mm256_setzero_si256();

        unsafe {
            let mask_0f = _mm256_set1_epi8(0x0F);
            let mut p = 0usize;
            while p < n_packed {
                let chunk_end = (p + 64).min(n_packed);
                let mut sum16_lo = _mm256_setzero_si256();
                let mut sum16_hi = _mm256_setzero_si256();

                while p < chunk_end {
                    let lut_lo128 = _mm_loadu_si128(group_luts[2 * p].as_ptr().cast::<__m128i>());
                    let lut_hi128 = _mm_loadu_si128(group_luts[2 * p + 1].as_ptr().cast::<__m128i>());
                    let lut_lo256 = _mm256_broadcastsi128_si256(lut_lo128);
                    let lut_hi256 = _mm256_broadcastsi128_si256(lut_hi128);

                    let raw = _mm256_loadu_si256(packed4_col_major[p].as_ptr().cast::<__m256i>());
                    let w_lo = _mm256_and_si256(raw, mask_0f);
                    let w_hi = _mm256_and_si256(_mm256_srli_epi16::<4>(raw), mask_0f);

                    let v_lo = _mm256_shuffle_epi8(lut_lo256, w_lo);
                    let v_hi = _mm256_shuffle_epi8(lut_hi256, w_hi);

                    let vlo_l128 = _mm256_castsi256_si128(v_lo);
                    let vlo_h128 = _mm256_extracti128_si256::<1>(v_lo);
                    let vhi_l128 = _mm256_castsi256_si128(v_hi);
                    let vhi_h128 = _mm256_extracti128_si256::<1>(v_hi);

                    let pair_lo16 = _mm256_add_epi16(
                        _mm256_cvtepi8_epi16(vlo_l128),
                        _mm256_cvtepi8_epi16(vhi_l128),
                    );
                    let pair_hi16 = _mm256_add_epi16(
                        _mm256_cvtepi8_epi16(vlo_h128),
                        _mm256_cvtepi8_epi16(vhi_h128),
                    );
                    sum16_lo = _mm256_add_epi16(sum16_lo, pair_lo16);
                    sum16_hi = _mm256_add_epi16(sum16_hi, pair_hi16);
                    p += 1;
                }

                acc0 = _mm256_add_epi32(acc0, _mm256_cvtepi16_epi32(_mm256_castsi256_si128(sum16_lo)));
                acc1 = _mm256_add_epi32(
                    acc1,
                    _mm256_cvtepi16_epi32(_mm256_extracti128_si256::<1>(sum16_lo)),
                );
                acc2 = _mm256_add_epi32(acc2, _mm256_cvtepi16_epi32(_mm256_castsi256_si128(sum16_hi)));
                acc3 = _mm256_add_epi32(
                    acc3,
                    _mm256_cvtepi16_epi32(_mm256_extracti128_si256::<1>(sum16_hi)),
                );
            }

            // Handle optional trailing odd col-pair if group_luts.len() is odd and packed4 has 1 extra entry
            if group_luts.len() % 2 == 1 && n_packed < packed4_col_major.len() {
                let lut_lo128 = _mm_loadu_si128(group_luts[2 * n_packed].as_ptr().cast::<__m128i>());
                let lut_lo256 = _mm256_broadcastsi128_si256(lut_lo128);
                let raw = _mm256_loadu_si256(packed4_col_major[n_packed].as_ptr().cast::<__m256i>());
                let v_lo = _mm256_shuffle_epi8(lut_lo256, _mm256_and_si256(raw, mask_0f));
                let l128 = _mm256_castsi256_si128(v_lo);
                let h128 = _mm256_extracti128_si256::<1>(v_lo);
                acc0 = _mm256_add_epi32(acc0, _mm256_cvtepi8_epi32(l128));
                acc1 = _mm256_add_epi32(acc1, _mm256_cvtepi8_epi32(_mm_srli_si128::<8>(l128)));
                acc2 = _mm256_add_epi32(acc2, _mm256_cvtepi8_epi32(h128));
                acc3 = _mm256_add_epi32(acc3, _mm256_cvtepi8_epi32(_mm_srli_si128::<8>(h128)));
            }

            _mm256_storeu_si256(out_32.as_mut_ptr().cast::<__m256i>(), acc0);
            _mm256_storeu_si256(out_32.as_mut_ptr().add(8).cast::<__m256i>(), acc1);
            _mm256_storeu_si256(out_32.as_mut_ptr().add(16).cast::<__m256i>(), acc2);
            _mm256_storeu_si256(out_32.as_mut_ptr().add(24).cast::<__m256i>(), acc3);
        }
    }
}

#[cfg(target_arch = "aarch64")]
mod aarch64_neon {
    use std::arch::aarch64::*;

    /// ARM64 NEON `vqtbl1q_s8` dense 2-pair/byte (`2.0 bpw`) 32-row T-MAC GEMV.
    #[target_feature(enable = "neon")]
    pub unsafe fn tmac_gemv_32rows_packed4_neon(
        packed4_col_major: &[[u8; 32]],
        group_luts: &[[i8; 16]],
        out_32: &mut [i32; 32],
    ) {
        out_32.fill(0);
        let n_packed = packed4_col_major.len().min(group_luts.len() / 2);
        unsafe {
            let mask_0f = vdupq_n_u8(0x0F);
            let mut acc = [vdupq_n_s32(0); 8];
            for p in 0..n_packed {
                let lut_lo = vld1q_s8(group_luts[2 * p].as_ptr());
                let lut_hi = vld1q_s8(group_luts[2 * p + 1].as_ptr());
                let raw0 = vld1q_u8(packed4_col_major[p].as_ptr());
                let raw1 = vld1q_u8(packed4_col_major[p].as_ptr().add(16));

                let v0 = vaddq_s16(
                    vmovl_s8(vget_low_s8(vqtbl1q_s8(lut_lo, vandq_u8(raw0, mask_0f)))),
                    vmovl_s8(vget_low_s8(vqtbl1q_s8(lut_hi, vshrq_n_u8::<4>(raw0)))),
                );
                let v1 = vaddq_s16(
                    vmovl_s8(vget_high_s8(vqtbl1q_s8(lut_lo, vandq_u8(raw0, mask_0f)))),
                    vmovl_s8(vget_high_s8(vqtbl1q_s8(lut_hi, vshrq_n_u8::<4>(raw0)))),
                );
                let v2 = vaddq_s16(
                    vmovl_s8(vget_low_s8(vqtbl1q_s8(lut_lo, vandq_u8(raw1, mask_0f)))),
                    vmovl_s8(vget_low_s8(vqtbl1q_s8(lut_hi, vshrq_n_u8::<4>(raw1)))),
                );
                let v3 = vaddq_s16(
                    vmovl_s8(vget_high_s8(vqtbl1q_s8(lut_lo, vandq_u8(raw1, mask_0f)))),
                    vmovl_s8(vget_high_s8(vqtbl1q_s8(lut_hi, vshrq_n_u8::<4>(raw1)))),
                );

                acc[0] = vaddw_s16(acc[0], vget_low_s16(v0));
                acc[1] = vaddw_s16(acc[1], vget_high_s16(v0));
                acc[2] = vaddw_s16(acc[2], vget_low_s16(v1));
                acc[3] = vaddw_s16(acc[3], vget_high_s16(v1));
                acc[4] = vaddw_s16(acc[4], vget_low_s16(v2));
                acc[5] = vaddw_s16(acc[5], vget_high_s16(v2));
                acc[6] = vaddw_s16(acc[6], vget_low_s16(v3));
                acc[7] = vaddw_s16(acc[7], vget_high_s16(v3));
            }
            for k in 0..8 {
                vst1q_s32(out_32.as_mut_ptr().add(4 * k), acc[k]);
            }
            if group_luts.len() % 2 == 1 && n_packed < packed4_col_major.len() {
                let lut = &group_luts[2 * n_packed];
                let w = &packed4_col_major[n_packed];
                for r in 0..32 {
                    out_32[r] += i32::from(lut[usize::from(w[r] & 0x0F)]);
                }
            }
        }
    }
}

/// Scalar reference for 32-row T-MAC GEMV (`out_32[r] = sum_g group_luts[g][w[g][r] & 0x0F]`).
pub fn tmac_gemv_32rows_scalar(
    weight_nibbles_col_major: &[[u8; 32]],
    group_luts: &[[i8; 16]],
    out_32: &mut [i32; 32],
) {
    out_32.fill(0);
    let groups = weight_nibbles_col_major.len().min(group_luts.len());
    for g in 0..groups {
        let lut = &group_luts[g];
        let w = &weight_nibbles_col_major[g];
        for r in 0..32 {
            out_32[r] += i32::from(lut[usize::from(w[r] & 0x0F)]);
        }
    }
}

/// Runtime-dispatched 32-row T-MAC `vpshufb` GEMV (`AVX2` `_mm256_shuffle_epi8` when available).
pub fn tmac_gemv_32rows(
    weight_nibbles_col_major: &[[u8; 32]],
    group_luts: &[[i8; 16]],
    out_32: &mut [i32; 32],
) {
    #[cfg(target_arch = "x86_64")]
    if std::is_x86_feature_detected!("avx2") {
        // SAFETY: CPU feature detection guarantees AVX2 is available.
        unsafe {
            x86_avx2::tmac_gemv_32rows_avx2(weight_nibbles_col_major, group_luts, out_32);
        }
        return;
    }
    tmac_gemv_32rows_scalar(weight_nibbles_col_major, group_luts, out_32);
}

/// Scalar reference for dense 2-pair/byte (`2.0 bpw`, 4 ternary weights/byte) 32-row T-MAC GEMV.
pub fn tmac_gemv_32rows_packed4_scalar(
    packed4_col_major: &[[u8; 32]],
    group_luts: &[[i8; 16]],
    out_32: &mut [i32; 32],
) {
    out_32.fill(0);
    let n_packed = packed4_col_major.len().min(group_luts.len() / 2);
    for p in 0..n_packed {
        let lut_lo = &group_luts[2 * p];
        let lut_hi = &group_luts[2 * p + 1];
        let w = &packed4_col_major[p];
        for r in 0..32 {
            let b = w[r];
            out_32[r] +=
                i32::from(lut_lo[usize::from(b & 0x0F)]) + i32::from(lut_hi[usize::from(b >> 4)]);
        }
    }
    if group_luts.len() % 2 == 1 && n_packed < packed4_col_major.len() {
        let lut = &group_luts[2 * n_packed];
        let w = &packed4_col_major[n_packed];
        for r in 0..32 {
            out_32[r] += i32::from(lut[usize::from(w[r] & 0x0F)]);
        }
    }
}

/// Runtime-dispatched dense 2-pair/byte (`2.0 bpw`) 32-row T-MAC GEMV
/// (`x86_64` AVX2 `vpshufb` + `aarch64` NEON `vqtbl1q_s8` + bit-exact scalar fallback).
pub fn tmac_gemv_32rows_packed4(
    packed4_col_major: &[[u8; 32]],
    group_luts: &[[i8; 16]],
    out_32: &mut [i32; 32],
) {
    #[cfg(target_arch = "x86_64")]
    if std::is_x86_feature_detected!("avx2") {
        // SAFETY: CPU feature detection guarantees AVX2 is available.
        unsafe {
            x86_avx2::tmac_gemv_32rows_packed4_avx2(packed4_col_major, group_luts, out_32);
        }
        return;
    }
    #[cfg(target_arch = "aarch64")]
    {
        // SAFETY: NEON is mandatory on aarch64.
        unsafe {
            aarch64_neon::tmac_gemv_32rows_packed4_neon(packed4_col_major, group_luts, out_32);
        }
        return;
    }
    #[allow(unreachable_code)]
    tmac_gemv_32rows_packed4_scalar(packed4_col_major, group_luts, out_32);
}

/// Pack a row-major ternary matrix `q[row * cols + col] ∈ {-1, 0, +1}` into dense
/// **2-pair/byte (`2.0 bpw`)** column-major 32-row T-MAC blocks (`2×` denser than 1-nibble/byte).
/// Requires `rows % 32 == 0` and `cols % 2 == 0`.
#[must_use]
pub fn pack_ternary_2pairs_per_byte(rows: usize, cols: usize, q: &[i8]) -> Vec<[u8; 32]> {
    assert_eq!(rows % 32, 0, "rows ({rows}) must be divisible by 32");
    assert_eq!(cols % 2, 0, "cols ({cols}) must be even");
    let row_blocks = rows / 32;
    let col_pairs = cols / 2;
    let packed_pairs = col_pairs.div_ceil(2);
    let mut out = vec![[0u8; 32]; row_blocks * packed_pairs];
    for rb in 0..row_blocks {
        for pp in 0..packed_pairs {
            let cp_lo = 2 * pp;
            let cp_hi = cp_lo + 1;
            let mut blk = [0u8; 32];
            for (r, dst) in blk.iter_mut().enumerate() {
                let row_idx = rb * 32 + r;
                let base = row_idx * cols;
                let w0 = (q[base + 2 * cp_lo] + 1).clamp(0, 2) as u8;
                let w1 = (q[base + 2 * cp_lo + 1] + 1).clamp(0, 2) as u8;
                let nib_lo = (w0 + 3 * w1) & 0x0F;
                let nib_hi = if cp_hi < col_pairs {
                    let w2 = (q[base + 2 * cp_hi] + 1).clamp(0, 2) as u8;
                    let w3 = (q[base + 2 * cp_hi + 1] + 1).clamp(0, 2) as u8;
                    (w2 + 3 * w3) & 0x0F
                } else {
                    4 // (0, 0) neutral pair
                };
                *dst = nib_lo | (nib_hi << 4);
            }
            out[rb * packed_pairs + pp] = blk;
        }
    }
    out
}

/// Pack a row-major ternary matrix `q[row * cols + col] ∈ {-1, 0, +1}` using **`TerEffic` 5-trits/byte
/// (`3^5 = 243 < 256`, `1.60 bpw`, `2.5×` denser than 1-nibble/byte)** in column-major 32-row blocks.
/// Requires `rows % 32 == 0`. Trailing columns not divisible by 5 are zero-padded (`w = 0 -> digit 1`).
#[must_use]
pub fn pack_ternary_5trits_per_byte(rows: usize, cols: usize, q: &[i8]) -> Vec<[u8; 32]> {
    assert_eq!(rows % 32, 0, "rows ({rows}) must be divisible by 32");
    let row_blocks = rows / 32;
    let groups5 = cols.div_ceil(5);
    let mut out = vec![[0u8; 32]; row_blocks * groups5];
    for rb in 0..row_blocks {
        for g5 in 0..groups5 {
            let mut blk = [0u8; 32];
            for r in 0..32 {
                let row_idx = rb * 32 + r;
                let base = row_idx * cols;
                let mut code = 0u8;
                let mut pow3 = 1u8;
                for k in 0..5 {
                    let c = 5 * g5 + k;
                    let digit = if c < cols {
                        (q[base + c] + 1).clamp(0, 2) as u8
                    } else {
                        1u8 // zero weight
                    };
                    code += digit * pow3;
                    pow3 = pow3.saturating_mul(3);
                }
                blk[r] = code;
            }
            out[rb * groups5 + g5] = blk;
        }
    }
    out
}

/// Precompute a 243-entry `i16` LUT for one 5-column `TerEffic` group (`a[0..5]`) via base-3 recurrence.
#[inline]
#[must_use]
pub fn build_tereffic5_lut243(a: [i16; 5]) -> [i16; 243] {
    let mut lut = [0i16; 243];
    let mut idx = 0usize;
    for d4 in -1..=1i16 {
        let s4 = d4 * a[4];
        for d3 in -1..=1i16 {
            let s3 = s4 + d3 * a[3];
            for d2 in -1..=1i16 {
                let s2 = s3 + d2 * a[2];
                for d1 in -1..=1i16 {
                    let s1 = s2 + d1 * a[1];
                    for d0 in -1..=1i16 {
                        lut[idx] = s1 + d0 * a[0];
                        idx += 1;
                    }
                }
            }
        }
    }
    lut
}

/// Evaluate a 32-row **`TerEffic` 5-trits/byte (`1.60 bpw`)** multiplier-free LUT GEMV.
pub fn tmac_gemv_32rows_tereffic5(
    packed5_col_major: &[[u8; 32]],
    group_luts243: &[[i16; 243]],
    out_32: &mut [i32; 32],
) {
    out_32.fill(0);
    let groups = packed5_col_major.len().min(group_luts243.len());
    for g in 0..groups {
        let lut = &group_luts243[g];
        let w = &packed5_col_major[g];
        for r in 0..32 {
            out_32[r] += i32::from(lut[usize::from(w[r])]);
        }
    }
}

/// Fast Walsh-Hadamard Transform (`FWHT`, `BitNet v2` `H-BitLinear`):
/// Multiplication-free $O(D \log_2 D)$ butterfly transform with exact involution `fwht(fwht(x)) == x`.
pub fn fwht_in_place(x: &mut [f32]) {
    let n = x.len();
    if n <= 1 || !n.is_power_of_two() {
        return;
    }
    let mut h = 1usize;
    while h < n {
        let step = h << 1;
        let mut i = 0usize;
        while i < n {
            for j in i..(i + h) {
                let u = x[j];
                let v = x[j + h];
                x[j] = u + v;
                x[j + h] = u - v;
            }
            i += step;
        }
        h = step;
    }
    let scale = 1.0 / (n as f32).sqrt();
    for val in x.iter_mut() {
        *val *= scale;
    }
}

/// Build a 16-entry Nonary (`9+1`) `vpshufb` LUT for activation `a: i8`:
/// - `0 (+0 PosZero)` -> `0` (Hard Prune)
/// - `1 (+1 PosOne)`  -> `+a` (Deva Excitatory Unit)
/// - `2 (+2 PosTwo)`  -> `+2a` (Deva Dyadic Shift)
/// - `3 (0* PrimeZero)` -> `0` (Latent Curiosity Wellspring)
/// - `4 (1* PrimeOne)`  -> `+a` (120° Phase Rotor Real Projection)
/// - `5 (2* PrimeTwo)`  -> `+2a` (Law 7 Locked Constitutional Canary)
/// - `6 (-0 NegZero)`   -> `0` (Rakshasa Shunting Veto Clamp)
/// - `7 (-1 NegOne)`    -> `-a` (Rakshasa Inhibitory Unit)
/// - `8 (-2 NegTwo)`    -> `-2a` (Rakshasa Counterfactual Qof Shift)
/// - `9..15`            -> `0` (`|0|` Singularity / Reserved)
#[inline]
#[must_use]
pub fn build_nonary_lut_single(a: i8) -> [i8; 16] {
    let ai = i16::from(a);
    let p1 = ai.clamp(-127, 127) as i8;
    let p2 = (ai * 2).clamp(-127, 127) as i8;
    let n1 = (-ai).clamp(-127, 127) as i8;
    let n2 = (-ai * 2).clamp(-127, 127) as i8;
    [0, p1, p2, 0, p1, p2, 0, n1, n2, 0, 0, 0, 0, 0, 0, 0]
}

/// Evaluate a `3^k` Nonary Fractal Quantization 32-row block across `1..=3` concentric Navamsha shells:
/// $$y = s_0 \cdot (N_0 x) + \frac{s_1}{3} \cdot (N_1 x) + \frac{s_2}{9} \cdot (N_2 x)$$
/// Uses hardware AVX2 `vpshufb` (`tmac_gemv_32rows`) on each unlocked shell and returns `(out_f32, veto_mask_u32)`
/// where bit `r` of `veto_mask_u32` is `1` if row `r` encountered an active `-0` (`nibble == 6`) Rakshasa Shunting Veto.
pub fn tmac_nonary_fractal_gemv_32rows(
    shells_col_major: &[&[[u8; 32]]],
    shell_scales: &[f32],
    act_i8: &[i8],
    act_scale: f32,
    unlocked_shells: usize,
    out_f32: &mut [f32; 32],
) -> u32 {
    let luts: Vec<[i8; 16]> = act_i8
        .iter()
        .copied()
        .map(build_nonary_lut_single)
        .collect();
    let mut veto_mask = 0u32;
    if let Some(s0) = shells_col_major.first() {
        let cols = s0.len().min(act_i8.len());
        for c in 0..cols {
            if act_i8[c] != 0 {
                let col_nibs = &s0[c];
                for r in 0..32 {
                    if (col_nibs[r] & 0x0F) == 6 {
                        veto_mask |= 1u32 << r;
                    }
                }
            }
        }
    }
    out_f32.fill(0.0);
    let n_shells = unlocked_shells
        .min(shells_col_major.len())
        .min(shell_scales.len());
    let mut harmonic_div = 1.0f32;
    for s_idx in 0..n_shells {
        let mut raw_i32 = [0i32; 32];
        tmac_gemv_32rows(shells_col_major[s_idx], &luts, &mut raw_i32);
        let eff_scale = (shell_scales[s_idx] / harmonic_div) * act_scale;
        for r in 0..32 {
            out_f32[r] += (raw_i32[r] as f32) * eff_scale;
        }
        harmonic_div *= 3.0;
    }
    if veto_mask != 0 {
        for r in 0..32 {
            if (veto_mask >> r) & 1 == 1 {
                out_f32[r] = 0.0;
            }
        }
    }
    veto_mask
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_avx2_vpshufb_qfhrr_and_tmac_exact_parity() {
        for seed in 0u32..256 {
            let a: [u8; 64] = std::array::from_fn(|i| ((i as u32 * 13 + seed * 7) % 16) as u8);
            let b: [u8; 64] = std::array::from_fn(|i| ((i as u32 * 5 + seed * 11 + 3) % 16) as u8);

            let mut bound_simd = [0u8; 64];
            let mut bound_scalar = [0u8; 64];
            qfhrr_bind_64d(&a, &b, &mut bound_simd);
            qfhrr_bind_64d_scalar(&a, &b, &mut bound_scalar);
            assert_eq!(bound_simd, bound_scalar);

            let sim_simd = qfhrr_triangular_sim_64d(&a, &b);
            let sim_scalar = qfhrr_triangular_sim_64d_scalar(&a, &b);
            assert_eq!(sim_simd, sim_scalar);

            let ap = pack_64_nibbles_to_32(&a);
            let bp = pack_64_nibbles_to_32(&b);
            let sim_p = qfhrr_packed32_triangular_sim(&ap, &bp);
            let sim_ps = qfhrr_packed32_triangular_sim_scalar(&ap, &bp);
            assert_eq!(sim_p, sim_ps);
            assert_eq!(sim_p, sim_scalar);
        }

        // Exact T-MAC 32-row column-major vpshufb GEMV parity
        let groups = 64usize;
        let weights: Vec<[u8; 32]> = (0..groups)
            .map(|g| std::array::from_fn(|r| ((g * 7 + r * 3) % 16) as u8))
            .collect();
        let luts: Vec<[i8; 16]> = (0..groups)
            .map(|g| std::array::from_fn(|n| ((((g + n * 5) % 15) as i8) - 7) * 3))
            .collect();
        let mut out_simd = [0i32; 32];
        let mut out_scalar = [0i32; 32];
        tmac_gemv_32rows(&weights, &luts, &mut out_simd);
        tmac_gemv_32rows_scalar(&weights, &luts, &mut out_scalar);
        assert_eq!(out_simd, out_scalar);
    }

    #[test]
    fn test_dense_packed4_and_tereffic5_exact_zero_diff_parity() {
        let rows = 64usize;
        let cols = 260usize; // > 256 cols to stress multi-chunk i16->i32 flush!
        let mut q = vec![0i8; rows * cols];
        for r in 0..rows {
            for c in 0..cols {
                q[r * cols + c] = (((r * 17 + c * 31 + 7) % 3) as i8) - 1;
            }
        }
        let x_i8: Vec<i8> = (0..cols)
            .map(|c| (((c * 29 + 11) % 63) as i8) - 31)
            .collect();

        // Ground-truth exact dot product
        let mut expected = vec![0i32; rows];
        for r in 0..rows {
            let mut s = 0i32;
            for c in 0..cols {
                s += i32::from(q[r * cols + c]) * i32::from(x_i8[c]);
            }
            expected[r] = s;
        }

        // 1. Dense 2-pair/byte (2.0 bpw) AVX2 + scalar parity
        let col_pairs = cols / 2;
        let mut luts = Vec::with_capacity(col_pairs);
        for cp in 0..col_pairs {
            let a0 = i16::from(x_i8[2 * cp]);
            let a1 = i16::from(x_i8[2 * cp + 1]);
            let mut lut = [0i8; 16];
            for w0 in 0..=2i16 {
                for w1 in 0..=2i16 {
                    lut[(w0 + 3 * w1) as usize] = ((w0 - 1) * a0 + (w1 - 1) * a1) as i8;
                }
            }
            luts.push(lut);
        }
        let packed4 = pack_ternary_2pairs_per_byte(rows, cols, &q);
        let pp_per_rb = col_pairs.div_ceil(2);
        assert_eq!(packed4.len(), (rows / 32) * pp_per_rb);

        for rb in 0..(rows / 32) {
            let slice = &packed4[rb * pp_per_rb..(rb + 1) * pp_per_rb];
            let mut out_simd = [0i32; 32];
            let mut out_scal = [0i32; 32];
            tmac_gemv_32rows_packed4(slice, &luts, &mut out_simd);
            tmac_gemv_32rows_packed4_scalar(slice, &luts, &mut out_scal);
            assert_eq!(out_simd, out_scal, "packed4 AVX2 vs scalar diff != 0");
            assert_eq!(&out_simd[..], &expected[rb * 32..(rb + 1) * 32]);
        }

        // Stress worst-case i8::MIN (-128) & i8::MAX (+127) LUT saturation (proving zero i16 overflow!)
        let extreme_luts: Vec<[i8; 16]> = (0..260)
            .map(|g| if g % 2 == 0 { [-128i8; 16] } else { [127i8; 16] })
            .collect();
        let mut ext_simd = [0i32; 32];
        let mut ext_scal = [0i32; 32];
        tmac_gemv_32rows_packed4(&packed4[..130], &extreme_luts, &mut ext_simd);
        tmac_gemv_32rows_packed4_scalar(&packed4[..130], &extreme_luts, &mut ext_scal);
        assert_eq!(ext_simd, ext_scal);

        // 2. TerEffic 5-trits/byte (1.60 bpw) exact parity
        let packed5 = pack_ternary_5trits_per_byte(rows, cols, &q);
        let groups5 = cols.div_ceil(5);
        assert_eq!(packed5.len(), (rows / 32) * groups5);
        let mut luts243 = Vec::with_capacity(groups5);
        for g5 in 0..groups5 {
            let mut a = [0i16; 5];
            for k in 0..5 {
                if 5 * g5 + k < cols {
                    a[k] = i16::from(x_i8[5 * g5 + k]);
                }
            }
            luts243.push(build_tereffic5_lut243(a));
        }
        for rb in 0..(rows / 32) {
            let slice = &packed5[rb * groups5..(rb + 1) * groups5];
            let mut out5 = [0i32; 32];
            tmac_gemv_32rows_tereffic5(slice, &luts243, &mut out5);
            assert_eq!(&out5[..], &expected[rb * 32..(rb + 1) * 32]);
        }
    }

    /// Exhaustive refinement gate connecting our Lean-4 `--verdict`-certified Bend 2 laws
    /// (`valkyrie_tm7b_v5/LAWS.bend`) directly to the live Rust `wm-gen4-lut` implementation:
    ///   - Laws 1-2: Ratio-3 Tied `3*q1 + q2 <-> {-4..+4}` exact bijection across all 9 pairs
    ///   - Law 3: `pack_ternary_2pairs_per_byte` lossless roundtrip across all 3^4 = 81 4-trit quads
    ///   - Law 4: T-MAC `i8` LUT entry overflow freedom across ALL `a0, a1 in [-31..+31]` & `w0, w1 in {-1,0,+1}`
    #[test]
    fn test_bend2_spec_refinement_ratio3_packed4_and_i8_bounds() {
        // Laws 1-2: Ratio-3 balanced-ternary bijection (exact match to muninn3_train_colab.py & PhysIR FPGA)
        let mut seen_q9 = [false; 9];
        for q1 in -1i8..=1 {
            for q2 in -1i8..=1 {
                let q9 = 3 * q1 + q2;
                assert!((-4..=4).contains(&q9));
                seen_q9[(q9 + 4) as usize] = true;
                let dec_q1 = q9.signum() * ((q9.abs() + 1) / 3);
                let dec_q2 = q9 - 3 * dec_q1;
                assert_eq!((dec_q1, dec_q2), (q1, q2), "Ratio-3 inverse failed for q9={q9}");
            }
        }
        assert!(seen_q9.iter().all(|&b| b), "Ratio-3 did not cover all 9 levels -4..+4");

        // Law 3: Exhaustive 3^4 = 81 4-trit quad lossless codec test through live `pack_ternary_2pairs_per_byte`
        let mut q_mat = vec![0i8; 32 * 4];
        let mut idx = 0usize;
        for w0 in -1i8..=1 {
            for w1 in -1i8..=1 {
                for w2 in -1i8..=1 {
                    for w3 in -1i8..=1 {
                        let r = idx % 32;
                        q_mat[r * 4] = w0;
                        q_mat[r * 4 + 1] = w1;
                        q_mat[r * 4 + 2] = w2;
                        q_mat[r * 4 + 3] = w3;
                        let packed = pack_ternary_2pairs_per_byte(32, 4, &q_mat);
                        let b = packed[0][r];
                        let lo = (b & 0x0F) as i8;
                        let hi = (b >> 4) as i8;
                        assert!(lo <= 8 && hi <= 8, "packed4 nibble out of [0..8] range");
                        assert_eq!((lo % 3) - 1, w0);
                        assert_eq!((lo / 3) - 1, w1);
                        assert_eq!((hi % 3) - 1, w2);
                        assert_eq!((hi / 3) - 1, w3);
                        idx += 1;
                    }
                }
            }
        }
        assert_eq!(idx, 81);

        // Law 4: Exhaustive zero-overflow proof for ALL (a0, a1) in [-31..+31]^2 and (w0, w1) in {-1,0,+1}^2
        for a0 in -31i16..=31 {
            for a1 in -31i16..=31 {
                for w0 in -1i16..=1 {
                    for w1 in -1i16..=1 {
                        let s = w0 * a0 + w1 * a1;
                        assert!((-62..=62).contains(&s));
                        assert_eq!(i16::from(s as i8), s, "i8 LUT truncation detected!");
                    }
                }
            }
        }
    }
}
