package dev.vla.client.nav;

import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.PriorityQueue;
import java.util.Set;
import net.minecraft.block.BlockState;
import net.minecraft.client.MinecraftClient;
import net.minecraft.util.math.BlockPos;
import net.minecraft.world.World;

/**
 * 客户端局部 A* 寻路（LocalPathfinder V2，重构自 mc-collector PathPlanner 的
 * PriorityQueue + 碰撞缓存 + dig-through 设计）。
 *
 * <p>职责：在用户当前位置到下一个服务器航点之间，做小范围（半径 24）3D A* 搜索，
 * 绕开树木/石头/墙角等局部障碍。只跑在客户端线程（NavExecutor.tick() 同步调用），
 * 不触及网络或 gRPC。
 *
 * <p>V2 相对 V1 的改进（对照 mc-collector PathPlanner）：
 * <ul>
 *   <li><b>二叉堆 + 懒删除</b>：PriorityQueue 替换 TreeSet——V1 的 TreeSet 同 f 值
 *       坐标比较导致插入/删除 O(log n) 且常数巨大，堆 + gScore 校验等价且快一个数量级。</li>
 *   <li><b>数组块缓存</b>：搜索区域固定后线性索引 O(1) 查寻，同一坐标最多读一次
 *       {@link World#getBlockState}（V1 每方向每动作各查一次，重复读同一格）。</li>
 *   <li><b>dig-through</b>：前方实心块可挖穿（成本 digCost=20），Move 携带 digTarget，
 *       执行器先挖再走——V1 撞墙只能"朝目标尽量近"回退，容易原地打转。</li>
 *   <li><b>step_up 头顶检查</b>：跳上 1 格台时检查 y+3 头顶（跳跃顶点），治树冠挡跳。</li>
 *   <li><b>返回挖块列表</b>：LocalResult{points, digTargets}，NavExecutor 只挖计划内方块。</li>
 * </ul>
 *
 * <p>动作集（每节点 ≤5 方向×3 动作）：
 * 同层走（直/斜，斜走无切角）/ step_up（1 格台，含头顶检查）/ fall 1..3 /
 * dig-through（前方脚格+头格可挖穿，成本 digCost）。2+ 格高差交给服务端 A*。
 */
public final class LocalPathfinder {

    /** 搜索半径（块）。 */
    private static final int RADIUS = 24;
    /** 最大展开次数（半径 24² ≈ 2304 格，实际远小于上限）。 */
    private static final int MAX_EXPANSIONS = 8000;

    /** 斜走成本（与启发一致）。 */
    private static final double SQRT2 = Math.sqrt(2.0);
    /** 跳上 1 格成本。 */
    private static final double STEP_UP_COST = 2.0;
    /** 下落基础成本 + 每额外高度增量。 */
    private static final double FALL_BASE = 1.0;
    private static final double FALL_EXTRA = 0.25;
    /** 最大安全下落高度（&gt;3 格摔伤）。 */
    private static final int MAX_FALL = 3;
    /** 进入水格（脚或头）的附加成本：不硬禁只加价，让 A* 只在没有旱路时才下水。 */
    private static final double WATER_COST = 3.0;
    /** 挖穿一格的成本（对照 mc-collector digCost=30：绕路 ≤digCost 时优先绕路，否则直挖）。 */
    private static final double DIG_COST = 20.0;
    /** 放置一格补落脚点的成本（M11.5 难点③：垫方块过沟/填坑；比挖穿便宜——放置快且不吃 dig_penalty）。 */
    private static final double PLACE_COST = 12.0;
    /** 避让集软成本（M11.5：重规划时绕开失败走廊——「换一条路」而非原路重算）。 */
    private static final double AVOID_COST = 15.0;
    /** 启发式权重（加权 A*，扩展面收窄）。 */
    private static final double HEURISTIC_WEIGHT = 1.1;

    /** 8 方向顺序（确定性）。 */
    private static final int[][] DIRS = {
            {-1, 0}, {0, -1}, {1, 0}, {0, 1},
            {-1, -1}, {1, -1}, {-1, 1}, {1, 1},
    };

    private LocalPathfinder() {
    }

