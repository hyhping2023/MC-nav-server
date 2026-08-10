package dev.vla.client.net;

import dev.vla.client.gfx.FrameGrabber;

import javax.imageio.IIOImage;
import javax.imageio.ImageIO;
import javax.imageio.ImageWriteParam;
import javax.imageio.ImageWriter;
import javax.imageio.stream.ImageOutputStream;
import java.awt.image.BufferedImage;
import java.io.ByteArrayOutputStream;
import java.nio.ByteBuffer;
import java.util.concurrent.ConcurrentLinkedQueue;

/**
 * M3 帧上行（DESIGN.md §5.5 / §9.2）：后台守护线程。
 *
 * <p>从无锁队列取 {@link FrameData} → RGBA→ARGB {@link BufferedImage} →
 * JPEG（质量 0.85）→ 二进制 WS 消息。帧头（M11 起 23B，docs/p1_protocol.md §2.3）：
 * {@code [4B frame_id BE][4B last_server_tick BE][8B wall_nanos BE]
 * [2B buttons BE][1B hotbar][2B yaw_delta int16][2B pitch_delta int16][JPEG]}。
 * {@link WsServer} 无连接会话时直接丢弃该帧（不积压）。由 {@code VlaClient} 启停。
 */
public final class FrameSender implements Runnable {

    private static final float JPEG_QUALITY = 0.85f;
    /** M7 流控：帧上行上限（30fps）。客户端满帧率上行 >> Python 每 step 收一帧，
     * 不限制会让 TCP 缓冲堆积、客户端 WS 写阻塞（keepalive pong 发不出 → 连接判死）。 */
    private static final long MIN_SEND_INTERVAL_MS = 33;

    private final WsServer server;
    private final ConcurrentLinkedQueue<FrameGrabber.FrameData> queue;
    private final int width;
    private final int height;

    private volatile boolean running = false;
    private Thread thread;
    /** M11.5：上一个**实际发出**帧的相机绝对角（发送线程私有）；差分在发送时做。 */
    private float lastSentYaw = Float.NaN;
    private float lastSentPitch = Float.NaN;

    public FrameSender(WsServer server, ConcurrentLinkedQueue<FrameGrabber.FrameData> queue,
                       int width, int height) {
        this.server = server;
        this.queue = queue;
        this.width = width;
        this.height = height;
    }

    public void start() {
        if (thread != null && thread.isAlive()) {
            return;
        }
        running = true;
        thread = new Thread(this, "vla-frame-sender");
        thread.setDaemon(true);
        thread.start();
    }

    public void stop() {
        running = false;
    }

    @Override
    public void run() {
        long lastSend = 0;
        while (running) {
            FrameGrabber.FrameData frame = queue.poll();
            if (frame == null) {
                try {
                    Thread.sleep(1);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    return;
                }
                continue;
            }
            if (!server.hasSession()) {
                continue; // 无连接会话，丢弃
            }

            // M7 流控：限速（间隔未到先 sleep），并把积压帧丢弃只留最新一帧
            long now = System.currentTimeMillis();
            long wait = MIN_SEND_INTERVAL_MS - (now - lastSend);
            if (wait > 0) {
                try {
                    Thread.sleep(wait);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    return;
                }
            }
            FrameGrabber.FrameData toSend = frame;
            FrameGrabber.FrameData newest;
            while ((newest = queue.poll()) != null) {
                toSend = newest; // 丢弃中间帧，只保留最新
            }
            lastSend = System.currentTimeMillis();

            try {
                byte[] jpeg = encodeJpeg(toSend.rgba, toSend.width, toSend.height);
                server.sendBinary(pack(toSend, jpeg));
            } catch (Exception e) {
                System.err.println("[vla-client] frame send error: " + e);
            }
        }
    }

