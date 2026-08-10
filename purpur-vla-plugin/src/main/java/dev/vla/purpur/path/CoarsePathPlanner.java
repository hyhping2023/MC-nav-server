package dev.vla.purpur.path;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import org.bukkit.Material;
import org.bukkit.World;
import org.bukkit.util.BlockVector;

/**
 * 粗航点规划器（M11.5 两层导航，DESIGN.md §17.3 难点⑤）。
 *
 * <p>服务端只回答「大方向 + 可落脚的途径点」，不做全局 A*：
 * <ol>
 *   <li>直线可达（{@link DirectPathPlanner} LOS）→ {@code [start, goal]} 两航点（不变）。</li>
 *   <li>直线被挡 → 沿 start→goal 连线每 {@link #SPACING} 格采样一列，垂直方向在插值
 *       高度 ±{@link #SNAP_WINDOW} 内吸附最近可站格（脚+头可通行、脚下实心非危险）；
 *       该列不可站时向邻列（±1、±2 环）借位；仍不可站则跳过该采样点（两点间空档交给
 *       客户端 LocalPathfinder 局部绕障/挖穿）。</li>
 *   <li>目标格不可站时在 3D 邻域（水平 ±3、垂直 -4..+4）找最近可站格作为终点；
 *       连终点都找不到 → {@code found=false}（调用方黑名单/游走兜底）。</li>
 * </ol>
 *
 * <p>成本：O(距离/8) 次列扫描，无搜索爆炸；输出航点间距 ≤8 格、垂直落差有限，恰好落在
 * 客户端 LocalPathfinder（半径 24、y ±6）的局部规划能力内。
 */
public final class CoarsePathPlanner {

    /** 采样间距（格）：与客户端局部规划半径（24）留 3 倍余量。 */
    private static final int SPACING = 8;
    /** 垂直吸附窗口：插值高度 ± 此值内找可站格。 */
    private static final int SNAP_WINDOW = 8;
    /** 邻列借位偏移环（±1、±2）。 */
    private static final int[][] NEIGHBOR_RING = {
            {1, 0}, {-1, 0}, {0, 1}, {0, -1},
            {1, 1}, {1, -1}, {-1, 1}, {-1, -1},
            {2, 0}, {-2, 0}, {0, 2}, {0, -2},
    };

    private CoarsePathPlanner() {
    }

    /**
     * 计算 start→goal 的粗航点。返回类型复用 {@link DirectPathPlanner.PathResult}
     * （waypoints 为途径点序列；details 恒空；PathReply 组装零改动）。
     */
    public static DirectPathPlanner.PathResult findPath(
            World world, BlockVector start, BlockVector goal, String costMode) {
        // 1) 直线可达 → 老行为（两航点）
        DirectPathPlanner.PathResult direct = DirectPathPlanner.findPath(world, start, goal, costMode);
        if (direct.found) {
            return direct;
        }

        // 2) 终点整形：目标不可站 → 3D 邻域找最近可站格
        BlockVector g = adjustGoal(world, goal);
        if (g == null) {
            return direct;   // found=false 原样返回
        }

        // 3) 直线采样 + 落地吸附
        int sx = start.getBlockX();
        int sy = start.getBlockY();
        int sz = start.getBlockZ();
        double dx = g.getBlockX() - sx;
        double dy = g.getBlockY() - sy;
        double dz = g.getBlockZ() - sz;
        double horiz = Math.sqrt(dx * dx + dz * dz);
        int samples = Math.max(1, (int) Math.ceil(horiz / SPACING));

        List<BlockVector> wps = new ArrayList<>();
        wps.add(new BlockVector(sx, sy, sz));
        int checked = 0;
        for (int i = 1; i < samples; i++) {
            double t = i / (double) samples;
            int x = (int) Math.round(sx + dx * t);
            int yHint = (int) Math.round(sy + dy * t);
            int z = (int) Math.round(sz + dz * t);
            checked++;
            BlockVector snapped = snapToStandable(world, x, yHint, z);
            if (snapped == null) {
                for (int[] off : NEIGHBOR_RING) {
                    snapped = snapToStandable(world, x + off[0], yHint, z + off[1]);
                    if (snapped != null) {
                        break;
                    }
                }
            }
            if (snapped == null) {
                continue;   // 空档交给客户端局部规划
            }
            BlockVector last = wps.get(wps.size() - 1);
            if (!snapped.equals(last)) {
                wps.add(snapped);
            }
        }
        if (!g.equals(wps.get(wps.size() - 1))) {
            wps.add(g);
        }
        return new DirectPathPlanner.PathResult(true, wps, Collections.emptyList(), checked);
    }

    /** 目标格不可站时在 3D 邻域（水平 ±3、垂直 -4..+4）找曼哈顿距离最近的可站格。 */
    private static BlockVector adjustGoal(World world, BlockVector goal) {
        int gx = goal.getBlockX();
        int gy = goal.getBlockY();
        int gz = goal.getBlockZ();
        if (standable(world, gx, gy, gz)) {
            return new BlockVector(gx, gy, gz);
        }
        BlockVector best = null;
        int bestDist = Integer.MAX_VALUE;
        for (int dx = -3; dx <= 3; dx++) {
            for (int dz = -3; dz <= 3; dz++) {
                for (int dy = -4; dy <= 4; dy++) {
                    int x = gx + dx;
                    int y = gy + dy;
                    int z = gz + dz;
                    if (!standable(world, x, y, z)) {
                        continue;
                    }
                    int d = Math.abs(dx) + Math.abs(dy) + Math.abs(dz);
                    if (d < bestDist) {
                        bestDist = d;
                        best = new BlockVector(x, y, z);
                    }
                }
            }
        }
        return best;
    }

    /** 在 (x, z) 列的 yHint ± SNAP_WINDOW 内找离 yHint 最近的可站格（0, +1, -1, +2, -2…）。 */
    private static BlockVector snapToStandable(World world, int x, int yHint, int z) {
        for (int k = 0; k <= SNAP_WINDOW; k++) {
            if (standable(world, x, yHint + k, z)) {
                return new BlockVector(x, yHint + k, z);
            }
            if (k > 0 && standable(world, x, yHint - k, z)) {
                return new BlockVector(x, yHint - k, z);
            }
        }
        return null;
    }

    /** 可站：脚格+头格可通行、脚下实心且非危险（与 DirectPathPlanner 同口径）。 */
    private static boolean standable(World world, int x, int y, int z) {
        return passable(world, x, y, z) && passable(world, x, y + 1, z)
                && solidGround(world, x, y - 1, z);
    }

    private static boolean passable(World world, int x, int y, int z) {
        if (y < world.getMinHeight() || y > world.getMaxHeight()) {
            return false;
        }
        Material m = world.getBlockData(x, y, z).getMaterial();
        return !isHazard(m) && (m.isAir() || !m.isSolid());
    }

    private static boolean solidGround(World world, int x, int y, int z) {
        if (y < world.getMinHeight()) {
            return false;
        }
        Material m = world.getBlockData(x, y, z).getMaterial();
        return m.isSolid() && !isHazard(m);
    }

    /** 与 {@link DirectPathPlanner} / 客户端 BlockTraits 同口径的危险方块。 */
    private static boolean isHazard(Material m) {
        return m == Material.LAVA
                || m == Material.FIRE
                || m == Material.SOUL_FIRE
                || m == Material.MAGMA_BLOCK
                || m == Material.CACTUS
                || m == Material.POWDER_SNOW
                || m == Material.SWEET_BERRY_BUSH
                || m == Material.WITHER_ROSE
                || m == Material.LAVA_CAULDRON;
    }
}
