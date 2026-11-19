package com.restlessotaku.huq

import android.app.Activity
import android.content.pm.ActivityInfo
import android.net.Uri
import android.os.Bundle
import android.provider.OpenableColumns
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.pager.HorizontalPager
import androidx.compose.foundation.pager.rememberPagerState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.documentfile.provider.DocumentFile
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File

data class TrackItem(val name: String, val uri: Uri)

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        assets.open("default.hrir").use { NativeEngine.initHrirBank(it.readBytes()) }
        setContent {
            MaterialTheme(colorScheme = darkColorScheme(
                background = Color(0xFF121212),
                surface = Color(0xFF1E1E1E),
                primary = Color(0xFF0A84FF)
            )) {
                HuqThreeScreenApp()
            }
        }
    }
}

fun fmtTime(sec: Float): String {
    val s = sec.toInt().coerceAtLeast(0)
    return "%02d:%02d".format(s / 60, s % 60)
}

@Composable
fun HuqThreeScreenApp() {
    val ctx = LocalContext.current
    val activity = ctx as? Activity
    val scope = rememberCoroutineScope()
    val pagerState = rememberPagerState(initialPage = 1, pageCount = { 3 })

    var playlist by remember { mutableStateOf<List<TrackItem>>(emptyList()) }
    var currentIndex by remember { mutableIntStateOf(-1) }
    var title by remember { mutableStateOf("No Track Loaded") }
    var srText by remember { mutableStateOf("Stream SR: -") }
    var isPlaying by remember { mutableStateOf(false) }
    var progress by remember { mutableFloatStateOf(0f) }
    var durationSec by remember { mutableFloatStateOf(0f) }
    var vizSamples by remember { mutableStateOf(FloatArray(128)) }

    var showDsp by remember { mutableStateOf(false) }
    var az by remember { mutableFloatStateOf(60f) }
    var el by remember { mutableFloatStateOf(0f) }
    var vol by remember { mutableFloatStateOf(0.49f) }
    var bassDb by remember { mutableFloatStateOf(0f) }
    var trebleDb by remember { mutableFloatStateOf(0f) }
    var tonalMatch by remember { mutableStateOf(true) }
    var hardClip by remember { mutableStateOf(true) }
    var bypass by remember { mutableStateOf(false) }
    var bitPerfect by remember { mutableStateOf(false) }

    fun syncDsp() = NativeEngine.setDspFull(
        az, el, vol, bassDb, trebleDb, tonalMatch, bypass, bitPerfect, hardClip
    )

    fun playUri(uri: Uri, displayName: String) {
        scope.launch {
            title = displayName
            val path = withContext(Dispatchers.IO) {
                val tmp = File(ctx.cacheDir, "active_track")
                ctx.contentResolver.openInputStream(uri)?.use { inp ->
                    tmp.outputStream().use { out -> inp.copyTo(out) }
                }
                tmp.absolutePath
            }
            val sr = withContext(Dispatchers.IO) { NativeEngine.loadTrack(path) }
            if (sr > 0) {
                srText = "Stream SR: ${sr / 1000.0} kHz"
                durationSec = NativeEngine.getDurationSec()
                isPlaying = true
                syncDsp()
            }
        }
    }

    fun playAt(idx: Int) {
        if (idx in playlist.indices) {
            currentIndex = idx
            playUri(playlist[idx].uri, playlist[idx].name)
        }
    }

    LaunchedEffect(pagerState.currentPage) {
        activity?.requestedOrientation = if (pagerState.currentPage == 2) {
            ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE
        } else {
            ActivityInfo.SCREEN_ORIENTATION_PORTRAIT
        }
    }

    LaunchedEffect(isPlaying) {
        val buf = FloatArray(128)
        while (isPlaying) {
            progress = NativeEngine.getProgress()
            NativeEngine.getVizSamples(buf)
            vizSamples = buf.copyOf()
            if (NativeEngine.isFinished()) {
                if (currentIndex + 1 < playlist.size) playAt(currentIndex + 1)
                else isPlaying = false
            }
            delay(50)
        }
    }

    val filePicker = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        if (uri != null) {
            var disp = "Track"
            ctx.contentResolver.query(uri, null, null, null, null)?.use { c ->
                val i = c.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                if (c.moveToFirst() && i >= 0) disp = c.getString(i)
            }
            playlist = listOf(TrackItem(disp, uri))
            playAt(0)
        }
    }

    val folderPicker = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocumentTree()) { treeUri ->
        if (treeUri != null) {
            scope.launch {
                val items = withContext(Dispatchers.IO) {
                    val exts = setOf("flac", "wav", "mp3", "m4a", "ogg", "opus", "aac")
                    val root = DocumentFile.fromTreeUri(ctx, treeUri)
                    root?.listFiles()
                        ?.filter { it.isFile && (it.name?.substringAfterLast('.', "")?.lowercase() in exts) }
                        ?.sortedBy { it.name?.lowercase() }
                        ?.mapNotNull { f -> f.name?.let { TrackItem(it, f.uri) } }
                        ?: emptyList()
                }
                playlist = items
                if (items.isNotEmpty()) playAt(0)
            }
        }
    }

    HorizontalPager(state = pagerState, modifier = Modifier.fillMaxSize().background(Color(0xFF121212))) { page ->
        when (page) {
            0 -> Column(Modifier.fillMaxSize().padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                    Text("Library (${playlist.size})", color = Color.White, fontSize = 18.sp, fontWeight = FontWeight.Bold)
                    Button(onClick = { folderPicker.launch(null) }) { Text("Open Folder") }
                }
                if (playlist.isEmpty()) {
                    Text("Tap 'Open Folder' to scan FLAC/WAV/MP3 tracks. Swipe left for Player.", color = Color.Gray, fontSize = 13.sp)
                } else {
                    LazyColumn(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                        itemsIndexed(playlist) { idx, item ->
                            val active = idx == currentIndex
                            Text(
                                text = "${idx + 1}. ${item.name}",
                                color = if (active) Color(0xFF0A84FF) else Color(0xFFE0E0E0),
                                fontWeight = if (active) FontWeight.Bold else FontWeight.Normal,
                                fontSize = 14.sp,
                                modifier = Modifier.fillMaxWidth()
                                    .background(if (active) Color(0xFF1E2633) else Color(0xFF1A1A1C), RoundedCornerShape(6.dp))
                                    .clickable { playAt(idx) }
                                    .padding(10.dp)
                            )
                        }
                    }
                }
            }

            1 -> Column(
                Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
                verticalArrangement = Arrangement.spacedBy(10.dp)
            ) {
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                    Text("ɦuk — Anechoic Engine", color = Color.White, fontSize = 18.sp, fontWeight = FontWeight.Bold)
                    Text("◀ Lib | Scope ▶", color = Color.Gray, fontSize = 11.sp)
                }
                Text(srText, color = Color(0xFF34C759), fontSize = 12.sp)
                Text(title, color = Color(0xFFE5E5EA), fontSize = 14.sp, maxLines = 1)
                MiniGraphsRow(vizSamples)
                Slider(value = progress, onValueChange = { progress = it; NativeEngine.seek(it) })
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                    Text(fmtTime(progress * durationSec), color = Color.Gray, fontSize = 11.sp)
                    Text(fmtTime(durationSec), color = Color.Gray, fontSize = 11.sp)
                }
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedButton(onClick = { playAt((currentIndex - 1).coerceAtLeast(0)) }) { Text("Prev") }
                    Button(onClick = { isPlaying = !isPlaying; NativeEngine.setPlaying(isPlaying) }) { Text(if (isPlaying) "Pause" else "Play") }
                    OutlinedButton(onClick = { playAt(currentIndex + 1) }) { Text("Next") }
                    OutlinedButton(onClick = { filePicker.launch(arrayOf("audio/*")) }) { Text("File") }
                }
                Text("Volume: ${(vol * 100).toInt()}%", color = Color.LightGray, fontSize = 12.sp)
                Slider(value = vol, onValueChange = { vol = it; syncDsp() })
                FilledTonalButton(onClick = { showDsp = !showDsp }, modifier = Modifier.fillMaxWidth()) {
                    Text(if (showDsp) "DSP & Acoustics ▲" else "DSP & Acoustics ▼")
                }
                AnimatedVisibility(visible = showDsp) {
                    Column(
                        Modifier.fillMaxWidth().background(Color(0xFF1A1A1C), RoundedCornerShape(10.dp)).padding(12.dp),
                        verticalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                            listOf(30f, 45f, 60f, 90f).forEach { preset ->
                                OutlinedButton(onClick = { az = preset; syncDsp() }, contentPadding = PaddingValues(horizontal = 10.dp)) { Text("${preset.toInt()}°", fontSize = 12.sp) }
                            }
                            OutlinedButton(onClick = {
                                az = 60f; el = 0f; bassDb = 0f; trebleDb = 0f; tonalMatch = true; hardClip = true; bypass = false; bitPerfect = false; syncDsp()
                            }, contentPadding = PaddingValues(horizontal = 10.dp)) { Text("Reset", fontSize = 12.sp) }
                        }
                        Text("Azimuth: ${az.toInt()}°", color = Color.LightGray, fontSize = 12.sp)
                        Slider(value = az, valueRange = -90f..90f, onValueChange = { az = it; syncDsp() })
                        Text("Elevation: ${el.toInt()}°", color = Color.LightGray, fontSize = 12.sp)
                        Slider(value = el, valueRange = -45f..45f, onValueChange = { el = it; syncDsp() })
                        Text("Bass Trim: ${"%.1f".format(bassDb)} dB", color = Color.LightGray, fontSize = 12.sp)
                        Slider(value = bassDb, valueRange = -12f..12f, onValueChange = { bassDb = it; syncDsp() })
                        Text("Treble Trim: ${"%.1f".format(trebleDb)} dB", color = Color.LightGray, fontSize = 12.sp)
                        Slider(value = trebleDb, valueRange = -12f..12f, onValueChange = { trebleDb = it; syncDsp() })
                        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            FilterChip(selected = tonalMatch, onClick = { tonalMatch = !tonalMatch; syncDsp() }, label = { Text("Tonal Match") })
                            FilterChip(selected = hardClip, onClick = { hardClip = !hardClip; syncDsp() }, label = { Text(if (hardClip) "Hard Clip" else "Soft Tanh") })
                        }
                        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            FilterChip(selected = bypass, onClick = { bypass = !bypass; if (!bypass) bitPerfect = false; syncDsp() }, label = { Text("Bypass DSP") })
                            FilterChip(selected = bitPerfect, onClick = { bitPerfect = !bitPerfect; if (bitPerfect) bypass = true; syncDsp() }, label = { Text("Bit-Perfect") })
                        }
                    }
                }
            }
            2 -> FullscreenGraphPage(vizSamples, title, srText)
        }
    }
}
