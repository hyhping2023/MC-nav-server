package dev.vla.client;

import dev.vla.client.net.WsServer;
import net.fabricmc.api.ClientModInitializer;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * VLA 受控客户端主入口（M1：通信底座）。
 *
 * 模式：
 * - API_MODE：Python 控制中枢接管输入/动作，客户端向 WS 服务器上行帧与状态
 * - HUMAN_MODE：透明放行人工操作（调试/人工演示）
 *
 * onInitializeClient 中新开守护线程启动内嵌 WsServer（端口见 {@link #WS_PORT}），
 * WsHandler 把 mode 变更写入 {@link #apiMode} 并打日志。
 */
public final class VlaClient implements ClientModInitializer {
    public static final String MOD_ID = "vla-client";
    public static final String API_MODE = "api";
    public static final String HUMAN_MODE = "human";

    /** WS 端口：默认 30001，可用系统属性 {@code vla.ws.port} 覆盖。 */
    public static final int WS_PORT = Integer.getInteger("vla.ws.port", 30001);

    private static final Logger LOGGER = LoggerFactory.getLogger(MOD_ID);

    private static volatile VlaClient INSTANCE;

    private volatile boolean apiMode = false;

    @Override
    public void onInitializeClient() {
        INSTANCE = this;
        LOGGER.info("[vla-client] VlaClient loaded (M1)");

        Thread wsThread = new Thread(() -> {
            WsServer server = new WsServer(WS_PORT, new WsServer.WsHandler() {
                @Override
                public void onModeChange(String mode) {
                    apiMode = API_MODE.equals(mode);
                    LOGGER.info("[vla-client] WS mode -> {} (apiMode={})", mode, apiMode);
                }

                @Override
                public void onConnect(String session) {
                    LOGGER.info("[vla-client] WS session connected: {}", session);
                }

                @Override
                public void onDisconnect(String session) {
                    LOGGER.info("[vla-client] WS session disconnected: {}", session);
                }
            });
            try {
                server.start();
                LOGGER.info("[vla-client] WS server listening on port {}", WS_PORT);
            } catch (Exception e) {
                LOGGER.error("[vla-client] failed to start WS server", e);
            }
        }, "vla-ws-server");
        wsThread.setDaemon(true);
        wsThread.start();
    }

    public boolean isApiMode() {
        return apiMode;
    }

    public static VlaClient getInstance() {
        return INSTANCE;
    }
}
