package dev.vla.client.nav;

import dev.vla.client.VlaClient;
import dev.vla.client.input.ActionCmd;
import java.util.function.Consumer;
import net.minecraft.block.BlockState;
import net.minecraft.block.Blocks;
import net.minecraft.client.network.ClientPlayerEntity;
import net.minecraft.item.BlockItem;
import net.minecraft.item.ItemStack;
import net.minecraft.registry.Registries;
import net.minecraft.util.math.BlockPos;
import net.minecraft.util.math.Vec3d;

/**
 * 垫方块爬高技能（pillar-up，M11）——客户端逐 tick 执行器。
 *
 * <p>动作序列就是人类的垫楼方式：<b>挖掉头顶 → 视角朝正下 → 原地跳 → 到顶点时放一块 →
 * 落到刚放的块上 → 重复</b>。每轮净升 1 格。
 *
 * <h2>为什么必须在客户端做</h2>
 * Python 侧一个 step = 2 服务端 tick + 往返延迟 ≈ 5–10 tick，而跳跃的可放置窗口只有
 * 6 tick（见下），且 {@code use} 是电平保持字段——Python 无法把放置对齐到顶点。老的
 * {@code collect_wood_agent.place_climb} 手写 FSM 就是因此不稳定（外加挖错格子、pitch
 * 符号写反两个 bug）。
 *
 * <h2>三条硬约束（1.20.1 字节码 + 跳跃积分核实）</h2>
 * <ol>
 *   <li><b>只需挖 {@code fy+2}，不必挖 {@code fy+3}。</b> 玩家高 1.8，脚格 {@code fy}、
 *       头格 {@code fy+1} 是自身碰撞箱。{@code fy+2} 有方块 → 天花板 {@code fy+2.0}
 *       → 最大 Δy = 2.0−1.8 = 0.2，永远垫不上去；{@code fy+3} 有方块 → 最大 Δy = 1.2，
 *       仍 &gt; 1.0，够用（撞头时 vy 被碰撞清零，正好落进放置判据）。</li>
 *   <li><b>放置窗口 = 跳跃第 3~8 tick。</b> v₀=0.42、v ← (v−0.08)×0.98 积分得
 *       Δy = .420/.753/<b>1.001</b>/1.166/1.249/<b>1.252</b>/1.177/1.024/.797。
 *       Δy &gt; 1.0 时脚格上移一格，目标格 {@code (bx,fy,bz)} 空出且不与碰撞箱相交
 *       —— {@code World#canPlace} → {@code isSpaceEmpty} 是严格判交，服务端也要过
 *       这一关。顶点 tick 6（Δy=1.252）余量最大。</li>
 *   <li><b>不数 tick，测位移。</b> 判据 {@code Δy ≥ 1.05 && vy ≤ 0.02} 恰好命中顶点
 *       前后，且天然抗延迟/抗撞头/抗跳跃增益。</li>
 * </ol>
 *
 * <h2>时序（{@code MinecraftClient.tick()} 字节码顺序核实）</h2>
 * {@code itemUseCooldown--} → {@code handleInputEvents()}（消费 use/attack）→
 * {@code GameRenderer.tick()} → {@code ClientWorld.tickEntities()}（玩家跳跃/移动）→
 * {@code END_CLIENT_TICK}（本类运行）。所以本 tick 置的 {@code use} 在下一 tick 的
 * {@code handleInputEvents} 被消费，此时玩家仍在本 tick 观测到的位置（tickEntities 还没跑）
 * —— 顶点判据成立即放置成功。
 *
 * <p>{@code handleInputEvents} 中 use 有两条路径：{@code while(wasPressed())} 与
 * {@code if(isPressed() && itemUseCooldown==0)}。{@link dev.vla.client.input.ActionApplier}
 * 用的 {@code setPressed} 走第二条，能触发放置；放置后 {@code itemUseCooldown=4}
 * 保证一次跳跃只放一块，所以本类每跳只脉冲一拍 {@code use}。
 *
 * <p>{@code crosshairTarget} 在渲染线程 {@code GameRenderer.renderWorld} 更新（逐帧），
 * 对 END_CLIENT_TICK 而言最多陈旧 1 帧。正下方射线在 1 格宽的列内、对
 * Δy ∈ (0,2) 命中同一格，故垫方块不受这个陈旧性影响；但**视角必须先收敛再跳**
 * （SETTLE 相位），否则准星还停在转向前的方向。
 *
 * <p>线程模型：全部方法在客户端线程，方法级 synchronized 兜底（同 NavExecutor）。
 */
