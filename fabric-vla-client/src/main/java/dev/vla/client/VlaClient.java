package dev.vla.client;

import dev.vla.client.input.ActionApplier;
import dev.vla.client.input.ActionCmd;
import dev.vla.client.net.WsServer;
import net.fabricmc.api.ClientModInitializer;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientTickEvents;
import net.minecraft.client.MinecraftClient;
import net.minecraft.client.option.KeyBinding;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.concurrent.atomic.AtomicReference;

/**
 * VLA 受控客户端主入口（M1 通信底座 + M2 客户端控制）。
 *
 * <p>模式：
 * <ul>
 *   <li>API_MODE：Python 控制中枢接管输入/动作（KeyboardInputMixin/MouseMixin 隔离物理键鼠，
 *       END_CLIENT_TICK 把 {@link #currentAction} 交给 {@link ActionApplier} 注入）</li>
 *   <li>HUMAN_MODE：透明放行人工操作</li>
 * </ul>
 */
public final class VlaClient implements ClientModInitializer {
    public static final String MOD_ID = "vla-client";
    public static final String API_MODE = "api";
    public static final String HUMAN_MODE = "human";

    /** WS 端口：默认 30001，可用系统属性 {@code vla.ws.port} 覆盖。 */
    public static final int WS_PORT = Integer.getInteger("vla.ws.port", 30001);

    private static final Logger LOGGER = LoggerFactory.getLogger(MOD_ID);

    private static volatile VlaClient INSTANCE;

    /** WS 线程写入的原子动作缓冲；每 tick 由 END_CLIENT_TICK 消费。 */
    private static final AtomicReference<ActionCmd> currentAction = new AtomicReference<>();

    private volatile boolean apiMode = false;

    @Override
    public void onInitializeClient() {
        INSTANCE = this;
        LOGGER.info("[vla-client] VlaClient loaded (M2)");

        registerTickHandler();

        Thread wsThread = new Thread(() -> {
            WsServer server = new WsServer(WS_PORT, new WsServer.WsHandler() {
                @Override
                public void onModeChange(String mode) {
                    apiMode = API_MODE.equals(mode);
                    LOGGER.info("[vla-client] WS mode -> {} (apiMode={})", mode, apiMode);
                    unpressAllKeys(); // 防粘键
                    if (!apiMode) {
                        currentAction.set(null);
                    }
                }

                @Override
                public void onAction(ActionCmd cmd) {
                    LOGGER.info("[vla-client] WS action: hotbar={} camera=[{},{}] f/b/l/r={}/{}/{}/{} jump={} sneak={} sprint={} attack={} use={} drop={} inv={}",
                            cmd.hotbar, cmd.camera[0], cmd.camera[1],
                            cmd.forward, cmd.back, cmd.left, cmd.right,
                            cmd.jump, cmd.sneak, cmd.sprint,
                            cmd.attack, cmd.use, cmd.drop, cmd.inventory);
                    currentAction.set(cmd);
                }

                @Override
                public void onResetCamera(float yaw, float pitch) {
                    LOGGER.info("[vla-client] WS reset_camera yaw={} pitch={}", yaw, pitch);
                    MinecraftClient client = MinecraftClient.getInstance();
                    if (client != null && client.player != null) {
                        // setYaw/setPitch 必须在客户端线程执行
                        client.execute(() -> {
                            if (client.player != null) {
                                client.player.setYaw(yaw);
                                client.player.setPitch(pitch);
                            }
                        });
                    }
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

    /**
     * 每 tick 末尾：先释放上一 tick 注入的按键（防粘键），再把动作交给 ActionApplier 注入。
     * 动作按 tick 消费（getAndSet(null)），保证 camera 增量只作用一次、无动作时移动归零。
     */
    private void registerTickHandler() {
        ClientTickEvents.END_CLIENT_TICK.register(client -> {
            if (!apiMode) {
                return; // HUMAN_MODE 透明
            }
            ActionApplier.resetKeys(client);
            ActionCmd cmd = currentAction.getAndSet(null);
            if (cmd != null && client.player != null) {
                ActionApplier.apply(client, client.player, cmd);
            }
        });
    }

    /** 防粘键：模式切换时清空全部按键按下状态。 */
    private void unpressAllKeys() {
        MinecraftClient client = MinecraftClient.getInstance();
        if (client == null || client.options == null) {
            return;
        }
        for (KeyBinding keyBinding : client.options.allKeys) {
            keyBinding.setPressed(false);
        }
    }

    public static boolean isApiMode() {
        return INSTANCE != null && INSTANCE.apiMode;
    }

    public static ActionCmd getCurrentAction() {
        return currentAction.get();
    }

    public static VlaClient getInstance() {
        return INSTANCE;
    }
}
