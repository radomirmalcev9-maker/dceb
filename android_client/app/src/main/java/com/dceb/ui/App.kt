package com.dceb.ui

import android.content.Context
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.scale
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.unit.dp
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import com.dceb.R
import com.dceb.ai.AiViewModel
import com.dceb.core.Channel
import com.dceb.core.NetworkClient
import com.dceb.ir.IrParser
import com.dceb.ir.IrTransmitter
import com.dceb.usb.DuckyInterpreter
import com.dceb.usb.HidGadget
import kotlinx.coroutines.launch

@Composable
fun App(context: Context, net: NetworkClient, vm: AiViewModel) {
    val nav = rememberNavController()
    val navBackStack by nav.currentBackStackEntryFlow.collectAsState(initial = null)

    Scaffold(
        bottomBar = {
            NavigationBar {
                listOf("ai" to "AI", "dashboard" to "Device", "ir" to "IR",
                       "hid" to "HID", "settings" to "Net").forEach { (route, label) ->
                    NavigationBarItem(
                        selected = navBackStack?.destination?.route == route,
                        onClick = { nav.navigate(route) { popUpTo(nav.graph.id) { saveState = true } } },
                        label = { Text(label) },
                        icon = { Text(label.first().toString()) })
                }
            }
        }
    ) { padding ->
        NavHost(nav, startDestination = "ai", modifier = Modifier.padding(padding)) {
            composable("ai") { AiScreen(context, vm, net) }
            composable("dashboard") {
                DashboardScreen(
                    health = vm.health.collectAsState().value,
                    hwText = vm.hw.collectAsState().value?.summary ?: "not probed",
                    onProbe = { vm.refreshHardware(context) },
                    onRefreshHealth = { vm.refreshHealth() })
            }
            composable("ir") { IrScreen(context, net) }
            composable("hid") { HidScreen() }
            composable("settings") { SettingsScreen(net) }
        }
    }
}

@Composable
private fun DashboardScreen(health: org.json.JSONObject?, hwText: String,
                            onProbe: () -> Unit, onRefreshHealth: () -> Unit) {
    Column(Modifier.padding(16.dp).fillMaxSize(), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text("Do Cats Eat Bats — Device", style = MaterialTheme.typography.titleLarge)
        Button(onClick = onProbe) { Text("Probe hardware (root)") }
        Button(onClick = onRefreshHealth) { Text("Ping PC server") }
        Text("Server health: ${health?.toString() ?: "—"}")
        Text(hwText, style = MaterialTheme.typography.bodyMedium)
    }
}

@Composable
private fun IrScreen(context: Context, net: NetworkClient) {
    val scope = rememberCoroutineScope()
    var query by remember { mutableStateOf("") }
    var categories by remember { mutableStateOf<List<String>>(emptyList()) }
    var selectedCategory by remember { mutableStateOf<String?>("Projectors") } // projectors first
    var results by remember { mutableStateOf<List<Map<String, String>>>(emptyList()) }
    var fileText by remember { mutableStateOf("") }
    var status by remember { mutableStateOf("") }
    var parsed by remember { mutableStateOf<List<IrParser.IrSignal>>(emptyList()) }

    LaunchedEffect(Unit) {
        runCatching {
            categories = net.irdbCategories()
            results = net.irdbSearch("", selectedCategory)
        }
    }

    Column(Modifier.padding(16.dp).fillMaxSize(), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text("IR Blaster", style = MaterialTheme.typography.titleLarge)
        LazyRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            items(categories) { cat ->
                FilterChip(
                    selected = selectedCategory == cat,
                    onClick = {
                        selectedCategory = cat
                        scope.launch { runCatching { results = net.irdbSearch(query, cat) } }
                    },
                    label = { Text(cat) })
            }
        }
        OutlinedTextField(query, { query = it }, label = { Text("Search IRDB on server (brand/model)") })
        Button(onClick = { scope.launch {
            runCatching { results = net.irdbSearch(query, selectedCategory) }
        } }) { Text("Search") }
        LazyColumn(Modifier.weight(1f)) {
            items(results) { r ->
                TextButton(onClick = { scope.launch {
                    runCatching {
                        fileText = net.irdbFile(r["category"]!!, r["brand"]!!, r["model"]!!)
                        parsed = IrParser.parse(fileText).signals
                        status = "parsed ${parsed.size} signals"
                    }
                } }) { Text("${r["category"]}/${r["brand"]}/${r["model"]}") }
            }
        }
        Text(status)
        LazyColumn(Modifier.weight(1f)) {
            items(parsed) { s ->
                TextButton(onClick = { scope.launch {
                    status = if (IrTransmitter.transmitSignal(context, s))
                        "sent: ${s.name}" else "FAILED: ${s.name}"
                } }) { Text("▶ ${s.name} (${s.protocol}, ${s.frequency}Hz)") }
            }
        }
        Text(status, style = MaterialTheme.typography.bodySmall)
    }
}