public final class PillarExecutor {

    // ---- 放置判据（见类注释约束 3） ----
    /** 放置所需最小上升高度（格）。1.0 是脚格换格的临界，留 0.05 余量抗浮点/延迟。 */
    private static final double PLACE_MIN_DY = 1.05;
    /** 放置所需最大竖直速度：顶点附近才放（tick 5 vy=.003、tick 6 vy=-.075）。 */
    private static final double PLACE_MAX_VY = 0.02;

    // ---- SETTLE 判据 ----
    /** 起跳前水平速度上限（格/tick）：有残余速度会飘出方块列，垫歪后踩空。 */
    private static final double SETTLE_MAX_HSPEED = 0.01;
    /** 起跳前视角收敛判据（度）。 */
    private static final double SETTLE_PITCH_EPS = 1.0;
    /** 视角收敛后仍等待的 tick 数：给渲染线程至少一帧刷新 crosshairTarget。 */
    private static final int SETTLE_TICKS = 2;
    /** 判定「脚踩满方块」的容差：{@code getY() - feetY} 超过此值即半砖/雪层/矮墙。 */
    private static final double FULL_BLOCK_EPS = 0.01;
    /** SETTLE 相位超时（冰面/流水里水平速度永不衰减 → 放弃）。 */
    private static final int SETTLE_TIMEOUT = 40;

    // ---- 超时/重试 ----
    /** 挖头顶单块超时（tick）。 */
    private static final int DIG_TIMEOUT = 120;
    /** 单次跳跃的空中最大 tick 数（正常 ~10；超时按落地处理）。 */
    private static final int AIR_TIMEOUT = 30;
    /** 切槽等待 tick 数（hotbar 由 wasPressed 消费，下一 tick 才生效）。 */
    private static final int EQUIP_WAIT = 3;
    /** 同一层连续放置失败上限（超过 → FAILED）。 */
    private static final int MAX_ATTEMPTS = 4;
    /** 整个技能的总 tick 预算（防任何未预料的活锁）。 */
    private static final int TOTAL_TIMEOUT = 1200;

    /** 上报状态。 */
    public enum Status {
        /** 达到目标高度 / 放满 maxBlocks。 */
        DONE,
        /** 放成一块（每块一次，供 Python 打逐帧标签）。 */
        PROGRESS,
        /** 头顶不可挖 / 无方块 / 落水 / 反复放置失败 / 超时。 */
        FAILED,
        /** 外部取消。 */
        CANCELLED,
    }

    /** 失败细分原因（Python 侧据此选择兜底策略，勿改字面量——已进数据集标签）。 */
    public static final String R_HEAD_BLOCKED = "head_blocked";
    public static final String R_NO_BLOCK_ITEM = "no_block_item";
    public static final String R_OUT_OF_BLOCKS = "out_of_blocks";
    public static final String R_IN_FLUID = "in_fluid";
    public static final String R_NO_SETTLE = "no_settle";
    public static final String R_UNEVEN_GROUND = "uneven_ground";
    public static final String R_PLACE_FAILED = "place_failed";
    public static final String R_DIG_TIMEOUT = "dig_timeout";
    public static final String R_TIMEOUT = "timeout";

    public record StatusEvent(Status status, int placed, int feetY, String reason, String detail) {}

    /** 内部相位。 */
    private enum Phase { CLEAR_HEAD, EQUIP, SETTLE, JUMP, AIRBORNE, VERIFY }

