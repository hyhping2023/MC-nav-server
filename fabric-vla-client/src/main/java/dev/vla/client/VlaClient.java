package dev.vla.client;

import net.fabricmc.api.ClientModInitializer;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * VLA 受控客户端主入口（M0 骨架）。
 *
 * 模式占位（M1 使用）：
 * - API_MODE：Python 控制中枢接管输入/动作，客户端向 WS 服务器上行帧与状态
 * - HUMAN_MODE：透明放行人工操作（调试/人工演示）
 */
public final class VlaClient implements ClientModInitializer {
    public static final String MOD_ID = "vla-client";
    public static final String API_MODE = "api";
    public static final String HUMAN_MODE = "human";

    private static final Logger LOGGER = LoggerFactory.getLogger(MOD_ID);

    @Override
    public void onInitializeClient() {
        LOGGER.info("[vla-client] VlaClient loaded (M0)");
    }
}
