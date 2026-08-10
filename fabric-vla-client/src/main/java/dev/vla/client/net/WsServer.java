package dev.vla.client.net;

import com.google.gson.Gson;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.google.gson.JsonParseException;
import com.google.gson.JsonParser;
import dev.vla.client.input.ActionCmd;
import org.java_websocket.WebSocket;
import org.java_websocket.handshake.ClientHandshake;
import org.java_websocket.server.WebSocketServer;

import java.net.InetSocketAddress;
import java.nio.ByteBuffer;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * VLA 客户端内嵌 WS 服务器（M1 通信底座，P1.1）。
 *
 * 纯 Java，不依赖任何 Minecraft 类，可独立运行测试（见 main）。
 *
 * 协议（P→C 下行 / C→P 上行，见 docs/p1_protocol.md）：
 * - ping → {"type":"pong","ts":<epoch_ms>,"api_mode":<bool>}
 * - {"cmd":"mode","mode":"api"|"human"} → 回调 onModeChange + {"type":"mode_ok","mode":...}
 * - {"cmd":"set_capture_ui","hud":true|false} → 回调 onSetCaptureUi + {"type":"capture_ui_ok","hud":...}
 * - {@code {"cmd":"set_turn_speed","deg":40.0}} → 回调 onSetTurnSpeed + {"type":"turn_speed_ok","deg":...}
 * - {@code {"cmd":"pillar_up","target_y":72,"max_blocks":8,"item":"minecraft:dirt"}}
 *   → 回调 onPillarUp + {"type":"pillar_ok",...}；进度/结束经 pillar_status 上行
 * - {@code {"cmd":"pillar_cancel"}} → 回调 onPillarCancel + {"type":"pillar_cancel_ok"}
 * - {@code {"cmd":"set_humanize","enabled":true,"seed":42}} → 回调 onSetHumanize + {"type":"humanize_ok",...}
 * - {@code {"cmd":"set_tool_mode","mode":"auto"|"melee"|"none"}} → 回调 onSetToolMode + {"type":"tool_mode_ok",...}
 * - {"cmd":"disconnect"} → {"type":"bye"} + 关闭会话
 * - 未知 cmd / 非法 JSON / 非法 mode → {"type":"error","message":...}
 */
public class WsServer extends WebSocketServer {

    /** 与游戏侧解耦的回调接口。 */
    public interface WsHandler {
        void onModeChange(String mode);

        void onConnect(String session);

        void onDisconnect(String session);

        /** M2：收到原始动作指令（WsServer 不解析 MC 相关语义，仅回调）。 */
        default void onAction(ActionCmd cmd) {
        }

        /** M2：收到视角重置指令。 */
        default void onResetCamera(float yaw, float pitch) {
        }

        /** M7.2：运行时切换抓帧分辨率（0 = 原生 framebuffer 分辨率，保留游戏原始比例）。 */
        default void onSetCapture(int width, int height) {
        }

        /** M7.3：客户端用自身眼位计算到世界坐标的精确朝向（消除服务端 pos 滞后的瞄准偏差）。 */
        default void onLookAt(double x, double y, double z) {
        }

        /** M9.2：同 onLookAt，pitchClamp>0 时夹紧 |pitch|（approach 平视前进，不低头看脚下）。 */
        default void onLookAt(double x, double y, double z, double pitchClamp) {
            onLookAt(x, y, z);
        }

        /** M9.1：切换 HUD 抓帧（true = GameRenderer.render TAIL 抓含 HUD/手/准星的完整画面；false = 默认纯净画面）。 */
        default void onSetCaptureUi(boolean hud) {
        }

        /** M9.1：覆盖平滑视角每 tick 最大转角（deg/tick，默认 40.0）。 */
        default void onSetTurnSpeed(double degPerTick) {
        }

        /** M9.3：收到本地导航航点（有序方块坐标 [x,y,z]，来自服务端 A* 的 walk/jump/fall 位置）。
         *  digTargets=计划要挖的方块（客户端只挖这些，杜绝乱挖掘）。每元素 JsonObject：
         *  {@code {x,y,z}} 或 {@code {x,y,z,block:"minecraft:stone",tool:"diamond_pickaxe"}}
         *  ——block/tool 由规划器标注，客户端据此切工具（老格式无 tool → 客户端按方块自动判断）。 */
        default void onGotoPath(List<int[]> waypoints, List<JsonObject> digTargets) {
        }