    /** 寻路结果：路径点 + 沿路需挖穿的方块 + 需放置补落脚点的格（执行器先处理再走）。 */
    public static final class LocalResult {
        public final List<BlockPos> points;
        public final List<BlockPos> digTargets;
        /** M11.5：需放置方块补足的落脚格（1 格宽沟/1 格深坑，站定放置后按普通 walk 通过）。 */
        public final List<BlockPos> placeTargets;

        LocalResult(List<BlockPos> points, List<BlockPos> digTargets, List<BlockPos> placeTargets) {
            this.points = points;
            this.digTargets = digTargets;
            this.placeTargets = placeTargets;
        }
    }

    /** 兼容重载：无避让集。 */
    public static LocalResult findPath(BlockPos start, BlockPos goal) {
        return findPath(start, goal, null);
    }

    /**
     * 从 {@code start}（玩家脚格）到 {@code goal}（目标航点格）的局部路径。
     *
     * <p>在工作线程（客户端线程）同步调用，读 {@link World#getBlockState} 时经数组缓存
     * （每格一次）。搜索失败时返回朝目标的"尽量近"路径，而非空列表（保证客户端始终有
     * 移动方向，碰壁几格后自然会解锁新路径）。只有起点不可站时才返回空列表。
     *
     * @param avoid 避让集（M11.5）：重规划时把失败走廊（撞墙块/卡死脚位）软加价
     *              {@link #AVOID_COST}——下一次重试真正「换一条路」而不是原路重算。null=不避让
     */
    public static LocalResult findPath(BlockPos start, BlockPos goal, Set<BlockPos> avoid) {
        MinecraftClient client = MinecraftClient.getInstance();
        if (client == null || client.world == null) {
            return new LocalResult(Collections.emptyList(), Collections.emptyList(),
                    Collections.emptyList());
        }
        World world = client.world;

        int sx = start.getX();
        int sy = start.getY();
        int sz = start.getZ();
        int gx = goal.getX();
        int gy = goal.getY();
        int gz = goal.getZ();

        // 搜索区域（数组缓存定界）
        int minX = Math.min(sx, gx) - RADIUS;
        int maxX = Math.max(sx, gx) + RADIUS;
        int minZ = Math.min(sz, gz) - RADIUS;
        int maxZ = Math.max(sz, gz) + RADIUS;
        int minY = sy - 6;
        int maxY = sy + 6;
        BlockCache bc = new BlockCache(world, minX, maxX, minZ, maxZ, minY, maxY);

        // 起点校验（M11.5 修复）：老代码用裸 isAir() —— 玩家站在草丛/花里（非空气非实心）
        // 被误判「起点不可站」→ 返回空路径 → 局部规划整体失效（草原地图几乎必踩）。
        // 改用与搜索一致的 BlockTraits 口径；不要求脚下有地面（起跳/下落中也可规划）。
        if (!canStandAt(bc, sx, sy, sz)) {
            return new LocalResult(Collections.emptyList(), Collections.emptyList(),
                    Collections.emptyList());
        }

        // 目标微调：目标格 ±1 Y 找可站格；都不可站 → 最近可站格（方块内部/悬空）
        BlockPos g = null;
        for (int dy = -1; dy <= 1; dy++) {
            int ty = gy + dy;
            if (canStandAt(bc, gx, ty, gz) && hasGround(bc, gx, ty - 1, gz)) {
                g = new BlockPos(gx, ty, gz);
                break;
            }
        }
        if (g == null) {
            int bestDist = Integer.MAX_VALUE;
            for (int dx = -3; dx <= 3; dx++) {
                for (int dz = -3; dz <= 3; dz++) {
                    for (int dy = -3; dy <= 3; dy++) {
                        int tx = gx + dx;
                        int ty = gy + dy;
                        int tz = gz + dz;
                        if (canStandAt(bc, tx, ty, tz) && hasGround(bc, tx, ty - 1, tz)) {
                            int d = Math.abs(dx) + Math.abs(dy) + Math.abs(dz);
                            if (d < bestDist) {
                                bestDist = d;
                                g = new BlockPos(tx, ty, tz);
                            }
                        }
                    }
                }
            }
        }
        if (g == null) {
            return new LocalResult(Collections.emptyList(), Collections.emptyList(),
                    Collections.emptyList());
        }

        // 二叉堆 open set（f 升序、g 降序、坐标字典序）；懒删除靠 gScore 校验
        Comparator<Node> cmp = (a, b) -> {
            int c = Double.compare(a.f, b.f);
            if (c != 0) {
                return c;
            }
            c = Double.compare(b.g, a.g);
            if (c != 0) {
                return c;
            }
            c = Integer.compare(a.pos.getX(), b.pos.getX());
            if (c != 0) {
                return c;
            }
            c = Integer.compare(a.pos.getY(), b.pos.getY());
            if (c != 0) {
                return c;
            }
            return Integer.compare(a.pos.getZ(), b.pos.getZ());
        };
        PriorityQueue<Node> open = new PriorityQueue<>(cmp);
        Map<BlockPos, Double> gScore = new HashMap<>();
        Map<BlockPos, BlockPos> cameFrom = new HashMap<>();
        // 进入该格需先挖的方块（脚格 [+ 头格]；无则 null）
        Map<BlockPos, List<BlockPos>> digOf = new HashMap<>();
        // 进入该格需先放置补足的落脚格（M11.5 place_step；无则 null）
        Map<BlockPos, List<BlockPos>> placeOf = new HashMap<>();
        Set<BlockPos> closed = new HashSet<>();

        gScore.put(start, 0.0);
        open.add(new Node(start, 0.0, heuristic(start, g)));

        int expanded = 0;
        BlockPos foundAt = null;

        while (!open.isEmpty() && expanded < MAX_EXPANSIONS) {
            Node cur = open.poll();
            if (closed.contains(cur.pos)) {
                continue;
            }
            Double gCur = gScore.get(cur.pos);
            if (gCur == null || Double.compare(gCur, cur.g) != 0) {
                continue;  // 懒删除：旧副本
            }
            if (cur.pos.equals(g)) {
                foundAt = cur.pos;
                break;
            }
            closed.add(cur.pos);
            expanded++;

            double curG = cur.g;
            int cx = cur.pos.getX();
            int cy = cur.pos.getY();
            int cz = cur.pos.getZ();

            for (int[] d : DIRS) {
                int nx = cx + d[0];
                int nz = cz + d[1];
                boolean diagonal = d[0] != 0 && d[1] != 0;
                double moveCost = diagonal ? SQRT2 : 1.0;

                // 区域边界检查
                if (nx < bc.minX || nx > bc.maxX || nz < bc.minZ || nz > bc.maxZ) {
                    continue;
                }

                // 1) 同层走：目标脚格可站 + 脚下有地面 + 头格可站 + 斜走不切角
                if (canStandAt(bc, nx, cy, nz) && hasGround(bc, nx, cy - 1, nz)
                        && (!diagonal || diagonalClear(bc, cx, cy, cz, nx, nz))) {
                    relax(open, gScore, cameFrom, digOf, placeOf, closed, avoid, g, cur.pos, curG,
                            new BlockPos(nx, cy, nz), moveCost + enterCost(bc, nx, cy, nz), null, null);
                }

                // 2) 上台阶 (+1 Y)：前方脚格为 1 格实心台 + 台上可站 + 当前列头顶可起跳
                //    + 跳跃顶点不挡头。M11.5 修复：老代码此处第三个条件是
                //    isPassable(nx, cy, nz)，与 hasGround(nx, cy, nz)（要求同格实心）恒矛盾
                //    —— step_up 边从未生效，丘陵地形 A* 只能绕/挖/摔。起跳净空应查
                //    **当前列** (cx, cy+2, cz)（原地起跳后前移）。
                if (canStandAt(bc, nx, cy + 1, nz) && hasGround(bc, nx, cy, nz)
                        && isPassable(bc, cx, cy + 2, cz)
                        && isPassable(bc, nx, cy + 3, nz)) {  // 跳跃顶点 ~y+1.3 → 查 y+3
                    relax(open, gScore, cameFrom, digOf, placeOf, closed, avoid, g, cur.pos, curG,
                            new BlockPos(nx, cy + 1, nz),
                            moveCost + STEP_UP_COST + enterCost(bc, nx, cy + 1, nz), null, null);
                }

                // 3) 下台阶 (-1 Y)：前方脚格空 + 下方可站
                if (canStandAt(bc, nx, cy - 1, nz) && hasGround(bc, nx, cy - 2, nz)) {
                    relax(open, gScore, cameFrom, digOf, placeOf, closed, avoid, g, cur.pos, curG,
                            new BlockPos(nx, cy - 1, nz),
                            moveCost + FALL_BASE + enterCost(bc, nx, cy - 1, nz), null, null);
                }

                // 4) 下落 1..3：路径格全空 + 落脚可站（脚+头）+ 脚下有地面
                for (int h = 2; h <= MAX_FALL; h++) {
                    boolean pathClear = true;
                    for (int k = 1; k <= h; k++) {
                        if (!isPassable(bc, nx, cy - k, nz)) {
                            pathClear = false;
                            break;
                        }
                    }
                    if (pathClear && canStandAt(bc, nx, cy - h, nz)
                            && hasGround(bc, nx, cy - h - 1, nz)) {
                        relax(open, gScore, cameFrom, digOf, placeOf, closed, avoid, g, cur.pos, curG,
                                new BlockPos(nx, cy - h, nz),
                                FALL_BASE + FALL_EXTRA * (h - 1) + enterCost(bc, nx, cy - h, nz),
                                null, null);
                    }
                }

                // 5) dig-through：前方脚格（+头格）实心可挖 → 挖穿再走（mc-collector addMove 借鉴）。
                //    只走直向（斜向挖穿会切角穿墙）。
                //    M11：头格也实心时**两格都要进 digTargets**——老版本只记脚格，执行器挖穿
                //    脚格后仍被头格挡住，白算了一条 2×digCost 的边（头格常是树叶/低檐）。
                if (!diagonal && isDiggable(bc, nx, cy, nz) && hasGround(bc, nx, cy - 1, nz)) {
                    List<BlockPos> digs = new ArrayList<>(2);
                    digs.add(new BlockPos(nx, cy, nz));
                    double digCost = DIG_COST;
                    boolean headOk = true;
                    if (!isPassable(bc, nx, cy + 1, nz)) {
                        if (isDiggable(bc, nx, cy + 1, nz)) {
                            digs.add(new BlockPos(nx, cy + 1, nz));
                            digCost += DIG_COST;
                        } else {
                            headOk = false;   // 头格是基岩/危险格 → 这条边不可行
                        }
                    }
                    if (headOk) {
                        relax(open, gScore, cameFrom, digOf, placeOf, closed, avoid, g, cur.pos, curG,
                                new BlockPos(nx, cy, nz), moveCost + digCost, digs, null);
                    }
                }

                // 6) dig_step_up（M11.5 难点③「阶梯式挖出通道」）：上台阶被台阶脚格/头格挡住
                //    → 挖出楼梯位再跳上。台面 (nx, cy, nz) 必须实心（跳上去要站）；
                //    台阶脚格 (nx, cy+1, nz) / 头格 (nx, cy+2, nz) 中实心者必须可挖；至少
                //    挖一格（两格全空由 step_up 覆盖）。撞头跳顶点 1.2 > 1.0 足以登上
                //    （§5.7 跳跃积分），故不要求 (nx, cy+3, nz) 通行。只走直向。
                //    A* 连续串接此边即得「上行阶梯挖掘通道」——坑内无泥土时的脱困路径。
                if (!diagonal && hasGround(bc, nx, cy, nz)
                        && isPassable(bc, cx, cy + 2, cz)) {   // 当前列头顶可起跳
                    boolean feetBlocked = !isPassable(bc, nx, cy + 1, nz);
                    boolean headBlocked = !isPassable(bc, nx, cy + 2, nz);
                    if (feetBlocked || headBlocked) {
                        List<BlockPos> digs2 = new ArrayList<>(2);
                        boolean ok = true;
                        if (feetBlocked) {
                            if (isDiggable(bc, nx, cy + 1, nz)) {
                                digs2.add(new BlockPos(nx, cy + 1, nz));
                            } else {
                                ok = false;   // 基岩/危险格
                            }
                        }
                        if (ok && headBlocked) {
                            if (isDiggable(bc, nx, cy + 2, nz)) {
                                digs2.add(new BlockPos(nx, cy + 2, nz));
                            } else {
                                ok = false;
                            }
                        }
                        if (ok && !digs2.isEmpty()) {
                            relax(open, gScore, cameFrom, digOf, placeOf, closed, avoid, g, cur.pos, curG,
                                    new BlockPos(nx, cy + 1, nz),
                                    moveCost + STEP_UP_COST + DIG_COST * digs2.size()
                                            + enterCost(bc, nx, cy + 1, nz), digs2, null);
                        }
                    }
                }

                // 8) place_step（M11.5 难点③「用方块补足落脚点」）：前方身体两格可通行但
                //    脚下缺失（空气/水）→ 放一块补上再走。两种可放置支撑（站定即可命中，
                //    无需蹲边探身）：
                //    - 1 格深坑：下方 (nx,cy-2) 实心 → 瞄其顶面，放置落在坑格；
                //    - 1 格宽沟（任意深）：对侧同层 (nx+d,cy-1) 实心 → 瞄其朝沟侧面偏下点。
                //    只走直向；成本 PLACE_COST（低于挖穿——放置快且不吃 dig_penalty）。
                if (!diagonal && canStandAt(bc, nx, cy, nz)) {
                    int footing = bc.stateAt(nx, cy - 1, nz);
                    if (footing == BlockCache.OPEN || footing == BlockCache.WATER) {
                        boolean belowSolid = hasGround(bc, nx, cy - 2, nz);
                        boolean farSolid = hasGround(bc, nx + d[0], cy - 1, nz + d[1]);
                        if (belowSolid || farSolid) {
                            List<BlockPos> places = new ArrayList<>(1);
                            places.add(new BlockPos(nx, cy - 1, nz));
                            relax(open, gScore, cameFrom, digOf, placeOf, closed, avoid, g,
                                    cur.pos, curG, new BlockPos(nx, cy, nz),
                                    moveCost + PLACE_COST, null, places);
                        }
                    }
                }
            }
        }

        // 回溯路径
        List<BlockPos> path = new ArrayList<>();
        BlockPos cur = foundAt;
        if (cur == null) {
            // 搜索失败：返回"朝目标方向的尽量近路径"而不是空列表
            // 找 closed 中离目标最近的节点
            double best = Double.MAX_VALUE;
            for (BlockPos pos : closed) {
                double d = heuristic(pos, g);
                if (d < best) {
                    best = d;
                    cur = pos;
                }
            }
        }
        if (cur == null) {
            return new LocalResult(Collections.emptyList(), Collections.emptyList(),
                    Collections.emptyList());
        }

        // 回溯到起点，沿途收集 digTargets/placeTargets（进入每格前需挖/放的方块，去重保序）
        List<BlockPos> digs = new ArrayList<>();
        List<BlockPos> places = new ArrayList<>();
        Set<BlockPos> seenDigs = new HashSet<>();
        Set<BlockPos> seenPlaces = new HashSet<>();
        BlockPos trace = cur;
        while (trace != null) {
            List<BlockPos> stepPlaces = placeOf.get(trace);
            if (stepPlaces != null) {
                for (int i = stepPlaces.size() - 1; i >= 0; i--) {
                    BlockPos d = stepPlaces.get(i);
                    if (seenPlaces.add(d)) {
                        places.add(d);
                    }
                }
            }
            List<BlockPos> stepDigs = digOf.get(trace);
            if (stepDigs != null) {
                // 逆序遍历：整条路径最后会 reverse，此处先反着放才能保持
                // 「同一步内先挖脚格再挖头格」的顺序
                for (int i = stepDigs.size() - 1; i >= 0; i--) {
                    BlockPos d = stepDigs.get(i);
                    if (seenDigs.add(d)) {
                        digs.add(d);
                    }
                }
            }
            path.add(trace);
            trace = cameFrom.get(trace);
        }
        Collections.reverse(path);
        Collections.reverse(digs);    // 与路径同序（先挖的在前）
        Collections.reverse(places);  // 与路径同序（先放的在前）

        // LOS 压缩：跳过直线可达的中间点（客户端朝白点中心走即可）
        List<BlockPos> points = compress(bc, path);
        return new LocalResult(points, digs, places);
    }

