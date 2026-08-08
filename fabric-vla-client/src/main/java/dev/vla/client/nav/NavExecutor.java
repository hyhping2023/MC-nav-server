package dev.vla.client.nav;

import dev.vla.client.VlaClient;
import dev.vla.client.input.ActionCmd;
import java.util.ArrayList;
import java.util.List;
import java.util.function.Consumer;
import net.minecraft.block.BlockState;
import net.minecraft.client.network.ClientPlayerEntity;
import net.minecraft.util.math.BlockPos;
import net.minecraft.util.math.MathHelper;

/**
 * 本地路径跟随控制器（M9.3）：服务端/Python 下发航点后，逐 tick 自行转向+前进+侧移，
 * 做碰撞箱检测、到达判定、卡死检测，并经 WS 上报状态。**不做全局规划**（全局 A* 在服务端）。
 *
 * <p>线程模型：setPath/cancel 由 WS 线程经 {@code client.execute} 调度到客户端线程；
 * tick 由 END_CLIENT_TICK 在客户端线程调用——全部方法都在客户端线程，方法级 synchronized 兜底。
 *
 * <p>状态机：活跃时逐 tick 驱动移动；发出 arrived / blocked_* / stuck 后暂停（active=false，
 * 等 Python 发 goto_cancel 或新 goto_path 接管）。
 */
public final class NavExecutor {

    /** 到航点格子中心 3D 距离 < 此值 → 到达（推进下一航点）。 */
    private static final double ARRIVE_DIST = 0.8;
    /** |yaw 误差| 超过此值 → 前进同时侧移朝目标。 */
    private static final double STRAFE_DEG = 10.0;
    /** 碰撞采样距离（沿玩家面向，米）。 */
    private static final double LOOK_AHEAD = 1.0;
    /** 卡死窗口（tick）。 */
    private static final int STUCK_TICKS = 30;
    /** 卡死判定：窗口内到当前航点距离未缩短的最小值（米）。 */
    private static final double STUCK_MIN_PROGRESS = 0.2;
    /** 导航视角 pitch 夹紧（平视前进，不低头看脚下航点）。 */
    private static final double PITCH_CLAMP = 20.0;

    /** 上报状态。 */
    public enum Status { ARRIVED, BLOCKED_BREAKABLE, BLOCKED_WALL, STUCK }

    /** 状态事件：status、相关坐标（pos=阻挡块/玩家位置，wp=当前航点）、detail。 */
    public record StatusEvent(Status status, BlockPos pos, BlockPos wp, String detail) {
    }

    private final Consumer<StatusEvent> statusListener;
    private List<BlockPos> waypoints;
    private int idx = 0;
    private boolean active = false;
    private double lastDistToWp = Double.NaN;
    private int noProgressTicks = 0;

    public NavExecutor(Consumer<StatusEvent> statusListener) {
        this.statusListener = statusListener;
    }

    /** 设置新导航路径（替换当前；空路径直接结束）。 */
    public synchronized void setPath(List<BlockPos> path) {
        this.waypoints = path == null ? null : new ArrayList<>(path);
        this.idx = 0;
        this.active = this.waypoints != null && !this.waypoints.isEmpty();
        this.lastDistToWp = Double.NaN;
        this.noProgressTicks = 0;
    }

    /** 取消导航（释放移动由调用方清 currentAction）。 */
    public synchronized void cancel() {
        this.active = false;
        this.waypoints = null;
        this.idx = 0;
        this.lastDistToWp = Double.NaN;
        this.noProgressTicks = 0;
    }

    public synchronized boolean isActive() {
        return active;
    }

