package dev.vla.purpur.path;

import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;
import org.bukkit.Material;
import org.bukkit.World;
import org.bukkit.block.data.BlockData;
import org.bukkit.util.BlockVector;

/**
 * 3D A* 寻路（DESIGN.md §4.5，M6 交付物，任务 2.6）。
 *
 * <p>XZ 8 方向 + 垂直 3 档（同层/跳上/落下）。方块成本表：
 * 空气=1、可跳方块（非实心，如草丛/花）=2、实心/岩浆/水=不可通行；
 * {@code cost_mode=="water"} 时水可通行（成本 2）。
 *
 * <p>确定性：固定邻域顺序 + TreeSet（f 升序、g 降序、坐标字典序）作为 open set，
 * 避免 HashSet 随机性；迭代上限 {@link #MAX_EXPANSIONS} 防挂；路径压缩为拐点序列。
 */
public final class AStar {

    /** 不可通行成本标记。 */
    public static final int IMPASSABLE = -1;
    /** 迭代上限（防极端地形挂死主/工作线程）。 */
    public static final int MAX_EXPANSIONS = 200_000;

    /** 固定 8 方向顺序（直向 4 + 斜向 4，确定性）。 */
    private static final int[][] DIRS = {
            {-1, 0}, {0, -1}, {1, 0}, {0, 1},
            {-1, -1}, {1, -1}, {-1, 1}, {1, 1},
    };

    private AStar() {
    }

    /** 寻路结果。 */
    public static final class PathResult {
        public final boolean found;
        /** 拐点序列（含起点与目标点；未找到时为空）。 */
        public final List<BlockVector> waypoints;
        /** 实际扩展节点数（调试用）。 */
        public final int expanded;

        PathResult(boolean found, List<BlockVector> waypoints, int expanded) {
            this.found = found;
            this.waypoints = waypoints;
            this.expanded = expanded;
        }
    }

