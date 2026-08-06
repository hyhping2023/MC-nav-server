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

loom {
    // M0: 仅骨架，无 mixin 目标类；M1 起添加 accessWidener 与 mixin 配置
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