    // ---- 内部工具 ----

    private static void relax(PriorityQueue<Node> open, Map<BlockPos, Double> gScore,
                              Map<BlockPos, BlockPos> cameFrom,
                              Map<BlockPos, List<BlockPos>> digOf,
                              Map<BlockPos, List<BlockPos>> placeOf,
                              Set<BlockPos> closed, Set<BlockPos> avoid, BlockPos goal,
                              BlockPos from, double curG,
                              BlockPos node, double cost, List<BlockPos> digTargets,
                              List<BlockPos> placeTargets) {
        double tentative = curG + cost + avoidPenalty(avoid, node);
        Double prev = gScore.get(node);
        if (prev != null && prev <= tentative) {
            return;
        }
        if (closed.contains(node) && prev != null) {
            return;
        }
        gScore.put(node, tentative);
        cameFrom.put(node, from);
        digOf.put(node, digTargets);
        placeOf.put(node, placeTargets);
        open.add(new Node(node, tentative, tentative + heuristic(node, goal)));
    }

    /** 避让集软成本（M11.5）：节点身体两格或脚下命中失败走廊 → 加价（不硬禁）。 */
    private static double avoidPenalty(Set<BlockPos> avoid, BlockPos node) {
        if (avoid == null || avoid.isEmpty()) {
            return 0.0;
        }
        if (avoid.contains(node) || avoid.contains(node.up()) || avoid.contains(node.down())) {
            return AVOID_COST;
        }
        return 0.0;
    }

