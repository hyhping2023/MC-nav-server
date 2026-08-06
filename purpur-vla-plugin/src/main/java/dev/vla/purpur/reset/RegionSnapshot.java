package dev.vla.purpur.reset;

import java.nio.charset.StandardCharsets;
import java.util.zip.CRC32;
import org.bukkit.World;
import org.bukkit.block.data.BlockData;
import org.bukkit.util.Vector;

/**
 * 立方体区域方块快照（L1 内存回滚，DESIGN.md §4.4）。
 *
 * <p>捕获时记录 origin(minX,minY,minZ) 与 size，方块存为扁平 {@link BlockData} 数组，
 * 索引 {@code (x-x0)*sizeY*sizeZ + (y-y0)*sizeZ + (z-z0)}。读取走 Paper 快速 API
 * {@code world.getBlockData(x,y,z)}（int 版本）；回滚逐格
 * {@code getBlockAt(x,y,z).setBlockData(data,false)}（applyPhysics=false，防连锁更新，
 * §4.4 避坑）。
 */
public final class RegionSnapshot {

    private final String worldName;
    private final int originX;
    private final int originY;
    private final int originZ;
    private final int sizeX;
    private final int sizeY;
    private final int sizeZ;
    private final BlockData[] blocks;
    private final String checksum;

    private RegionSnapshot(String worldName, int originX, int originY, int originZ,
                           int sizeX, int sizeY, int sizeZ, BlockData[] blocks) {
        this.worldName = worldName;
        this.originX = originX;
        this.originY = originY;
        this.originZ = originZ;
        this.sizeX = sizeX;
        this.sizeY = sizeY;
        this.sizeZ = sizeZ;
        this.blocks = blocks;
        this.checksum = computeCrc32(blocks);
    }

    /**
     * 捕获以 {@code center} 为中心、半宽 {@code halfExtent} 的立方体区域快照
     * （边长 = 2*halfExtent+1）。
     */
    public static RegionSnapshot capture(World world, Vector center, int halfExtent) {
        int x0 = center.getBlockX() - halfExtent;
        int y0 = center.getBlockY() - halfExtent;
        int z0 = center.getBlockZ() - halfExtent;
        int sx = 2 * halfExtent + 1;
        int sy = 2 * halfExtent + 1;
        int sz = 2 * halfExtent + 1;
        BlockData[] data = new BlockData[sx * sy * sz];
        for (int x = 0; x < sx; x++) {
            for (int y = 0; y < sy; y++) {
                for (int z = 0; z < sz; z++) {
                    data[x * sy * sz + y * sz + z] = world.getBlockData(x0 + x, y0 + y, z0 + z);
                }
            }
        }
        return new RegionSnapshot(world.getName(), x0, y0, z0, sx, sy, sz, data);
    }

    /** 回滚：逐格 setBlockData，applyPhysics=false（§4.4 必须关闭物理更新）。 */
    public void restore(World world) {
        for (int x = 0; x < sizeX; x++) {
            for (int y = 0; y < sizeY; y++) {
                for (int z = 0; z < sizeZ; z++) {
                    world.getBlockAt(originX + x, originY + y, originZ + z)
                            .setBlockData(blocks[x * sizeY * sizeZ + y * sizeZ + z], false);
                }
            }
        }
    }

    /** 对所有 BlockData 的 {@code getAsString()} 顺序拼接做 CRC32，返回 8 位 hex。 */
    public String checksum() {
        return checksum;
    }

    public int blockCount() {
        return blocks.length;
    }

    public String worldName() {
        return worldName;
    }

    public int minX() {
        return originX;
    }

    public int minY() {
        return originY;
    }

    public int minZ() {
        return originZ;
    }

    public int maxX() {
        return originX + sizeX - 1;
    }

    public int maxY() {
        return originY + sizeY - 1;
    }

    public int maxZ() {
        return originZ + sizeZ - 1;
    }

    private static String computeCrc32(BlockData[] blocks) {
        CRC32 crc = new CRC32();
        for (BlockData b : blocks) {
            byte[] bytes = b.getAsString().getBytes(StandardCharsets.UTF_8);
            crc.update(bytes, 0, bytes.length);
            crc.update((byte) '\n');
        }
        return String.format("%08x", crc.getValue());
    }

    @Override
    public String toString() {
        return "RegionSnapshot{" + worldName + " origin=(" + originX + "," + originY + "," + originZ
                + ") size=(" + sizeX + "x" + sizeY + "x" + sizeZ + ") blocks=" + blocks.length
                + " checksum=" + checksum + "}";
    }
}