    /**
     * 客户端线程每 tick 调用：推进/转向/碰撞/卡死检测，返回本 tick 应注入的移动动作
     * （null = 导航已结束/应停止移动）。调用方把返回的动作写入 currentAction。
     */
    public synchronized ActionCmd tick(ClientPlayerEntity player) {
        if (!active || waypoints == null || player == null) {
            return null;
        }
        // 越过已到达的航点
        while (idx < waypoints.size() && distToWpCenter(player, waypoints.get(idx)) < ARRIVE_DIST) {
            idx++;
        }
        if (idx >= waypoints.size()) {
            BlockPos last = waypoints.get(waypoints.size() - 1);
            fire(Status.ARRIVED, null, last, "reached final waypoint");
            return null;
        }
        BlockPos wp = waypoints.get(idx);
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

        // 相机目标：客户端用自身脚位算精确朝向（复用平滑转向）
        double targetYaw = Math.toDegrees(Math.atan2(-dx, dz));
        double targetPitch = h > 1e-6 ? Math.toDegrees(Math.atan2(-dy, h)) : 0.0;
        if (targetPitch > PITCH_CLAMP) {
            targetPitch = PITCH_CLAMP;
        } else if (targetPitch < -PITCH_CLAMP) {
            targetPitch = -PITCH_CLAMP;
        }
        VlaClient.getInstance().setCameraTarget(targetYaw, targetPitch);

        // 卡死检测：到当前航点距离在窗口内未缩短
        double dist = Math.sqrt(dx * dx + dy * dy + dz * dz);
        if (!Double.isNaN(lastDistToWp) && dist < lastDistToWp - 0.05) {
            noProgressTicks = 0;
        } else {
            noProgressTicks++;
        }
        lastDistToWp = dist;
        if (noProgressTicks >= STUCK_TICKS && dist > STUCK_MIN_PROGRESS) {
            fire(Status.STUCK, new BlockPos(player.getBlockX(), player.getBlockY(), player.getBlockZ()),
                    wp, "no progress for " + STUCK_TICKS + " ticks");
            return null;
        }

        // 碰撞箱检测：沿面向 LOOK_AHEAD 采样脚格+头格
        Status blocked = collisionAhead(player, LOOK_AHEAD);
        if (blocked != null) {
            double fx = -Math.sin(Math.toRadians(player.getYaw()));
            double fz = Math.cos(Math.toRadians(player.getYaw()));
            BlockPos hit = new BlockPos(
                    (int) Math.floor(px + fx * LOOK_AHEAD),
                    (int) Math.floor(py),
                    (int) Math.floor(pz + fz * LOOK_AHEAD));
            fire(blocked, hit, wp, "blocked ahead");
            return null;
        }

        // 移动动作：前进 + 侧移朝目标 + 上台阶跳
        ActionCmd cmd = new ActionCmd();
        cmd.forward = true;
        double curYaw = player.getYaw();
        double yawErr = MathHelper.wrapDegrees(targetYaw - curYaw);
        if (yawErr < -STRAFE_DEG) {
            cmd.left = true;    // 目标在更低 yaw（玩家左侧）
        } else if (yawErr > STRAFE_DEG) {
            cmd.right = true;
        }
        cmd.jump = (wp.getY() + 0.5) > py + 0.5;
        return cmd;
    }

    /**
     * 沿玩家面向在 LOOK_AHEAD 处采样脚格与头格（2 格身高 hitbox）：
     * 脚实心+头实心 → 挡（不可通过）；脚空气+头实心 → 挡（头顶过不去）；
     * 脚实心+头空气 → 1 格台阶，自动上（不算挡）。返回 blocked 状态或 null。
     */
    private Status collisionAhead(ClientPlayerEntity player, double ahead) {
        double fx = -Math.sin(Math.toRadians(player.getYaw()));
        double fz = Math.cos(Math.toRadians(player.getYaw()));
        double px = player.getX();
        double py = player.getY();
        double pz = player.getZ();
        int bx = (int) Math.floor(px + fx * ahead);
        int by = (int) Math.floor(py);
        int bz = (int) Math.floor(pz + fz * ahead);
        BlockState feet = player.getWorld().getBlockState(new BlockPos(bx, by, bz));
        BlockState head = player.getWorld().getBlockState(new BlockPos(bx, by + 1, bz));
        boolean feetSolid = isSolid(feet);
        boolean headSolid = isSolid(head);
        if (feetSolid && headSolid) {
            return isBreakable(feet) ? Status.BLOCKED_BREAKABLE : Status.BLOCKED_WALL;
        }
        if (!feetSolid && headSolid) {
            return isBreakable(head) ? Status.BLOCKED_BREAKABLE : Status.BLOCKED_WALL;
        }
        return null; // 空气或 1 格台阶 → 可通过
    }

    private static boolean isSolid(BlockState state) {
        if (state == null || state.isAir()) {
            return false;
        }
        return state.isSolid();
    }

    /** 可挖判定：实心且硬度 >= 0（基岩/屏障等硬度 <0 不可挖 → 墙）。 */
    private static boolean isBreakable(BlockState state) {
        return state != null && state.getBlock().getHardness() >= 0.0f;
    }

    private double distToWpCenter(ClientPlayerEntity player, BlockPos wp) {
        double dx = wp.getX() + 0.5 - player.getX();
        double dy = wp.getY() + 0.5 - player.getY();
        double dz = wp.getZ() + 0.5 - player.getZ();
        return Math.sqrt(dx * dx + dy * dy + dz * dz);
    }

    private void fire(Status status, BlockPos pos, BlockPos wp, String detail) {
        this.active = false; // 上报后暂停，等 Python 接管
        if (statusListener != null) {
            try {
                statusListener.accept(new StatusEvent(status, pos, wp, detail));
            } catch (Exception e) {
                System.err.println("[NavExecutor] status listener failed: " + e);
            }
        }
    }
}
