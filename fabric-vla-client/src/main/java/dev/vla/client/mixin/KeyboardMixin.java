package dev.vla.client.mixin;

import dev.vla.client.VlaClient;
import dev.vla.client.input.KeyRecorder;
import net.minecraft.client.Keyboard;
import net.minecraft.client.MinecraftClient;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

/**
 * M11 真人按键录制：HUMAN_MODE 下记录真实键盘事件（不 cancel，透明放行）。
 *
 * <p>genSources 核实：1.20.1 Yarn {@code Keyboard#onKey(long, int, int, int, int)}
 * （window, key, scancode, action, modifiers；GLFW action: 1=press 0=release 2=repeat）。
 * 只记录 11 按键 + 快捷栏的按下/抬起事件，其余按键（F3/ESC 等）忽略。
 * API 模式完全跳过（物理键盘已隔离，注入路径由 KeyRecorder.diff 产生事件）。
 */
@Mixin(Keyboard.class)
public abstract class KeyboardMixin {

    @Inject(method = "onKey", at = @At("HEAD"))
    private void vlaRecordKey(long window, int key, int scancode, int action, int modifiers,
                              CallbackInfo ci) {
        if (VlaClient.isApiMode() || !KeyRecorder.isEnabled()) {
            return;
        }
        String name = resolveButton(key, scancode);
        if (name == null) {
            return;
        }
        KeyRecorder.record(name, action == 1, System.nanoTime());
    }

    private static String resolveButton(int key, int scancode) {
        MinecraftClient client = MinecraftClient.getInstance();
        if (client == null || client.options == null) {
            return null;
        }
        if (client.options.forwardKey.matchesKey(key, scancode)) return "forward";
        if (client.options.backKey.matchesKey(key, scancode)) return "back";
        if (client.options.leftKey.matchesKey(key, scancode)) return "left";
        if (client.options.rightKey.matchesKey(key, scancode)) return "right";
        if (client.options.jumpKey.matchesKey(key, scancode)) return "jump";
        if (client.options.sneakKey.matchesKey(key, scancode)) return "sneak";
        if (client.options.sprintKey.matchesKey(key, scancode)) return "sprint";
        if (client.options.attackKey.matchesKey(key, scancode)) return "attack";
        if (client.options.useKey.matchesKey(key, scancode)) return "use";
        if (client.options.dropKey.matchesKey(key, scancode)) return "drop";
        if (client.options.inventoryKey.matchesKey(key, scancode)) return "inventory";
        for (int i = 0; i < client.options.hotbarKeys.length; i++) {
            if (client.options.hotbarKeys[i].matchesKey(key, scancode)) {
                return "hotbar:" + i;
            }
        }
        return null;
    }
}