    private final Consumer<StatusEvent> statusListener;

    private boolean active = false;
    private Phase phase = Phase.CLEAR_HEAD;

    // ---- 参数 ----
    /** 目标高度：脚格 Y ≥ 此值即完成（Integer.MIN_VALUE = 不按高度停）。 */
    private int targetY = Integer.MIN_VALUE;
    private int maxBlocks = 8;
    /** 期望材料的注册名（null = 任意可放置方块）。 */
    private String wantItem;

    // ---- 计数 ----
    private int placed;
    private int attempts;
    private int totalTicks;
    private int phaseTicks;

    // ---- 单轮循环状态 ----
    /** 起跳瞬间的脚格 Y（放置目标格的 Y）。 */
    private int cycleFeetY;
    /** 起跳瞬间的 y 坐标（Δy 基准）。 */
    private double jumpBaseY;
    /** 本轮计划放置的格子（在 use 脉冲那一拍按当时 px/pz 重算，与准星射线同源）。 */
    private BlockPos expectBlock;
    /** 本轮是否已脉冲过 use（每跳只放一块，配合 itemUseCooldown=4）。 */
    private boolean pulsed;
    /** 正在挖的头顶块。 */
    private BlockPos digTarget;

    public PillarExecutor(Consumer<StatusEvent> statusListener) {
        this.statusListener = statusListener;
    }

    /**
     * 启动垫方块爬高。
     *
     * @param targetY   目标脚格 Y（脚格 ≥ 此值即 DONE）；{@code Integer.MIN_VALUE} = 只按 maxBlocks 停
     * @param maxBlocks 最多垫几块（&gt;0）
     * @param item      垫块材料注册名（如 {@code minecraft:dirt}）；null = 任意可放置方块
     */
    public synchronized void start(int targetY, int maxBlocks, String item) {
        this.targetY = targetY;
        this.maxBlocks = Math.max(1, maxBlocks);
        this.wantItem = (item == null || item.isEmpty()) ? null : item;
        this.active = true;
        this.phase = Phase.CLEAR_HEAD;
        this.placed = 0;
        this.attempts = 0;
        this.totalTicks = 0;
        this.phaseTicks = 0;
        this.expectBlock = null;
        this.digTarget = null;
        this.pulsed = false;
    }

    public synchronized void cancel() {
        if (active) {
            active = false;
            fire(Status.CANCELLED, -1, null, "cancelled by request");
        }
    }

    public synchronized boolean isActive() {
        return active;
    }

    public synchronized int getPlaced() {
        return placed;
    }

    /**
     * 每 tick 驱动一次；返回本 tick 要注入的动作（null = 技能已结束，调用方发空动作）。
     *
     * <p>本执行器活跃期间独占移动/跳跃/攻击/使用按键，绝不发 forward/back/left/right
     * —— 任何水平输入都会让玩家飘出方块列、垫歪后踩空。
     */
    public synchronized ActionCmd tick(ClientPlayerEntity player) {
        if (!active || player == null) {
            return null;
        }
        if (++totalTicks > TOTAL_TIMEOUT) {
            return fail(player, R_TIMEOUT, "exceeded " + TOTAL_TIMEOUT + " ticks");
        }
        // 落水/岩浆：垫方块语义失效（水里是游泳上浮），交回 Python 的溺水自救
        if (player.isTouchingWater() || player.isInLava()) {
            return fail(player, R_IN_FLUID, "player entered fluid");
        }

        int feetY = player.getBlockY();
        phaseTicks++;
        return switch (phase) {
            case CLEAR_HEAD -> tickClearHead(player, feetY);
            case EQUIP -> tickEquip(player);
            case SETTLE -> tickSettle(player, feetY);
            case JUMP -> tickJump();
            case AIRBORNE -> tickAirborne(player);
            case VERIFY -> tickVerify(player, feetY);
        };
    }

    // ---- 相位实现 ----