        /** M9.3：取消本地导航。 */
        default void onGotoCancel() {
        }

        /** M11：启动客户端垫方块爬高技能（pillar-up）。
         *  targetY=目标脚格 Y（{@link Integer#MIN_VALUE} = 只按 maxBlocks 停）；
         *  item=垫块材料注册名（null = 任意可放置方块）。 */
        default void onPillarUp(int targetY, int maxBlocks, String item) {
        }

        /** M11：取消垫方块爬高。 */
        default void onPillarCancel() {
        }

        /** M11：WS `state` 请求 → 状态 JSON（frame_id/aimed_block/held_item/fps/selected_slot）；
         *  null = 不可用（VlaClient 构造，含 crosshairTarget 射线）。 */
        default JsonObject onStateRequest() {
            return null;
        }

        /** M11：开关 key_event 按键事件上行（人类演示录制）。 */
        default void onSetKeyLog(boolean enabled) {
        }

        /** M11.5：开关执行器输出的人类化整形（步态微松/挖掘节奏/镜头微漂）；同 seed 可复现。 */
        default void onSetHumanize(boolean enabled, long seed) {
        }

        /** M11.6：视线工具策略档位（auto/melee/none），由编排器按任务下发（kill→melee、dig→auto、place→none）。 */
        default void onSetToolMode(String mode) {
        }
    }

    public static final String API_MODE = "api";
    public static final String HUMAN_MODE = "human";
    public static final int DEFAULT_PORT = 30001;

    private static final Gson GSON = new Gson();

    private final WsHandler handler;
    private final Map<WebSocket, String> sessions = new ConcurrentHashMap<>();
    private volatile boolean apiMode = false;

    public WsServer(int port, WsHandler handler) {
        super(new InetSocketAddress(port));
        this.handler = handler != null ? handler : new NoopHandler();
    }

    public WsServer(int port) {
        this(port, null);
    }

    public WsServer() {
        this(DEFAULT_PORT, null);
    }

    /** 当前模式是否为 API 模式（pong 上报用）。 */
    public boolean isApiMode() {
        return apiMode;
    }

    /** M3：是否存在连接中的会话（有会话才发帧，无会话丢弃）。 */
    public boolean hasSession() {
        return !sessions.isEmpty();
    }

    /** M3：二进制帧上行（DESIGN.md §9.2），发给所有连接中的会话。
     *
     * 每会话用 {@code slice()} 独立 ByteBuffer 视图，避免多会话共享 position。 */
    public void sendBinary(ByteBuffer data) {
        for (WebSocket conn : sessions.keySet()) {
            if (conn.isOpen()) {
                conn.send(data.slice());
            }
        }
    }

    /** M9.3：文本消息上行（goto_status 事件），发给所有连接中的会话。 */
    public void sendText(String text) {
        for (WebSocket conn : sessions.keySet()) {
            if (conn.isOpen()) {
                conn.send(text);
            }
        }
    }

    @Override
    public void onStart() {
        // 本地回环控制通道：禁用心跳超时，避免长连接被自动清理
        setConnectionLostTimeout(0);
    }

    @Override
    public void onOpen(WebSocket conn, ClientHandshake handshake) {
        String session = conn.getRemoteSocketAddress().toString();
        sessions.put(conn, session);
        handler.onConnect(session);
    }

    @Override
    public void onClose(WebSocket conn, int code, String reason, boolean remote) {
        String session = sessions.remove(conn);
        if (session != null) {
            handler.onDisconnect(session);
        }
    }

