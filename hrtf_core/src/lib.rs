mod biquad;

use biquad::Biquad;
use num_complex::Complex32;
use realfft::{ComplexToReal, RealFftPlanner, RealToComplex};
use std::slice;
use std::sync::Arc;

#[repr(C)]
#[derive(Clone, Copy)]
pub struct DspParams {
    pub volume: f32,
    pub bass_db: f32,
    pub treble_db: f32,
    pub tonal_match: bool,
    pub bypass: bool,
    pub bit_perfect: bool,
    pub hard_clip: bool,
}

pub struct HrtfEngine {
    fs: f32,
    block_size: usize,
    fft_size: usize,
    r2c: Arc<dyn RealToComplex<f32>>,
    c2r: Arc<dyn ComplexToReal<f32>>,
    in_pad_l: Vec<f32>,
    in_pad_r: Vec<f32>,
    spec_l: Vec<Complex32>,
    spec_r: Vec<Complex32>,
    out_spec_l: Vec<Complex32>,
    out_spec_r: Vec<Complex32>,
    raw_spec_l: Vec<Complex32>,
    time_out_l: Vec<f32>,
    time_out_r: Vec<f32>,
    time_raw_l: Vec<f32>,
    overlap_l: Vec<f32>,
    overlap_r: Vec<f32>,
    scratch_fwd: Vec<Complex32>,
    scratch_inv: Vec<Complex32>,
    h_ll: Vec<Complex32>,
    h_rl: Vec<Complex32>,
    h_lr: Vec<Complex32>,
    h_rr: Vec<Complex32>,
    h_ll_raw: Vec<Complex32>,
    h_rl_raw: Vec<Complex32>,
    has_hrir: bool,
    bass_l: Biquad,
    bass_r: Biquad,
    treble_l: Biquad,
    treble_r: Biquad,
    params: DspParams,
}

impl HrtfEngine {
    pub fn new(fs: f32, block_size: usize, fft_size: usize) -> Self {
        let spec_len = fft_size / 2 + 1;
        let mut planner = RealFftPlanner::<f32>::new();
        let r2c = planner.plan_fft_forward(fft_size);
        let c2r = planner.plan_fft_inverse(fft_size);
        let scratch_fwd = r2c.make_scratch_vec();
        let scratch_inv = c2r.make_scratch_vec();

        let params = DspParams {
            volume: 0.49,
            bass_db: 0.0,
            treble_db: 0.0,
            tonal_match: true,
            bypass: false,
            bit_perfect: false,
            hard_clip: true,
        };

        let mut engine = Self {
            fs,
            block_size,
            fft_size,
            r2c,
            c2r,
            in_pad_l: vec![0.0; fft_size],
            in_pad_r: vec![0.0; fft_size],
            spec_l: vec![Complex32::default(); spec_len],
            spec_r: vec![Complex32::default(); spec_len],
            out_spec_l: vec![Complex32::default(); spec_len],
            out_spec_r: vec![Complex32::default(); spec_len],
            raw_spec_l: vec![Complex32::default(); spec_len],
            time_out_l: vec![0.0; fft_size],
            time_out_r: vec![0.0; fft_size],
            time_raw_l: vec![0.0; fft_size],
            overlap_l: vec![0.0; fft_size],
            overlap_r: vec![0.0; fft_size],
            scratch_fwd,
            scratch_inv,
            h_ll: vec![Complex32::default(); spec_len],
            h_rl: vec![Complex32::default(); spec_len],
            h_lr: vec![Complex32::default(); spec_len],
            h_rr: vec![Complex32::default(); spec_len],
            h_ll_raw: vec![Complex32::default(); spec_len],
            h_rl_raw: vec![Complex32::default(); spec_len],
            has_hrir: false,
            bass_l: Biquad::default(),
            bass_r: Biquad::default(),
            treble_l: Biquad::default(),
            treble_r: Biquad::default(),
            params,
        };
        engine.update_eq();
        engine
    }

