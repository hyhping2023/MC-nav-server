package dev.vla.client.net;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import com.google.gson.JsonParseException;
import com.google.gson.JsonParser;
import org.java_websocket.WebSocket;
import org.java_websocket.handshake.ClientHandshake;
import org.java_websocket.server.WebSocketServer;

import java.net.InetSocketAddress;
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
 * - {"cmd":"disconnect"} → {"type":"bye"} + 关闭会话
 * - 未知 cmd / 非法 JSON / 非法 mode → {"type":"error","message":...}
 */
public class WsServer extends WebSocketServer {

    /** 与游戏侧解耦的回调接口。 */
    public interface WsHandler {
        void onModeChange(String mode);

        void onConnect(String session);

        void onDisconnect(String session);
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

    /**
     * 独立测试入口：`java -cp ... dev.vla.client.net.WsServer [port]`。
     * 启动后打印 {@code WS_HARNESS_READY port=<port>} 并保持运行。
     */
    public static void main(String[] args) throws Exception {
        int port = args.length > 0 ? Integer.parseInt(args[0]) : DEFAULT_PORT;
        WsServer server = new WsServer(port);
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
