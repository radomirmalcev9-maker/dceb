package com.dceb.ui

import android.content.Context
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Slider
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.TextFieldValue
import androidx.compose.ui.unit.dp
import com.dceb.ai.AiViewModel
import com.dceb.core.Channel
import com.dceb.core.NetworkClient
import com.dceb.core.SettingsRepository
import kotlinx.coroutines.launch
import androidx.compose.runtime.rememberCoroutineScope

/** Full settings: Capri, the AI, the archive, scanning, the connection. */
@Composable
fun SettingsScreen(context: Context, net: NetworkClient, vm: AiViewModel) {
    val scope = rememberCoroutineScope()
    Column(
        Modifier.fillMaxSize().padding(16.dp).verticalScroll(rememberScrollState()),
        verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text("Настройки", style = MaterialTheme.typography.titleLarge)
        CapriSection(context, vm)
        AiSection()
        ArchiveSection(net)
        ScanSection()
        ConnectionSection(net)
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            OutlinedButton(onClick = {
                SettingsRepository.reset()
            }) { Text("Сбросить всё") }
            Button(onClick = { SettingsRepository.bump(); vm.refreshHealth() }) {
                Text("Применить")
            }
        }
        Text("Изменения применяются сразу; «Сбросить всё» возвращает заводские "
           + "значения и чистит кэш настроек.", style = MaterialTheme.typography.bodySmall,
             color = MaterialTheme.colorScheme.secondary)
    }
}

@Composable
private fun Section(title: String, subtitle: String = "", body: @Composable () -> Unit) {
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(title, style = MaterialTheme.typography.titleSmall)
            if (subtitle.isNotBlank()) {
                Text(subtitle, style = MaterialTheme.typography.bodySmall,
                     color = MaterialTheme.colorScheme.secondary)
            }
            body()
        }
    }
}

@Composable
private fun SwitchRow(label: String, desc: String = "", checked: Boolean,
                      onChecked: (Boolean) -> Unit) {
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.SpaceBetween) {
        Column(Modifier.weight(1f)) {
            Text(label, style = MaterialTheme.typography.bodyMedium)
            if (desc.isNotBlank()) {
                Text(desc, style = MaterialTheme.typography.bodySmall,
                     color = MaterialTheme.colorScheme.secondary)
            }
        }
        Switch(checked = checked, onCheckedChange = onChecked)
    }
}

@Composable
private fun IntSliderRow(label: String, value: Int, range: IntRange, unit: String = "",
                         onFinish: (Int) -> Unit) {
    var v by remember { mutableFloatStateOf(value.toFloat()) }
    Column {
        Text("$label: ${v.toInt()}$unit", style = MaterialTheme.typography.bodyMedium)
        Slider(value = v, onValueChange = { v = it },
               valueRange = range.first.toFloat()..range.last.toFloat(),
               onValueChangeFinished = { onFinish(v.toInt()) })
    }
}

@Composable
private fun FloatSliderRow(label: String, value: Float, range: ClosedFloatingPointRange<Float>,
                           onFinish: (Float) -> Unit) {
    var v by remember { mutableFloatStateOf(value) }
    Column {
        Text("$label: ${"%.2f".format(v)}", style = MaterialTheme.typography.bodyMedium)
        Slider(value = v, onValueChange = { v = it }, valueRange = range,
               onValueChangeFinished = { onFinish(v) })
    }
}

@Composable
private fun CapriSection(context: Context, vm: AiViewModel) {
    val scope = rememberCoroutineScope()
    var enabled by remember { mutableStateOf(SettingsRepository.companionEnabled) }
    var wander by remember { mutableStateOf(SettingsRepository.companionWander) }
    var blink by remember { mutableStateOf(SettingsRepository.companionBlink) }
    var appContext by remember { mutableStateOf(SettingsRepository.collectAppContext) }
    var personaField by remember {
        mutableStateOf(TextFieldValue(SettingsRepository.persona))
    }

    Section("Capri — компаньон",
            "Спрайты берутся из assets/capri/ (manifest.json + idle_NN.png); "
              + "если пака нет — используется встроенный аватар.") {
        SwitchRow("Показывать Capri", "Она висит на экране терминала и болтает", enabled) {
            enabled = it
            SettingsRepository.companionEnabled = it
            if (!it) vm.clearCapriLine()
            SettingsRepository.bump()
        }
        SwitchRow("Самостоятельное движение", "Бродит по экрану, как в видео", wander) {
            wander = it
            SettingsRepository.companionWander = it
            SettingsRepository.bump()
        }
        SwitchRow("Моргание", "Короткое прищуривание раз в несколько секунд", blink) {
            blink = it
            SettingsRepository.companionBlink = it
            SettingsRepository.bump()
        }
        SwitchRow("Собирать контекст приложения",
                  "Какому приложению принадлежит активное окно — только название, "
                    + "через Accessibility", appContext) {
            appContext = it
            SettingsRepository.collectAppContext = it
        }
        IntSliderRow("Размер", SettingsRepository.companionSizeDp, 48..320, " dp") {
            SettingsRepository.companionSizeDp = it
            SettingsRepository.bump()
        }
        FloatSliderRow("Скорость", SettingsRepository.companionSpeed, 0.25f..3.0f) {
            SettingsRepository.companionSpeed = it
            SettingsRepository.bump()
        }
        FloatSliderRow("Прозрачность", SettingsRepository.companionOpacity, 0.2f..1.0f) {
            SettingsRepository.companionOpacity = it
            SettingsRepository.bump()
        }
        IntSliderRow("Длительность реплики", SettingsRepository.bubbleSeconds, 2..30, " с") {
            SettingsRepository.bubbleSeconds = it
            SettingsRepository.bump()
        }
        IntSliderRow("Болтовня без запроса", SettingsRepository.chatterSeconds, 0..600,
                     " с (0 — выкл)") {
            SettingsRepository.chatterSeconds = it
            SettingsRepository.bump()
        }
        OutlinedTextField(personaField, { personaField = it },
                          label = { Text("Характер Capri (персона)") },
                          modifier = Modifier.fillMaxWidth(),
                          supportingText = { Text("Пусто — характер с сервера") })
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = {
                SettingsRepository.persona = personaField.text
                SettingsRepository.bump()
            }) { Text("Сохранить персону") }
            OutlinedButton(onClick = { scope.launch { vm.companionSay(context) } }) {
                Text("Сказать сейчас")
            }
        }
    }
}

