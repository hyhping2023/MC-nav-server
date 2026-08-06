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
 * JPEG（质量 0.85）→ 二进制 WS 消息
 * {@code [4B frame_id BE][4B last_server_tick BE][8B wall_nanos BE][JPEG]}。
 * {@link WsServer} 无连接会话时直接丢弃该帧（不积压）。由 {@code VlaClient} 启停。
 */
public final class FrameSender implements Runnable {

    private static final float JPEG_QUALITY = 0.85f;

    private final WsServer server;
    private final ConcurrentLinkedQueue<FrameGrabber.FrameData> queue;
    private final int width;
    private final int height;

    private volatile boolean running = false;
    private Thread thread;

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
            try {
                byte[] jpeg = encodeJpeg(frame.rgba);
                server.sendBinary(pack(frame, jpeg));
            } catch (Exception e) {
                System.err.println("[vla-client] frame send error: " + e);
            }
        }
    }

    /** 打包二进制帧头 + JPEG：`[4B frame_id][4B last_server_tick][8B wall_nanos][JPEG]`。 */
    private static ByteBuffer pack(FrameGrabber.FrameData frame, byte[] jpeg) {
        ByteBuffer buf = ByteBuffer.allocate(16 + jpeg.length);
        buf.putInt(frame.frameId);
        buf.putInt(frame.lastServerTick);
        buf.putLong(frame.wallNanos);
        buf.put(jpeg);
        buf.flip();
        return buf;
    }

    /** RGBA(byte[]) → BGR BufferedImage → JPEG 字节。
     *
     * 注意：标准 JPEG 编码器不支持带 alpha 的图（TYPE_INT_ARGB 会抛
     * "Bogus input colorspace"），故用 TYPE_3BYTE_BGR（JPEG 无 alpha 通道）。 */
    private byte[] encodeJpeg(byte[] rgba) throws Exception {
        BufferedImage img = new BufferedImage(width, height, BufferedImage.TYPE_3BYTE_BGR);
        int[] pixels = new int[width * height];
        for (int i = 0; i < pixels.length; i++) {
            int a = rgba[i * 4 + 3] & 0xFF;
            int r = rgba[i * 4] & 0xFF;
            int g = rgba[i * 4 + 1] & 0xFF;
            int b = rgba[i * 4 + 2] & 0xFF;
            pixels[i] = (a << 24) | (r << 16) | (g << 8) | b;
        }
        img.setRGB(0, 0, width, height, pixels, 0, width);

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