    @Override
    public void onMessage(WebSocket conn, String message) {
        JsonObject obj;
        try {
            obj = JsonParser.parseString(message).getAsJsonObject();
        } catch (JsonParseException | IllegalStateException e) {
            sendError(conn, "invalid json: " + e.getMessage());
            return;
        }

        String cmd = obj.has("cmd") ? obj.get("cmd").getAsString() : null;
        if (cmd == null) {
            sendError(conn, "missing cmd");
            return;
        }

        switch (cmd) {
            case "ping" -> {
                JsonObject pong = new JsonObject();
                pong.addProperty("type", "pong");
                pong.addProperty("ts", System.currentTimeMillis());
                pong.addProperty("api_mode", apiMode);
                conn.send(GSON.toJson(pong));
            }
            case "mode" -> {
                String mode = obj.has("mode") ? obj.get("mode").getAsString() : null;
                if (!API_MODE.equals(mode) && !HUMAN_MODE.equals(mode)) {
                    sendError(conn, "invalid mode: " + mode);
                    break;
                }
                apiMode = API_MODE.equals(mode);
                handler.onModeChange(mode);
                JsonObject ok = new JsonObject();
                ok.addProperty("type", "mode_ok");
                ok.addProperty("mode", mode);
                conn.send(GSON.toJson(ok));
            }
            case "disconnect" -> {
                JsonObject bye = new JsonObject();
                bye.addProperty("type", "bye");
                conn.send(GSON.toJson(bye));
                conn.close();
            }
            case "action" -> {
                ActionCmd actionCmd;
                try {
                    actionCmd = ActionCmd.fromJson(obj);
                } catch (Exception e) {
                    sendError(conn, "invalid action: " + e.getMessage());
                    break;
                }
                handler.onAction(actionCmd);
                JsonObject ok = new JsonObject();
                ok.addProperty("type", "action_ok");
                ok.add("action", actionToJson(actionCmd)); // 回显解析字段，便于验证
                conn.send(GSON.toJson(ok));
            }
            case "reset_camera" -> {
                float yaw = obj.has("yaw") ? obj.get("yaw").getAsFloat() : 0.0f;
                float pitch = obj.has("pitch") ? obj.get("pitch").getAsFloat() : 0.0f;
                handler.onResetCamera(yaw, pitch);
                JsonObject ok = new JsonObject();
                ok.addProperty("type", "camera_ok");
                conn.send(GSON.toJson(ok));
            }
            case "set_capture" -> {
                // 0 = 原生 framebuffer 分辨率（保留游戏原始比例）；否则显式 WxH
                int w = obj.has("width") ? obj.get("width").getAsInt() : 0;
                int h = obj.has("height") ? obj.get("height").getAsInt() : 0;
                handler.onSetCapture(w, h);
                JsonObject ok = new JsonObject();
                ok.addProperty("type", "capture_ok");
                ok.addProperty("width", w);
                ok.addProperty("height", h);
                conn.send(GSON.toJson(ok));
            }
            case "look_at" -> {
                double x = obj.has("x") ? obj.get("x").getAsDouble() : 0.0;
                double y = obj.has("y") ? obj.get("y").getAsDouble() : 0.0;
                double z = obj.has("z") ? obj.get("z").getAsDouble() : 0.0;
                double pitchClamp = obj.has("pitch_clamp")
                        ? obj.get("pitch_clamp").getAsDouble() : 0.0;
                if (pitchClamp > 0) {
                    handler.onLookAt(x, y, z, pitchClamp);
                } else {
                    handler.onLookAt(x, y, z);
                }
                JsonObject ok = new JsonObject();
                ok.addProperty("type", "look_ok");
                ok.addProperty("x", x);
                ok.addProperty("y", y);
                ok.addProperty("z", z);
                conn.send(GSON.toJson(ok));
            }
            case "set_capture_ui" -> {
                boolean hud = obj.has("hud") && obj.get("hud").getAsBoolean();
                handler.onSetCaptureUi(hud);
                JsonObject ok = new JsonObject();
                ok.addProperty("type", "capture_ui_ok");
                ok.addProperty("hud", hud);
                conn.send(GSON.toJson(ok));
            }
            case "set_turn_speed" -> {
                double deg = obj.has("deg") ? obj.get("deg").getAsDouble() : 40.0;
                handler.onSetTurnSpeed(deg);
                JsonObject ok = new JsonObject();
                ok.addProperty("type", "turn_speed_ok");
                ok.addProperty("deg", deg);
                conn.send(GSON.toJson(ok));
            }
            case "goto_path" -> {
                List<int[]> wps = new ArrayList<>();
                if (obj.has("waypoints") && obj.get("waypoints").isJsonArray()) {
                    for (com.google.gson.JsonElement e : obj.getAsJsonArray("waypoints")) {
                        JsonArray pt = e.getAsJsonArray();
                        if (pt.size() >= 3) {
                            wps.add(new int[]{
                                    (int) pt.get(0).getAsDouble(),
                                    (int) pt.get(1).getAsDouble(),
                                    (int) pt.get(2).getAsDouble()});
                        }
                    }
                }
                // 可选 dig 列表：服务端/规划器计划要挖的方块（客户端只挖这些）。
                // 元素兼容两种格式：
                //   - 数组 [x, y, z]（老格式，无工具信息 → 客户端按方块自动判断工具）
                //   - 对象 {"x":..,"y":..,"z":..,"block":"minecraft:stone","tool":"diamond_pickaxe"}
                List<JsonObject> digs = new ArrayList<>();
                if (obj.has("dig") && obj.get("dig").isJsonArray()) {
                    for (com.google.gson.JsonElement e : obj.getAsJsonArray("dig")) {
                        if (e.isJsonObject()) {
                            digs.add(e.getAsJsonObject());
                        } else if (e.isJsonArray() && e.getAsJsonArray().size() >= 3) {
                            JsonArray pt = e.getAsJsonArray();
                            JsonObject o = new JsonObject();
                            o.addProperty("x", pt.get(0).getAsInt());
                            o.addProperty("y", pt.get(1).getAsInt());
                            o.addProperty("z", pt.get(2).getAsInt());
                            digs.add(o);
                        }
                    }
                }
                handler.onGotoPath(wps, digs);
                JsonObject ok = new JsonObject();
                ok.addProperty("type", "goto_ok");
                ok.addProperty("waypoints", wps.size());
                ok.addProperty("dig", digs.size());
                conn.send(GSON.toJson(ok));
            }
            case "goto_cancel" -> {
                handler.onGotoCancel();
                JsonObject ok = new JsonObject();
                ok.addProperty("type", "goto_cancel_ok");
                conn.send(GSON.toJson(ok));
            }
            case "pillar_up" -> {
                // 垫方块爬高：挖头顶 → 朝正下 → 跳 → 顶点放块 → 落地 → 循环。
                // target_y 缺省 = 不按高度停（只受 max_blocks 约束）。
                int targetY = obj.has("target_y") && !obj.get("target_y").isJsonNull()
                        ? obj.get("target_y").getAsInt() : Integer.MIN_VALUE;
                int maxBlocks = obj.has("max_blocks") ? obj.get("max_blocks").getAsInt() : 8;
                String item = obj.has("item") && !obj.get("item").isJsonNull()
                        ? obj.get("item").getAsString() : null;
                handler.onPillarUp(targetY, maxBlocks, item);
                JsonObject ok = new JsonObject();
                ok.addProperty("type", "pillar_ok");
                ok.addProperty("target_y", targetY);
                ok.addProperty("max_blocks", maxBlocks);
                ok.addProperty("item", item == null ? "" : item);
                conn.send(GSON.toJson(ok));
            }
            case "pillar_cancel" -> {
                handler.onPillarCancel();
                JsonObject ok = new JsonObject();
                ok.addProperty("type", "pillar_cancel_ok");
                conn.send(GSON.toJson(ok));
            }
            case "state" -> {
                JsonObject st = handler.onStateRequest();
                if (st == null) {
                    sendError(conn, "state unavailable");
                    break;
                }
                conn.send(GSON.toJson(st));
            }
            case "set_key_log" -> {
                boolean on = obj.has("enabled") && obj.get("enabled").getAsBoolean();
                handler.onSetKeyLog(on);
                JsonObject ok = new JsonObject();
                ok.addProperty("type", "key_log_ok");
                ok.addProperty("enabled", on);
                conn.send(GSON.toJson(ok));
            }
            case "set_humanize" -> {
                boolean on = obj.has("enabled") && obj.get("enabled").getAsBoolean();
                long seed = obj.has("seed") ? obj.get("seed").getAsLong() : 0L;
                handler.onSetHumanize(on, seed);
                JsonObject ok = new JsonObject();
                ok.addProperty("type", "humanize_ok");
                ok.addProperty("enabled", on);
                ok.addProperty("seed", seed);
                conn.send(GSON.toJson(ok));
            }
            case "set_tool_mode" -> {
                String mode = obj.has("mode") ? obj.get("mode").getAsString() : "auto";
                handler.onSetToolMode(mode);
                JsonObject ok = new JsonObject();
                ok.addProperty("type", "tool_mode_ok");
                ok.addProperty("mode", mode);
                conn.send(GSON.toJson(ok));
            }
            default -> sendError(conn, "unknown cmd: " + cmd);
        }
    }

