package dev.vla.client.mixin;

import net.minecraft.client.MinecraftClient;
import net.minecraft.client.util.Session;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.Mutable;
import org.spongepowered.asm.mixin.gen.Accessor;
import org.spongepowered.asm.mixin.gen.Invoker;

/**
 * M7：覆盖客户端 session 用户名（agent0 固定身份）。
 *
 * <p>{@code MinecraftClient.session} 是 private final 字段（Yarn 1.20.1 核实），
 * 用 Accessor + {@link Mutable} 生成 setter；VlaClient 在 autojoin 入服前写入
 * {@code new Session("agent0", offlineUuid, "token", LEGACY)}，使登录握手
 * （ConnectScreen → LoginHelloC2SPacket）携带固定用户名 agent0。
 */
@Mixin(MinecraftClient.class)
public interface MinecraftClientAccessor {

    @Mutable
    @Accessor("session")
    void setSession(Session session);

    /** API mode 下直接执行一次原版攻击逻辑，避免仅设置 attackKey 要等下一 tick。 */
    @Invoker("doAttack")
    boolean invokeDoAttack();
}