    /** NavV3 启发式：水平 octile（√2 对角）+ 垂直分量，加权 1.1。 */
    private static double heuristic(BlockPos a, BlockPos b) {
        int dx = Math.abs(a.getX() - b.getX());
        int dy = Math.abs(a.getY() - b.getY());
        int dz = Math.abs(a.getZ() - b.getZ());
        return (Math.max(dx, dz) + (SQRT2 - 1.0) * Math.min(dx, dz) + dy) * HEURISTIC_WEIGHT;
    }

    /** LOS 压缩：跳过直线段可达的中间点。 */
    private static List<BlockPos> compress(BlockCache bc, List<BlockPos> path) {
        if (path.size() <= 2) {
            return new ArrayList<>(path);
        }
        List<BlockPos> out = new ArrayList<>();
        out.add(path.get(0));
        BlockPos last = path.get(0);
        for (int i = 1; i < path.size(); i++) {
            BlockPos p = path.get(i);
            if (!losClear(bc, last, p)) {
                BlockPos keep = path.get(i - 1);
                if (!keep.equals(out.get(out.size() - 1))) {
                    out.add(keep);
                }
                last = keep;
            }
        }
        BlockPos end = path.get(path.size() - 1);
        if (!end.equals(out.get(out.size() - 1))) {
            out.add(end);
        }
        return out;
    }

