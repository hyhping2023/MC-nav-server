plugins {
    id("fabric-loom") version "1.5.8"
    id("java")
}

version = "0.1.0"
group = "dev.vla"

base {
    archivesName = "vla-client"
}

repositories {
    mavenCentral()
}

/**
 * 并发录制启动覆盖项（均为可选 Gradle project property）：
 *
 *   ./gradlew runClient -PvlaRunDir=../runtime/worker-00/client-run \
 *       -PvlaWsPort=30001 -PvlaClientXmx=1G
 *
 * 每个客户端必须有独立 runDir（autojoin/options/logs 均写在该目录）和独立 WS 端口。
 * 不设时保持 loom 默认 run/、客户端默认 WS 30001 与 Gradle 默认内存行为。
 */
val vlaRunDir = providers.gradleProperty("vlaRunDir")
val vlaWsPort = providers.gradleProperty("vlaWsPort")
val vlaClientXmx = providers.gradleProperty("vlaClientXmx")

loom {
    // M0: 仅骨架，无 mixin 目标类；M1 起添加 accessWidener 与 mixin 配置
    runs {
        named("client") {
            if (vlaRunDir.isPresent) {
                runDir(vlaRunDir.get())
            }
            if (vlaWsPort.isPresent) {
                vmArg("-Dvla.ws.port=${vlaWsPort.get()}")
            }
            if (vlaClientXmx.isPresent) {
                vmArg("-Xmx${vlaClientXmx.get()}")
            }
        }
    }
}

dependencies {
    // Minecraft + Yarn mappings + Fabric Loader
    minecraft("com.mojang:minecraft:${property("minecraft_version")}")
    mappings("net.fabricmc:yarn:${property("yarn_mappings")}:v2")
    modImplementation("net.fabricmc:fabric-loader:${property("loader_version")}")

    // Fabric API
    modImplementation("net.fabricmc.fabric-api:fabric-api:${property("fabric_version")}")

    // WebSocket 通信（VLA 控制中枢 <-> 客户端），打包进 mod jar
    implementation("org.java-websocket:Java-WebSocket:1.5.6")
    include("org.java-websocket:Java-WebSocket:1.5.6")
}

java {
    withSourcesJar()
}

tasks.withType<JavaCompile>().configureEach {
    options.encoding = "UTF-8"
    options.release = 17
}

tasks.processResources {
    inputs.property("version", project.version)
    filesMatching("fabric.mod.json") {
        expand("version" to project.version)
    }
}
