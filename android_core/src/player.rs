use crate::hrir::{build_spectra, HrirBank};
use hrtf_core::{DspParams, HrtfEngine};
use oboe::{
    AudioOutputCallback, AudioOutputStreamSafe, AudioStream, AudioStreamAsync,
    AudioStreamBuilder, DataCallbackResult, Output, PerformanceMode, SharingMode, Stereo,
};
use std::sync::{Arc, Mutex};

pub struct SharedState {
    pub engine: HrtfEngine,
    pub samples: Vec<f32>,
    pub frame_pos: usize,
    pub is_playing: bool,
    pub viz_buf: Vec<f32>,
}

struct OboeCallback {
    state: Arc<Mutex<SharedState>>,
}

impl AudioOutputCallback for OboeCallback {
    type FrameType = (f32, Stereo);

    fn on_audio_ready(
        &mut self,
        _stream: &mut dyn AudioOutputStreamSafe,
        frames: &mut [(f32, f32)],
    ) -> DataCallbackResult {
        let mut st = match self.state.try_lock() {
            Ok(g) => g,
            Err(_) => {
                frames.fill((0.0, 0.0));
                return DataCallbackResult::Continue;
            }
        };
        let n = frames.len();
        let total_frames = st.samples.len() / 2;
        if !st.is_playing || st.frame_pos + n > total_frames {
            frames.fill((0.0, 0.0));
            if st.frame_pos + n > total_frames { st.is_playing = false; }
            return DataCallbackResult::Continue;
        }
        let start = st.frame_pos * 2;
        let end = start + n * 2;
        if st.viz_buf.len() < n { st.viz_buf.resize(n, 0.0); }

        let in_slice = st.samples[start..end].to_vec();
        let mut out_slice = vec![0.0f32; n * 2];
        let mut viz = vec![0.0f32; n];
        st.engine.process_interleaved(&in_slice, &mut out_slice, &mut viz);
        st.frame_pos += n;

        for (i, frame) in frames.iter_mut().enumerate() {
            *frame = (out_slice[i * 2], out_slice[i * 2 + 1]);
        }
        DataCallbackResult::Continue
    }
}

pub struct AndroidAudioPlayer {
    pub state: Arc<Mutex<SharedState>>,
    pub stream: Option<AudioStreamAsync<Output, OboeCallback>>,
    pub bank: Option<HrirBank>,
    pub fs: u32,
    pub azimuth: f32,
    pub elevation: f32,
    pub params: DspParams,
}

impl AndroidAudioPlayer {
    pub fn new() -> Self {
        let params = DspParams {
            volume: 0.49, bass_db: 0.0, treble_db: 0.0,
            tonal_match: true, bypass: false, bit_perfect: false, hard_clip: true,
        };
        let state = Arc::new(Mutex::new(SharedState {
            engine: HrtfEngine::new(48000.0, 4096, 16384),
            samples: Vec::new(),
            frame_pos: 0,
            is_playing: false,
            viz_buf: vec![0.0; 4096],
        }));
        Self { state, stream: None, bank: None, fs: 48000, azimuth: 60.0, elevation: 0.0, params }
    }

    pub fn apply_hrir(&mut self) {
        if let Some(bank) = &self.bank {
            let [ll, rl, lr, rr, ll_r, rl_r] =
                build_spectra(bank, self.azimuth, self.elevation, self.fs, 16384);
            if let Ok(mut st) = self.state.lock() {
                st.engine.load_hrir_spectra(&ll, &rl, &lr, &rr, &ll_r, &rl_r);
            }
        }
    }

    pub fn open_stream(&mut self) {
        if let Some(mut old) = self.stream.take() { let _ = old.stop(); }
        let cb = OboeCallback { state: self.state.clone() };
        if let Ok(mut s) = AudioStreamBuilder::default()
            .set_performance_mode(PerformanceMode::LowLatency)
            .set_sharing_mode(SharingMode::Exclusive)
            .set_sample_rate(self.fs as i32)
            .set_channel_count::<Stereo>()
            .set_format::<f32>()
            .set_frames_per_callback(2048)
            .set_callback(cb)
            .open_stream()
        {
            let _ = s.start();
            self.stream = Some(s);
        }
    }
}
