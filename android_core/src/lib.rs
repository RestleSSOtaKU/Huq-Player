mod decoder;
mod hrir;
mod player;

use decoder::decode_file;
use hrir::HrirBank;
use hrtf_core::{hrtf_engine_set_params, HrtfEngine};
use jni::objects::{JByteArray, JClass, JFloatArray, JString};
use jni::sys::{jboolean, jfloat, jint};
use jni::JNIEnv;
use once_cell::sync::Lazy;
use player::AndroidAudioPlayer;
use std::sync::Mutex;

static PLAYER: Lazy<Mutex<AndroidAudioPlayer>> = Lazy::new(|| Mutex::new(AndroidAudioPlayer::new()));

#[no_mangle]
pub extern "system" fn Java_com_restlessotaku_huq_NativeEngine_initHrirBank(
    env: JNIEnv, _cls: JClass, bytes: JByteArray,
) {
    if let Ok(data) = env.convert_byte_array(bytes) {
        if let Ok(bank) = HrirBank::from_bytes(&data) {
            if let Ok(mut p) = PLAYER.lock() {
                p.bank = Some(bank);
                p.apply_hrir();
            }
        }
    }
}

#[no_mangle]
pub extern "system" fn Java_com_restlessotaku_huq_NativeEngine_loadTrack(
    mut env: JNIEnv, _cls: JClass, path: JString,
) -> jint {
    let path_str: String = match env.get_string(&path) {
        Ok(s) => s.into(),
        Err(_) => return 0,
    };
    let decoded = match decode_file(&path_str) {
        Ok(d) => d,
        Err(_) => return 0,
    };
    if let Ok(mut p) = PLAYER.lock() {
        p.fs = decoded.sample_rate;
        if let Ok(mut st) = p.state.lock() {
            st.engine = HrtfEngine::new(decoded.sample_rate as f32, 4096, 16384);
            st.samples = decoded.samples;
            st.frame_pos = 0;
            st.is_playing = true;
            st.finished = false;
        }
        p.apply_hrir();
        if let Ok(mut st) = p.state.lock() {
            unsafe { hrtf_engine_set_params(&mut st.engine as *mut HrtfEngine, p.params); }
        }
        p.open_stream();
        return decoded.sample_rate as jint;
    }
    0
}

#[no_mangle]
pub extern "system" fn Java_com_restlessotaku_huq_NativeEngine_setPlaying(
    _env: JNIEnv, _cls: JClass, playing: jboolean,
) {
    if let Ok(p) = PLAYER.lock() {
        if let Ok(mut st) = p.state.lock() { st.is_playing = playing != 0; }
    }
}

#[no_mangle]
pub extern "system" fn Java_com_restlessotaku_huq_NativeEngine_setDspFull(
    _env: JNIEnv, _cls: JClass,
    az: jfloat, el: jfloat, vol: jfloat, bass_db: jfloat, treble_db: jfloat,
    tonal_match: jboolean, bypass: jboolean, bit_perfect: jboolean, hard_clip: jboolean,
) {
    if let Ok(mut p) = PLAYER.lock() {
        let pos_changed = (p.azimuth - az).abs() > 0.01 || (p.elevation - el).abs() > 0.01;
        p.azimuth = az;
        p.elevation = el;
        p.params.volume = vol;
        p.params.bass_db = bass_db;
        p.params.treble_db = treble_db;
        p.params.tonal_match = tonal_match != 0;
        p.params.bypass = bypass != 0;
        p.params.bit_perfect = bit_perfect != 0;
        p.params.hard_clip = hard_clip != 0;
        if pos_changed { p.apply_hrir(); }
        if let Ok(mut st) = p.state.lock() {
            unsafe { hrtf_engine_set_params(&mut st.engine as *mut HrtfEngine, p.params); }
        }
    }
}

#[no_mangle]
pub extern "system" fn Java_com_restlessotaku_huq_NativeEngine_getProgress(
    _env: JNIEnv, _cls: JClass,
) -> jfloat {
    if let Ok(p) = PLAYER.lock() {
        if let Ok(st) = p.state.lock() {
            let total = st.samples.len() / 2;
            if total > 0 { return st.frame_pos as f32 / total as f32; }
        }
    }
    0.0
}

#[no_mangle]
pub extern "system" fn Java_com_restlessotaku_huq_NativeEngine_getDurationSec(
    _env: JNIEnv, _cls: JClass,
) -> jfloat {
    if let Ok(p) = PLAYER.lock() {
        if let Ok(st) = p.state.lock() {
            let total = st.samples.len() / 2;
            if p.fs > 0 { return total as f32 / p.fs as f32; }
        }
    }
    0.0
}

#[no_mangle]
pub extern "system" fn Java_com_restlessotaku_huq_NativeEngine_isFinished(
    _env: JNIEnv, _cls: JClass,
) -> jboolean {
    if let Ok(p) = PLAYER.lock() {
        if let Ok(mut st) = p.state.lock() {
            if st.finished { st.finished = false; return 1; }
        }
    }
    0
}

#[no_mangle]
pub extern "system" fn Java_com_restlessotaku_huq_NativeEngine_seek(
    _env: JNIEnv, _cls: JClass, ratio: jfloat,
) {
    if let Ok(p) = PLAYER.lock() {
        if let Ok(mut st) = p.state.lock() {
            let total = st.samples.len() / 2;
            st.frame_pos = ((ratio.clamp(0.0, 1.0) * total as f32) as usize).min(total.saturating_sub(1));
            st.finished = false;
            st.engine.reset_state();
        }
    }
}

#[no_mangle]
pub extern "system" fn Java_com_restlessotaku_huq_NativeEngine_getVizSamples(
    env: JNIEnv, _cls: JClass, out: JFloatArray,
) {
    if let Ok(p) = PLAYER.lock() {
        if let Ok(st) = p.state.lock() {
            let len = env.get_array_length(&out).unwrap_or(0) as usize;
            if len > 0 && !st.viz_buf.is_empty() {
                let mut down = vec![0.0f32; len];
                let step = (st.viz_buf.len() / len).max(1);
                for i in 0..len {
                    down[i] = st.viz_buf[(i * step).min(st.viz_buf.len() - 1)];
                }
                let _ = env.set_float_array_region(&out, 0, &down);
            }
        }
    }
}
