package dev.vla.purpur.world;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import org.bukkit.World;
import org.bukkit.util.BlockVector;
import vla.Vla.VoxelReply;

/**
 * 全局体素矩阵读取（DESIGN.md §4.6，M6 交付物，任务 2.6）。
 *
 * <p>立方体遍历 {@code (center ± halfExtent)}，palette 收集 {@code BlockData#getAsString()}
 * （Paper 快速 API {@code world.getBlockData(x,y,z)}，编译期无 NMS 依赖），
 * data 存 palette 局部索引，按 (x,y,z) 序（x 最内层）。
 * halfExtent 默认 16（→ 33³），cap 32（→ 65³）防卡服。
 */
public final class VoxelReader {

    public static final int DEFAULT_HALF_EXTENT = 16;
    public static final int MAX_HALF_EXTENT = 32;

    private VoxelReader() {
    }

    /** 读取以 {@code center} 为中心、半宽 {@code halfExtent} 的立方体体素。 */
    public static VoxelReply read(World world, BlockVector center, int halfExtent) {
        int extent = Math.min(Math.max(1, halfExtent), MAX_HALF_EXTENT);
        int size = 2 * extent + 1;
        int ox = center.getBlockX() - extent;
        int oy = center.getBlockY() - extent;
        int oz = center.getBlockZ() - extent;

        List<String> palette = new ArrayList<>();
        Map<String, Integer> index = new HashMap<>();
        int[] data = new int[size * size * size];
        int i = 0;
        for (int y = 0; y < size; y++) {
            for (int z = 0; z < size; z++) {
                for (int x = 0; x < size; x++) {
                    String s = world.getBlockData(ox + x, oy + y, oz + z).getAsString();
                    Integer idx = index.get(s);
                    if (idx == null) {
                        idx = palette.size();
                        index.put(s, idx);
                        palette.add(s);
                    }
                    data[i++] = idx;
                }
            }
        }

        VoxelReply.Builder b = VoxelReply.newBuilder()
                .addAllPalette(palette)
                .setOriginX(ox).setOriginY(oy).setOriginZ(oz)
                .setSize(size);
        for (int d : data) {
            b.addData(d);
        }
        return b.build();
    }
}
