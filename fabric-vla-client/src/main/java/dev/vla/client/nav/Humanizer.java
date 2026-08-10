package dev.vla.client.nav;

import dev.vla.client.input.ActionCmd;
import java.util.Random;
import net.minecraft.client.network.ClientPlayerEntity;

/**
 * 人类化整形滤波器（M11.5，DESIGN.md §17.2）。
 *
 * <p>作用在 <b>NavExecutor 输出</b>的 ActionCmd 上，让实际注入（并被帧头按键采样记录）
 * 的按键呈现人类节奏。三条规则：
 * <ul>
 *   <li><b>步态微松</b>：forward 连续按住 30-60 tick 后释放 2-3 tick（同时松 sprint）——
 *       人类不会几百 tick 纹丝不动地压着 W。</li>
 *   <li><b>挖掘节奏</b>：attack 连续按住 40-80 tick 后释放 2 tick——kit 方块（石头/泥土
 *       + 钻石工具）6-8 tick 一块，微松只落在连续多块挖掘的间隙，单块进度损失可忽略。</li>
 *   <li><b>镜头微漂</b>：非挖掘期 5% 概率 ±0.3° 抖动（挖掘瞄准期不抖，防脱靶）。</li>
 * </ul>
 *
 * <p><b>不整形的对象</b>：外部 VLA/Python 直发的 {@code action}（模型输出必须原样执行）；
 * {@code PillarExecutor} 输出（放置窗口只有跳跃第 3-8 tick，整形会打断技能时序，§5.7）。
 *
 * <p>随机源由 WS {@code set_humanize {enabled, seed}} 配置——同 seed 整形序列可复现
 * （录制数据可回放）。全部状态仅在客户端 tick 线程访问。
 */
public final class Humanizer {

    private static final int GAIT_HOLD_MIN = 30;
    private static final int GAIT_HOLD_MAX = 60;
    private static final int GAIT_RELEASE_MIN = 2;
    private static final int GAIT_RELEASE_MAX = 3;
    private static final int DIG_HOLD_MIN = 40;
    private static final int DIG_HOLD_MAX = 80;
    private static final int DIG_RELEASE = 2;
    private static final double DRIFT_PROB = 0.05;
    private static final double DRIFT_DEG = 0.3;

    private boolean enabled = false;
    private Random rng = new Random(0);

    // 步态状态
    private int forwardHeld = 0;
    private int forwardReleaseLeft = 0;
    private int nextGaitHold = 45;

    // 挖掘节奏状态
    private int attackHeld = 0;
    private int attackReleaseLeft = 0;
    private int nextDigHold = 60;

    /** WS set_humanize：开关 + 重置随机源（同 seed 可复现）。 */
    public void configure(boolean enabled, long seed) {
        this.enabled = enabled;
        this.rng = new Random(seed);
        this.forwardHeld = 0;
        this.forwardReleaseLeft = 0;
        this.nextGaitHold = pick(GAIT_HOLD_MIN, GAIT_HOLD_MAX);
        this.attackHeld = 0;
        this.attackReleaseLeft = 0;
        this.nextDigHold = pick(DIG_HOLD_MIN, DIG_HOLD_MAX);
    }

    public boolean isEnabled() {
        return enabled;
    }

    /**
     * 对执行器输出做整形（客户端 tick 线程，每 tick 一次）。就地修改并返回同一实例
     * （NavExecutor 每 tick 新建 ActionCmd，无共享）；null 原样返回。
     */
    public ActionCmd shape(ActionCmd cmd) {
        if (!enabled || cmd == null) {
            return cmd;
        }
        // 步态微松
        if (cmd.forward) {
            if (forwardReleaseLeft > 0) {
                forwardReleaseLeft--;
                cmd.forward = false;
                cmd.sprint = false;
            } else if (++forwardHeld >= nextGaitHold) {
                forwardHeld = 0;
                nextGaitHold = pick(GAIT_HOLD_MIN, GAIT_HOLD_MAX);
                forwardReleaseLeft = pick(GAIT_RELEASE_MIN, GAIT_RELEASE_MAX);
            }
        } else {
            forwardHeld = 0;
            forwardReleaseLeft = 0;
        }
        // 挖掘节奏
        if (cmd.attack) {
            if (attackReleaseLeft > 0) {
                attackReleaseLeft--;
                cmd.attack = false;
            } else if (++attackHeld >= nextDigHold) {
                attackHeld = 0;
                nextDigHold = pick(DIG_HOLD_MIN, DIG_HOLD_MAX);
                attackReleaseLeft = DIG_RELEASE;
            }
        } else {
            attackHeld = 0;
            attackReleaseLeft = 0;
        }
        return cmd;
    }

    /**
     * 镜头微漂（视角收敛后调用；{@code digging}=true 时不抖，防挖掘脱靶）。
     * 直接作用于玩家视角——帧头 yaw/pitch delta 会如实记录这份「人类手抖」。
     */
    public void cameraDrift(ClientPlayerEntity player, boolean digging) {
        if (!enabled || digging || player == null || rng.nextDouble() >= DRIFT_PROB) {
            return;
        }
        float dyaw = (float) ((rng.nextDouble() * 2 - 1) * DRIFT_DEG);
        float dpitch = (float) ((rng.nextDouble() * 2 - 1) * DRIFT_DEG * 0.5);
        player.setYaw(player.getYaw() + dyaw);
        player.setPitch(Math.max(-90f, Math.min(90f, player.getPitch() + dpitch)));
    }

    private int pick(int lo, int hi) {
        return lo + rng.nextInt(hi - lo + 1);
    }
}
