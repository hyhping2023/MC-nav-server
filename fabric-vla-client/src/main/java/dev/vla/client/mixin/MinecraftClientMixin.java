package dev.vla.client.mixin;

import dev.vla.client.VlaClient;
import net.minecraft.client.MinecraftClient;
import net.minecraft.client.gui.screen.GameMenuScreen;
import net.minecraft.client.gui.screen.Screen;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

/**
 * M7：API_MODE 下阻止暂停菜单（GameMenuScreen）被打开。
 *
 * <p>窗口失焦（runClient 后台运行 / 终端抢焦点）会触发原版
 * {@code MinecraftClient} 打开暂停菜单。菜单打开期间
 * {@code MinecraftClient.tick()} 跳过 {@code handleInputEvents} → 挖掘
 * （handleBlockBreaking）完全不执行，而移动经 KeyboardInputMixin 直接写 input
 * 不受影响 —— 表现为"能走但挖不动"（M7 实测 screen=GameMenuScreen 41/42 采样）。
 *
 * <p>在 END_CLIENT_TICK 里 setScreen(null) 关菜单不够：菜单会在下一 tick 的输入
 * 处理前重新打开，挖掘仍被逐 tick 跳过。因此直接阻止 setScreen 安装暂停菜单。
 * HUMAN_MODE 完全放行（玩家需 Esc 暂停）。
 */
@Mixin(MinecraftClient.class)
public abstract class MinecraftClientMixin {

    @Inject(method = "setScreen", at = @At("HEAD"), cancellable = true)
    private void vlaBlockPauseMenu(Screen screen, CallbackInfo ci) {
        if (VlaClient.isApiMode() && screen instanceof GameMenuScreen) {
            ci.cancel();
        }
    }
}
