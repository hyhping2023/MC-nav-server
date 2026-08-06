package dev.vla.client.mixin;

import dev.vla.client.VlaClient;
import dev.vla.client.input.ActionCmd;
import net.minecraft.client.MinecraftClient;
import net.minecraft.client.input.KeyboardInput;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

/**
 * M2 输入隔离：API_MODE 下接管移动输入（DESIGN.md §5.3）。
 *
 * <p>HUMAN_MODE 完全放行（透明）。API_MODE 下取消原逻辑，把 WS 动作的移动量
 * 写入 {@link Input} 的已核实 Yarn 字段（pressing 系 / movement 系 / jumping / sneaking）。
 *
 * <p>注：1.20.1 的 {@code Input} 基类没有 {@code sprinting} 字段（DESIGN.md §5.3
 * 伪代码中的旧字段在 1.20.1 已不存在），疾跑状态挂在实体上
 * （{@code Entity#setSprinting}），同时喂给 sprintKey 让原版 tickMovement 逻辑对齐。
 */
@Mixin(KeyboardInput.class)
public abstract class KeyboardInputMixin {

    @Inject(method = "tick", at = @At("HEAD"), cancellable = true)
    private void vlaApiTick(boolean slowDown, float slowDownFactor, CallbackInfo ci) {
        if (!VlaClient.isApiMode()) {
            return;
        }

        KeyboardInput self = (KeyboardInput) (Object) this;
        ActionCmd action = VlaClient.getCurrentAction();

        self.pressingForward = action != null && action.forward;
        self.pressingBack = action != null && action.back;
        self.pressingLeft = action != null && action.left;
        self.pressingRight = action != null && action.right;
        self.movementForward = (action != null && action.forward ? 1.0f : 0.0f)
                - (action != null && action.back ? 1.0f : 0.0f);
        self.movementSideways = (action != null && action.left ? 1.0f : 0.0f)
                - (action != null && action.right ? 1.0f : 0.0f);
        self.jumping = action != null && action.jump;
        self.sneaking = action != null && action.sneak;

        MinecraftClient client = MinecraftClient.getInstance();
        client.options.sprintKey.setPressed(action != null && action.sprint);
        if (client.player != null) {
            client.player.setSprinting(action != null && action.sprint);
        }

        ci.cancel();
    }
}
