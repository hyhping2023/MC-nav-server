package dev.vla.client;

import dev.vla.client.gfx.FrameGrabber;
import dev.vla.client.input.ActionApplier;
import dev.vla.client.input.ActionCmd;
import dev.vla.client.mixin.MinecraftClientAccessor;
import dev.vla.client.net.FrameSender;
import dev.vla.client.net.WsServer;
import net.fabricmc.api.ClientModInitializer;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientLifecycleEvents;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientTickEvents;
import net.fabricmc.fabric.api.client.networking.v1.ClientPlayNetworking;
import net.fabricmc.fabric.api.client.rendering.v1.WorldRenderEvents;
import net.fabricmc.fabric.api.networking.v1.PacketSender;
import net.minecraft.client.MinecraftClient;
import net.minecraft.client.gui.screen.ConnectScreen;
import net.minecraft.client.gui.screen.GameMenuScreen;
import net.minecraft.client.gui.screen.TitleScreen;
import net.minecraft.client.network.ClientPlayNetworkHandler;
import net.minecraft.client.network.ServerAddress;
import net.minecraft.client.network.ServerInfo;
import net.minecraft.client.option.KeyBinding;
import net.minecraft.client.util.Session;
import net.minecraft.network.PacketByteBuf;
import net.minecraft.util.Identifier;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.List;
import java.util.Optional;
import java.util.UUID;
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

    /**
     * 默认 API 模式（M7.2 需求：启动即屏蔽键鼠 + 失焦不弹菜单）。
     * 训练/演示场景无需真人操作，HUMAN_MODE 仅供人类演示采集（WS 切回）。
     */
    private volatile boolean apiMode = true;

    /** M7.1：切到 API 模式前的 pauseOnLostFocus 原值（退出时恢复）。 */
    private boolean savedPauseOnLostFocus = true;

    /** M8：最近一次经 vla:tick 插件消息收到的服务端权威 tick（网络线程写入、渲染线程读取）。
     *  -1 表示尚未收到任何广播（帧打标时回退 0）。 */
    private static volatile long lastServerTick = -1;

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
        registerTickChannel();
        registerTickHandler();
        registerFramePipeline();
        registerAutoJoin();

        // M7.2：启动即应用 API 模式 UI（不抓鼠标、失焦不弹菜单）——CLIENT_STARTED 在
        // 客户端线程触发，此时 MinecraftClient 已可用。
        ClientLifecycleEvents.CLIENT_STARTED.register(client -> applyApiModeUi(client, true));
    }

    /**
     * M8：接收服务端 vla:tick 插件消息（DESIGN.md §5.6），把权威 server_tick 写入
     * {@link #lastServerTick}，供帧打标（FrameGrabber）与 Python lockstep 对齐。
     *
     * <p>payload 12B（§5.6）：`[4B int serverTick BE][8B long wallNanos BE]`。
     * registerGlobalReceiver 同时把频道注册到客户端连接，服务端 sendPluginMessage 才可达
     * （1.20.1 签名 genSources/字节码核实：PlayChannelHandler#receive(client, handler, buf, sender)）。
     */
    private void registerTickChannel() {
        ClientPlayNetworking.registerGlobalReceiver(new Identifier("vla", "tick"),
                (MinecraftClient client, ClientPlayNetworkHandler handler,
                 PacketByteBuf buf, PacketSender responseSender) -> {
                    try {
                        int tick = buf.readInt();
                        buf.readLong(); // wallNanos（本地时钟无跨机漂移，暂不使用）
                        lastServerTick = tick;
                    } catch (Throwable t) {
                        // 防御：读失败（payload 长度异常/通道错位）只记日志，不让网络线程崩
                        // ——崩了会让 server 30s 后 kick（read timeout），表现为 collect_episodes
                        // 跑几个 episode 后 server 侧断连。
                        LOGGER.warn("[vla-client] vla:tick recv failed: {}", t.getMessage());
                    }
                });
    }

    private void startWsServer() {
        wsServer = new WsServer(WS_PORT, new WsServer.WsHandler() {
            @Override
            public void onModeChange(String mode) {
                boolean newApi = API_MODE.equals(mode);
                MinecraftClient client = MinecraftClient.getInstance();
                if (client != null) {
                    client.execute(() -> applyApiModeUi(client, newApi));
                }
                apiMode = newApi;
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

    /**
     * M3+M7：若 run/autojoin.txt 存在（两行：行1 host:port，行2 用户名），主菜单就绪后
     * 先用行2 覆盖 session 用户名（agent0 固定身份，Accessor Mixin），再编程式加入。
     */
    private void registerAutoJoin() {
        ClientLifecycleEvents.CLIENT_STARTED.register(client -> {
            String[] autoJoin = readAutoJoin(client);
            if (autoJoin == null) {
                return;
            }
            if (!autoJoinAttempted.compareAndSet(false, true)) {
                return;
            }

            String target = autoJoin[0];
            String username = autoJoin[1];
            if (username != null && !username.isEmpty()) {
                applySessionUsername(client, username);
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

    /**
     * M7：入服前用 Accessor 覆盖客户端 session 用户名（agent0 固定身份）。
     *
     * <p>UUID 用服务端离线映射 {@code UUID.nameUUIDFromBytes("OfflinePlayer:"+name)}
     * 保持一致；offline 模式（online-mode=false）下服务端不校验 accessToken。
     * Session 构造签名 genSources 核实（1.20.1 Yarn）：
     * {@code Session(String username, String uuid, String accessToken,
     * Optional<String> xuid, Optional<String> clientId, AccountType accountType)}。
     */
    private void applySessionUsername(MinecraftClient client, String username) {
        try {
            UUID uuid = UUID.nameUUIDFromBytes(
                    ("OfflinePlayer:" + username).getBytes(StandardCharsets.UTF_8));
            Session session = new Session(username, uuid.toString(), "token",
                    Optional.empty(), Optional.empty(), Session.AccountType.LEGACY);
            ((MinecraftClientAccessor) client).setSession(session);
            LOGGER.info("[vla-client] session username -> {} (offlineUuid={})", username, uuid);
        } catch (Exception e) {
            LOGGER.error("[vla-client] failed to override session username to " + username, e);
        }
    }

    /**
     * 读取 autojoin 配置（优先 client.runDirectory 下的 autojoin.txt）。
     * 返回 String[]{hostPort, username}；两行任一为空则对应字段为空串；无文件返回 null。
     */
    private String[] readAutoJoin(MinecraftClient client) {
        File[] candidates = {
                new File(client.runDirectory, "autojoin.txt"),
                new File("autojoin.txt"),
                new File("run/autojoin.txt"),
        };
        for (File file : candidates) {
            if (file.isFile()) {
                try {
                    List<String> lines = Files.readAllLines(file.toPath(), StandardCharsets.UTF_8);
                    if (lines.isEmpty()) {
                        return null;
                    }
                    String hostPort = lines.get(0).trim();
                    String username = lines.size() > 1 ? lines.get(1).trim() : "";
                    if (hostPort.isEmpty()) {
                        return null;
                    }
                    return new String[]{hostPort, username};
                } catch (IOException e) {
                    LOGGER.warn("[vla-client] failed to read autojoin.txt: {}", e.getMessage());
                    return null;
                }
            }
        }
        return null;
    }

    /**
     * 每 tick 末尾：先释放上一 tick 注入的按键（防粘键），再把最近动作注入。
     *
     * <p>M7.1（电平保持）：动作**不按 tick 消费**——`currentAction` 持续持有最近一次
     * WS 动作，直到被新动作替换。原因：env.step 跨多个游戏 tick（gRPC 阻塞 2 ticks +
     * 收帧 + Python 开销 ≈ 5-10 ticks），若每 tick 消费置空，forward/attack 占空比极低，
     * 玩家只能蠕动、挖掘进度永远攒不满。电平保持让"发一次动作 → 按住直到下一条替换"，
     * 匹配 VLA step 语义。一次性字段（camera 增量 / hotbar / drop / inventory）由
     * {@link ActionApplier#apply} 应用一次后清零，避免每 tick 重复触发。
     */
    private void registerTickHandler() {
        ClientTickEvents.END_CLIENT_TICK.register(client -> {
            // M7：API_MODE 下窗口失焦会自动打开暂停菜单（GameMenuScreen），它挡住
            // handleInputEvents → 挖掘失效。每 tick 强制关闭（M7.1 已置
            // pauseOnLostFocus=false 从源头防止，此为兜底）。
            if (apiMode && client.currentScreen instanceof GameMenuScreen) {
                client.setScreen(null);
            }
            if (!apiMode) {
                return; // HUMAN_MODE 透明
            }
            ActionApplier.resetKeys(client);
            ActionCmd cmd = currentAction.get();   // 电平保持：不消费
            if (cmd != null && client.player != null) {
                ActionApplier.apply(client, client.player, cmd);
            }
        });
    }

    /** M7.1：API 模式 UI——失焦不弹暂停菜单、释放鼠标捕获；退出恢复原 pauseOnLostFocus。 */
    private void applyApiModeUi(MinecraftClient client, boolean enable) {
        if (client == null || client.options == null) {
            return;
        }
        if (enable) {
            savedPauseOnLostFocus = client.options.pauseOnLostFocus;
            client.options.pauseOnLostFocus = false;
            if (client.mouse != null && client.mouse.isCursorLocked()) {
                client.mouse.unlockCursor();
            }
        } else {
            client.options.pauseOnLostFocus = savedPauseOnLostFocus;
        }
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

    /** M8：帧采集时读取最近已知服务端权威 tick（未收到任何 vla:tick 广播时为 -1）。 */
    public static long getLastServerTick() {
        return lastServerTick;
    }

    public static VlaClient getInstance() {
        return INSTANCE;
    }
}
