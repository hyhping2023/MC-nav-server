package dev.vla.purpur.path;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import org.bukkit.Material;
import org.bukkit.World;
import org.bukkit.block.data.BlockData;
import org.bukkit.util.BlockVector;

/**
 * 直线占位寻路（Oracle 数据生成器改造：服务端全局 A* 退役，DESIGN.md §4.5 更新）。
 *
 * <p>数据生成不再追求最优轨迹（Python Oracle 策略做目标选择与局部移动，客户端
 * NavExecutor/LocalPathfinder 做局部绕障）。服务端只需回答"直线是否可达"：
 * <ul>
 *   <li>start→goal 直线段（脚格+头格）全部可通行且终点可站 → 返回
 *       {@code [start, goal]} 两个航点，{@code found=true}（客户端 LocalPathfinder
 *       在两点间自主绕障/挖穿）。</li>
 *   <li>直线被挡 → 返回 {@code found=false}（waypoints/details 为空）。调用方
 *       （collect_wood_agent 的"no path → blacklist + wander"）游走后重扫——不追求
 *       最优时"不可达即放弃"是可接受的兜底。</li>
 * </ul>
 *
 * <p>不再生成 dig/dig_down/place 动作级航点（details 恒为空）；不保留
 * {@code cost_mode} 语义（全部模式等价：直线判定）；删除原 AStar 的
 * VLA_ASTAR_DEBUG 钩子。向后兼容：PathReply 结构、Python {@code _goto_plan} /
 * NavExecutor / LocalPathfinder / PathVisualizer 消费链路零改动。
 */
public final class DirectPathPlanner {

    /** 采样步长（格）：0.5 格一步（与原 LOS 判定一致）。 */
    private static final double SAMPLE_STEP = 0.5;

    private DirectPathPlanner() {
    }

    /** 寻路结果（与 AStar.PathResult 同构，Python 消费方无感知）。 */
    public static final class PathResult {
        public final boolean found;
        /** 拐点序列（直线可达时 = [start, goal]；未找到时为空）。 */
        public final List<BlockVector> waypoints;
        /** 动作级航点（恒为空——Oracle 生成器局部动作 Python/客户端自决）。 */
        public final List<Waypoint> details;
        /** 采样格数（调试用）。 */
        public final int expanded;

        PathResult(boolean found, List<BlockVector> waypoints, List<Waypoint> details, int expanded) {
            this.found = found;
            this.waypoints = waypoints;
            this.details = details;
            this.expanded = expanded;
        }
    }

    /** 动作级航点（占位保留，恒为 WALK；details 列表本身为空）。 */
    public static final class Waypoint {
        public final BlockVector pos;
        public final String action;
        public final BlockVector target;

        Waypoint(BlockVector pos, String action, BlockVector target) {
            this.pos = pos;
            this.action = action;
            this.target = target;
        }
    }

    /**
     * 直线可达性判定：start→goal 整条线段（脚格+头格 0.5 格采样）全部可通行、
     * 终点可站且脚下有地面 → {@code [start, goal]} 两航点；否则 {@code found=false}。
     *
     * <p>任何 costMode 等价（直线判定）；调用方传入的 start/goal 原样使用
     * （不微调——Oracle 生成器按"目标块在视野内可采集"自己决定终点）。
     *
     * <p>M11.5：passable/solid 补 HAZARD 判据（与客户端 {@code BlockTraits} 同口径）——
     * 老代码 {@code !m.isSolid()} 把岩浆当可通行，直线判定会把 agent 直接派进岩浆池
     * （「岩浆铺在石头上」终点脚下 isSolid 通过 → 整条直线放行）。现在 passable 拒绝
     * 岩浆/火/岩浆块/仙人掌等，solid 同样拒绝（岩浆块/仙人掌虽实心但踩不得）。
     */
    public static PathResult findPath(World world, BlockVector start, BlockVector goal, String costMode) {
        int sx = start.getBlockX();
        int sy = start.getBlockY();
        int sz = start.getBlockZ();
        int gx = goal.getBlockX();
        int gy = goal.getBlockY();
        int gz = goal.getBlockZ();

        // 终点必须可站（脚格可通行 + 头格可通行 + 脚下有地面）
        if (!passable(world, gx, gy, gz) || !passable(world, gx, gy + 1, gz)
                || !solid(world, gx, gy - 1, gz)) {
            return new PathResult(false, Collections.emptyList(), Collections.emptyList(), 0);
        }

        double dx = gx - sx;
        double dy = gy - sy;
        double dz = gz - sz;
        double dist = Math.sqrt(dx * dx + dy * dy + dz * dz);
        int steps = Math.max(1, (int) Math.ceil(dist * 2.0));
        int checked = 0;
        for (int i = 1; i < steps; i++) {
            double t = i / (double) steps;
            int x = (int) Math.round(sx + dx * t);
            int y = (int) Math.round(sy + dy * t);
            int z = (int) Math.round(sz + dz * t);
            checked++;
            if (!passable(world, x, y, z) || !passable(world, x, y + 1, z)) {
                return new PathResult(false, Collections.emptyList(), Collections.emptyList(), checked);
            }
        }

        List<BlockVector> wps = new ArrayList<>(2);
        wps.add(new BlockVector(sx, sy, sz));
        wps.add(new BlockVector(gx, gy, gz));
        return new PathResult(true, wps, Collections.emptyList(), checked);
    }

    /** 可通行：空气/非实心且非危险（岩浆/火/岩浆块/仙人掌等接触即伤，一律拒）。 */
    private static boolean passable(World world, int x, int y, int z) {
        if (y < world.getMinHeight() || y > world.getMaxHeight()) {
            return false;
        }
        BlockData data = world.getBlockData(x, y, z);
        Material m = data.getMaterial();
        return !isHazard(m) && (m.isAir() || !m.isSolid());
    }

    /** 可当站立面：实心且非危险（岩浆块/仙人掌虽 isSolid 但踩不得）。 */
    private static boolean solid(World world, int x, int y, int z) {
        if (y < world.getMinHeight()) {
            return false;
        }
        BlockData data = world.getBlockData(x, y, z);
        Material m = data.getMaterial();
        return m.isSolid() && !isHazard(m);
    }

    /** 接触即掉血/陷入（与客户端 BlockTraits#isHazard 同口径的 Bukkit 实现）。 */
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
