package dev.vla.client.nav;

import dev.vla.client.VlaClient;
import dev.vla.client.VlaClient.DigPlan;
import dev.vla.client.input.ActionCmd;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.function.Consumer;
import net.minecraft.block.BlockState;
import net.minecraft.client.network.ClientPlayerEntity;
import net.minecraft.util.math.BlockPos;
import net.minecraft.util.math.MathHelper;

/**
 * 本地路径跟随控制器（两层导航 M10，V2）：服务端下发长程航点（黄色），客户端做航点间局部绕障
 * 并在 WS 上行 path_debug（白色）。碰壁后先本地重规划——不立即上报 STUCK，消除原地打转。
 *
 * <p>V2 相对 V1 的改进（配合 LocalPathfinder V2）：
 * <ul>
 *   <li><b>本地挖穿执行</b>：LocalPathfinder 返回 digTargets（计划挖的方块），本地路径
 *       推进前先挖穿——不再把可挖块上报 Python 慢挖。</li>
 *   <li><b>stuck 恢复链</b>：卡死 → 本地重规划（≤3 次）→ 上报 STUCK。需要登高时
 *       由 {@link PillarExecutor} 专门执行脚底垫方块，而不原地乱挖。</li>
 *   <li><b>碰撞检测多点采样</b>：1.0~3.0 米三条射线，台阶（1 格台）不误报墙。</li>
 *   <li><b>LocalResult 返回</b>：localPath 带挖块列表（本地只挖计划内方块，杜绝乱挖掘）。</li>
 * </ul>
 *
 * <p>线程模型：全部方法在客户端线程，方法级 synchronized 兜底。
 */
public final class NavExecutor {

    /** 到航点格子中心 3D 距离 < 此值 → 到达（推进下一航点）。 */
    private static final double ARRIVE_DIST = 0.8;
    /** 前瞻距离：距当前航点 < 此值且还有下一航点 → 直接朝下一航点走。 */
    private static final double LOOKAHEAD_DIST = 1.5;
    /** |yaw 误差| 超过此值 → 前进同时侧移朝目标。 */
    private static final double STRAFE_DEG = 10.0;
    /** 碰撞采样距离（沿玩家面向，米；三条射线取最近实心）。 */
    private static final double[] LOOK_AHEADS = {1.0, 2.0, 3.0};
    /** 卡死窗口（tick）：距离到航点无改善则先尝试 LocalPathfinder。 */
    private static final int STUCK_TICKS = 15;
    /** 卡死判定：窗口内到当前航点距离未缩短的最小值（米）。 */
    private static final double STUCK_MIN_PROGRESS = 0.2;
    /** XZ 几乎不动（水平位移 < 此值/tick）连续 N tick → 先尝试 LocalPathfinder。 */
    private static final double STUCK_XZ_EPS = 0.02;
    /** XZ 不动窗口（tick）。 */
    private static final int STUCK_XZ_TICKS = 12;
    /** 本地挖穿放弃阈值（tick）。 */
    private static final int DIG_ABANDON_TICKS = 120;
    /** 导航视角 pitch 夹紧。 */
    private static final double PITCH_CLAMP = 20.0;
    /** 本地重规划上限（用尽后上报 STUCK 交由上层决定换目标或垫方块）。 */
    private static final int LOCAL_REPLAN_MAX = 3;

    // M11.6 冲刺滞回（latch）：进入冲刺的最小水平距 / 最大 yaw 误差。
    private static final double SPRINT_ON_DIST = 5.0;
    private static final double SPRINT_ON_YAW = 15.0;
    // 退出冲刺的最大水平距 / 最小 yaw 误差（死区 3-5m、15-30° 内保持现状，防边界抖动）。
    private static final double SPRINT_OFF_DIST = 3.0;
    private static final double SPRINT_OFF_YAW = 30.0;

    /** 上报状态。 */
    public enum Status { ARRIVED, BLOCKED_BREAKABLE, BLOCKED_WALL, STUCK }

    public record StatusEvent(Status status, BlockPos pos, BlockPos wp, String detail) {}

    private final Consumer<StatusEvent> statusListener;
    private final Consumer<List<BlockPos>> pathDebugListener;

    // ---- 服务端航点 ----
    private List<BlockPos> waypoints;
    private int idx = 0;
    private boolean active = false;
    /** move_only：站位导航阶段禁止本地挖穿/填坑，绝不输出 attack。 */
    private boolean moveOnly = false;

