package dev.vla.client.gfx;

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
 * <p>帧头协议（DESIGN.md §9.2）：frame_id 由本类单调递增；lastServerTick 为 M8
 * tick 对齐占位（先填 0）；wallNanos 取采集时刻墙钟。
 */
public final class FrameGrabber {

    public static final int SIZE = 224;

    private final AtomicInteger frameId = new AtomicInteger(0);
    private final ConcurrentLinkedQueue<FrameData> queue;

    private int colorTexture = -1;
    private int fboId = -1;
    private ByteBuffer readBuf;
    private boolean initialized = false;

    public FrameGrabber(ConcurrentLinkedQueue<FrameData> queue) {
        this.queue = queue;
    }

    /** 惰性初始化 224×224 小 FBO（GL30 color texture + FBO）；仅在渲染线程调用。 */
    private void init() {
        if (initialized) {
            return;
        }
        initialized = true;

        colorTexture = GL11.glGenTextures();
        GL11.glBindTexture(GL11.GL_TEXTURE_2D, colorTexture);
        GL11.glTexImage2D(GL11.GL_TEXTURE_2D, 0, GL11.GL_RGBA8, SIZE, SIZE, 0,
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
            System.err.println("[vla-client] FrameGrabber: small FBO incomplete");
        }
        GL30.glBindFramebuffer(GL30.GL_FRAMEBUFFER, 0);
        GL11.glBindTexture(GL11.GL_TEXTURE_2D, 0);

        readBuf = ByteBuffer.allocateDirect(SIZE * SIZE * 4);
    }

    /** 渲染线程钩子（WorldRenderEvents.LAST：世界+实体渲染完、HUD 前）。 */
    public void capture() {
        MinecraftClient client = MinecraftClient.getInstance();
        Framebuffer main = client.getFramebuffer();
        if (main == null || client.world == null) {
            return;
        }
        int srcW = main.textureWidth;
        int srcH = main.textureHeight;
        if (srcW <= 0 || srcH <= 0) {
            return;
        }
        if (!initialized) {
            init();
        }

        // 主缓冲 → 224×224 小 FBO（GPU 下采样）
        GL30.glBindFramebuffer(GL30.GL_READ_FRAMEBUFFER, main.fbo);
        GL30.glBindFramebuffer(GL30.GL_DRAW_FRAMEBUFFER, fboId);
        GL30.glBlitFramebuffer(0, 0, srcW, srcH, 0, 0, SIZE, SIZE,
                GL11.GL_COLOR_BUFFER_BIT, GL11.GL_LINEAR);

        // 小 FBO → CPU 读回 RGBA
        GL30.glBindFramebuffer(GL30.GL_READ_FRAMEBUFFER, fboId);
        GL11.glPixelStorei(GL11.GL_PACK_ALIGNMENT, 1);
        readBuf.clear();
        GL11.glReadPixels(0, 0, SIZE, SIZE, GL11.GL_RGBA, GL11.GL_UNSIGNED_BYTE, readBuf);

        // 恢复主缓冲绑定（避免 HUD 画进小 FBO）
        GL30.glBindFramebuffer(GL30.GL_FRAMEBUFFER, main.fbo);

        // 上下翻转行序（GL 原点在左下）
        byte[] raw = new byte[SIZE * SIZE * 4];
        readBuf.rewind();
        readBuf.get(raw);
        byte[] rgba = new byte[SIZE * SIZE * 4];
        for (int y = 0; y < SIZE; y++) {
            System.arraycopy(raw, (SIZE - 1 - y) * SIZE * 4, rgba, y * SIZE * 4, SIZE * 4);
        }

        // lastServerTick=0 占位（M8 tick 对齐才填真实值）
        queue.add(new FrameData(rgba, frameId.getAndIncrement(), System.nanoTime(), 0));
    }

    /** 一帧像素数据；仅传引用（ConcurrentLinkedQueue），渲染线程零编码。 */
    public static final class FrameData {
        public final byte[] rgba;
        public final int frameId;
        public final long wallNanos;
        public final int lastServerTick;

        public FrameData(byte[] rgba, int frameId, long wallNanos, int lastServerTick) {
            this.rgba = rgba;
            this.frameId = frameId;
            this.wallNanos = wallNanos;
            this.lastServerTick = lastServerTick;
        }
    }
}