    @Override
    public void onError(WebSocket conn, Exception ex) {
        System.err.println("[WsServer] error: " + ex);
    }

    private void sendError(WebSocket conn, String message) {
        JsonObject err = new JsonObject();
        err.addProperty("type", "error");
        err.addProperty("message", message);
        conn.send(GSON.toJson(err));
    }

    /** 把解析出的动作回显为 JSON（action_ok 附带，供 harness 断言解析结果）。 */
    private static JsonObject actionToJson(ActionCmd cmd) {
        JsonObject j = new JsonObject();
        j.addProperty("forward", cmd.forward);
        j.addProperty("back", cmd.back);
        j.addProperty("left", cmd.left);
        j.addProperty("right", cmd.right);
        j.addProperty("jump", cmd.jump);
        j.addProperty("sneak", cmd.sneak);
        j.addProperty("sprint", cmd.sprint);
        j.addProperty("attack", cmd.attack);
        j.addProperty("use", cmd.use);
        j.addProperty("drop", cmd.drop);
        j.addProperty("inventory", cmd.inventory);
        j.addProperty("hotbar", cmd.hotbar);
        JsonArray cam = new JsonArray();
        cam.add(cmd.camera[0]);
        cam.add(cmd.camera[1]);
        j.add("camera", cam);
        return j;
    }

