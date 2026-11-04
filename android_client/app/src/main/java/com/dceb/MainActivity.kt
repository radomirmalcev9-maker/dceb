package com.dceb

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.lifecycle.viewmodel.compose.viewModel
import com.dceb.ai.AiViewModel
import com.dceb.core.NetworkClient
import com.dceb.ui.App

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val net = NetworkClient()
        setContent {
            val vm: AiViewModel = viewModel(factory = object : androidx.lifecycle.ViewModelProvider.Factory {
                @Suppress("UNCHECKED_CAST")
                override fun <T : androidx.lifecycle.ViewModel> create(modelClass: Class<T>): T =
                    AiViewModel(net) as T
            })
            App(context = this, net = net, vm = vm)
        }
    }
}