@Composable
private fun AiSection() {
    Section("ИИ и память") {
        IntSliderRow("Реплик в истории", SettingsRepository.historyTurns, 0..60) {
            SettingsRepository.historyTurns = it
        }
        FloatSliderRow("Temperature", SettingsRepository.temperature, 0.0f..1.5f) {
            SettingsRepository.temperature = it
        }
        IntSliderRow("Max tokens", SettingsRepository.maxTokens, 64..4096) {
            SettingsRepository.maxTokens = it
        }
        var archive by remember { mutableStateOf(SettingsRepository.archiveEnabled) }
        SwitchRow("Архивировать чат", "Каждый запрос уходит в archives/ на сервере", archive) {
            archive = it
            SettingsRepository.archiveEnabled = it
        }
    }
}

@Composable
private fun ArchiveSection(net: NetworkClient) {
    val scope = rememberCoroutineScope()
    var files by remember { mutableStateOf<List<Pair<String, Int>>>(emptyList()) }
    var info by remember { mutableStateOf("") }

    fun refresh() {
        scope.launch {
            info = try {
                files = net.archivesList()
                val stats = net.archiveStats()
                "дней: ${stats.optJSONObject("stats")?.optInt("days") ?: 0}, "
                  .plus("открытых сессий: ${stats.optJSONObject("stats")?.optInt("open_sessions")
                      ?: 0}")
            } catch (e: Exception) {
                "не удалось получить: ${e.message}"
            }
        }
    }
    LaunchedEffect(Unit) { refresh() }

    Section("Архив чата", "Формат: archives/<YYYY-MM-DD>.jsonl на сервере") {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = { refresh() }) { Text("Обновить") }
        }
        if (info.isNotBlank()) Text(info, style = MaterialTheme.typography.bodySmall)
        files.forEach { (name, size) ->
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.SpaceBetween) {
                Text("$name (${size / 1024} КБ)", style = MaterialTheme.typography.bodySmall,
                     modifier = Modifier.weight(1f))
                OutlinedButton(onClick = {
                    scope.launch {
                        runCatching { net.deleteArchive(name) }
                        refresh()
                    }
                }) { Text("Удалить") }
            }
        }
    }
}

@Composable
private fun ScanSection() {
    var target by remember { mutableStateOf(SettingsRepository.scanTarget) }
    var ports by remember { mutableStateOf(SettingsRepository.scanPorts) }
    var banners by remember { mutableStateOf(SettingsRepository.scanBanners) }
    var ping by remember { mutableStateOf(SettingsRepository.scanPing) }
    var allowPublic by remember { mutableStateOf(SettingsRepository.allowPublic) }

    Section("Сканирование сети (своей)",
            "Значения по умолчанию для вкладки Device. Подтверждение владения "
              + "спрашивается каждый раз.") {
        OutlinedTextField(target, { target = it }, label = { Text("Цель по умолчанию") },
                          modifier = Modifier.fillMaxWidth(),
                          supportingText = { Text("IP, CIDR или хост") })
        OutlinedTextField(ports, { ports = it }, label = { Text("Порты") },
                          modifier = Modifier.fillMaxWidth(),
                          supportingText = { Text("через запятую, до 128 штук") })
        SwitchRow("Снимать баннеры", "Короткое приветствие сервиса", banners) {
            banners = it
            SettingsRepository.scanBanners = it
        }
        SwitchRow("ICMP-ping перед опросом", "Быстрее на больших подсетях", ping) {
            ping = it
            SettingsRepository.scanPing = it
        }
        SwitchRow("Разрешить публичные диапазоны",
                  "Только при письменном разрешении — сервер всё равно спросит отдельно",
                  allowPublic) {
            allowPublic = it
            SettingsRepository.allowPublic = it
        }
        Button(onClick = {
            SettingsRepository.scanTarget = target
            SettingsRepository.scanPorts = ports
        }) { Text("Сохранить") }
    }
}

@Composable
private fun ConnectionSection(net: NetworkClient) {
    var url by remember { mutableStateOf(net.customBaseUrl.ifBlank { SettingsRepository.serverUrl }) }
    Section("Подключение", "Канал до твоего ПК") {
        Channel.entries.forEach { ch ->
            Row(verticalAlignment = Alignment.CenterVertically) {
                androidx.compose.material3.RadioButton(
                    selected = net.channel == ch && url.isBlank(),
                    onClick = {
                        net.channel = ch
                        net.customBaseUrl = ""
                        url = ""
                        SettingsRepository.serverUrl = ""
                    })
                Text(ch.name + " — " + ch.baseUrl, style = MaterialTheme.typography.bodySmall)
            }
        }
        OutlinedTextField(url, {
            url = it
            net.customBaseUrl = it
            SettingsRepository.serverUrl = it
        }, label = { Text("Свой URL (LAN/Tailscale)") }, modifier = Modifier.fillMaxWidth())
    }
}
