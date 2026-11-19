package com.restlessotaku.huq

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.media.MediaMetadataRetriever
import android.net.Uri
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.gestures.detectVerticalDragGestures
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

fun extractCoverArt(ctx: Context, uri: Uri): Bitmap? {
    return try {
        val mmr = MediaMetadataRetriever()
        mmr.setDataSource(ctx, uri)
        val pic = mmr.embeddedPicture
        mmr.release()
        if (pic != null) BitmapFactory.decodeByteArray(pic, 0, pic.size) else null
    } catch (_: Exception) {
        null
    }
}

@Composable
fun UappCoverArtCard(cover: Bitmap?, modifier: Modifier = Modifier) {
    Box(
        modifier = modifier
            .size(168.dp)
            .clip(RoundedCornerShape(14.dp))
            .background(
                Brush.linearGradient(listOf(Color(0xFF1F242D), Color(0xFF13161C)))
            )
            .border(1.dp, Color(0xFF2E3440), RoundedCornerShape(14.dp)),
        contentAlignment = Alignment.Center
    ) {
        if (cover != null) {
            Image(
                bitmap = cover.asImageBitmap(),
                contentDescription = "Album Art",
                contentScale = ContentScale.Crop,
                modifier = Modifier.fillMaxSize()
            )
        } else {
            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                Text("ɦuk", color = Color(0xFF0A84FF), fontSize = 36.sp, fontWeight = FontWeight.Bold)
                Text("BIT-PERFECT HRTF", color = Color(0xFF8E8E93), fontSize = 10.sp, letterSpacing = 1.5.sp)
            }
        }
    }
}

@Composable
fun LandscapeScopeScreen(
    samples: FloatArray,
    title: String,
    srText: String,
    onSwipeUpExit: () -> Unit
) {
    Column(
        Modifier
            .fillMaxSize()
            .background(Color(0xFF0C0D10))
            .pointerInput(Unit) {
                detectVerticalDragGestures { _, dragAmount ->
                    if (dragAmount < -28f) onSwipeUpExit()
                }
            }
            .padding(14.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        Row(
            Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Column(Modifier.weight(1f)) {
                Text("ɦuk — Live Output Scope", color = Color.White, fontSize = 15.sp, fontWeight = FontWeight.Bold)
                Text("$srText  •  $title", color = Color(0xFF34C759), fontSize = 11.sp, maxLines = 1)
            }
            TextButton(onClick = onSwipeUpExit) {
                Text("▲ Swipe Up / Back", color = Color(0xFF0A84FF), fontSize = 12.sp)
            }
        }
        WaveformBox(samples, Modifier.weight(1f).fillMaxWidth(), strokeWidth = 2.5f)
        SpectrumBarsBox(samples, Modifier.weight(0.75f).fillMaxWidth())
    }
}
