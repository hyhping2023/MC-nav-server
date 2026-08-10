package dev.vla.client.nav;

import net.minecraft.client.network.ClientPlayerEntity;
import net.minecraft.util.math.BlockPos;
import net.minecraft.util.math.MathHelper;

/**
 * 瞄准角计算（yaw/pitch）——全客户端唯一实现。
 *
 * <p>存在的唯一原因是修一个曾经三处同源的 bug：老代码统一写
 * {@code pitch = h > 1e-6 ? toDegrees(atan2(-dy, h)) : 0.0}，当目标块与玩家**同一列**
 * （正上方的头顶块、正下方的垫块落点）时 {@code h == 0} → pitch 被算成 0（平视），
 * 于是「挖头顶」和「朝正下垫方块」这两个动作永远瞄不中。正确行为是 h≈0 时按 dy
 * 符号直接给 ∓90：
 *
 * <pre>
 *   MC pitch 约定：-90 = 正上方，0 = 平视，+90 = 正下方
 *   dy &gt; 0（目标在眼睛上方） → pitch = -90
 *   dy &lt; 0（目标在眼睛下方） → pitch = +90
 * </pre>
 *
 * <p>纯静态工具，无状态，客户端线程调用。
 */
public final class Aim {

    /** 正下方 / 正上方 pitch 常量（MC 约定）。 */
    public static final double PITCH_DOWN = 90.0;
    public static final double PITCH_UP = -90.0;

    /** 水平距离小于此值视为「同一列」，pitch 走 ±90 分支而非 atan2。 */
    private static final double H_EPS = 1e-6;

    private Aim() {
    }

    /** 从 (px,pz) 朝 (tx,tz) 的 yaw（度，MC 约定）。同点时返回 0。 */
    public static double yaw(double px, double pz, double tx, double tz) {
        double dx = tx - px;
        double dz = tz - pz;
        if (Math.abs(dx) < H_EPS && Math.abs(dz) < H_EPS) {
            return 0.0;
        }
        return Math.toDegrees(Math.atan2(-dx, dz));
    }

    /**
     * 从眼位 (px,eyeY,pz) 朝 (tx,ty,tz) 的 pitch（度，MC 约定，已夹紧 ±90）。
     *
     * <p>水平距离 ≈0 时不退化成平视，按 dy 符号给 ∓90 —— 见类注释。
     */
    public static double pitch(double px, double eyeY, double pz, double tx, double ty, double tz) {
        double dx = tx - px;
        double dy = ty - eyeY;
        double dz = tz - pz;
        double h = Math.sqrt(dx * dx + dz * dz);
        if (h < H_EPS) {
            if (Math.abs(dy) < H_EPS) {
                return 0.0;  // 目标就在眼位上，无方向可言
            }
            return dy > 0 ? PITCH_UP : PITCH_DOWN;
        }
        return MathHelper.clamp(Math.toDegrees(Math.atan2(-dy, h)), PITCH_UP, PITCH_DOWN);
    }

    /**
     * 瞄准 {@code pos} 方块中心所需的 {@code {yaw, pitch}}（用玩家自身眼位算，
     * 消除服务端 pos 滞后的瞄准偏差）。调用方自行喂给 VlaClient#setCameraTarget。
     */
    public static double[] atBlockCenter(ClientPlayerEntity player, BlockPos pos) {
        return atPoint(player, pos.getX() + 0.5, pos.getY() + 0.5, pos.getZ() + 0.5);
    }

    /**
     * 瞄准世界坐标点 {@code (tx,ty,tz)} 所需的 {@code {yaw, pitch}}（M11.5：
     * place_step 需要瞄支撑块的**指定面上的点**而非中心——瞄中心会命中顶面，
     * 放到错误格）。
     */
    public static double[] atPoint(ClientPlayerEntity player, double tx, double ty, double tz) {
        double px = player.getX();
        double pz = player.getZ();
        double eyeY = player.getEyePos().getY();
        return new double[]{yaw(px, pz, tx, tz), pitch(px, eyeY, pz, tx, ty, tz)};
    }
}