    /**
     * 相位 1：完成判定 + 挖掉头顶块 {@code (bx, fy+2, bz)}。
     *
     * <p>完成判定**只在本相位做**：CLEAR_HEAD 是每轮循环的入口，且必然落地。若放在
     * tick() 顶部，空中 Δy&gt;1.0 时 {@code getBlockY()} 会瞬时等于 {@code cycleFeetY+1}
     * → 在方块还没放上时就误报 DONE，玩家随后落回原高度。
     *
     * <p>挖 {@code fy+2} 而不是 {@code fy+1}：{@code fy+1} 是玩家头格（恒为空气，检查
     * 恒真——老实现的 off-by-one 就在这里），真正挡住跳跃的是 {@code fy+2}。见类注释约束 1。
     */
    private ActionCmd tickClearHead(ClientPlayerEntity player, int feetY) {
        if (placed >= maxBlocks || (targetY != Integer.MIN_VALUE && feetY >= targetY)) {
            active = false;
            fire(Status.DONE, feetY, null,
                    "placed=" + placed + " feetY=" + feetY + " targetY=" + targetY);
            return null;
        }
        BlockPos head = new BlockPos(player.getBlockX(), feetY + 2, player.getBlockZ());
        BlockState state = player.getWorld().getBlockState(head);
        if (isPassable(state)) {
            digTarget = null;
            toPhase(Phase.EQUIP);
            return new ActionCmd();
        }
        if (!isDiggable(state)) {
            return fail(player, R_HEAD_BLOCKED,
                    "head block not diggable at " + head + " (" + blockName(state) + ")");
        }
        if (!head.equals(digTarget)) {
            digTarget = head;
            phaseTicks = 0;
        }
        if (phaseTicks > DIG_TIMEOUT) {
            return fail(player, R_DIG_TIMEOUT, "dig gave up at " + head);
        }
        aimAt(player, head);
        ActionCmd cmd = new ActionCmd();
        cmd.attack = true;
        return cmd;
    }

    /** 相位 2：确保主手持可放置方块（不对则切槽等 EQUIP_WAIT tick 再验）。 */
    private ActionCmd tickEquip(ClientPlayerEntity player) {
        ItemStack held = player.getMainHandStack();
        if (isPlaceable(held) && matchesWant(held)) {
            toPhase(Phase.SETTLE);
            return new ActionCmd();
        }
        if (phaseTicks <= EQUIP_WAIT) {
            return new ActionCmd();  // 上一拍的 hotbar 还没被 wasPressed 消费
        }
        int slot = findSlot(player);
        if (slot < 0) {
            return fail(player, wantItem == null ? R_NO_BLOCK_ITEM : R_OUT_OF_BLOCKS,
                    "no placeable block in hotbar (want=" + wantItem + ")");
        }
        ActionCmd cmd = new ActionCmd();
        cmd.hotbar = slot;
        phaseTicks = 0;
        return cmd;
    }

