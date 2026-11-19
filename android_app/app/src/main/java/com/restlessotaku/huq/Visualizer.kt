package com.restlessotaku.huq

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlin.math.abs

@Composable
fun MiniGraphsRow(samples: FloatArray, modifier: Modifier = Modifier) {
    Row(
        modifier = modifier.fillMaxWidth().height(64.dp),
        horizontalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        WaveformBox(samples, Modifier.weight(1f).fillMaxHeight(), strokeWidth = 1.5f)
        SpectrumBarsBox(samples, Modifier.weight(1f).fillMaxHeight())
    }
}

@Composable
fun WaveformBox(samples: FloatArray, modifier: Modifier = Modifier, strokeWidth: Float = 2f) {
    Canvas(
        modifier = modifier
            .background(Color(0xFF161618), RoundedCornerShape(8.dp))
            .border(1.dp, Color(0xFF2C2C2E), RoundedCornerShape(8.dp))
            .padding(6.dp)
    ) {
        val midY = size.height / 2f
        drawLine(Color(0xFF2C2C2E), Offset(0f, midY), Offset(size.width, midY), 1f)
        if (samples.size < 2) return@Canvas
        val path = Path()
        val stepX = size.width / (samples.size - 1)
        for (i in samples.indices) {
            val y = midY - (samples[i].coerceIn(-1f, 1f) * midY * 0.9f)
            if (i == 0) path.moveTo(0f, y) else path.lineTo(i * stepX, y)
        }
        drawPath(path, Color(0xFF34C759), style = Stroke(width = strokeWidth))
    }
}

@Composable
fun SpectrumBarsBox(samples: FloatArray, modifier: Modifier = Modifier) {
    Canvas(
        modifier = modifier
            .background(Color(0xFF161618), RoundedCornerShape(8.dp))
            .border(1.dp, Color(0xFF2C2C2E), RoundedCornerShape(8.dp))
            .padding(6.dp)
    ) {
        val bins = 24
        if (samples.size < bins) return@Canvas
        val step = samples.size / bins
        val barW = (size.width / bins) * 0.7f
        val gap = size.width / bins
        for (b in 0 until bins) {
            var peak = 0f
            for (k in 0 until step) {
                val v = abs(samples[(b * step + k).coerceAtMost(samples.lastIndex)])
                if (v > peak) peak = v
            }
            val barH = (peak * size.height * 1.4f).coerceIn(2f, size.height)
            drawRect(
                color = Color(0xFF0A84FF),
                topLeft = Offset(b * gap, size.height - barH),
                size = androidx.compose.ui.geometry.Size(barW, barH)
            )
        }
    }
}

@Composable
fun FullscreenGraphPage(samples: FloatArray, title: String, srText: String) {
    Column(
        Modifier.fillMaxSize().background(Color(0xFF0E0E10)).padding(14.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Text("ɦuk — Live Output Scope", color = Color.White, fontSize = 15.sp)
            Text(srText, color = Color(0xFF34C759), fontSize = 12.sp)
        }
        Text(title, color = Color(0xFF9E9EA4), fontSize = 12.sp, maxLines = 1)
        WaveformBox(samples, Modifier.weight(1f).fillMaxWidth(), strokeWidth = 2.5f)
        SpectrumBarsBox(samples, Modifier.weight(0.7f).fillMaxWidth())
    }
}
