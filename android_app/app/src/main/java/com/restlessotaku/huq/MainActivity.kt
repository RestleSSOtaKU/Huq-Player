package com.restlessotaku.huq

import android.net.Uri
import android.os.Bundle
import android.provider.OpenableColumns
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        assets.open("default.hrir").use {
            NativeEngine.initHrirBank(it.readBytes())
        }
        setContent {
            MaterialTheme(colorScheme = darkColorScheme(
                background = Color(0xFF121212),
                surface = Color(0xFF1E1E1E),
                primary = Color(0xFF007AFF)
            )) {
                HuqPlayerScreen()
            }
        }
    }
}

@Composable
fun HuqPlayerScreen() {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    var title by remember { mutableStateOf("No Track Loaded") }
    var srText by remember { mutableStateOf("Stream SR: -") }
    var isPlaying by remember { mutableStateOf(false) }
    var progress by remember { mutableFloatStateOf(0f) }
    var az by remember { mutableFloatStateOf(60f) }
    var el by remember { mutableFloatStateOf(0f) }
    var vol by remember { mutableFloatStateOf(0.49f) }
    var bypass by remember { mutableStateOf(false) }
    var bitPerfect by remember { mutableStateOf(false) }

    fun syncDsp() = NativeEngine.setDsp(az, el, vol, bypass, bitPerfect)

    LaunchedEffect(isPlaying) {
        while (isPlaying) {
            progress = NativeEngine.getProgress()
            delay(100)
        }
    }

    val picker = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri: Uri? ->
        if (uri != null) {
            scope.launch {
                val (name, path) = withContext(Dispatchers.IO) {
                    var disp = "Track"
                    ctx.contentResolver.query(uri, null, null, null, null)?.use { c ->
                        val idx = c.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                        if (c.moveToFirst() && idx >= 0) disp = c.getString(idx)
                    }
                    val tmp = File(ctx.cacheDir, disp)
                    ctx.contentResolver.openInputStream(uri)?.use { inp ->
                        tmp.outputStream().use { out -> inp.copyTo(out) }
                    }
                    disp to tmp.absolutePath
                }
                title = name
                val sr = withContext(Dispatchers.IO) { NativeEngine.loadTrack(path) }
                if (sr > 0) {
                    srText = "Stream SR: ${sr / 1000.0} kHz (AAudio Exclusive)"
                    isPlaying = true
                    syncDsp()
                }
            }
        }
    }

    Column(
        Modifier.fillMaxSize().background(Color(0xFF121212)).padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        Text("ɦuk — Anechoic Engine", color = Color.White, fontSize = 20.sp, fontWeight = FontWeight.Bold)
        Text(srText, color = Color(0xFF34C759), fontSize = 12.sp)
        Text(title, color = Color(0xFFE0E0E0), fontSize = 15.sp, maxLines = 1)

        Slider(value = progress, onValueChange = { progress = it; NativeEngine.seek(it) })

        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            Button(onClick = { picker.launch(arrayOf("audio/*")) }) { Text("Open Track") }
            Button(onClick = {
                isPlaying = !isPlaying
                NativeEngine.setPlaying(isPlaying)
            }) { Text(if (isPlaying) "Pause" else "Play") }
        }

        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            FilterChip(selected = bypass, onClick = {
                bypass = !bypass
                if (!bypass) bitPerfect = false
                syncDsp()
            }, label = { Text("Bypass DSP") })
            FilterChip(selected = bitPerfect, onClick = {
                bitPerfect = !bitPerfect
                if (bitPerfect) bypass = true
                syncDsp()
            }, label = { Text("Bit-Perfect") })
        }

        Text("Volume: ${(vol * 100).toInt()}%", color = Color.LightGray, fontSize = 13.sp)
        Slider(value = vol, onValueChange = { vol = it; syncDsp() })

        Text("Azimuth: ${az.toInt()}°", color = Color.LightGray, fontSize = 13.sp)
        Slider(value = az, valueRange = -90f..90f, onValueChange = { az = it; syncDsp() })

        Text("Elevation: ${el.toInt()}°", color = Color.LightGray, fontSize = 13.sp)
        Slider(value = el, valueRange = -45f..45f, onValueChange = { el = it; syncDsp() })
    }
}