    /**
     * 相位 3：站定 + 视角朝正下收敛。
     *
     * <p>四个必要条件：<b>落地</b>（否则跳不起来）、<b>水平速度≈0</b>（残余速度会让玩家
     * 飘出方块列，垫歪后踩空）、<b>pitch 已收敛到 +90</b>（准星在渲染线程逐帧更新，
     * 转向当拍的准星还是旧方向——所以收敛后再等 SETTLE_TICKS 拍）、<b>脚踩满方块</b>。
     *
     * <p>最后一条是真实存在的不可行情形：站在半砖/雪层/矮墙上时 {@code getY()} 落在
     * 格子中间（如 fy+0.5），最大跳高 1.25 只能到 fy+1.75，仍与 fy+1 那一格相交
     * → 放置必被 {@code isSpaceEmpty} 拒。vanilla 里人也垫不上去（得先跳上整块）。
     * 与其反复重试耗尽 attempts，不如立刻 FAILED(uneven_ground) 让上层换挖阶梯。
     */
    private ActionCmd tickSettle(ClientPlayerEntity player, int feetY) {
        if (phaseTicks > SETTLE_TIMEOUT) {
            return fail(player, R_NO_SETTLE, "cannot settle (moving surface?)");
        }
        // 视角目标：保持当前 yaw，pitch 打到正下（Aim.PITCH_DOWN = +90，MC 约定）
        VlaClient.getInstance().setCameraTarget(player.getYaw(), Aim.PITCH_DOWN);

        Vec3d v = player.getVelocity();
        boolean still = Math.hypot(v.x, v.z) < SETTLE_MAX_HSPEED;
        boolean aimed = Math.abs(player.getPitch() - Aim.PITCH_DOWN) < SETTLE_PITCH_EPS;
        if (!player.isOnGround() || !still || !aimed) {
            return new ActionCmd();  // 空动作：不带任何水平输入
        }
        if (player.getY() - feetY > FULL_BLOCK_EPS) {
            return fail(player, R_UNEVEN_GROUND,
                    "standing on a partial block (y=" + player.getY() + ", feetY=" + feetY + ")");
        }
        // 目标格提前查一次：被半砖/花草之外的不可替换方块占住时立刻放弃，不白跳
        BlockPos target = new BlockPos(player.getBlockX(), feetY, player.getBlockZ());
        BlockState at = player.getWorld().getBlockState(target);
        if (!at.isAir() && !at.isReplaceable()) {
            return fail(player, R_PLACE_FAILED, "feet cell occupied: " + blockName(at));
        }
        if (phaseTicks < SETTLE_TICKS) {
            return new ActionCmd();
        }
        cycleFeetY = feetY;
        jumpBaseY = player.getY();
        pulsed = false;
        toPhase(Phase.JUMP);
        return new ActionCmd();
    }

    /** 相位 4：起跳（只脉冲一拍 jump；持续按住会落地即刻重跳，抢在 VERIFY 之前）。 */
    private ActionCmd tickJump() {
        toPhase(Phase.AIRBORNE);
        ActionCmd cmd = new ActionCmd();
        cmd.jump = true;
        return cmd;
    }

    /**
     * 相位 5：空中——到顶点时脉冲一拍 {@code use}。
     *
     * <p>判据 {@code Δy ≥ 1.05 && vy ≤ 0.02}（类注释约束 2/3）。放置格按**当拍的 px/pz**
     * 现算：这与 vanilla 准星射线同源（都从玩家中心朝正下），保证「射线命中的格」＝
     * 「我们校验并期待的格」，玩家贴近格边界时也不会记错列。
     */
    private ActionCmd tickAirborne(ClientPlayerEntity player) {
        double dy = player.getY() - jumpBaseY;
        double vy = player.getVelocity().y;

        if (!pulsed && dy >= PLACE_MIN_DY && vy <= PLACE_MAX_VY) {
            BlockPos target = new BlockPos(player.getBlockX(), cycleFeetY, player.getBlockZ());
            if (canPlaceAt(player, target)) {
                expectBlock = target;
                pulsed = true;
                ActionCmd cmd = new ActionCmd();
                cmd.use = true;   // 下一 tick handleInputEvents 消费，玩家仍在顶点
                return cmd;
            }
        }
        // isOnGround 至少等 2 拍再认：起跳指令在下一 tick 的 tickEntities 才生效，
        // 第 1 拍读到的仍可能是 onGround=true，立刻转 VERIFY 会白扣一次 attempt。
        if ((phaseTicks >= 2 && player.isOnGround()) || phaseTicks > AIR_TIMEOUT) {
            toPhase(Phase.VERIFY);
        }
        return new ActionCmd();
    }

