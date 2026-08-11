package dev.vla.client;

import com.google.gson.Gson;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import dev.vla.client.gfx.FrameGrabber;
import dev.vla.client.input.ActionApplier;
import dev.vla.client.input.ActionCmd;
import dev.vla.client.input.KeyRecorder;
import dev.vla.client.mixin.MinecraftClientAccessor;
import dev.vla.client.nav.Aim;
import dev.vla.client.nav.NavExecutor;
import dev.vla.client.nav.PillarExecutor;
import dev.vla.client.nav.ToolPolicy;
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
import net.minecraft.client.network.ClientPlayerEntity;
import net.minecraft.client.network.ServerAddress;
import net.minecraft.client.network.ServerInfo;
import net.minecraft.client.option.KeyBinding;
import net.minecraft.client.util.Session;
import net.minecraft.item.ItemStack;
import net.minecraft.network.PacketByteBuf;
import net.minecraft.registry.Registries;
import net.minecraft.util.hit.BlockHitResult;
import net.minecraft.util.math.BlockPos;
import net.minecraft.util.math.MathHelper;
import net.minecraft.util.math.Vec3d;
import net.minecraft.util.Identifier;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
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

    /**
     * M9.1：HUD 抓帧开关（默认 false）。false = WorldRenderEvents.LAST 抓纯净画面
     * （世界+实体渲染完、HUD 前，VLA 观测默认）；true = GameRenderer.render TAIL 抓
     * 含手+HUD+准星的完整画面（demo 录制需要完整 UI）。两个钩子互斥，同一时刻只有一个抓帧。
     */
    private volatile boolean captureUi = false;

    /** M9.1：平滑视角每 tick 最大转角（deg/tick，DESIGN.md §5.3；WS set_turn_speed 可覆盖）。
     *  M9.2：40→90——approach 转向不再逐点罚站（90°/tick = 90° 拐弯 1 tick 到位）。 */
    private static final double MAX_TURN_DEG = 90.0;
    private volatile double maxTurnDeg = MAX_TURN_DEG;

    /** M9.1：视角插值目标（look_at/reset_camera 只更新目标，END_CLIENT_TICK 每 tick 平滑转向）。 */
    private volatile double targetYaw = 0.0;
    private volatile double targetPitch = 0.0;
    private volatile boolean cameraTargetActive = false;

    /** M3：帧队列（渲染线程只入队，FrameSender 后台线程消费编码+上行）。 */
    private final ConcurrentLinkedQueue<FrameGrabber.FrameData> frameQueue = new ConcurrentLinkedQueue<>();

    private static final Gson GSON = new Gson();

    private WsServer wsServer;
    private FrameGrabber frameGrabber;
    private FrameSender frameSender;
    private final AtomicBoolean autoJoinAttempted = new AtomicBoolean(false);
    /** M9.3：本地路径跟随控制器（goto_path/goto_cancel 驱动；活跃时拥有移动按键）。 */
    private NavExecutor navExecutor;
    /** M11：垫方块爬高技能（pillar_up/pillar_cancel 驱动；活跃时优先于 navExecutor 拥有按键）。 */
    private PillarExecutor pillarExecutor;
    /** M11.5：人类化整形滤波器（只整形执行器输出；WS set_humanize 配置，§17.2）。 */
    private final dev.vla.client.nav.Humanizer humanizer = new dev.vla.client.nav.Humanizer();
    /** M11.6：视线工具策略（crosshair 命中 → 切工具；档位由 WS set_tool_mode 下发）。 */
    private final ToolPolicy toolPolicy = new ToolPolicy();

    /** M11：上一 tick 注入的动作副本（KeyRecorder.diff 产生按键 down/up 事件）。 */
    private ActionCmd lastInjectedAction = new ActionCmd();

    /**
     * 挖块计划：位置 + 规划器标注的方块/工具（规划器查体素后写入；
     * block/tool 可为 null → NavExecutor 按方块自动判断工具）。
     */
    public record DigPlan(BlockPos pos, String block, String tool) {}

    @Override
    public void onInitializeClient() {
        INSTANCE = this;
        LOGGER.info("[vla-client] VlaClient loaded (M3)");

        startWsServer();
        registerTickChannel();
        registerTickHandler();
        registerFramePipeline();
        registerAutoJoin();

        // M9.3 + M10：本地路径跟随控制器——状态经 WS 上行 goto_status，
        // 局部路径点经 WS 上行 path_debug（供 PathVisualizer 白色粒子可视化）。
        navExecutor = new NavExecutor(event -> {
            JsonObject json = new JsonObject();
            json.addProperty("type", "goto_status");
            json.addProperty("state", event.status().name().toLowerCase());
            json.add("pos", toJsonArray(event.pos()));
            json.add("wp", toJsonArray(event.wp()));
            json.addProperty("detail", event.detail());
            if (wsServer != null) {
                wsServer.sendText(json.toString());
            }
            currentAction.set(releaseLevels(currentAction.get()));
            LOGGER.info("[vla-client] goto_status {} wp={} pos={}", event.status(),
                    event.wp(), event.pos());
        }, localPath -> {
            // M10：客户端局部路径（LocalPathfinder）→ WS path_debug 上行
            JsonObject json = new JsonObject();
            json.addProperty("type", "path_debug");
            JsonArray arr = new JsonArray();
            for (BlockPos p : localPath) {
                JsonArray pt = new JsonArray();
                pt.add(p.getX());
                pt.add(p.getY());
                pt.add(p.getZ());
                arr.add(pt);
            }
            json.add("points", arr);
            if (wsServer != null) {
                wsServer.sendText(json.toString());
            }
            LOGGER.info("[vla-client] path_debug points={}", localPath.size());
        });

        // M11：垫方块爬高技能——进度/结束经 WS 上行 pillar_status。
        // FAILED 带 reason（head_blocked / no_block_item / in_fluid / place_failed ...），
        // Python 侧据此选择兜底（挖阶梯 / 换目标 / teleport）。
        pillarExecutor = new PillarExecutor(event -> {
            JsonObject json = new JsonObject();
            json.addProperty("type", "pillar_status");
            json.addProperty("state", event.status().name().toLowerCase());
            json.addProperty("placed", event.placed());
            json.addProperty("feet_y", event.feetY());
            json.addProperty("reason", event.reason() == null ? "" : event.reason());
            json.addProperty("detail", event.detail());
            if (wsServer != null) {
                wsServer.sendText(json.toString());
            }
            if (event.status() != PillarExecutor.Status.PROGRESS) {
                currentAction.set(new ActionCmd());   // 结束即释放按键
            }
            LOGGER.info("[vla-client] pillar_status {} placed={} feetY={} reason={}",
                    event.status(), event.placed(), event.feetY(), event.reason());
        });

        // M7.2：启动即应用 API 模式 UI（不抓鼠标、失焦不弹菜单）——CLIENT_STARTED 在
        // 客户端线程触发，此时 MinecraftClient 已可用。
        ClientLifecycleEvents.CLIENT_STARTED.register(client -> applyApiModeUi(client, true));
    }

    /** M9.3：把 BlockPos 转成 [x,y,z] 数组 JSON（null → 空数组）。 */
    private static JsonArray toJsonArray(BlockPos pos) {
        JsonArray arr = new JsonArray();
        if (pos != null) {
            arr.add(pos.getX());
            arr.add(pos.getY());
            arr.add(pos.getZ());
        }
        return arr;
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
                    // M9.1：不再瞬间 setYaw/setPitch（画面瞬移），只设置插值目标，
                    // 由 END_CLIENT_TICK 每 tick 平滑转向（DESIGN.md §5.3）。
                    client.execute(() -> {
                        if (client.player != null) {
                            setCameraTarget(yaw, pitch);
                        }
                    });
                }
            }

            @Override
            public void onSetCaptureUi(boolean hud) {
                LOGGER.info("[vla-client] WS set_capture_ui hud={}", hud);
                captureUi = hud;
            }

            @Override
            public void onSetTurnSpeed(double degPerTick) {
                LOGGER.info("[vla-client] WS set_turn_speed deg={}", degPerTick);
                maxTurnDeg = degPerTick;
            }

            @Override
            public void onGotoPath(List<int[]> waypoints, List<JsonObject> digTargets,
                    boolean moveOnly) {
                LOGGER.info("[vla-client] WS goto_path wps={} dig={} mode={}", waypoints.size(),
                        digTargets == null ? 0 : digTargets.size(),
                        moveOnly ? "move_only" : "default");
                MinecraftClient client = MinecraftClient.getInstance();
                if (client != null) {
                    client.execute(() -> {
                        List<BlockPos> poses = new ArrayList<>();
                        for (int[] w : waypoints) {
                            poses.add(new BlockPos(w[0], w[1], w[2]));
                        }
                        List<DigPlan> digs = new ArrayList<>();
                        if (digTargets != null) {
                            for (JsonObject d : digTargets) {
                                int x = d.get("x").getAsInt();
                                int y = d.get("y").getAsInt();
                                int z = d.get("z").getAsInt();
                                String block = d.has("block") && !d.get("block").isJsonNull()
                                        ? d.get("block").getAsString() : null;
                                String tool = d.has("tool") && !d.get("tool").isJsonNull()
                                        ? d.get("tool").getAsString() : null;
                                digs.add(new DigPlan(new BlockPos(x, y, z), block, tool));
                            }
                        }
                        navExecutor.setPath(poses, digs, moveOnly);
                    });
                }
            }

            @Override
            public void onGotoCancel() {
                LOGGER.info("[vla-client] WS goto_cancel");
                MinecraftClient client = MinecraftClient.getInstance();
                if (client != null) {
                    client.execute(() -> {
                        navExecutor.cancel();
                        // 释放移动按键但保留未消费的 hotbar/camera（防吞选槽，见 releaseLevels）
                        currentAction.set(releaseLevels(currentAction.get()));
                    });
                }
            }

            @Override
            public void onPillarUp(int targetY, int maxBlocks, String item) {
                LOGGER.info("[vla-client] WS pillar_up target_y={} max_blocks={} item={}",
                        targetY, maxBlocks, item);
                MinecraftClient client = MinecraftClient.getInstance();
                if (client != null) {
                    client.execute(() -> {
                        // 垫方块要求水平速度≈0，与导航的持续 forward 互斥 → 先停导航
                        navExecutor.cancel();
                        currentAction.set(releaseLevels(currentAction.get()));
                        pillarExecutor.start(targetY, maxBlocks, item);
                    });
                }
            }

            @Override
            public void onPillarCancel() {
                LOGGER.info("[vla-client] WS pillar_cancel");
                MinecraftClient client = MinecraftClient.getInstance();
                if (client != null) {
                    client.execute(() -> {
                        pillarExecutor.cancel();
                        currentAction.set(releaseLevels(currentAction.get()));
                    });
                }
            }

            @Override
            public void onSetKeyLog(boolean enabled) {
                LOGGER.info("[vla-client] WS set_key_log enabled={}", enabled);
                KeyRecorder.setEnabled(enabled);
            }

            @Override
            public void onSetHumanize(boolean enabled, long seed) {
                LOGGER.info("[vla-client] WS set_humanize enabled={} seed={}", enabled, seed);
                MinecraftClient client = MinecraftClient.getInstance();
                if (client != null) {
                    client.execute(() -> humanizer.configure(enabled, seed));
                }
            }

            @Override
            public void onSetToolMode(String mode) {
                LOGGER.info("[vla-client] WS set_tool_mode mode={}", mode);
                MinecraftClient client = MinecraftClient.getInstance();
                if (client != null) {
                    client.execute(() -> toolPolicy.setMode(mode));
                }
            }

            @Override
            public JsonObject onStateRequest() {
                return buildStateJson();
            }

            @Override
            public void onSetCapture(int width, int height) {
                LOGGER.info("[vla-client] WS set_capture {}x{} (0=原生)", width, height);
                MinecraftClient client = MinecraftClient.getInstance();
                if (client != null) {
                    // FrameGrabber 的 GL 操作必须在渲染线程执行
                    client.execute(() -> {
                        if (frameGrabber != null) {
                            frameGrabber.setResolution(width, height);
                        }
                    });
                }
            }

            @Override
            public void onLookAt(double x, double y, double z) {
                onLookAt(x, y, z, 0.0);
            }

            @Override
            public void onLookAt(double x, double y, double z, double pitchClamp) {
                MinecraftClient client = MinecraftClient.getInstance();
                if (client != null) {
                    // 用客户端自身眼位算精确朝向（消除服务端 pos 滞后的瞄准偏差）
                    client.execute(() -> {
                        if (client.player == null) {
                            return;
                        }
                        Vec3d eye = client.player.getEyePos();
                        // M9.1：只更新插值目标（平滑转向），由 END_CLIENT_TICK 收敛；目标值精确 → 收敛后零误差
                        // M9.2：pitchClamp>0 时夹紧 |pitch|（approach 瞄航点格中心但保持平视，不低头）
                        // M11：pitch 经 Aim 计算——目标与玩家同一列（正上/正下）时给 ∓90 而不是
                        //      退化成平视（老代码 h≈0 → 0.0，「挖头顶/瞄脚下」永远瞄不中）。
                        double pitch = Aim.pitch(eye.x, eye.y, eye.z, x, y, z);
                        if (pitchClamp > 0) {
                            pitch = MathHelper.clamp(pitch, -pitchClamp, pitchClamp);
                        }
                        setCameraTarget(Aim.yaw(eye.x, eye.z, x, z), pitch);
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
                // WS 断开后清空电平动作；否则最后一次 attack 会持续到下一次连接。
                // M9.3：同时取消本地导航，防止玩家继续自动走。
                if (navExecutor != null) {
                    navExecutor.cancel();
                }
                if (pillarExecutor != null) {
                    pillarExecutor.cancel();
                }
                currentAction.set(null);
                unpressAllKeys();
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

        // 世界+实体渲染完后、HUD 前（画面纯净，无准星/血条污染像素观测）。
        // M9.1：captureUi=true 时改由 GameRendererMixin（GameRenderer.render TAIL）抓
        // 含 HUD 的完整画面，此处加守卫避免两个钩子同时抓帧。
        WorldRenderEvents.LAST.register(ctx -> {
            if (!VlaClient.isCaptureUi()) {
                frameGrabber.capture();
            }
        });
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
            if (client.player == null) {
                return;
            }
            if (pillarExecutor.isActive()) {
                // M11：垫方块技能优先于导航拥有按键——它要求水平速度≈0，
                // 与 NavExecutor 的持续 forward 互斥。放置窗口只有跳跃第 3-8 tick，
                // **不做人类化整形**（§17.2：整形会打断技能时序）。
                ActionCmd pillarCmd = pillarExecutor.tick(client.player);
                currentAction.set(pillarCmd != null ? pillarCmd
                        : releaseLevels(currentAction.get()));
            } else if (navExecutor.isActive()) {
                // M11.5：导航输出经 Humanizer 整形（步态微松/挖掘节奏），实际注入并被
                // 帧头按键采样记录的按键呈现人类节奏。外部 VLA 直发 action 不整形。
                ActionCmd navCmd = humanizer.shape(navExecutor.tick(client.player));
                if (navCmd != null) {
                    currentAction.set(navCmd);
                } else {
                    currentAction.set(releaseLevels(currentAction.get()));
                }
            }
            // 先收敛视角，再注入 attack/use。按键会驱动下一游戏 tick 的原版逻辑，
            // 若先注入再转向，攻击会用上一 tick 的准星方向，表现为"瞄准了但不出剑/打偏"。
            interpolateCamera(client.player);
            ActionCmd cmd = currentAction.get();   // 电平保持：不消费
            // M11.5：镜头微漂（人类手抖）——视角插值之后、挖掘瞄准期除外；
            // 帧头 yaw/pitch delta 会如实记录这份抖动。
            humanizer.cameraDrift(client.player,
                    (pillarExecutor != null && pillarExecutor.isActive())
                            || (cmd != null && cmd.attack));
            // M11.6：视线工具策略——crosshair 命中 → 切工具（auto/melee/none）。
            // busy（挖穿/放置子模式或 pillar）时跳过，由技能自己选槽；切槽发生在
            // 动作注入之前，保证本 tick 的 look_at+attack 用上新工具。
            toolPolicy.apply(client, client.player,
                    (pillarExecutor != null && pillarExecutor.isActive())
                            || (navExecutor != null && navExecutor.isBusy()));
            if (cmd != null) {
                // M11：diff 注入动作 → key_event 上行（按下/抬起事件，帧↔按键对齐补充）。
                // 必须先 diff 再 copy（apply 会把一次性字段 hotbar/drop/inventory 清零）。
                KeyRecorder.diff(lastInjectedAction, cmd);
                lastInjectedAction = cmd.copy();
                ActionApplier.apply(client, client.player, cmd);
                if (cmd.attack) {
                    ((MinecraftClientAccessor) (Object) client).invokeDoAttack();
                }
            }
            // M11：排空按键事件（含 HUMAN 模式 mixin 记录的），经 WS 文本上行
            KeyRecorder.drainTo(text -> {
                if (wsServer != null) {
                    wsServer.sendText(text);
                }
            });
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

    /**
     * 释放电平按键但**保留未消费的一次性字段**（hotbar/camera/drop/inventory，M11.5）。
     *
     * <p>修「杀猪不持剑」竞争：Python 发 goto_cancel 后紧跟 action{hotbar=剑}——
     * cancel 的清空任务经 client.execute 调度，晚于 WS 线程写入的 hotbar 动作执行，
     * 老代码 `currentAction.set(new ActionCmd())` 把选槽整个吞掉（实测 kill 攻击帧
     * 大多持铲/镐）。释放电平、保留 one-shot 即无此竞争。
     */
    private static ActionCmd releaseLevels(ActionCmd cur) {
        ActionCmd a = cur != null ? cur.copy() : new ActionCmd();
        a.forward = false;
        a.back = false;
        a.left = false;
        a.right = false;
        a.jump = false;
        a.sneak = false;
        a.sprint = false;
        a.attack = false;
        a.use = false;
        return a;
    }

    /** M9.1：设置视角插值目标（客户端线程调用）；pitch 夹紧 ±90（原版范围）。 */
    public void setCameraTarget(double yaw, double pitch) {
        targetYaw = yaw;
        targetPitch = MathHelper.clamp(pitch, -90.0, 90.0);
        cameraTargetActive = true;
    }

    /**
     * M9.1：每 tick 向视角目标插值（DESIGN.md §5.3 平滑转向，消除 setYaw/setPitch 瞬移闪现）。
     *
     * <p>yaw 走最短角差（{@link MathHelper#wrapDegrees(double)} 归一化到 [-180,180]），
     * 每 tick 限幅 maxTurnDeg（默认 40.0，WS set_turn_speed 可覆盖）；pitch 同样限幅且
     * 夹紧 ±90。误差 <0.05° 时直接置目标并停用插值 —— 瞄准目标固定后收敛即稳定不动
     * （挖矿期准星稳定），新 look_at/reset_camera 会重新激活。
     */
    private void interpolateCamera(ClientPlayerEntity player) {
        if (!cameraTargetActive) {
            return;
        }
        double curYaw = player.getYaw();
        double curPitch = player.getPitch();
        double dYaw = MathHelper.wrapDegrees(targetYaw - curYaw);
        double dPitch = targetPitch - curPitch;
        if (Math.abs(dYaw) < 0.05 && Math.abs(dPitch) < 0.05) {
            player.setYaw((float) targetYaw);
            player.setPitch((float) targetPitch);
            cameraTargetActive = false; // 收敛完成，不再动
            return;
        }
        double step = maxTurnDeg;
        double stepYaw = MathHelper.clamp(dYaw, -step, step);
        double stepPitch = MathHelper.clamp(dPitch, -step, step);
        player.setYaw((float) (curYaw + stepYaw));
        player.setPitch((float) MathHelper.clamp(curPitch + stepPitch, -90.0, 90.0));
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

    /** M11：WS state 上行（docs/p1_protocol.md §2.4）——aimed_block/held_item/fps/selected_slot。 */
    private JsonObject buildStateJson() {
        JsonObject j = new JsonObject();
        j.addProperty("type", "state");
        j.addProperty("frame_id", frameGrabber != null ? frameGrabber.getLastFrameId() : -1);
        j.addProperty("last_server_tick", lastServerTick);
        MinecraftClient client = MinecraftClient.getInstance();
        if (client == null) {
            return j;
        }
        j.addProperty("fps", client.getCurrentFps());
        if (client.player != null) {
            j.addProperty("selected_slot", client.player.getInventory().selectedSlot);
            ItemStack stack = client.player.getMainHandStack();
            j.addProperty("held_item", stack.isEmpty()
                    ? "" : Registries.ITEM.getId(stack.getItem()).toString());
        } else {
            j.addProperty("selected_slot", -1);
            j.addProperty("held_item", "");
        }
        // 准星瞄准方块（crosshairTarget 射线命中）
        if (client.crosshairTarget instanceof BlockHitResult bhr) {
            BlockPos p = bhr.getBlockPos();
            j.addProperty("aimed_block_x", p.getX());
            j.addProperty("aimed_block_y", p.getY());
            j.addProperty("aimed_block_z", p.getZ());
            if (client.player != null) {
                j.addProperty("aimed_block_distance",
                        client.player.getEyePos().distanceTo(bhr.getPos()));
            }
        }
        // M11.5：准星瞄准实体（近战出剑门控——编排器只在准星实际套住目标实体时才
        // 挥击，否则乱挥剑/剑砍到猪身前的方块）
        if (client.crosshairTarget instanceof net.minecraft.util.hit.EntityHitResult ehr
                && client.player != null) {
            j.addProperty("aimed_entity", Registries.ENTITY_TYPE.getId(
                    ehr.getEntity().getType()).toString());
            j.addProperty("aimed_entity_dist",
                    ehr.getEntity().distanceTo(client.player));
        }
        return j;
    }

    public static boolean isApiMode() {
        return INSTANCE != null && INSTANCE.apiMode;
    }

    /** M9.1：HUD 抓帧开关（WorldRenderEvents.LAST 守卫 / GameRendererMixin 判断用）。 */
    public static boolean isCaptureUi() {
        return INSTANCE != null && INSTANCE.captureUi;
    }

    /** M9.1：HUD 抓帧入口（GameRendererMixin 的 GameRenderer.render TAIL 调用）；
     * 仅 captureUi=true 时抓帧（含手+HUD+准星的完整画面）。渲染线程调用。 */
    public static void captureFrameIfUi() {
        if (INSTANCE != null && INSTANCE.captureUi && INSTANCE.frameGrabber != null) {
            INSTANCE.frameGrabber.capture();
        }
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