    // ---- 卡死检测 ----
    private double lastDistToWp = Double.NaN;
    private int noProgressTicks = 0;
    private double lastTickX;
    private double lastTickZ;
    private int xzStillTicks;

    // ---- 本地挖穿（服务端 dig 计划 + 本地 dig 计划） ----
    private DigPlan digTarget;
    private int digTicks;
    private List<DigPlan> digTargets;
    private List<BlockPos> localDigTargets;   // LocalPathfinder 计划挖的方块（无工具标注）
    // ---- 本地放置补落脚点（M11.5 place_step） ----
    private List<BlockPos> localPlaceTargets;  // LocalPathfinder 计划放置的落脚格
    private BlockPos placeTarget;
    private int placeTicks;
    /** 挖/放子模式的触达门控（米）：超出先沿路走近，防站远处空挥。 */
    private static final double SUBMODE_REACH = 4.0;
    /** 放置子模式放弃阈值（tick）。 */
    private static final int PLACE_ABANDON_TICKS = 80;
    /** 避让集（M11.5「换路」）：历次失败走廊（撞墙块/卡死脚位），重规划时软加价绕开。 */
    private final Set<BlockPos> avoidCells = new HashSet<>();

    // ---- 局部绕障（M10） ----
    private List<BlockPos> localPath;
    private int localIdx = 0;
    private int localReplanCount = 0;
    private final Set<BlockPos> failedBlockers = new HashSet<>();  // 本地重规划已绕不过的块
    /** M11.6 冲刺 latch：一旦开启持续到「关闭条件」才熄灭（死区内保持），防每 tick 硬阈值抖动。 */
    private boolean sprintLatched = false;

    public NavExecutor(Consumer<StatusEvent> statusListener,
                       Consumer<List<BlockPos>> pathDebugListener) {
        this.statusListener = statusListener;
        this.pathDebugListener = pathDebugListener;
    }

    /** 返回当前局部路径（供可视化）。 */
    public synchronized List<BlockPos> getLocalPath() {
        return localPath;
    }

    public synchronized void setPath(List<BlockPos> path, List<DigPlan> digs) {
        setPath(path, digs, false);
    }

    public synchronized void setPath(List<BlockPos> path, List<DigPlan> digs, boolean moveOnly) {
        this.waypoints = path == null ? null : new ArrayList<>(path);
        this.digTargets = digs == null ? null : new ArrayList<>(digs);
        this.moveOnly = moveOnly;
        this.idx = 0;
        this.active = this.waypoints != null && !this.waypoints.isEmpty();
        this.lastDistToWp = Double.NaN;
        this.noProgressTicks = 0;
        this.xzStillTicks = 0;
        this.digTarget = null;
        this.digTicks = 0;
        this.localPath = null;
        this.localIdx = 0;
        this.localReplanCount = 0;
        this.localDigTargets = null;
        this.localPlaceTargets = null;
        this.placeTarget = null;
        this.placeTicks = 0;
        this.failedBlockers.clear();
        this.avoidCells.clear();
        this.sprintLatched = false;
    }

    public synchronized void cancel() {
        this.active = false;
        this.waypoints = null;
        this.digTargets = null;
        this.idx = 0;
        this.lastDistToWp = Double.NaN;
        this.noProgressTicks = 0;
        this.xzStillTicks = 0;
        this.digTarget = null;
        this.digTicks = 0;
        this.localPath = null;
        this.localIdx = 0;
        this.localReplanCount = 0;
        this.localDigTargets = null;
        this.localPlaceTargets = null;
        this.placeTarget = null;
        this.placeTicks = 0;
        this.failedBlockers.clear();
        this.avoidCells.clear();
        this.sprintLatched = false;
        this.moveOnly = false;
    }

    public synchronized boolean isActive() {
        return active;
    }

    /** M11.6：挖穿/放置子模式进行中——此时工具由规划决定，视线工具策略（ToolPolicy）不覆盖。 */
    public synchronized boolean isBusy() {
        return digTarget != null || placeTarget != null;
    }