    private static boolean losClear(BlockCache bc, BlockPos a, BlockPos b) {
        double dx = b.getX() - a.getX();
        double dy = b.getY() - a.getY();
        double dz = b.getZ() - a.getZ();
        double dist = Math.sqrt(dx * dx + dy * dy + dz * dz);
        int steps = Math.max(1, (int) Math.ceil(dist * 2.0));
        for (int i = 1; i < steps; i++) {
            double t = i / (double) steps;
            int x = (int) Math.round(a.getX() + dx * t);
            int y = (int) Math.round(a.getY() + dy * t);
            int z = (int) Math.round(a.getZ() + dz * t);
            if (!canStandAt(bc, x, y, z)) {
                return false;
            }
        }
        return true;
    }

    // ---- 方块通过性（数组缓存，每格最多读一次世界） ----

    /** 一次寻路的块缓存：区域定界数组，O(1) 索引；槽值懒加载。
     *
     * <p>四态（M11 由三态扩展）：{@code UNREAD/SOLID/OPEN/WATER/HAZARD}。老版本只有
     * SOLID/OPEN 两个有效态且由 {@code isSolid()} 判定，岩浆/水都落进 OPEN → A* 会
     * 主动把 agent 路由进岩浆池（见 {@link BlockTraits} 类注释）。 */
    private static final class BlockCache {
        /** 0=未读，其余对应 {@link BlockTraits.Kind}。 */
        private static final byte UNREAD = 0;
        private static final byte SOLID = 1;
        private static final byte OPEN = 2;
        private static final byte WATER = 3;
        private static final byte HAZARD = 4;