    /**
     * 3D A*：从 {@code start} 到 {@code goal}（块坐标）。
     *
     * <p>终点判定放宽为切比雪夫距离 ≤1（目标点本身可能是实心方块，无法站入）。
     */
    public static PathResult findPath(World world, BlockVector start, BlockVector goal, String costMode) {
        String mode = costMode == null || costMode.isEmpty() ? "default" : costMode;

        int minY = world.getMinHeight();
        int maxY = world.getMaxHeight() - 1;
        int startY = start.getBlockY();

        // 起点微调：若脚下格不可站（异常站位），向上找首个可站格
        while (startY < maxY
                && !(canOccupy(world, start.getBlockX(), startY, start.getBlockZ(), mode)
                && canOccupy(world, start.getBlockX(), startY + 1, start.getBlockZ(), mode))) {
            startY++;
        }
        BlockVector s = new BlockVector(start.getBlockX(), startY, start.getBlockZ());

        // 目标微调：目标格不可站（山体内部/水下等）时向上找首个可站格，
        // 使"到达目标上方地表"也判定为可达（上限 +12，避免无界上浮）
        int goalY = goal.getBlockY();
        int goalCap = Math.min(maxY, goal.getBlockY() + 12);
        while (goalY < goalCap
                && !(canOccupy(world, goal.getBlockX(), goalY, goal.getBlockZ(), mode)
                && canOccupy(world, goal.getBlockX(), goalY + 1, goal.getBlockZ(), mode))) {
            goalY++;
        }
        BlockVector g = new BlockVector(goal.getBlockX(), goalY, goal.getBlockZ());

        // 搜索边界：起点/终点周围扩张（防无界搜索导致 200k 上限被打满）
        int minX = Math.min(s.getBlockX(), g.getBlockX()) - 24;
        int maxX = Math.max(s.getBlockX(), g.getBlockX()) + 24;
        int minZ = Math.min(s.getBlockZ(), g.getBlockZ()) - 24;
        int maxZ = Math.max(s.getBlockZ(), g.getBlockZ()) + 24;
        int loY = Math.max(minY, Math.min(s.getBlockY(), g.getBlockY()) - 8);
        int hiY = Math.min(maxY, Math.max(s.getBlockY(), g.getBlockY()) + 20);

        Comparator<Entry> cmp = (a, b) -> {
            int c = Double.compare(a.f, b.f);
            if (c != 0) {
                return c;
            }
            c = Double.compare(b.g, a.g);
            if (c != 0) {
                return c;
            }
            c = Integer.compare(a.pos.getBlockX(), b.pos.getBlockX());
            if (c != 0) {
                return c;
            }
            c = Integer.compare(a.pos.getBlockY(), b.pos.getBlockY());
            if (c != 0) {
                return c;
            }
            return Integer.compare(a.pos.getBlockZ(), b.pos.getBlockZ());
        };

        TreeSet<Entry> open = new TreeSet<>(cmp);
        Map<BlockVector, Double> gScore = new HashMap<>();
        Map<BlockVector, BlockVector> cameFrom = new HashMap<>();
        Set<BlockVector> closed = new HashSet<>();

        gScore.put(s, 0.0);
        open.add(new Entry(s, 0.0, heuristic(s, g)));

        int expanded = 0;
        BlockVector foundAt = null;

        while (!open.isEmpty() && expanded < MAX_EXPANSIONS) {
            Entry cur = open.pollFirst();
            if (closed.contains(cur.pos)) {
                continue;
            }
            if (near(cur.pos, g)) {
                foundAt = cur.pos;
                break;
            }
            closed.add(cur.pos);
            expanded++;

            double curG = cur.g;
            int cx = cur.pos.getBlockX();
            int cy = cur.pos.getBlockY();
            int cz = cur.pos.getBlockZ();

            for (int[] d : DIRS) {
                int nx = cx + d[0];
                int nz = cz + d[1];

                // 1) 同层行走（脚下有地面）
                tryRelax(world, open, gScore, cameFrom, closed, g, cmp,
                        cur.pos, curG, nx, cy, nz, mode, loY, hiY, minX, maxX, minZ, maxZ, 1.0,
                        canOccupy(world, nx, cy, nz, mode)
                                && canOccupy(world, nx, cy + 1, nz, mode)
                                && isSolid(world, nx, cy - 1, nz, mode));
                // 2) 落下/下台阶（目标格可站且原格让出头部空间）
                tryRelax(world, open, gScore, cameFrom, closed, g, cmp,
                        cur.pos, curG, nx, cy - 1, nz, mode, loY, hiY, minX, maxX, minZ, maxZ, 1.0,
                        canOccupy(world, nx, cy - 1, nz, mode)
                                && canOccupy(world, nx, cy, nz, mode));
                // 3) 跳上 1 格（前方墙实心、落脚格可站、头顶留空；成本 2）
                tryRelax(world, open, gScore, cameFrom, closed, g, cmp,
                        cur.pos, curG, nx, cy + 1, nz, mode, loY, hiY, minX, maxX, minZ, maxZ, 2.0,
                        isSolid(world, nx, cy, nz, mode)
                                && canOccupy(world, nx, cy + 1, nz, mode)
                                && canOccupy(world, nx, cy + 2, nz, mode));
            }
        }

        if (foundAt == null) {
            return new PathResult(false, Collections.emptyList(), expanded);
        }

        List<BlockVector> path = new ArrayList<>();
        BlockVector cur = foundAt;
        while (cur != null) {
            path.add(cur);
            cur = cameFrom.get(cur);
        }
        Collections.reverse(path);
        // 原始目标点本身作为最后一个航点（即使不可站，标记目标位置）
        if (!path.isEmpty() && !path.get(path.size() - 1).equals(goal)) {
            path.add(goal);
        }
        return new PathResult(true, compress(path), expanded);
    }

    // ---- 邻域扩展与 A* 核心 ----

    private static void tryRelax(World world, TreeSet<Entry> open, Map<BlockVector, Double> gScore,
                                 Map<BlockVector, BlockVector> cameFrom, Set<BlockVector> closed,
                                 BlockVector goal, Comparator<Entry> cmp,
                                 BlockVector from, double curG,
                                 int nx, int ny, int nz, String mode,
                                 int loY, int hiY, int minX, int maxX, int minZ, int maxZ,
                                 double edgeCost, boolean valid) {
        if (!valid) {
            return;
        }
        if (ny < loY || ny > hiY || nx < minX || nx > maxX || nz < minZ || nz > maxZ) {
            return;
        }
        BlockVector node = new BlockVector(nx, ny, nz);
        double tentative = curG + edgeCost;
        Double prev = gScore.get(node);
        if (prev != null && prev <= tentative) {
            return;
        }
        if (closed.contains(node) && prev != null) {
            return;
        }
        gScore.put(node, tentative);
        cameFrom.put(node, from);
        open.add(new Entry(node, tentative, tentative + heuristic(node, goal)));
    }