    public synchronized ActionCmd tick(ClientPlayerEntity player) {
        if (!active || waypoints == null || player == null) {
            return null;
        }

        // 越过已到达的服务端航点。两条推进判据（M11.5）：
        // ① 3D 距航点中心 < ARRIVE_DIST；
        // ② **越段推进**：玩家到下一航点的距离已小于「当前航点到下一航点」的段长
        //    ——说明当前航点已被越过/根本没必要回踩。没有②时，起点航点因调用方
        //    取整误差（int 截断 vs floor，负坐标错一格）落在 0.8 圈外 → idx 卡 0，
        //    lookahead 一失效 swp 就回落到起点航点 → 导航掉头振荡（实测复现）。
        int prevIdx = idx;
        while (idx < waypoints.size()) {
            if (distToWpCenter(player, waypoints.get(idx)) < ARRIVE_DIST) {
                idx++;
                continue;
            }
            if (idx + 1 < waypoints.size()
                    && distToWpCenter(player, waypoints.get(idx + 1))
                            < wpDistance(waypoints.get(idx), waypoints.get(idx + 1))) {
                idx++;
                continue;
            }
            break;
        }
        if (idx != prevIdx) {
            lastDistToWp = Double.NaN;
            noProgressTicks = 0;
            localPath = null;
            localIdx = 0;
            localReplanCount = 0;
            localDigTargets = null;
            localPlaceTargets = null;
            placeTarget = null;
            placeTicks = 0;
            failedBlockers.clear();
        }
        if (idx >= waypoints.size()) {
            BlockPos last = waypoints.get(waypoints.size() - 1);
            fire(Status.ARRIVED, null, last, "reached final waypoint");
            return null;
        }

        // 当前目标服务端航点（含前瞻）
        BlockPos swp = waypoints.get(idx);
        if (idx + 1 < waypoints.size()
                && distToWpCenter(player, waypoints.get(idx)) < LOOKAHEAD_DIST) {
            swp = waypoints.get(idx + 1);
        }

        // 0) 挖穿子模式：服务端 dig 计划 / 本地 dig 列表 / 卡死脱困挖掘共用。
        //    M11.5 触达门控：目标超出 SUBMODE_REACH 时先沿路走近（挂起子模式），
        //    防止对远处方块空挥到 DIG_ABANDON_TICKS。
        if (!moveOnly && digTarget != null && distToWpCenter(player, digTarget.pos()) > SUBMODE_REACH) {
            digTarget = null;
            digTicks = 0;
        }
        if (!moveOnly && digTarget != null) {
            BlockPos digPos = digTarget.pos();
            if (player.getWorld().getBlockState(digPos).isAir()) {
                if (digTargets != null) digTargets.remove(digTarget);
                if (localDigTargets != null) localDigTargets.remove(digPos);
                digTarget = null;
                digTicks = 0;
            } else {
                digTicks++;
                // 规划器标注了工具 → 按工具切槽（先切再挖，防用错工具挖不动）；
                // 未标注（本地绕障挖穿/卡死脱困挖掘）→ 按方块挖掘 tag 自动选（M11.5 难点④）。
                selectDigTool(player, digTarget);
                aimAtBlock(player, digPos);
                if (digTicks > DIG_ABANDON_TICKS) {
                    fire(Status.BLOCKED_BREAKABLE, digPos, swp, "dig gave up after " + digTicks);
                    return null;
                }
                ActionCmd dig = new ActionCmd();
                dig.attack = true;
                return dig;
            }
        }

        // 1) 选当前目标：局部路径点 or 服务端航点
        BlockPos wp;
        boolean followingLocal = false;
        if (localPath != null && !localPath.isEmpty()) {
            // 越过已到达的局部点
            while (localIdx < localPath.size()
                    && distToWpCenter(player, localPath.get(localIdx)) < ARRIVE_DIST) {
                localIdx++;
            }
            if (localIdx >= localPath.size()) {
                localPath = null;
                localIdx = 0;
                localDigTargets = null;
                wp = swp;  // 局部路径完成 → 恢复服务端直走
            } else {
                wp = localPath.get(localIdx);
                followingLocal = true;
            }
        } else {
            wp = swp;
        }

        // 1.4) 预计算局部路径（V2 关键改进）：没有局部路径且距当前航点远 → 立即用
        // LocalPathfinder 规划到航点的整条绕障路径，沿 A* 路径走（不直线撞墙才绕）。
        // 限制频率：每 tick 算一次太贵（小范围 A* <1ms 可接受），且路径短时会反复重算。
        if (!followingLocal && localPath == null
                && distToWpCenter(player, swp) > ARRIVE_DIST + 2.0) {
            BlockPos feet = new BlockPos(player.getBlockX(), player.getBlockY(), player.getBlockZ());
            LocalPathfinder.LocalResult result = LocalPathfinder.findPath(
                    feet, swp, avoidCells, !moveOnly);
            if (result != null && result.points.size() > 1) {
                localPath = result.points;
                localIdx = 1;
                localDigTargets = result.digTargets;
                localPlaceTargets = result.placeTargets;
                if (pathDebugListener != null) pathDebugListener.accept(localPath);
                wp = localPath.get(localIdx);
                followingLocal = true;
            }
        }

        // 1.5) 本地路径推进前先挖穿计划方块（LocalPathfinder digTargets）
        if (!moveOnly && followingLocal && localDigTargets != null && !localDigTargets.isEmpty()) {
            BlockPos toDig = null;
            for (BlockPos d : localDigTargets) {
                if (!player.getWorld().getBlockState(d).isAir()) {
                    toDig = d;
                    break;
                }
            }
            if (toDig != null) {
                digTarget = new DigPlan(toDig, null, null);   // 本地计划无工具标注
                digTicks = 0;
                selectDigTool(player, digTarget);   // M11.6：首 tick 就切对工具（MELEE 下防"剑挖土"）
                aimAtBlock(player, toDig);
                ActionCmd dig = new ActionCmd();
                dig.attack = true;
                return dig;
            }
        }

        // 1.6) place 子模式（M11.5 难点③）：本地路径推进前先放置补落脚点。
        //      站定（不前进）→ 选泥土 → 瞄支撑（1 格深坑瞄下方块中心=命中其顶面；
        //      1 格宽沟瞄对侧支撑朝沟侧面偏下点）→ settle 后 use 脉冲 → 校验落脚格实心。
        if (!moveOnly && placeTarget != null) {
            if (BlockTraits.isGround(player.getWorld().getBlockState(placeTarget))) {
                if (localPlaceTargets != null) localPlaceTargets.remove(placeTarget);
                placeTarget = null;
                placeTicks = 0;
            } else {
                placeTicks++;
                if (placeTicks > PLACE_ABANDON_TICKS) {
                    fire(Status.BLOCKED_WALL, placeTarget, swp,
                            "place_step gave up after " + placeTicks);
                    return null;
                }
                ToolPolicy.selectToolCategory(player, "dirt");
                aimAtPlaceSupport(player, placeTarget);
                ActionCmd placeCmd = new ActionCmd();
                // settle ≥3 tick 等视角收敛（crosshair 渲染线程更新），之后每 5 tick 一次
                // use 脉冲（itemUseCooldown=4 防连放）
                placeCmd.use = placeTicks > 3 && placeTicks % 5 == 0;
                return placeCmd;
            }
        }
        if (!moveOnly && followingLocal && localPlaceTargets != null && !localPlaceTargets.isEmpty()) {
            BlockPos toPlace = null;
            for (BlockPos c : localPlaceTargets) {
                if (!BlockTraits.isGround(player.getWorld().getBlockState(c))) {
                    toPlace = c;
                    break;
                }
            }
            if (toPlace != null && distToWpCenter(player, toPlace) <= SUBMODE_REACH - 0.5) {
                placeTarget = toPlace;
                placeTicks = 0;
                ActionCmd stop = new ActionCmd();   // 站定进入放置（防走进坑里）
                return stop;
            }
        }

        double wx = wp.getX() + 0.5;
        double wy = wp.getY() + 0.5;
        double wz = wp.getZ() + 0.5;
        double px = player.getX();
        double py = player.getY();
        double pz = player.getZ();
        double dx = wx - px;
        double dy = wy - py;
        double dz = wz - pz;
        double h = Math.sqrt(dx * dx + dz * dz);
        double dist = Math.sqrt(dx * dx + dy * dy + dz * dz);

        // 2) 碰撞检测 → 局部绕障（M10：异步计算，不停步）
        if (!followingLocal) {
            BlockPos blocker = collisionToWaypoint(player, swp);
            if (blocker != null && !failedBlockers.contains(blocker)) {
                if (!moveOnly && digTargets != null) {
                    for (DigPlan dp : digTargets) {
                        if (dp.pos().equals(blocker)) {
                            digTarget = dp;   // 带工具标注的计划块
                            break;
                        }
                    }
                }
                if (digTarget == null) {   // 未在计划内 → 普通碰撞块（本地绕障/上报）
                    digTarget = null;
                }
                if (digTarget != null) {
                    digTicks = 0;
                    aimAtBlock(player, digTarget.pos());
                }
                if (isBreakable(player.getWorld().getBlockState(blocker))) {
                    fire(Status.BLOCKED_BREAKABLE, blocker, swp,
                            "unplanned breakable block: " + blocker);
                    return null;
                }
                if (localReplanCount < LOCAL_REPLAN_MAX) {
                    // 局部 A* 绕障碍（异步计算，本 tick 照常走路）
                    BlockPos feet = new BlockPos(player.getBlockX(), player.getBlockY(), player.getBlockZ());
                    LocalPathfinder.LocalResult result = LocalPathfinder.findPath(
                            feet, swp, avoidCells, !moveOnly);
                    localReplanCount++;
                    failedBlockers.add(blocker);
                    avoidCells.add(blocker);   // M11.5「换路」：下次重规划绕开失败走廊
                    if (result != null && result.points.size() > 1) {
                        localPath = result.points;
                        localIdx = 1;
                        localDigTargets = result.digTargets;
                        localPlaceTargets = result.placeTargets;
                        if (pathDebugListener != null) pathDebugListener.accept(localPath);
                    }
                    // 照常前进：不 return null
                }
                if (localReplanCount >= LOCAL_REPLAN_MAX) {
                    fire(Status.BLOCKED_WALL, blocker, swp,
                            "local replan exhausted (" + LOCAL_REPLAN_MAX + ")");
                    return null;
                }
            }
        }

        // 3) 卡死检测
        if (!Double.isNaN(lastDistToWp) && dist < lastDistToWp - 0.05) {
            noProgressTicks = 0;
        } else {
            noProgressTicks++;
        }
        lastDistToWp = dist;

        double dxz = Math.hypot(player.getX() - lastTickX, player.getZ() - lastTickZ);
        lastTickX = player.getX();
        lastTickZ = player.getZ();
        if (dxz < STUCK_XZ_EPS) {
            xzStillTicks++;
        } else {
            xzStillTicks = 0;
        }

        // XZ 不动或 3D 无进展 → 本地恢复链：重规划后直接上报 STUCK。
        // 导航不再用跳跃/乱挖突破高度；受控任务由上层发 pillar_up，再由
        // PillarExecutor 在脚底正下方垫方块登高。
        if ((xzStillTicks >= STUCK_XZ_TICKS || noProgressTicks >= STUCK_TICKS)
                && dist > STUCK_MIN_PROGRESS) {
            if (localReplanCount < LOCAL_REPLAN_MAX) {
                BlockPos feet = new BlockPos(player.getBlockX(), player.getBlockY(), player.getBlockZ());
                avoidCells.add(feet);   // M11.5「换路」：卡死脚位加入避让集
                LocalPathfinder.LocalResult result = LocalPathfinder.findPath(
                        feet, swp, avoidCells, !moveOnly);
                localReplanCount++;
                xzStillTicks = 0;
                noProgressTicks = 0;
                if (result != null && result.points.size() > 1) {
                    localPath = result.points;
                    localIdx = 1;
                    localDigTargets = result.digTargets;
                    localPlaceTargets = result.placeTargets;
                    if (pathDebugListener != null) pathDebugListener.accept(localPath);
                }
            }
            if (localReplanCount >= LOCAL_REPLAN_MAX) {
                fire(Status.STUCK,
                        new BlockPos(player.getBlockX(), player.getBlockY(), player.getBlockZ()),
                        swp, "local replan exhausted (" + LOCAL_REPLAN_MAX + ")");
                return null;
            }
        }

        // 4) 相机目标
        double targetYaw = Math.toDegrees(Math.atan2(-dx, dz));
        double targetPitch = h > 1e-6 ? Math.toDegrees(Math.atan2(-dy, h)) : 0.0;
        if (targetPitch > PITCH_CLAMP) targetPitch = PITCH_CLAMP;
        else if (targetPitch < -PITCH_CLAMP) targetPitch = -PITCH_CLAMP;
        VlaClient.getInstance().setCameraTarget(targetYaw, targetPitch);

        // 5) 移动动作。受控平原任务禁止导航跳跃/跳挖；高目标统一交由
        // PillarExecutor 垫方块到合适高度后再采集。
        ActionCmd cmd = new ActionCmd();
        cmd.forward = true;
        double curYaw = player.getYaw();
        double yawErr = MathHelper.wrapDegrees(targetYaw - curYaw);
        if (yawErr < -STRAFE_DEG) cmd.left = true;
        else if (yawErr > STRAFE_DEG) cmd.right = true;
        // 远距冲刺（M11.5→M11.6）：追击逃跑实体/赶路时人类会疾跑——不冲刺永远追不上
        // 受惊的猪（惊逃 1.25× > 步行 0.28）。近距/大转向不冲（冲过头）。
        // M11.6 改为 latch + 死区：h>5 && |yawErr|<15 开启，h<3 || |yawErr|>30 熄灭，
        // 中间区间保持现状——旧版每 tick 硬阈值重算，跨航点/局部路径点频繁开关冲刺。
        double absYawErr = Math.abs(yawErr);
        if (sprintLatched) {
            if (h < SPRINT_OFF_DIST || absYawErr > SPRINT_OFF_YAW) {
                sprintLatched = false;
            }
        } else if (h > SPRINT_ON_DIST && absYawErr < SPRINT_ON_YAW) {
            sprintLatched = true;
        }
        cmd.sprint = sprintLatched;
        cmd.jump = false;
        return cmd;
    }

