plugins {
    java
    id("com.gradleup.shadow") version "8.3.5"
    id("com.google.protobuf") version "0.9.4"
}

group = "dev.vla"
version = "0.1.0"

repositories {
    maven("https://repo.papermc.io/repository/maven-public/")
    mavenCentral()
}

dependencies {
    compileOnly("io.papermc.paper:paper-api:1.20.1-R0.1-SNAPSHOT")
    // grpc-stub 的 POM 不传递 javax.annotation-api（compileOnly），但生成的代码用到 @javax.annotation.Generated
    compileOnly("javax.annotation:javax.annotation-api:1.3.2")
    // gRPC 通信底座（M1）
    implementation("io.grpc:grpc-netty-shaded:1.62.2")
    implementation("io.grpc:grpc-protobuf:1.62.2")
    implementation("io.grpc:grpc-stub:1.62.2")
    implementation("io.grpc:grpc-services:1.62.2") // ProtoReflectionService
    implementation("com.google.protobuf:protobuf-java:3.25.3")
}

java {
    toolchain {
        languageVersion.set(JavaLanguageVersion.of(21))
    }
}

protobuf {
    protoc {
        artifact = "com.google.protobuf:protoc:3.25.3"
    }
    plugins {
        create("grpc") {
            artifact = "io.grpc:protoc-gen-grpc-java:1.62.2"
        }
    }
    generateProtoTasks {
        all().forEach {
            it.plugins {
                create("grpc")
            }
        }
    }
}

tasks.shadowJar {
    archiveFileName.set("vla-purpur.jar")
    // relocate gRPC/protobuf/guava 等重型依赖，避免与服务器自带库版本冲突
    relocate("io.grpc", "dev.vla.shadow.io.grpc")
    relocate("io.netty", "dev.vla.shadow.io.netty")
    relocate("com.google.protobuf", "dev.vla.shadow.com.google.protobuf")
    relocate("com.google.common", "dev.vla.shadow.com.google.common")
    relocate("org.checkerframework", "dev.vla.shadow.org.checkerframework")
}

tasks.jar {
    // 普通 jar 仅供调试；部署用 shadowJar 产物
    archiveFileName.set("vla-purpur-plain.jar")
}

// shadow 插件默认不把 shadowJar 挂到 build/assemble，显式挂钩，保证 `./gradlew build` 产出部署 jar
tasks.build {
    dependsOn(tasks.shadowJar)
}
