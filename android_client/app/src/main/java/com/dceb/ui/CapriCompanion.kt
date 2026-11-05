package com.dceb.ui

import android.content.Context
import android.graphics.BitmapFactory
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Image
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectDragGestures
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.dp
import com.dceb.R
import com.dceb.ai.AiViewModel
import com.dceb.core.SettingsRepository
import kotlinx.coroutines.delay
import org.json.JSONObject
import kotlin.math.roundToInt
import kotlin.random.Random

/**
 * Capri, living on the screen.
 *
 * Sprite source, in priority order:
 *  1. `assets/capri/manifest.json` + `assets/capri/idle_NN.png` — a pack built
 *     by `pc_server/tools/make_sprite_pack.py` (transparent PNGs, no background).
 *  2. the bundled `capri_avatar` drawable, animated procedurally.
 *
 * She drifts, bobs, blinks and says lines the model wrote. Nothing here reads
 * another app's data: the only context gathered is the foreground package, and
 * only when the owner enabled it in settings.
 */
private data class SpritePack(val frames: List<ImageBitmap>, val fps: Int)

private fun loadSpritePack(context: Context): SpritePack? = try {
    val names = context.assets.list("capri")?.filter { it.startsWith("idle_") }?.sorted()
    if (names.isNullOrEmpty()) {
        null
    } else {
        var fps = 8
        runCatching {
            context.assets.open("capri/manifest.json").use { stream ->
                val json = JSONObject(stream.readBytes().toString(Charsets.UTF_8))
                fps = json.optInt("fps", 8).coerceIn(1, 30)
            }
        }
        val frames = names.mapNotNull { name ->
            runCatching {
                context.assets.open("capri/$name").use { stream ->
                    BitmapFactory.decodeStream(stream)?.asImageBitmap()
                }
            }.getOrNull()
        }
        if (frames.isEmpty()) null else SpritePack(frames, fps)
    }
} catch (e: Exception) {
    null
}