    /** 碰撞检测：朝目标航点方向 1.0/2.0/3.0 米三条射线，返回最近实心块（台阶不误报）。 */
    private BlockPos collisionToWaypoint(ClientPlayerEntity player, BlockPos wp) {
        double tx = wp.getX() + 0.5;
        double tz = wp.getZ() + 0.5;
        double dx = tx - player.getX();
        double dz = tz - player.getZ();
        double h = Math.sqrt(dx * dx + dz * dz);
        if (h < 1e-6) return null;
        double fx = dx / h;
        double fz = dz / h;
        for (double ahead : LOOK_AHEADS) {
            BlockPos b = sampleColumn(player, fx, fz, ahead);
            if (b != null) {
                return b;
            }
        }
        return null;
    }

    /**
     * 玩家前方 {@code ahead} 米处脚格/头格采样：双脚实心返回脚格，仅头实心返回头格。
     * 脚下有 1 格台（前方脚格实心但脚下即地面）不算墙；受控平原中这类台阶交由上层
     * 重新规划或 pillar_up 处理，不以导航跳跃跨越。
     */
    private BlockPos sampleColumn(ClientPlayerEntity player, double fx, double fz, double ahead) {
        double px = player.getX();
        double py = player.getY();
        double pz = player.getZ();
        int bx = (int) Math.floor(px + fx * ahead);
        int by = (int) Math.floor(py);
        int bz = (int) Math.floor(pz + fz * ahead);
        BlockPos feetPos = new BlockPos(bx, by, bz);
        BlockPos headPos = new BlockPos(bx, by + 1, bz);
        boolean feetSolid = isSolid(player.getWorld().getBlockState(feetPos));
        boolean headSolid = isSolid(player.getWorld().getBlockState(headPos));
        if (feetSolid && headSolid) return feetPos;
        if (!feetSolid && headSolid) return headPos;
        return null;
    }