@Composable
private fun HidScreen() {
    val scope = rememberCoroutineScope()
    var script by remember { mutableStateOf("GUI r\nDELAY 500\nSTRING echo dceb\nENTER") }
    var status by remember { mutableStateOf("") }
    var confirm by remember { mutableStateOf(false) }

    Column(Modifier.padding(16.dp).fillMaxSize(), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text("USB HID Gadget (ConfigFS)", style = MaterialTheme.typography.titleLarge)
        Text("⚠ Types into the machine this phone is plugged into. Authorized machines only.",
             style = MaterialTheme.typography.bodySmall)
        OutlinedTextField(script, { script = it }, modifier = Modifier.fillMaxWidth().weight(1f),
                          label = { Text("DuckyScript") })
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = { scope.launch { status = "gadget: ${HidGadget.enableHid()}" } }) {
                Text("Enable HID gadget") }
            Button(onClick = { confirm = true }, enabled = !confirm) { Text("Run script") }
        }
        if (confirm) {
            AlertDialog(
                onDismissRequest = { confirm = false },
                title = { Text("Confirm HID execution") },
                text = { Text("This will type the script into the connected host. Proceed?") },
                confirmButton = { Button(onClick = {
                    confirm = false
                    scope.launch { status = "reports written: ${DuckyInterpreter.run(script = script)}" }
                }) { Text("Type it") } },
                dismissButton = { Button(onClick = { confirm = false }) { Text("Cancel") } })
        }
        Text(status, style = MaterialTheme.typography.bodySmall)
    }
}

@Composable
private fun AiScreen(context: Context, vm: AiViewModel, net: NetworkClient) {
    val scope = rememberCoroutineScope()
    var input by remember { mutableStateOf("") }
    var mode by remember { mutableStateOf("analyze_ir") }
    var status by remember { mutableStateOf("") }
    val messages by vm.messages.collectAsState()
    val pending by vm.pending.collectAsState()
    val history by vm.history.collectAsState()

    LaunchedEffect(Unit) { vm.connectStreaming() }

    // Pulsing animation for the Capri avatar + "online" dot.
    val pulse by rememberInfiniteTransition(label = "capri-pulse").animateFloat(
        initialValue = 0.6f, targetValue = 1.4f,
        animationSpec = infiniteRepeatable(tween(900), RepeatMode.Reverse), label = "pulse")

    Column(Modifier.padding(16.dp).fillMaxSize(), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Image(
                painter = painterResource(id = R.drawable.capri_avatar),
                contentDescription = "Capri",
                modifier = Modifier.size(40.dp).scale(pulse * 0.7f + 0.3f).clip(CircleShape))
            Box(Modifier.size(12.dp).scale(pulse).background(Color(0xFF00E676), CircleShape))
            Text("Capricorn AI Terminal", style = MaterialTheme.typography.titleLarge)
        }
        Text("Личный лабораторный ассистент: анализирует дампы и предлагает действия "
           + "только на твоих устройствах — каждое действие требует подтверждения.",
             style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.secondary)
        LazyColumn(Modifier.weight(1f)) {
            items(messages) { m ->
                Text(if (m.fromUser) "▶ ${m.text}" else "◀ ${m.text}",
                     style = MaterialTheme.typography.bodySmall)
            }
        }
        pending?.let { p ->
            Card {
                Column(Modifier.padding(8.dp)) {
                    Text("Server suggests action: ${p.action}", style = MaterialTheme.typography.titleSmall)
                    Text(p.explanation, style = MaterialTheme.typography.bodySmall)
                    Row {
                        Button(onClick = {
                            // Explicit user approval — execution happens here, on tap only.
                            scope.launch { status = vm.executePending(context) }
                        }) { Text("Approve") }
                        Button(onClick = { vm.dismissPending() }) { Text("Reject") }
                    }
                    if (status.isNotEmpty()) Text(status, style = MaterialTheme.typography.bodySmall)
                }
            }
        }
        OutlinedTextField(mode, { mode = it }, label = { Text("mode") })
        OutlinedTextField(input, { input = it }, label = { Text("raw data / question") })
        Button(onClick = {
            scope.launch {
                vm.send(mode, input)
                if (mode.startsWith("dump")) vm.sendDump(mode.removePrefix("dump:"), input)
            }
        }) { Text("Send to server") }
    }
}

@Composable
private fun SettingsScreen(net: NetworkClient) {
    var url by remember { mutableStateOf(net.customBaseUrl) }
    Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text("Connection channel", style = MaterialTheme.typography.titleLarge)
        Channel.entries.forEach { ch ->
            Row {
                RadioButton(selected = net.channel == ch && url.isBlank(),
                            onClick = { net.channel = ch; net.customBaseUrl = "" })
                Text(ch.name + " — " + ch.baseUrl)
            }
        }
        OutlinedTextField(url, { url = it; net.customBaseUrl = it },
                          label = { Text("Custom base URL (LAN/Tailscale IP)") })
    }
}
