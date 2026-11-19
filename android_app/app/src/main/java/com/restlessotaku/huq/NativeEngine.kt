package com.restlessotaku.huq

object NativeEngine {
    init {
        System.loadLibrary("huq_android")
    }

    external fun initHrirBank(bytes: ByteArray)
    external fun loadTrack(path: String): Int
    external fun setPlaying(playing: Boolean)
    external fun setDsp(
        az: Float,
        el: Float,
        vol: Float,
        bypass: Boolean,
        bitPerfect: Boolean
    )
    external fun getProgress(): Float
    external fun seek(ratio: Float)
}