    /**
     * 打包二进制帧头 + JPEG（M11 帧头 23B）：
     * `[4B frame_id][4B last_server_tick][8B wall_nanos][2B buttons][1B hotbar]
     * [2B yaw_delta int16][2B pitch_delta int16][JPEG]`。
     * yaw/pitch delta 用 int16 定点（*100 度），夹紧 ±327.67°。
     *
     * <p>M11.5：delta 在**发送时**对上一个实际发出的帧做差分（yaw 走最短角差）——
     * 流控丢帧的转角自动并入下一发送帧，∑Δ 严格等于视角终态−初态（老实现按渲染帧
     * 差分，丢帧连带丢转角，实测 54° 下压只剩 0.3° 进数据）。
     */
    private ByteBuffer pack(FrameGrabber.FrameData frame, byte[] jpeg) {
        FrameGrabber.KeyState keys = frame.keys;
        float dyaw = 0f;
        float dpitch = 0f;
        if (!Float.isNaN(lastSentYaw)) {
            dyaw = wrapDegrees(keys.yawAbs - lastSentYaw);
            dpitch = keys.pitchAbs - lastSentPitch;
        }
        lastSentYaw = keys.yawAbs;
        lastSentPitch = keys.pitchAbs;
        ByteBuffer buf = ByteBuffer.allocate(23 + jpeg.length);
        buf.putInt(frame.frameId);
        buf.putInt(frame.lastServerTick);
        buf.putLong(frame.wallNanos);
        buf.putShort((short) keys.buttons);
        buf.put((byte) (keys.selectedSlot >= 0 ? keys.selectedSlot : 0xFF));
        buf.putShort(q(dyaw));
        buf.putShort(q(dpitch));
        buf.put(jpeg);
        buf.flip();
        return buf;
    }

    /** 归一化角差到 [-180, 180]（最短角差；不依赖 MC 类，保持本类纯 Java）。 */
    private static float wrapDegrees(float deg) {
        float d = deg % 360.0f;
        if (d >= 180.0f) {
            d -= 360.0f;
        }
        if (d < -180.0f) {
            d += 360.0f;
        }
        return d;
    }

    /** float 度 → int16 定点（*100，夹紧 ±327.67）。 */
    private static short q(float deg) {
        long v = Math.round(deg * 100.0);
        if (v > 32767) {
            v = 32767;
        }
        if (v < -32767) {
            v = -32767;
        }
        return (short) v;
    }

    /** RGBA(byte[]) → BGR BufferedImage → JPEG 字节。
     *
     * 注意：标准 JPEG 编码器不支持带 alpha 的图（TYPE_INT_ARGB 会抛
     * "Bogus input colorspace"），故用 TYPE_3BYTE_BGR（JPEG 无 alpha 通道）。
     * 尺寸取每帧的 width/height（抓帧分辨率可运行时切换，如 demo 原生分辨率）。 */
    private byte[] encodeJpeg(byte[] rgba, int w, int h) throws Exception {
        BufferedImage img = new BufferedImage(w, h, BufferedImage.TYPE_3BYTE_BGR);
        int[] pixels = new int[w * h];
        for (int i = 0; i < pixels.length; i++) {
            int a = rgba[i * 4 + 3] & 0xFF;
            int r = rgba[i * 4] & 0xFF;
            int g = rgba[i * 4 + 1] & 0xFF;
            int b = rgba[i * 4 + 2] & 0xFF;
            pixels[i] = (a << 24) | (r << 16) | (g << 8) | b;
        }
        img.setRGB(0, 0, w, h, pixels, 0, w);

        ImageWriter writer = null;
        try (ByteArrayOutputStream out = new ByteArrayOutputStream();
             ImageOutputStream ios = ImageIO.createImageOutputStream(out)) {
            writer = ImageIO.getImageWritersByFormatName("jpeg").next();
            ImageWriteParam param = writer.getDefaultWriteParam();
            param.setCompressionMode(ImageWriteParam.MODE_EXPLICIT);
            param.setCompressionQuality(JPEG_QUALITY);
            writer.setOutput(ios);
            writer.write(null, new IIOImage(img, null, null), param);
            writer.dispose();
            writer = null;
            return out.toByteArray();
        } finally {
            if (writer != null) {
                writer.dispose();
            }
        }
    }
}
