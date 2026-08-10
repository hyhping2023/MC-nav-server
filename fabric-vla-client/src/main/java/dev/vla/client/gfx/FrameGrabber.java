package dev.vla.client.gfx;

import dev.vla.client.VlaClient;
import dev.vla.client.input.KeyRecorder;
import net.minecraft.client.MinecraftClient;
import net.minecraft.client.gl.Framebuffer;
import org.lwjgl.opengl.GL11;
import org.lwjgl.opengl.GL30;

import java.nio.ByteBuffer;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * M3 高效抓帧（DESIGN.md §5.5）。
 *
 * <p>渲染线程专用：把主帧缓冲（{@link MinecraftClient#getFramebuffer()}，已含场景）
 * 用 GPU 下采样 {@code glBlitFramebuffer} 到 224×224 小 FBO，再 {@code glReadPixels}
 * 读出 RGBA 并上下翻转（GL 左下原点 → 图像左上原点），打包成 {@link FrameData}
 * 进无锁队列；JPEG 编码与 WS 上行放在后台线程 {@code FrameSender}（渲染线程零编码）。
 *
 * <p>帧头协议（DESIGN.md §9.2 / docs/p1_protocol.md §2.3）：frame_id 由本类单调递增；
 * lastServerTick 为 M8 经 vla:tick 插件消息缓存的服务端权威 tick（VlaClient.getLastServerTick）；
 * wallNanos 取采集时刻墙钟。M11：帧头附带按键位掩码 + 选中槽 + 相机增量（KeyState），
 * 实现帧↔按键按构造对齐（每帧自带采集时刻的按键状态）。
 */
public final class FrameGrabber {

    /** 默认抓帧分辨率（VLA 观测尺寸）；WS set_capture 可覆盖，0=原生 framebuffer 分辨率。 */
    public static final int SIZE = 224;

    private final AtomicInteger frameId = new AtomicInteger(0);
    private final ConcurrentLinkedQueue<FrameData> queue;

    private int colorTexture = -1;
    private int fboId = -1;
    private ByteBuffer readBuf;
    private boolean initialized = false;
    private int initW = -1;
    private int initH = -1;

    /** 目标抓帧尺寸；0 表示"用主 framebuffer 原生分辨率"（保留游戏原始比例，demo 视频用）。 */
    private volatile int targetWidth = SIZE;
    private volatile int targetHeight = SIZE;

    public FrameGrabber(ConcurrentLinkedQueue<FrameData> queue) {
        this.queue = queue;
    }

    /** M11：最近一次已采集的帧号（未采集过返回 -1）；WS state 上行标注用。 */
    public int getLastFrameId() {
        return frameId.get() - 1;
    }

    /** 运行时切换抓帧分辨率（WS set_capture 回调；渲染线程调用）。0 = 原生 framebuffer 尺寸。 */
    public void setResolution(int width, int height) {
        this.targetWidth = width;
        this.targetHeight = height;
    }

    /** 按指定尺寸（重新）初始化小 FBO；仅在渲染线程调用。 */
    private void init(int w, int h) {
        if (fboId != -1) {
            GL30.glDeleteFramebuffers(fboId);
        }
        if (colorTexture != -1) {
            GL11.glDeleteTextures(colorTexture);
        }

        colorTexture = GL11.glGenTextures();
        GL11.glBindTexture(GL11.GL_TEXTURE_2D, colorTexture);
        GL11.glTexImage2D(GL11.GL_TEXTURE_2D, 0, GL11.GL_RGBA8, w, h, 0,
                GL11.GL_RGBA, GL11.GL_UNSIGNED_BYTE, (ByteBuffer) null);
        GL11.glTexParameteri(GL11.GL_TEXTURE_2D, GL11.GL_TEXTURE_MIN_FILTER, GL11.GL_LINEAR);
        GL11.glTexParameteri(GL11.GL_TEXTURE_2D, GL11.GL_TEXTURE_MAG_FILTER, GL11.GL_LINEAR);
        GL11.glTexParameteri(GL11.GL_TEXTURE_2D, GL11.GL_TEXTURE_WRAP_S, GL30.GL_CLAMP_TO_EDGE);
        GL11.glTexParameteri(GL11.GL_TEXTURE_2D, GL11.GL_TEXTURE_WRAP_T, GL30.GL_CLAMP_TO_EDGE);

        fboId = GL30.glGenFramebuffers();
        GL30.glBindFramebuffer(GL30.GL_FRAMEBUFFER, fboId);
        GL30.glFramebufferTexture2D(GL30.GL_FRAMEBUFFER, GL30.GL_COLOR_ATTACHMENT0,
                GL11.GL_TEXTURE_2D, colorTexture, 0);
        if (GL30.glCheckFramebufferStatus(GL30.GL_FRAMEBUFFER) != GL30.GL_FRAMEBUFFER_COMPLETE) {
            System.err.println("[vla-client] FrameGrabber: small FBO incomplete (" + w + "x" + h + ")");
        }
        GL30.glBindFramebuffer(GL30.GL_FRAMEBUFFER, 0);
        GL11.glBindTexture(GL11.GL_TEXTURE_2D, 0);

        readBuf = ByteBuffer.allocateDirect(w * h * 4);
        initW = w;
        initH = h;
    }

    /** 渲染线程钩子（WorldRenderEvents.LAST：世界+实体渲染完、HUD 前）。 */
    public void capture() {
        MinecraftClient client = MinecraftClient.getInstance();
        Framebuffer main = client.getFramebuffer();
        if (main == null || client.world == null || client.player == null) {
            return;
        }
        int srcW = main.textureWidth;
        int srcH = main.textureHeight;
        if (srcW <= 0 || srcH <= 0) {
            return;
        }
        // 有效抓帧尺寸：target==0 时用 framebuffer 原生尺寸（保留游戏原始比例）
        int cw = targetWidth > 0 ? targetWidth : srcW;
        int ch = targetHeight > 0 ? targetHeight : srcH;
        if (!initialized) {
            initialized = true;
            init(cw, ch);
        } else if (cw != initW || ch != initH) {
            init(cw, ch); // 分辨率切换 → 重建 FBO
        }

        // 主缓冲 → cw×ch 小 FBO（GPU 下采样）
        GL30.glBindFramebuffer(GL30.GL_READ_FRAMEBUFFER, main.fbo);
        GL30.glBindFramebuffer(GL30.GL_DRAW_FRAMEBUFFER, fboId);
        if (targetWidth > 0 && targetHeight > 0 && (cw != srcW || ch != srcH)) {
            // 显式尺寸：按源比例居中适配（letterbox 黑边），不拉伸游戏画面
            double scale = Math.min((double) cw / srcW, (double) ch / srcH);
            int dw = Math.max(1, (int) Math.floor(srcW * scale));
            int dh = Math.max(1, (int) Math.floor(srcH * scale));
            int dx = (cw - dw) / 2;
            int dy = (ch - dh) / 2;
            GL11.glClearColor(0f, 0f, 0f, 1f);
            GL11.glClear(GL11.GL_COLOR_BUFFER_BIT);
            GL30.glBlitFramebuffer(0, 0, srcW, srcH, dx, dy, dx + dw, dy + dh,
                    GL11.GL_COLOR_BUFFER_BIT, GL11.GL_LINEAR);
        } else {
            // native（target 0）或同尺寸：整幅拷贝
            GL30.glBlitFramebuffer(0, 0, srcW, srcH, 0, 0, cw, ch,
                    GL11.GL_COLOR_BUFFER_BIT, GL11.GL_LINEAR);
        }

        // 小 FBO → CPU 读回 RGBA
        GL30.glBindFramebuffer(GL30.GL_READ_FRAMEBUFFER, fboId);
        GL11.glPixelStorei(GL11.GL_PACK_ALIGNMENT, 1);
        readBuf.clear();
        GL11.glReadPixels(0, 0, cw, ch, GL11.GL_RGBA, GL11.GL_UNSIGNED_BYTE, readBuf);

        // 恢复主缓冲绑定（避免 HUD 画进小 FBO）
        GL30.glBindFramebuffer(GL30.GL_FRAMEBUFFER, main.fbo);

        // 上下翻转行序（GL 原点在左下）
        byte[] raw = new byte[cw * ch * 4];
        readBuf.rewind();
        readBuf.get(raw);
        byte[] rgba = new byte[cw * ch * 4];
        for (int y = 0; y < ch; y++) {
            System.arraycopy(raw, (ch - 1 - y) * cw * 4, rgba, y * cw * 4, cw * 4);
        }

        // M8：帧打标真实 lastServerTick（VlaClient 经 vla:tick 收到的服务端权威 tick；
        // 尚未收到广播（-1）时回退 0，避免写入 unsigned 全 1 破坏对齐统计）
        long tick = VlaClient.getLastServerTick();
        int lastServerTick = (tick >= 0 && tick <= Integer.MAX_VALUE) ? (int) tick : 0;

        // M11：采集帧号先写入 KeyRecorder（key_event 标注归属帧），再采样按键状态。
        // 按键状态 = 帧采集时刻的注入/真实按键位掩码 + 选中槽 + 帧间相机增量。
        int fid = frameId.getAndIncrement();
        KeyRecorder.setLastFrameId(fid);
        queue.add(new FrameData(rgba, cw, ch, fid, System.nanoTime(), lastServerTick,
                sampleKeys(client)));
    }

    /** M11：采样帧采集时刻的按键状态（渲染线程调用）。

     * <p>M11.5 修复：相机记**绝对角**而非帧间差分——差分在这里做会被 FrameSender 的
     * 30fps 流控丢帧连带丢掉（实测 look_at 下压 54° 只剩 0.3° 进数据），改由
     * FrameSender 在发送时对上一个**实际发出**的帧做差分，丢帧转角自动并入下一帧，
     * 积分严格闭合（∑Δ = 终态 − 初态）。 */
    private KeyState sampleKeys(MinecraftClient client) {
        int buttons = KeyRecorder.sampleButtons(client);
        int slot = client.player != null ? client.player.getInventory().selectedSlot : -1;
        return new KeyState(buttons, slot, client.player.getYaw(), client.player.getPitch());
    }

    /** 一帧像素数据；仅传引用（ConcurrentLinkedQueue），渲染线程零编码。 */
    public static final class FrameData {
        public final byte[] rgba;
        public final int width;
        public final int height;
        public final int frameId;
        public final long wallNanos;
        public final int lastServerTick;
        /** M11：帧采集时刻的按键状态（帧↔按键对齐）。 */
        public final KeyState keys;

        public FrameData(byte[] rgba, int width, int height, int frameId,
                         long wallNanos, int lastServerTick, KeyState keys) {
            this.rgba = rgba;
            this.width = width;
            this.height = height;
            this.frameId = frameId;
            this.wallNanos = wallNanos;
            this.lastServerTick = lastServerTick;
            this.keys = keys;
        }
    }

    /** M11：帧采集时刻的按键状态快照。 */
    public static final class KeyState {
        /** 11 按键位掩码（bit0=forward … bit10=inventory，见 KeyRecorder）。 */
        public final int buttons;
        /** 当前选中的快捷栏槽位 0-8；-1 = 无。 */
        public final int selectedSlot;
        /** 帧采集时刻相机 yaw 绝对角（度；差分在 FrameSender 发送时做，见 sampleKeys）。 */
        public final float yawAbs;
        /** 帧采集时刻相机 pitch 绝对角（度）。 */
        public final float pitchAbs;

        public KeyState(int buttons, int selectedSlot, float yawAbs, float pitchAbs) {
            this.buttons = buttons;
            this.selectedSlot = selectedSlot;
            this.yawAbs = yawAbs;
            this.pitchAbs = pitchAbs;
        }
    }
}