        final World world;
        final int minX;
        final int maxX;
        final int minZ;
        final int maxZ;
        final int minY;
        final int maxY;
        private final int sizeX;
        private final int sizeZ;
        private final int sizeY;
        private final byte[] cells;

        BlockCache(World world, int minX, int maxX, int minZ, int maxZ, int minY, int maxY) {
            this.world = world;
            this.minX = minX;
            this.maxX = maxX;
            this.minZ = minZ;
            this.maxZ = maxZ;
            this.minY = minY;
            this.maxY = maxY;
            this.sizeX = maxX - minX + 1;
            this.sizeZ = maxZ - minZ + 1;
            this.sizeY = maxY - minY + 1;
            this.cells = new byte[sizeX * sizeZ * sizeY];
        }

        /** 该格状态；越界（区域外/y 界外）视为实心墙。 */
        private int stateAt(int x, int y, int z) {
            if (y < minY || y > maxY) {
                return SOLID;
            }
            int lx = x - minX;
            int lz = z - minZ;
            if (lx < 0 || lx >= sizeX || lz < 0 || lz >= sizeZ) {
                return SOLID;
            }
            int idx = (lx * sizeZ + lz) * sizeY + (y - minY);
            byte v = cells[idx];
            if (v == UNREAD) {
                // 懒加载：同一坐标只读一次世界，后续全部内存查询
                BlockState state = world.getBlockState(new BlockPos(x, y, z));
                v = switch (BlockTraits.of(state)) {
                    case SOLID -> SOLID;
                    case WATER -> WATER;
                    case HAZARD -> HAZARD;
                    case OPEN -> OPEN;
                };
                cells[idx] = v;
            }
            return v;
        }
    }