    private static void aimAtBlock(ClientPlayerEntity player, BlockPos pos) {
        // 经 Aim 计算：目标块与玩家同一列（正上方的头顶块、正下方的脚下块）时
        // pitch 走 ±90 而不是退化成平视——见 Aim 类注释（老代码此处 h≈0 时给 0.0，
        // 导致「挖头顶」永远瞄不中）。
        double[] yp = Aim.atBlockCenter(player, pos);
        VlaClient.getInstance().setCameraTarget(yp[0], yp[1]);
    }

    /**
     * M11.6：按 dig 计划选择挖掘工具——规划器标注的工具优先（selectToolSlot），
     * 未标注则按方块挖掘 tag 自动选（ToolPolicy.selectToolCategory）。
     *
     * <p>在 <b>digTarget 首次设置的同一 tick</b> 调用（section 1.5 本地挖穿 / 卡死脱困
     * 挖掘），否则 attack 已由上一 tick 的 MELEE 持剑发出——kill 追击时"用剑挖泥土"
     * 正是第一下剑挥在土上（MELEE 全程持剑，digTarget 设好前手里是剑）。
     */
    private void selectDigTool(ClientPlayerEntity player, DigPlan plan) {
        if (plan.tool() != null) {
            selectToolSlot(player, plan.tool());
            return;
        }
        String category = BlockTraits.toolFor(player.getWorld().getBlockState(plan.pos()));
        if (category != null) {
            ToolPolicy.selectToolCategory(player, category);
        }
    }

