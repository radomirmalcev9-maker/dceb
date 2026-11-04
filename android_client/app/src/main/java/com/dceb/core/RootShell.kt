package com.dceb.core

import com.topjohnwu.superuser.Shell
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * Async root command executor backed by libsu.
 * A single persistent `su` shell is reused; commands run sequentially on IO.
 */
object RootShell {

    @Volatile
    var isRootAvailable: Boolean = false
        private set

    init {
        // libsu performs the root check lazily on first use; do it eagerly.
        runCatching {
            isRootAvailable = Shell.isAppGrantedRoot() == true
            if (isRootAvailable) Shell.getShell()
        }.onFailure { isRootAvailable = false }
    }

    data class Result(val exitCode: Int, val stdout: String, val stderr: String) {
        val ok: Boolean get() = exitCode == 0
    }

    /** Run one privileged command, suspending. Output is capped to avoid OOM on huge logs. */
    suspend fun exec(vararg command: String, maxOutput: Int = 200_000): Result =
        withContext(Dispatchers.IO) {
            if (!isRootAvailable) return@withContext Result(-1, "", "root not available")
            val shell = Shell.getShell()
            val result = shell.newJob().add(*command).exec()
            val stdout = result.out.joinToString("\n").take(maxOutput)
            val stderr = result.err.joinToString("\n").take(maxOutput)
            Result(result.code, stdout, stderr)
        }

    /** Convenience: `su -c <single string>` equivalent via libsu job. */
    suspend fun exec(commandLine: String): Result = exec("sh", "-c", commandLine)
}
