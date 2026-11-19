use num_complex::Complex32;
use realfft::RealFftPlanner;

pub struct HrirBank {
    pub fs: u32,
    pub ir_len: usize,
    pub positions: Vec<(f32, f32)>, // (azimuth_deg, elevation_deg)
    pub irs: Vec<Vec<f32>>,         // [pos][2 * ir_len] interleaved L/R
}

impl HrirBank {
    pub fn from_bytes(data: &[u8]) -> Result<Self, String> {
        if data.len() < 16 || &data[0..4] != b"HUQH" {
            return Err("Invalid HRIR bank header".into());
        }
        let fs = u32::from_le_bytes(data[4..8].try_into().unwrap());
        let n_pos = u32::from_le_bytes(data[8..12].try_into().unwrap()) as usize;
        let ir_len = u32::from_le_bytes(data[12..16].try_into().unwrap()) as usize;
        let mut offset = 16;
        let mut positions = Vec::with_capacity(n_pos);
        let mut irs = Vec::with_capacity(n_pos);

        for _ in 0..n_pos {
            if offset + 8 + ir_len * 8 > data.len() {
                return Err("Truncated HRIR bank".into());
            }
            let az = f32::from_le_bytes(data[offset..offset + 4].try_into().unwrap());
            let el = f32::from_le_bytes(data[offset + 4..offset + 8].try_into().unwrap());
            offset += 8;
            let mut ir = Vec::with_capacity(ir_len * 2);
            for _ in 0..(ir_len * 2) {
                ir.push(f32::from_le_bytes(data[offset..offset + 4].try_into().unwrap()));
                offset += 4;
            }
            positions.push((az, el));
            irs.push(ir);
        }
        Ok(Self { fs, ir_len, positions, irs })
    }

    pub fn nearest(&self, az: f32, el: f32) -> &[f32] {
        let mut best_i = 0;
        let mut best_d = f32::MAX;
        for (i, &(paz, pel)) in self.positions.iter().enumerate() {
            let d = (paz - az).powi(2) + (pel - el).powi(2);
            if d < best_d { best_d = d; best_i = i; }
        }
        &self.irs[best_i]
    }
}

pub fn build_spectra(
    bank: &HrirBank, az: f32, el: f32, target_fs: u32, fft_size: usize,
) -> [Vec<Complex32>; 6] {
    let left_ir = bank.nearest(az, el);
    let right_ir = bank.nearest(-az, el);
    let mut planner = RealFftPlanner::<f32>::new();
    let r2c = planner.plan_fft_forward(fft_size);
    let spec_len = fft_size / 2 + 1;

    let make_fft = |ch_idx: usize, ir_pair: &[f32]| -> Vec<Complex32> {
        let ratio = bank.fs as f32 / target_fs as f32;
        let out_len = ((bank.ir_len as f32 / ratio).round() as usize).min(fft_size);
        let mut buf = vec![0.0f32; fft_size];
        for i in 0..out_len {
            let src_pos = i as f32 * ratio;
            let idx = (src_pos as usize).min(bank.ir_len - 1);
            let frac = src_pos - idx as f32;
            let s0 = ir_pair[idx * 2 + ch_idx];
            let s1 = ir_pair[((idx + 1).min(bank.ir_len - 1)) * 2 + ch_idx];
            buf[i] = s0 + frac * (s1 - s0);
        }
        let mut spec = vec![Complex32::default(); spec_len];
        let _ = r2c.process(&mut buf, &mut spec);
        spec
    };

    let h_ll = make_fft(0, left_ir);
    let h_lr = make_fft(1, left_ir);
    let h_rl = make_fft(0, right_ir);
    let h_rr = make_fft(1, right_ir);
    [h_ll.clone(), h_rl.clone(), h_lr, h_rr, h_ll, h_rl]
}

pub fn build_tonal_curve(
    bank: &HrirBank, az: f32, el: f32, target_fs: u32, fft_size: usize,
) -> Vec<f32> {
    let [h_ll, h_rl, _, _, _, _] = build_spectra(bank, az, el, target_fs, fft_size);
    let [ref_ll, ref_rl, _, _, _, _] = build_spectra(bank, 0.0, 0.0, target_fs, fft_size);
    let spec_len = fft_size / 2 + 1;
    let mut raw_gain = vec![1.0f32; spec_len];

    for i in 0..spec_len {
        let cur_mag = (h_ll[i].norm_sqr() + h_rl[i].norm_sqr()).sqrt().max(1e-5);
        let ref_mag = (ref_ll[i].norm_sqr() + ref_rl[i].norm_sqr()).sqrt().max(1e-5);
        let g = (ref_mag / cur_mag).clamp(0.25, 4.0); // +/- 12 dB cap
        raw_gain[i] = g;
    }

    // 1/3-octave style moving-window smoothing across bins
    let mut smooth = vec![1.0f32; spec_len];
    for i in 0..spec_len {
        let half_w = (i / 6).max(2);
        let lo = i.saturating_sub(half_w);
        let hi = (i + half_w).min(spec_len - 1);
        let mut sum = 0.0f32;
        for k in lo..=hi { sum += raw_gain[k]; }
        smooth[i] = sum / (hi - lo + 1) as f32;
    }
    smooth
}
