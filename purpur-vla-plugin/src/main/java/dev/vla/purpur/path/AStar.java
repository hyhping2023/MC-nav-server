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
 * 3D A* 寻路（DESIGN.md §4.5，M6 交付物，任务 2.6；路径避让增强）。
 *
 * <p>XZ 8 方向 + 垂直 3 档（同层/跳上/落下）。方块成本表：
 * 空气=1、可跳方块（非实心，如草丛/花）=2、实心/岩浆/水=不可通行；
 * {@code cost_mode=="water"} 时水可通行（成本 2）。
 *
 * <p>玩家 2 格身高：所有节点（行走/跳跃/下落、起终点微调）用 {@link #canStand}
 * 校验——脚格非实心（可站）**且** 头顶格（y+1）非实心/可跨过，杜绝穿过 1 格高洞、
 * 树冠下檐等顶头被卡。斜向移动额外校验两个正交邻格（{@link #diagonalClear}），
 * 防止切墙角生成物理上走不过的斜穿路径。
 *
 * <p>确定性：固定邻域顺序 + TreeSet（f 升序、g 降序、坐标字典序）作为 open set，
 * 避免 HashSet 随机性；迭代上限 {@link #MAX_EXPANSIONS} 防挂；路径压缩为拐点序列
 * （每段至多 2 格，避免长直线航点切角贴障碍）。
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

        // 起点微调：若脚下格不可站（异常站位），向上找首个可站格（含头部空间）
        while (startY < maxY
                && !canStand(world, start.getBlockX(), startY, start.getBlockZ(), mode)) {
            startY++;
        }
        BlockVector s = new BlockVector(start.getBlockX(), startY, start.getBlockZ());

        // 目标微调：目标格本身不可站（原木是实心）时，在目标周围 3D 邻域
        // （水平 ±2、垂直 -8..+2）找最近的"可站格"作为导航终点——优先落在
        // 树根旁的地面，而不是树冠上方（旧实现只向上找，会抬到树顶之上不可达）。
        BlockVector g = adjustGoal(world, goal, mode, minY, maxY);
        if (g == null) {
            return new PathResult(false, Collections.emptyList(), 0);
        }

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
                boolean diagonal = d[0] != 0 && d[1] != 0;

                // 1) 同层行走：目标格可站（脚+头）、脚下有地面；斜向还要求两个正交邻格
                //    非实心（不切墙角，玩家 0.6 格宽无法从两实心方块间斜穿）
                tryRelax(world, open, gScore, cameFrom, closed, g, cmp,
                        cur.pos, curG, nx, cy, nz, mode, loY, hiY, minX, maxX, minZ, maxZ, 1.0,
                        canStand(world, nx, cy, nz, mode)
                                && isSolid(world, nx, cy - 1, nz, mode)
                                && (!diagonal || diagonalClear(world, cx, cy, cz, nx, nz, mode)));
                // 2) 落下/下台阶：落脚格可站（脚+头）
                tryRelax(world, open, gScore, cameFrom, closed, g, cmp,
                        cur.pos, curG, nx, cy - 1, nz, mode, loY, hiY, minX, maxX, minZ, maxZ, 1.0,
                        canStand(world, nx, cy - 1, nz, mode));
                // 3) 跳上 1 格：前方墙实心、落脚格可站（含头顶留空）；成本 2
                tryRelax(world, open, gScore, cameFrom, closed, g, cmp,
                        cur.pos, curG, nx, cy + 1, nz, mode, loY, hiY, minX, maxX, minZ, maxZ, 2.0,
                        isSolid(world, nx, cy, nz, mode)
                                && canStand(world, nx, cy + 1, nz, mode));
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

    /**
     * 玩家 2 格身高可站判定：脚格可站入 **且** 头顶格（y+1）可站入/可跨过。
     * 用于行走/跳跃/下落节点与起终点微调，杜绝穿过 1 格高洞、树冠下檐等顶头卡位。
     */
    private static boolean canStand(World world, int x, int y, int z, String mode) {
        return canOccupy(world, x, y, z, mode)
                && canOccupy(world, x, y + 1, z, mode);
    }

    /**
     * 目标微调：在目标周围 3D 邻域（水平 ±2、垂直 -8..+2）找最近的可站格。
     *
     * <p>旧实现只向上找，树冠上方 log 会把目标抬到树顶之上（玩家不可达）→ 寻路失败。
     * 本方法优先落在 log 旁的地面（垂直向下找、水平就近），玩家可走到后挖掘。
     * 找不到（完全悬空/熔岩湖）返回 null。
     */
    private static BlockVector adjustGoal(World world, BlockVector goal, String mode,
                                          int minY, int maxY) {
        BlockVector best = null;
        double bestScore = Double.MAX_VALUE;
        int gx = goal.getBlockX();
        int gy = goal.getBlockY();
        int gz = goal.getBlockZ();
        for (int dx = -2; dx <= 2; dx++) {
            for (int dz = -2; dz <= 2; dz++) {
                for (int dy = -8; dy <= 2; dy++) {
                    int y = gy + dy;
                    if (y < minY || y > maxY) {
                        continue;
                    }
                    int x = gx + dx;
                    int z = gz + dz;
                    if (!canStand(world, x, y, z, mode)) {
                        continue;
                    }
                    // 评分：水平距离权重高、垂直偏差次之（倾向地面与贴树）
                    double score = Math.hypot(dx, dz) * 2.0 + Math.abs(dy);
                    if (score < bestScore) {
                        bestScore = score;
                        best = new BlockVector(x, y, z);
                    }
                }
            }
        }
        return best;
    }

    /**
     * 斜向移动切角校验：从 (cx,cz) 斜走到 (nx,nz) 时，两个正交邻格
     * (cx,nz) 与 (nx,cz) 必须都非实心，否则玩家会被墙角卡住（0.6 格宽
     * 无法从两实心方块间斜穿）。
     */
    private static boolean diagonalClear(World world, int cx, int cy, int cz,
                                         int nx, int nz, String mode) {
        return canOccupy(world, cx, cy, nz, mode)
                && canOccupy(world, nx, cy, cz, mode);
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

    /**
     * 压缩为拐点序列：方向变化处保留；且与上一个保留点切比雪夫距离 ≥{@code MAX_SPAN}
     * 时也保留中间节点，保证相邻航点 ≤2 格——避免长直线航点段切角贴障碍、
     * 让 agent 频繁转向贴近 A* 安全路径。
     */
    private static final int MAX_SPAN = 2;

    /** 把连续路径压缩为航点序列（拐点 + 每段 ≤MAX_SPAN 格的中间节点）。 */
    private static List<BlockVector> compress(List<BlockVector> path) {
        List<BlockVector> out = new ArrayList<>();
        if (path.isEmpty()) {
            return out;
        }
        out.add(path.get(0));
        BlockVector lastKept = path.get(0);
        for (int i = 1; i < path.size() - 1; i++) {
            BlockVector p = path.get(i);
            BlockVector d1 = dir(path.get(i - 1), p);
            BlockVector d2 = dir(p, path.get(i + 1));
            boolean turn = !d1.equals(d2);
            boolean span = Math.max(Math.abs(p.getBlockX() - lastKept.getBlockX()),
                    Math.max(Math.abs(p.getBlockY() - lastKept.getBlockY()),
                            Math.abs(p.getBlockZ() - lastKept.getBlockZ()))) >= MAX_SPAN;
            if (turn || span) {
                out.add(p);
                lastKept = p;
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