    /**
     * 规划器标注的工具 → 在快捷栏 0-8 找对应槽位并选中（仅当未手持时切换）。
     * 工具为注册名如 {@code minecraft:diamond_pickaxe}；找不到或已在手则跳过。
     */
    private void selectToolSlot(ClientPlayerEntity player, String tool) {
        try {
            net.minecraft.item.ItemStack held = player.getMainHandStack();
            if (!held.isEmpty()
                    && net.minecraft.registry.Registries.ITEM.getId(held.getItem()).toString().equals(tool)) {
                return;   // 已在手
            }
            for (int i = 0; i < 9; i++) {
                net.minecraft.item.ItemStack stack = player.getInventory().getStack(i);
                if (!stack.isEmpty()
                        && net.minecraft.registry.Registries.ITEM.getId(stack.getItem()).toString().equals(tool)) {
                    player.getInventory().selectedSlot = i;
                    break;
                }
            }
        } catch (Exception e) {
            // 工具名非法/热键缺失：忽略，客户端按方块自动判断
        }
    }

    /**
     * 瞄准 place_step 落脚格的可放置支撑面（M11.5）：
     * 下方实心（1 格深坑）→ 瞄下方块**中心**（自上而下命中其顶面，放置恰落坑格）；
     * 否则找对侧同层实心支撑（1 格宽沟）→ 瞄其**朝沟侧面偏下点**（瞄中心会命中顶面，
     * 把方块放到支撑上方的错误格）。都没有 → 瞄格中心兜底（多半失败，靠放弃阈值退出）。
     */
    private void aimAtPlaceSupport(ClientPlayerEntity player, BlockPos gap) {
        BlockPos below = gap.down();
        if (BlockTraits.isGround(player.getWorld().getBlockState(below))) {
            double[] yp = Aim.atBlockCenter(player, below);
            VlaClient.getInstance().setCameraTarget(yp[0], yp[1]);
            return;
        }
        for (int[] d : new int[][]{{1, 0}, {-1, 0}, {0, 1}, {0, -1}}) {
            BlockPos far = gap.add(d[0], 0, d[1]);
            if (BlockTraits.isGround(player.getWorld().getBlockState(far))) {
                // 支撑块朝沟的侧面上的点：从中心向沟方向推 0.51、纵向取面中偏下（+0.3）
                double tx = far.getX() + 0.5 - d[0] * 0.51;
                double ty = far.getY() + 0.3;
                double tz = far.getZ() + 0.5 - d[1] * 0.51;
                double[] yp = Aim.atPoint(player, tx, ty, tz);
                VlaClient.getInstance().setCameraTarget(yp[0], yp[1]);
                return;
            }
        }
        double[] yp = Aim.atBlockCenter(player, gap);
        VlaClient.getInstance().setCameraTarget(yp[0], yp[1]);
    }