    private static double heuristic(BlockVector a, BlockVector b) {
        double dx = a.getBlockX() - b.getBlockX();
        double dy = a.getBlockY() - b.getBlockY();
        double dz = a.getBlockZ() - b.getBlockZ();
        return Math.sqrt(dx * dx + dy * dy + dz * dz);
    }

    /** 目标判定：切比雪夫距离 ≤1。 */
    private static boolean near(BlockVector p, BlockVector g) {
        return Math.abs(p.getBlockX() - g.getBlockX()) <= 1
                && Math.abs(p.getBlockY() - g.getBlockY()) <= 1
                && Math.abs(p.getBlockZ() - g.getBlockZ()) <= 1;
    }

    // ---- 方块成本 ----

    /** 安全取块：y 越界（世界高度外）返回 null，视为不可站入的实心边界。 */
    private static BlockData blockData(World world, int x, int y, int z) {
        if (y < world.getMinHeight() || y >= world.getMaxHeight()) {
            return null;
        }
        return world.getBlockData(x, y, z);
    }

    /** 该格可否站入（空气/可跳/水(water 模式)）：成本 ≥0。 */
    private static boolean canOccupy(World world, int x, int y, int z, String mode) {
        BlockData d = blockData(world, x, y, z);
        return d != null && blockCost(d, mode) >= 0;
    }

    /** 该格是否为实心地面（实心方块或岩浆；water 模式下水不算地面；越界视为实心）。 */
    private static boolean isSolid(World world, int x, int y, int z, String mode) {
        BlockData d = blockData(world, x, y, z);
        return d == null || blockCost(d, mode) == IMPASSABLE;
    }

    /**
     * 方块成本表（§4.5）：air=1，可跳方块（非实心）=2，实心/岩浆=∞；
     * 水默认 ∞，{@code cost_mode=="water"} 时为 2。
     */
    private static int blockCost(BlockData data, String mode) {
        Material m = data.getMaterial();
        if (m == Material.LAVA) {
            return IMPASSABLE;
        }
        if (m == Material.WATER) {
            return "water".equals(mode) ? 2 : IMPASSABLE;
        }
        if (m.isAir()) {
            return 1;
        }
        if (!m.isSolid()) {
            return 2; // 草丛/花/农作物等可跳方块
        }
        return IMPASSABLE;
    }

    // ---- 拐点压缩 ----

    /** 把连续路径压缩为拐点序列（方向变化处保留）。 */
    private static List<BlockVector> compress(List<BlockVector> path) {
        List<BlockVector> out = new ArrayList<>();
        if (path.isEmpty()) {
            return out;
        }
        out.add(path.get(0));
        for (int i = 1; i < path.size() - 1; i++) {
            BlockVector d1 = dir(path.get(i - 1), path.get(i));
            BlockVector d2 = dir(path.get(i), path.get(i + 1));
            if (!d1.equals(d2)) {
                out.add(path.get(i));
            }
        }
        out.add(path.get(path.size() - 1));
        // 去连续重复
        List<BlockVector> dedup = new ArrayList<>();
        for (BlockVector p : out) {
            if (dedup.isEmpty() || !dedup.get(dedup.size() - 1).equals(p)) {
                dedup.add(p);
            }
        }
        return dedup;
    }

    private static BlockVector dir(BlockVector a, BlockVector b) {
        return new BlockVector(
                Integer.signum(b.getBlockX() - a.getBlockX()),
                Integer.signum(b.getBlockY() - a.getBlockY()),
                Integer.signum(b.getBlockZ() - a.getBlockZ()));
    }

    /** open set 元素：按 f/g/坐标比较（equals/hashCode 仅看位置，与比较器一致）。 */
    private static final class Entry {
        final BlockVector pos;
        final double g;
        final double f;

        Entry(BlockVector pos, double g, double f) {
            this.pos = pos;
            this.g = g;
            this.f = f;
        }

        @Override
        public boolean equals(Object o) {
            return o instanceof Entry e && pos.equals(e.pos);
        }

        @Override
        public int hashCode() {
            return pos.hashCode();
        }
    }
}
