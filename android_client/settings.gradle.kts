pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
        // libsu (com.github.topjohnwu.libsu) публикуется только на JitPack
        maven { url = uri("https://jitpack.io") }
    }
}
rootProject.name = "DoCatsEatBats"
include(":app")
