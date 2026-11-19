mod decoder;
mod hrir;
mod player;

use decoder::decode_file;
use hrir::HrirBank;
use hrtf_core::{hrtf_engine_set_params, HrtfEngine};
use jni::objects::{JByteArray, JClass, JString};
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
pub extern "system" fn Java_com_restlessotaku_huq_NativeEngine_setDsp(
    _env: JNIEnv, _cls: JClass,
    az: jfloat, el: jfloat, vol: jfloat, bypass: jboolean, bit_perfect: jboolean,
) {
    if let Ok(mut p) = PLAYER.lock() {
        let pos_changed = (p.azimuth - az).abs() > 0.01 || (p.elevation - el).abs() > 0.01;
        p.azimuth = az;
        p.elevation = el;
        p.params.volume = vol;
        p.params.bypass = bypass != 0;
        p.params.bit_perfect = bit_perfect != 0;
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
pub extern "system" fn Java_com_restlessotaku_huq_NativeEngine_seek(
    _env: JNIEnv, _cls: JClass, ratio: jfloat,
) {
    if let Ok(p) = PLAYER.lock() {
        if let Ok(mut st) = p.state.lock() {
            let total = st.samples.len() / 2;
            st.frame_pos = ((ratio.clamp(0.0, 1.0) * total as f32) as usize).min(total.saturating_sub(1));
            st.engine.reset_state();
        }
    }
}
