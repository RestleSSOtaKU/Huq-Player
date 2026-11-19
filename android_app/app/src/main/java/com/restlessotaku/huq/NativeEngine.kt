package com.restlessotaku.huq

object NativeEngine {
    init {
        System.loadLibrary("c++_shared")
        System.loadLibrary("huq_android")
    }

    external fun initHrirBank(bytes: ByteArray)
    external fun loadTrack(path: String): Int
    external fun setPlaying(playing: Boolean)
    external fun setDspFull(
        az: Float,
        el: Float,
        vol: Float,
        bassDb: Float,
        trebleDb: Float,
        tonalMatch: Boolean,
        bypass: Boolean,
        bitPerfect: Boolean,
        hardClip: Boolean
    )
    external fun getProgress(): Float
    external fun getDurationSec(): Float
    external fun isFinished(): Boolean
    external fun seek(ratio: Float)
    external fun getVizSamples(out: FloatArray)
}
