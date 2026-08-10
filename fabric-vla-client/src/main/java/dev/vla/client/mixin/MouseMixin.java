package dev.vla.client.mixin;

import dev.vla.client.VlaClient;
import dev.vla.client.input.KeyRecorder;
import net.minecraft.client.Mouse;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;

/**
 * M2+M7+M7.1 输入隔离：API_MODE 下取消鼠标视角更新（物理鼠标失效，DESIGN.md §5.3）。
 *
 * <p>genSources 核实：1.20.1 Yarn 中 {@link Mouse#updateMouse()} 是应用鼠标视角的
 * 方法（内部调用 {@code player.changeLookDirection}）。HUMAN_MODE 完全放行。
 *
 * <p>M7：API_MODE 下强制 {@code isCursorLocked()} 返回 true。原版 {@code lockCursor()}
 * 在窗口失焦时直接 return（cursorLocked 保持 false），而挖掘依赖
 * {@code MinecraftClient.handleBlockBreaking} 的
 * {@code attackKey.isPressed() && mouse.isCursorLocked()} 判定 —— 窗口失焦会导致
 * 挖掘完全失效（移动不受影响，因为移动经 KeyboardInputMixin 直接写 input）。
 *
 * <p>M7.1（修复用户反馈：API 模式仍抓鼠标 + 失焦弹菜单）：
 * - 取消 {@code lockCursor()}：API 模式下**不物理抓取鼠标**（光标自由、不再"强制读取"）。
 * - {@code isCursorLocked()} 的谎报 true 保留（handleBlockBreaking 依赖它）；
 *   真实抓取与谎报解耦后，用户鼠标不被劫持，挖掘仍可用。
 * - 失焦弹菜单由 {@code VlaClient} 置 {@code options.pauseOnLostFocus=false} 解决。
 */
@Mixin(Mouse.class)
public abstract class MouseMixin {

    @Inject(method = "updateMouse", at = @At("HEAD"), cancellable = true)
    private void vlaApiMouse(CallbackInfo ci) {
        if (VlaClient.isApiMode()) {
            ci.cancel();
        }
    }

    @Inject(method = "isCursorLocked", at = @At("HEAD"), cancellable = true)
    private void vlaApiCursorLocked(CallbackInfoReturnable<Boolean> cir) {
        if (VlaClient.isApiMode()) {
            cir.setReturnValue(true);
        }
    }

    @Inject(method = "lockCursor", at = @At("HEAD"), cancellable = true)
    private void vlaApiNoGrab(CallbackInfo ci) {
        if (VlaClient.isApiMode()) {
            ci.cancel();
        }
    }

    @Inject(method = "onMouseButton", at = @At("HEAD"), cancellable = true)
    private void vlaApiNoClick(long window, int button, int action, int mods, CallbackInfo ci) {
        // M11：HUMAN 模式 + 录制开启 → 记录真实鼠标点击（默认绑定 0=攻击 1=使用 2=拾取），
        // 然后透明放行；API 模式照旧取消（物理鼠标隔离）。
        if (!VlaClient.isApiMode()) {
            if (KeyRecorder.isEnabled()) {
                String key = switch (button) {
                    case 0 -> "attack";
                    case 1 -> "use";
                    case 2 -> "middle";
                    default -> null;
                };
                if (key != null) {
                    KeyRecorder.record(key, action == 1, System.nanoTime());
                }
            }
            return;
        }
        ci.cancel();
    }
}