    fn update_eq(&mut self) {
        self.bass_l.set_low_shelf(100.0, self.params.bass_db, 0.707, self.fs);
        self.bass_r.set_low_shelf(100.0, self.params.bass_db, 0.707, self.fs);
        self.treble_l.set_high_shelf(10000.0, self.params.treble_db, 0.707, self.fs);
        self.treble_r.set_high_shelf(10000.0, self.params.treble_db, 0.707, self.fs);
    }

    pub fn load_hrir_spectra(
        &mut self,
        ll: &[Complex32],
        rl: &[Complex32],
        lr: &[Complex32],
        rr: &[Complex32],
        ll_raw: &[Complex32],
        rl_raw: &[Complex32],
    ) {
        if ll.len() == self.h_ll.len() {
            self.h_ll.copy_from_slice(ll);
            self.h_rl.copy_from_slice(rl);
            self.h_lr.copy_from_slice(lr);
            self.h_rr.copy_from_slice(rr);
            self.h_ll_raw.copy_from_slice(ll_raw);
            self.h_rl_raw.copy_from_slice(rl_raw);
            self.has_hrir = true;
        }
    }

    pub fn reset_state(&mut self) {
        self.overlap_l.fill(0.0);
        self.overlap_r.fill(0.0);
        self.bass_l.reset();
        self.bass_r.reset();
        self.treble_l.reset();
        self.treble_r.reset();
    }
    pub fn process_interleaved(&mut self, input: &[f32], output: &mut [f32], hrir_l_viz: &mut [f32]) {
        let frames = input.len() / 2;
        if self.params.bit_perfect {
            output.copy_from_slice(input);
            for f in 0..frames {
                hrir_l_viz[f] = input[f * 2];
            }
            return;
        }
        if self.params.bypass || !self.has_hrir || frames > self.block_size {
            for f in 0..frames {
                let mut l = input[f * 2] * self.params.volume;
                let mut r = input[f * 2 + 1] * self.params.volume;
                if self.params.hard_clip {
                    l = l.clamp(-1.0, 1.0);
                    r = r.clamp(-1.0, 1.0);
                }
                output[f * 2] = l;
                output[f * 2 + 1] = r;
                hrir_l_viz[f] = l;
            }
            return;
        }

        self.in_pad_l.fill(0.0);
        self.in_pad_r.fill(0.0);
        for f in 0..frames {
            self.in_pad_l[f] = input[f * 2];
            self.in_pad_r[f] = input[f * 2 + 1];
        }

        let _ = self.r2c.process_with_scratch(&mut self.in_pad_l, &mut self.spec_l, &mut self.scratch_fwd);
        let _ = self.r2c.process_with_scratch(&mut self.in_pad_r, &mut self.spec_r, &mut self.scratch_fwd);

        let inv_n = 1.0 / (self.fft_size as f32);
        for k in 0..self.spec_l.len() {
            let l = self.spec_l[k];
            let r = self.spec_r[k];
            self.out_spec_l[k] = (l * self.h_ll[k] + r * self.h_rl[k]) * inv_n;
            self.out_spec_r[k] = (l * self.h_lr[k] + r * self.h_rr[k]) * inv_n;
            if self.params.tonal_match {
                self.raw_spec_l[k] = (l * self.h_ll_raw[k] + r * self.h_rl_raw[k]) * inv_n;
            }
        }

        let _ = self.c2r.process_with_scratch(&mut self.out_spec_l, &mut self.time_out_l, &mut self.scratch_inv);
        let _ = self.c2r.process_with_scratch(&mut self.out_spec_r, &mut self.time_out_r, &mut self.scratch_inv);

        if self.params.tonal_match {
            let _ = self.c2r.process_with_scratch(&mut self.raw_spec_l, &mut self.time_raw_l, &mut self.scratch_inv);
            hrir_l_viz[..frames].copy_from_slice(&self.time_raw_l[..frames]);
        } else {
            hrir_l_viz[..frames].copy_from_slice(&self.in_pad_l[..frames]);
        }

        let overlap_tail = self.fft_size - frames;
        for f in 0..frames {
            let mut l = self.time_out_l[f] + self.overlap_l[f];
            let mut r = self.time_out_r[f] + self.overlap_r[f];

            if self.params.bass_db != 0.0 {
                l = self.bass_l.process_sample(l);
                r = self.bass_r.process_sample(r);
            }
            if self.params.treble_db != 0.0 {
                l = self.treble_l.process_sample(l);
                r = self.treble_r.process_sample(r);
            }

            l *= self.params.volume;
            r *= self.params.volume;

            if self.params.hard_clip {
                l = l.clamp(-1.0, 1.0);
                r = r.clamp(-1.0, 1.0);
            }

            output[f * 2] = l;
            output[f * 2 + 1] = r;
        }

        self.overlap_l.fill(0.0);
        self.overlap_r.fill(0.0);
        self.overlap_l[..overlap_tail].copy_from_slice(&self.time_out_l[frames..frames + overlap_tail]);
        self.overlap_r[..overlap_tail].copy_from_slice(&self.time_out_r[frames..frames + overlap_tail]);
    }
}

