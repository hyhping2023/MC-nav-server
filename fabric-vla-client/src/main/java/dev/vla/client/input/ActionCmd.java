package dev.vla.client.input;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;

/**
 * M2 原始动作（tick 级，DESIGN.md §7.1 / docs/p1_protocol.md §2.2）。
 *
 * <p>不依赖任何 Minecraft 类，可在游戏进程外独立解析（WsServer harness 使用）。
 */
public class ActionCmd {

    public boolean forward;
    public boolean back;
    public boolean left;
    public boolean right;

    public boolean jump;
    public boolean sneak;
    public boolean sprint;

    public boolean attack;
    public boolean use;
    public boolean drop;
    public boolean inventory;

    /** 快捷栏槽位 0-8；-1 表示不切换。 */
    public int hotbar = -1;

    /** 视角增量 {@code {pitchDelta, yawDelta}}（度）。 */
    public final float[] camera = {0.0f, 0.0f};

    public static ActionCmd fromJson(JsonObject obj) {
        ActionCmd cmd = new ActionCmd();
        cmd.forward = getBool(obj, "forward");
        cmd.back = getBool(obj, "back");
        cmd.left = getBool(obj, "left");
        cmd.right = getBool(obj, "right");
        cmd.jump = getBool(obj, "jump");
        cmd.sneak = getBool(obj, "sneak");
        cmd.sprint = getBool(obj, "sprint");
        cmd.attack = getBool(obj, "attack");
        cmd.use = getBool(obj, "use");
        cmd.drop = getBool(obj, "drop");
        cmd.inventory = getBool(obj, "inventory");
        if (obj.has("hotbar")) {
            cmd.hotbar = obj.get("hotbar").getAsInt();
        }
        if (obj.has("camera") && obj.get("camera").isJsonArray()) {
            JsonArray cam = obj.getAsJsonArray("camera");
            if (cam.size() >= 2) {
                cmd.camera[0] = cam.get(0).getAsFloat();
                cmd.camera[1] = cam.get(1).getAsFloat();
            }
        }
        return cmd;
    }

    private static boolean getBool(JsonObject obj, String name) {
        return obj.has(name) && obj.get(name).getAsBoolean();
    }
}