    /** 玩家身体可占据该格（非实心、非危险；水算可占据但会被 enterCost 加价）。 */
    private static boolean isPassable(BlockCache bc, int x, int y, int z) {
        int s = bc.stateAt(x, y, z);
        return s != BlockCache.SOLID && s != BlockCache.HAZARD;
    }

    /** 2 格身高（脚+头）都可占据。 */
    private static boolean canStandAt(BlockCache bc, int x, int y, int z) {
        return isPassable(bc, x, y, z) && isPassable(bc, x, y + 1, z);
    }

    /** 脚下有站立面（只认 SOLID：岩浆块/仙人掌虽实心但归 HAZARD，踩不得）。 */
    private static boolean hasGround(BlockCache bc, int x, int y, int z) {
        return bc.stateAt(x, y, z) == BlockCache.SOLID;
    }

    /**
     * 进入 {@code (x,y,z)} 落脚格的附加成本：脚格或头格是水 → 加 {@link #WATER_COST}。
     *
     * <p>不硬禁水（浅溪/齐膝水该能走），只加价——让 A* 只在没有旱路时才下水，
     * 从源头压低「溺水自救」这条昂贵兜底链的触发频率。
     */
    private static double enterCost(BlockCache bc, int x, int y, int z) {
        if (bc.stateAt(x, y, z) == BlockCache.WATER
                || bc.stateAt(x, y + 1, z) == BlockCache.WATER) {
            return WATER_COST;
        }
        return 0.0;
    }

    /** 斜走时两个正交邻格都可占据（防斜穿实心角；危险格同样拒绝——0.6 宽玩家会擦到）。 */
    private static boolean diagonalClear(BlockCache bc, int cx, int cy, int cz, int nx, int nz) {
        return isPassable(bc, cx, cy, nz) && isPassable(bc, nx, cy, cz);
    }

    /** 可挖穿的实心块（HAZARD/WATER/OPEN 均非 SOLID → 自动排除，无需再点名液体）。 */
    private static boolean isDiggable(BlockCache bc, int x, int y, int z) {
        if (bc.stateAt(x, y, z) != BlockCache.SOLID) {
            return false;
        }
        BlockState state = bc.world.getBlockState(new BlockPos(x, y, z));
        // 基岩/屏障：hardness = -1，永远挖不动
        return state.getBlock().getHardness() >= 0.0f;
    }

    // ---- 内部类 ----

    /** A* 搜索节点。 */
    private static final class Node {
        final BlockPos pos;
        final double g;
        final double f;

        Node(BlockPos pos, double g, double f) {
            this.pos = pos;
            this.g = g;
            this.f = f;
        }

        @Override
        public boolean equals(Object o) {
            return o instanceof Node n && pos.equals(n.pos);
        }

        @Override
        public int hashCode() {
            return pos.hashCode();
        }
    }
}
