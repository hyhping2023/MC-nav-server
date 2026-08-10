package dev.vla.client.input;

import com.google.gson.JsonObject;
import dev.vla.client.VlaClient;
import net.minecraft.client.MinecraftClient;

import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.function.Consumer;

/**
 * M11 按键事件记录器（人类演示录制，DESIGN.md §11.3 / docs/p1_protocol.md §2.4）。
 *
 * <p>两种事件源：
 * <ul>
 *   <li>API 模式：{@link #diff} 比较前后注入的 {@link ActionCmd}，对发生变化的按键产生
 *       down/up 事件（模拟人类的按键按下/抬起时序）；</li>
 *   <li>HUMAN 模式（未来真人）：由 {@code KeyboardMixin}/{@code MouseMixin} 记录真实
 *       键鼠事件（{@link #record}）。</li>
 * </ul>
 *
 * <p>事件经 WS 文本上行 {@code {"type":"key_event","key":"forward","down":true,
 * "tick":<serverTick>,"wall_nanos":<nano>,"frame_id":<最近帧>}}。
 * 帧↔按键对齐：帧二进制头自带按键位掩码（帧采集时刻的按键状态，FrameGrabber），
 * key_event 提供精确的按下/抬起时刻。
 *
 * <p>线程安全：并发队列，任意线程 record，客户端 tick 线程 drainTo 上行。
 */
public final class KeyRecorder {

    private static final ConcurrentLinkedQueue<String> EVENTS = new ConcurrentLinkedQueue<>();
    private static volatile boolean enabled = false;
    /** 最近一次帧号（FrameGrabber 每帧写入），给事件打"归属帧"标记。 */
    private static volatile int lastFrameId = -1;

    private KeyRecorder() {
    }

    public static void setEnabled(boolean on) {
        enabled = on;
    }

    public static boolean isEnabled() {
        return enabled;
    }

    /** FrameGrabber 每采集一帧写入帧号，供 key_event 标注归属帧。 */
    public static void setLastFrameId(int frameId) {
        lastFrameId = frameId;
    }

    /**
     * 11 按键位序（与 Python action_space.BUTTONS 一致）：
     * forward=bit0, back=bit1, left=bit2, right=bit3, jump=bit4, sneak=bit5,
     * sprint=bit6, attack=bit7, use=bit8, drop=bit9, inventory=bit10。
     */
    public static int buttonMaskFromAction(ActionCmd cmd) {
        int m = 0;
        if (cmd == null) {
            return m;
        }
        if (cmd.forward) m |= 1 << 0;
        if (cmd.back) m |= 1 << 1;
        if (cmd.left) m |= 1 << 2;
        if (cmd.right) m |= 1 << 3;
        if (cmd.jump) m |= 1 << 4;
        if (cmd.sneak) m |= 1 << 5;
        if (cmd.sprint) m |= 1 << 6;
        if (cmd.attack) m |= 1 << 7;
        if (cmd.use) m |= 1 << 8;
        if (cmd.drop) m |= 1 << 9;
        if (cmd.inventory) m |= 1 << 10;
        return m;
    }

    /** HUMAN 模式：从真实键位绑定采样按键位掩码（物理键鼠按下状态）。 */
    public static int sampleButtonsFromBindings(MinecraftClient client) {
        if (client == null || client.options == null) {
            return 0;
        }
        int m = 0;
        if (client.options.forwardKey.isPressed()) m |= 1 << 0;
        if (client.options.backKey.isPressed()) m |= 1 << 1;
        if (client.options.leftKey.isPressed()) m |= 1 << 2;
        if (client.options.rightKey.isPressed()) m |= 1 << 3;
        if (client.options.jumpKey.isPressed()) m |= 1 << 4;
        if (client.options.sneakKey.isPressed()) m |= 1 << 5;
        if (client.options.sprintKey.isPressed()) m |= 1 << 6;
        if (client.options.attackKey.isPressed()) m |= 1 << 7;
        if (client.options.useKey.isPressed()) m |= 1 << 8;
        if (client.options.dropKey.isPressed()) m |= 1 << 9;
        if (client.options.inventoryKey.isPressed()) m |= 1 << 10;
        return m;
    }

    /**
     * 帧采集时刻的按键位掩码：API 模式 = 当前注入动作（移动走 KeyboardInputMixin 直接写
     * input 字段，forwardKey 等并未置位，故必须读注入动作）；HUMAN 模式 = 真实键位。
     */
    public static int sampleButtons(MinecraftClient client) {
        if (VlaClient.isApiMode()) {
            return buttonMaskFromAction(VlaClient.getCurrentAction());
        }
        return sampleButtonsFromBindings(client);
    }

    /** 记录一条离散按键事件（HUMAN 模式 mixin 调用）。 */
    public static void record(String key, boolean down, long wallNanos) {
        if (!enabled) {
            return;
        }
        JsonObject j = new JsonObject();
        j.addProperty("type", "key_event");
        j.addProperty("key", key);
        j.addProperty("down", down);
        j.addProperty("tick", VlaClient.getLastServerTick());
        j.addProperty("wall_nanos", wallNanos);
        j.addProperty("frame_id", lastFrameId);
        EVENTS.add(j.toString());
    }

    /**
     * API 模式：diff 前后注入动作，对发生变化的按键产生 down/up 事件（仅电平型按键 +
     * 一次性 hotbar 按下；hotbar 只报 down，不报 up——快捷栏选择是一次性动作）。
     */
    public static void diff(ActionCmd prev, ActionCmd cur) {
        if (!enabled || cur == null) {
            return;
        }
        long now = System.nanoTime();
        if (prev == null) {
            prev = new ActionCmd();
        }
        emit(prev.forward, cur.forward, "forward", now);
        emit(prev.back, cur.back, "back", now);
        emit(prev.left, cur.left, "left", now);
        emit(prev.right, cur.right, "right", now);
        emit(prev.jump, cur.jump, "jump", now);
        emit(prev.sneak, cur.sneak, "sneak", now);
        emit(prev.sprint, cur.sprint, "sprint", now);
        emit(prev.attack, cur.attack, "attack", now);
        emit(prev.use, cur.use, "use", now);
        emit(prev.drop, cur.drop, "drop", now);
        emit(prev.inventory, cur.inventory, "inventory", now);
        if (cur.hotbar >= 0 && cur.hotbar != prev.hotbar) {
            record("hotbar:" + cur.hotbar, true, now);
        }
    }

    private static void emit(boolean p, boolean c, String key, long now) {
        if (p != c) {
            record(key, c, now);
        }
    }

    /** 排空积压事件（客户端 tick 线程调用，经 WS 文本上行）。 */
    public static void drainTo(Consumer<String> sink) {
        String e;
        while ((e = EVENTS.poll()) != null) {
            sink.accept(e);
        }
    }
}
