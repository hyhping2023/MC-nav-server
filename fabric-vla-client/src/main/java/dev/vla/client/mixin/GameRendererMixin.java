package dev.vla.client.mixin;

import dev.vla.client.VlaClient;
import net.minecraft.client.render.GameRenderer;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

/**
 * M9.1：HUD 抓帧（demo 录制需要完整 UI）。
 *
 * <p>genSources 核实（1.20.1 Yarn）：{@link GameRenderer#render(float, long, boolean)}
 * 在方法尾（TAIL）时主 framebuffer 已包含 世界 + 手 + HUD + 准星（inGameHud.render 已
 * 绘入 {@code client.getFramebuffer()}），此时抓帧得到完整 UI 画面。
 *
 * <p>仅在 {@link VlaClient#isCaptureUi()} 为 true 时抓帧（与 WorldRenderEvents.LAST 的
 * 默认纯净抓帧互斥，同一时刻只有一个钩子在抓帧）；VLA 观测（captureUi=false）不受影响。
 */
@Mixin(GameRenderer.class)
public abstract class GameRendererMixin {

    @Inject(method = "render", at = @At("TAIL"))
    private void vlaCaptureWithHud(float tickDelta, long startTime, boolean tick, CallbackInfo ci) {
        if (VlaClient.isCaptureUi()) {
            VlaClient.captureFrameIfUi();
        }
    }
}