#[no_mangle]
pub extern "C" fn hrtf_engine_create(fs: f32, block_size: usize, fft_size: usize) -> *mut HrtfEngine {
    Box::into_raw(Box::new(HrtfEngine::new(fs, block_size, fft_size)))
}

#[no_mangle]
pub unsafe extern "C" fn hrtf_engine_destroy(ptr: *mut HrtfEngine) {
    if !ptr.is_null() {
        drop(Box::from_raw(ptr));
    }
}

#[no_mangle]
pub unsafe extern "C" fn hrtf_engine_set_params(ptr: *mut HrtfEngine, params: DspParams) {
    if ptr.is_null() { return; }
    let engine = &mut *ptr;
    let eq_changed = engine.params.bass_db != params.bass_db || engine.params.treble_db != params.treble_db;
    engine.params = params;
    if eq_changed {
        engine.update_eq();
    }
}

#[no_mangle]
pub unsafe extern "C" fn hrtf_engine_set_filters(
    ptr: *mut HrtfEngine,
    ll: *const Complex32,
    rl: *const Complex32,
    lr: *const Complex32,
    rr: *const Complex32,
    ll_raw: *const Complex32,
    rl_raw: *const Complex32,
    spec_len: usize,
) {
    if ptr.is_null() { return; }
    let engine = &mut *ptr;
    engine.load_hrir_spectra(
        slice::from_raw_parts(ll, spec_len),
        slice::from_raw_parts(rl, spec_len),
        slice::from_raw_parts(lr, spec_len),
        slice::from_raw_parts(rr, spec_len),
        slice::from_raw_parts(ll_raw, spec_len),
        slice::from_raw_parts(rl_raw, spec_len),
    );
}

#[no_mangle]
pub unsafe extern "C" fn hrtf_engine_reset(ptr: *mut HrtfEngine) {
    if !ptr.is_null() {
        (*ptr).reset_state();
    }
}

#[no_mangle]
pub unsafe extern "C" fn hrtf_engine_process(
    ptr: *mut HrtfEngine,
    in_ptr: *const f32,
    out_ptr: *mut f32,
    hrir_viz_ptr: *mut f32,
    frames: usize,
) {
    if ptr.is_null() || in_ptr.is_null() || out_ptr.is_null() || hrir_viz_ptr.is_null() { return; }
    let engine = &mut *ptr;
    let len = frames * 2;
    engine.process_interleaved(
        slice::from_raw_parts(in_ptr, len),
        slice::from_raw_parts_mut(out_ptr, len),
        slice::from_raw_parts_mut(hrir_viz_ptr, frames),
    );
}
