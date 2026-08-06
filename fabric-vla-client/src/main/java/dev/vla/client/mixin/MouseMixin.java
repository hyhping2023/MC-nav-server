package dev.vla.client.mixin;

import dev.vla.client.VlaClient;
import net.minecraft.client.Mouse;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

/**
 * M2 输入隔离：API_MODE 下取消鼠标视角更新（物理鼠标失效，DESIGN.md §5.3）。
 *
 * <p>genSources 核实：1.20.1 Yarn 中 {@link Mouse#updateMouse()} 是应用鼠标视角的
 * 方法（内部调用 {@code player.changeLookDirection}）。HUMAN_MODE 完全放行。
 */
@Mixin(Mouse.class)
public abstract class MouseMixin {

    @Inject(method = "updateMouse", at = @At("HEAD"), cancellable = true)
    private void vlaApiMouse(CallbackInfo ci) {
        if (VlaClient.isApiMode()) {
            ci.cancel();
        }
    }
}