    /**
     * 独立测试入口：`java -cp ... dev.vla.client.net.WsServer [port]`。
     * 启动后打印 {@code WS_HARNESS_READY port=<port>} 并保持运行；
     * onAction/onResetCamera 打印解析结果，供 harness 验证。
     */
    public static void main(String[] args) throws Exception {
        int port = args.length > 0 ? Integer.parseInt(args[0]) : DEFAULT_PORT;
        WsServer server = new WsServer(port, new WsHandler() {
            @Override
            public void onModeChange(String mode) {
            }

            @Override
            public void onConnect(String session) {
            }

            @Override
            public void onDisconnect(String session) {
            }

            @Override
            public void onAction(ActionCmd cmd) {
                System.out.println("ACTION parsed: forward=" + cmd.forward + " back=" + cmd.back
                        + " left=" + cmd.left + " right=" + cmd.right + " jump=" + cmd.jump
                        + " sneak=" + cmd.sneak + " sprint=" + cmd.sprint + " attack=" + cmd.attack
                        + " use=" + cmd.use + " drop=" + cmd.drop + " inventory=" + cmd.inventory
                        + " hotbar=" + cmd.hotbar + " camera=[" + cmd.camera[0] + "," + cmd.camera[1] + "]");
                System.out.flush();
            }

            @Override
            public void onResetCamera(float yaw, float pitch) {
                System.out.println("RESET_CAMERA yaw=" + yaw + " pitch=" + pitch);
                System.out.flush();
            }
        });
        server.start();
        System.out.println("WS_HARNESS_READY port=" + port);
        System.out.flush();
        Thread.currentThread().join(); // 保持运行
    }

    private static final class NoopHandler implements WsHandler {
        @Override
        public void onModeChange(String mode) {
        }

        @Override
        public void onConnect(String session) {
        }

        @Override
        public void onDisconnect(String session) {
        }
    }
}
