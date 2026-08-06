package dev.vla.client;

import dev.vla.client.gfx.FrameGrabber;
import dev.vla.client.input.ActionApplier;
import dev.vla.client.input.ActionCmd;
import dev.vla.client.net.FrameSender;
import dev.vla.client.net.WsServer;
import net.fabricmc.api.ClientModInitializer;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientLifecycleEvents;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientTickEvents;
import net.fabricmc.fabric.api.client.rendering.v1.WorldRenderEvents;
import net.minecraft.client.MinecraftClient;
import net.minecraft.client.gui.screen.ConnectScreen;
import net.minecraft.client.gui.screen.TitleScreen;
import net.minecraft.client.network.ServerAddress;
import net.minecraft.client.network.ServerInfo;
import net.minecraft.client.option.KeyBinding;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;

/**
 * VLA 受控客户端主入口（M1 通信底座 + M2 客户端控制 + M3 抓帧/帧上行/autojoin）。
 *
 * <p>模式：
 * <ul>
 *   <li>API_MODE：Python 控制中枢接管输入/动作（KeyboardInputMixin/MouseMixin 隔离物理键鼠，
 *       END_CLIENT_TICK 把 {@link #currentAction} 交给 {@link ActionApplier} 注入）</li>
 *   <li>HUMAN_MODE：透明放行人工操作</li>
 * </ul>
 *
 * <p>M3：
 * <ul>
 *   <li>{@link FrameGrabber} 挂 WorldRenderEvents.LAST（世界+实体渲染完、HUD 前），渲染线程抓帧入队</li>
 *   <li>{@link FrameSender} 后台线程 JPEG 编码 + 二进制 WS 上行（§9.2）</li>
 *   <li>autojoin：run/autojoin.txt 存在时，CLIENT_STARTED 后编程式加入指定服务器</li>
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

    /** M3：帧队列（渲染线程只入队，FrameSender 后台线程消费编码+上行）。 */
    private final ConcurrentLinkedQueue<FrameGrabber.FrameData> frameQueue = new ConcurrentLinkedQueue<>();

    private WsServer wsServer;
    private FrameGrabber frameGrabber;
    private FrameSender frameSender;
    private final AtomicBoolean autoJoinAttempted = new AtomicBoolean(false);

    @Override
    public void onInitializeClient() {
        INSTANCE = this;
        LOGGER.info("[vla-client] VlaClient loaded (M3)");

        startWsServer();
        registerTickHandler();
        registerFramePipeline();
        registerAutoJoin();
    }

    private void startWsServer() {
        wsServer = new WsServer(WS_PORT, new WsServer.WsHandler() {
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

        Thread wsThread = new Thread(() -> {
            try {
                wsServer.start();
                LOGGER.info("[vla-client] WS server listening on port {}", WS_PORT);
            } catch (Exception e) {
                LOGGER.error("[vla-client] failed to start WS server", e);
            }
        }, "vla-ws-server");
        wsThread.setDaemon(true);
        wsThread.start();
    }

    /** M3：抓帧（渲染线程 FBO 下采样）+ 帧上行（后台守护线程 JPEG + WS 二进制）。 */
    private void registerFramePipeline() {
        frameGrabber = new FrameGrabber(frameQueue);
        frameSender = new FrameSender(wsServer, frameQueue, FrameGrabber.SIZE, FrameGrabber.SIZE);
        frameSender.start();

        // 世界+实体渲染完后、HUD 前（画面纯净，无准星/血条污染像素观测）
        WorldRenderEvents.LAST.register(ctx -> frameGrabber.capture());
    }

    /** M3：若 run/autojoin.txt 存在（内容 host:port），主菜单就绪后编程式加入。 */
    private void registerAutoJoin() {
        ClientLifecycleEvents.CLIENT_STARTED.register(client -> {
            String target = readAutoJoinTarget(client);
            if (target == null) {
                return;
            }
            if (!autoJoinAttempted.compareAndSet(false, true)) {
                return;
            }

            String host = target;
            int port = 25565;
            int colon = target.lastIndexOf(':');
            if (colon > 0) {
                host = target.substring(0, colon);
                try {
                    port = Integer.parseInt(target.substring(colon + 1).trim());
                } catch (NumberFormatException e) {
                    LOGGER.warn("[vla-client] invalid port in autojoin.txt: {}", target);
                }
            }
            final String connectHost = host;
            final int connectPort = port;
            client.execute(() -> {
                // ConnectScreen.connect 是 1.20.1 编程式加入服务器的标准入口（genSources 核实）
                ConnectScreen.connect(new TitleScreen(), client,
                        new ServerAddress(connectHost, connectPort),
                        new ServerInfo("vla-autojoin", target, false), false);
                LOGGER.info("[vla-client] autojoin -> {}:{}", connectHost, connectPort);
            });
        });
    }

    /** 读取 autojoin 目标（优先 client.runDirectory 下的 autojoin.txt）。 */
    private String readAutoJoinTarget(MinecraftClient client) {
        File[] candidates = {
                new File(client.runDirectory, "autojoin.txt"),
                new File("autojoin.txt"),
                new File("run/autojoin.txt"),
        };
        for (File file : candidates) {
            if (file.isFile()) {
                try {
                    String content = Files.readString(file.toPath(), StandardCharsets.UTF_8).trim();
                    return content.isEmpty() ? null : content;
                } catch (IOException e) {
                    LOGGER.warn("[vla-client] failed to read autojoin.txt: {}", e.getMessage());
                    return null;
                }
            }
        }
        return null;
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