    private static boolean isSolid(BlockState state) {
        if (state == null || state.isAir()) return false;
        return state.isSolid();
    }

    private static boolean isBreakable(BlockState state) {
        return state != null && state.getBlock().getHardness() >= 0.0f;
    }

    private double distToWpCenter(ClientPlayerEntity player, BlockPos wp) {
        double dx = wp.getX() + 0.5 - player.getX();
        double dy = wp.getY() + 0.5 - player.getY();
        double dz = wp.getZ() + 0.5 - player.getZ();
        return Math.sqrt(dx * dx + dy * dy + dz * dz);
    }

    /** 两航点中心距（越段推进判据用）。 */
    private static double wpDistance(BlockPos a, BlockPos b) {
        double dx = a.getX() - b.getX();
        double dy = a.getY() - b.getY();
        double dz = a.getZ() - b.getZ();
        return Math.sqrt(dx * dx + dy * dy + dz * dz);
    }

    private void fire(Status status, BlockPos pos, BlockPos wp, String detail) {
        this.active = false;
        this.localPath = null;
        this.localIdx = 0;
        this.localReplanCount = 0;
        this.localDigTargets = null;
        this.localPlaceTargets = null;
        this.placeTarget = null;
        this.placeTicks = 0;
        if (statusListener != null) {
            statusListener.accept(new StatusEvent(status, pos, wp, detail));
        }
    }
}