    /** 相位 6：落地校验——块真放上且脚格升 1 格才算一次成功，否则重试本层。 */
    private ActionCmd tickVerify(ClientPlayerEntity player, int feetY) {
        boolean blockThere = expectBlock != null
                && !isPassable(player.getWorld().getBlockState(expectBlock));
        if (blockThere && feetY >= cycleFeetY + 1) {
            placed++;
            attempts = 0;
            fire(Status.PROGRESS, feetY, null, "placed " + expectBlock);
            expectBlock = null;
            toPhase(Phase.CLEAR_HEAD);   // 新的一层要重新看头顶
            return new ActionCmd();
        }
        if (++attempts > MAX_ATTEMPTS) {
            return fail(player, R_PLACE_FAILED,
                    "failed " + attempts + " times at y=" + cycleFeetY
                            + " (blockThere=" + blockThere + " feetY=" + feetY + ")");
        }
        expectBlock = null;
        toPhase(Phase.CLEAR_HEAD);
        return new ActionCmd();
    }

    // ---- 工具 ----

    private void toPhase(Phase next) {
        phase = next;
        phaseTicks = 0;
    }

    private void aimAt(ClientPlayerEntity player, BlockPos pos) {
        double[] yp = Aim.atBlockCenter(player, pos);
        VlaClient.getInstance().setCameraTarget(yp[0], yp[1]);
    }

    private ActionCmd fail(ClientPlayerEntity player, String reason, String detail) {
        active = false;
        fire(Status.FAILED, player == null ? -1 : player.getBlockY(), reason, detail);
        return null;
    }

    private void fire(Status status, int feetY, String reason, String detail) {
        if (statusListener != null) {
            statusListener.accept(new StatusEvent(status, placed, feetY, reason, detail));
        }
    }

    /**
     * 放置合法性预检（客户端侧，尽量与 {@code World#canPlace} 一致）：
     * 目标格可替换、目标格下方实心（正下射线要有面可点）、且玩家碰撞箱不与目标格相交。
     *
     * <p>最后一条是服务端也会做的判交（{@code isSpaceEmpty}）：碰撞箱底面必须严格高于
     * 目标格顶面，也就是 Δy &gt; 1.0 —— 与放置判据同一件事，这里当断言复查。
     */
    private boolean canPlaceAt(ClientPlayerEntity player, BlockPos pos) {
        BlockState at = player.getWorld().getBlockState(pos);
        if (!at.isAir() && !at.isReplaceable()) {
            return false;
        }
        BlockState below = player.getWorld().getBlockState(pos.down());
        if (isPassable(below)) {
            return false;   // 下方无面可点：正下射线会穿过去，落点不是这一格
        }
        return player.getBoundingBox().minY >= pos.getY() + 1.0;
    }

    /** 主手可否放置（BlockItem 且非空）。 */
    private static boolean isPlaceable(ItemStack stack) {
        return stack != null && !stack.isEmpty() && stack.getItem() instanceof BlockItem;
    }

    private boolean matchesWant(ItemStack stack) {
        if (wantItem == null) {
            return true;
        }
        return wantItem.equals(Registries.ITEM.getId(stack.getItem()).toString());
    }

    /** 在快捷栏 0-8 里找期望材料（wantItem=null 时找任意 BlockItem）；找不到返回 -1。 */
    private int findSlot(ClientPlayerEntity player) {
        for (int i = 0; i < 9; i++) {
            ItemStack stack = player.getInventory().getStack(i);
            if (isPlaceable(stack) && matchesWant(stack)) {
                return i;
            }
        }
        return -1;
    }

    private static boolean isPassable(BlockState state) {
        return state == null || state.isAir() || !state.isSolid();
    }

    /** 可挖穿的实心块（与 LocalPathfinder#isDiggable 同口径：非基岩/屏障/液体）。 */
    private static boolean isDiggable(BlockState state) {
        if (state == null || isPassable(state)) {
            return false;
        }
        if (state.getBlock().getHardness() < 0.0f) {
            return false;   // 基岩/屏障：hardness = -1
        }
        return state.getBlock() != Blocks.BEDROCK && state.getBlock() != Blocks.BARRIER;
    }

    private static String blockName(BlockState state) {
        return state == null ? "null" : Registries.BLOCK.getId(state.getBlock()).toString();
    }
}