@Composable
fun CapriCompanion(context: Context, vm: AiViewModel, modifier: Modifier = Modifier) {
    // Re-read every setting whenever the repository revision changes.
    // NB: no return@key here on purpose -- a labelled return out of the
    // inline key{} block makes Kotlin 2.0 emit a $$$$$NON_LOCAL_RETURN$$$
    // class that D8 cannot dex ("Method name <anonymous> ... cannot be
    // represented in dex format"). A plain if{} has no such problem.
    val rev = remember { SettingsRepository.revision }
    key(rev) {
        if (SettingsRepository.companionEnabled) {

            val sizeDp = SettingsRepository.companionSizeDp
            val speed = SettingsRepository.companionSpeed
            val wander = SettingsRepository.companionWander
            val blinkEnabled = SettingsRepository.companionBlink
            val opacity = SettingsRepository.companionOpacity
            val bubbleSeconds = SettingsRepository.bubbleSeconds
            val chatterSeconds = SettingsRepository.chatterSeconds

            val pack = remember(context) { loadSpritePack(context) }
            var frame by remember(pack) { mutableIntStateOf(0) }
            var blinking by remember { mutableStateOf(false) }
            var dragX by remember { mutableFloatStateOf(0f) }
            var dragY by remember { mutableFloatStateOf(0f) }
            var target by remember { mutableStateOf(Offset.Zero) }

            // Frame cycling.
            LaunchedEffect(pack, speed) {
                val frames = pack?.frames ?: return@LaunchedEffect
                if (frames.size < 2) return@LaunchedEffect
                val step = (1000f / (pack.fps * speed.coerceAtLeast(0.25f))).toLong().coerceAtLeast(16L)
                while (true) {
                    delay(step)
                    frame = (frame + 1) % frames.size
                }
            }

            // Autonomous wandering: pick a new spot inside the box every few seconds.
            LaunchedEffect(wander, speed) {
                if (!wander) return@LaunchedEffect
                while (true) {
                    delay((2600f / speed.coerceAtLeast(0.25f)).toLong())
                    target = Offset(Random.nextFloat() * 2f - 1f, Random.nextFloat() * 2f - 1f)
                }
            }

            // Blink: a quick vertical squish, at human-ish intervals.
            LaunchedEffect(blinkEnabled) {
                if (!blinkEnabled) return@LaunchedEffect
                while (true) {
                    delay((2600L + Random.nextLong(3600L)))
                    blinking = true
                    delay(110L)
                    blinking = false
                }
            }

            // Idle chatter, when enabled.
            LaunchedEffect(chatterSeconds) {
                if (chatterSeconds <= 0) return@LaunchedEffect
                while (true) {
                    delay(chatterSeconds * 1000L)
                    vm.companionSay(context)
                }
            }

            val line by vm.capriLine.collectAsState()
            var visibleLine by remember { mutableStateOf<String?>(null) }
            LaunchedEffect(line, bubbleSeconds) {
                if (line.isNullOrBlank()) return@LaunchedEffect
                visibleLine = line
                delay(bubbleSeconds * 1000L)
                if (visibleLine == line) visibleLine = null
            }

            val bob by rememberInfiniteTransition(label = "capri-bob").animateFloat(
                initialValue = -4f, targetValue = 4f,
                animationSpec = infiniteRepeatable(
                    tween((1600 / speed.coerceAtLeast(0.25f)).toInt()), RepeatMode.Reverse),
                label = "bob")
            val offX by animateFloatAsState(target.x, tween(1800), label = "offX")
            val offY by animateFloatAsState(target.y, tween(1800), label = "offY")

            BoxWithConstraints(modifier = modifier, contentAlignment = Alignment.Center) {
                val driftX = ((maxWidth.value - sizeDp) / 2f).coerceAtLeast(0f)
                val driftY = ((maxHeight.value - sizeDp) / 2f).coerceAtLeast(0f)
                Box(contentAlignment = Alignment.BottomCenter) {
                    visibleLine?.let { text ->
                        Card(
                            shape = RoundedCornerShape(12.dp),
                            colors = CardDefaults.cardColors(
                                containerColor = MaterialTheme.colorScheme.surfaceVariant),
                            modifier = Modifier.padding(bottom = 8.dp).widthIn(max = 240.dp)) {
                            Text(text, style = MaterialTheme.typography.bodySmall,
                                 modifier = Modifier.padding(horizontal = 10.dp, vertical = 6.dp))
                        }
                    }
                    Surface(
                        shape = RoundedCornerShape(16.dp),
                        color = androidx.compose.ui.graphics.Color.Transparent,
                        modifier = Modifier
                            .offset {
                                IntOffset((offX * driftX + dragX).roundToInt(),
                                          (offY * driftY + dragY).roundToInt())
                            }
                            .size(sizeDp.dp)
                            .alpha(opacity)
                            .clickable { vm.companionSay(context) }
                            .pointerInput(wander) {
                                detectDragGestures(
                                    onDrag = { change, amount ->
                                        change.consume()
                                        dragX += amount.x
                                        dragY += amount.y
                                    })
                            }
                            .offset(y = bob.dp)
                    ) {
                        val squish = if (blinking) 0.82f else 1f
                        if (pack != null && pack.frames.isNotEmpty()) {
                            Image(
                                bitmap = pack.frames[frame % pack.frames.size],
                                contentDescription = "Capri",
                                contentScale = ContentScale.Fit,
                                modifier = Modifier.size(sizeDp.dp)
                                    .alpha(if (blinking) 0.9f else 1f))
                        } else {
                            Image(
                                painter = painterResource(id = R.drawable.capri_avatar),
                                contentDescription = "Capri",
                                contentScale = ContentScale.Fit,
                                modifier = Modifier
                                    .size(width = sizeDp.dp, height = (sizeDp * squish).dp))
                        }
                    }
                }
            }
        }
    }
}
