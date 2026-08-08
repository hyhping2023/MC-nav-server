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
 * - {"cmd":"set_turn_speed","deg":40.0} → 回调 onSetTurnSpeed + {"type":"turn_speed_ok","deg":...}
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

        /** M9.3：收到本地导航航点（有序方块坐标 [x,y,z]，来自服务端 A* 的 walk/jump/fall 位置）。 */
        default void onGotoPath(List<int[]> waypoints) {
        }

        /** M9.3：取消本地导航。 */
        default void onGotoCancel() {
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
                handler.onGotoPath(wps);
                JsonObject ok = new JsonObject();
                ok.addProperty("type", "goto_ok");
                ok.addProperty("waypoints", wps.size());
                conn.send(GSON.toJson(ok));
            }
            case "goto_cancel" -> {
                handler.onGotoCancel();
                JsonObject ok = new JsonObject();
                ok.addProperty("type", "goto_cancel_ok");
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
