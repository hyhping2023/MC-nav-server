package dev.vla.client.nav;

import net.minecraft.block.BlockState;
import net.minecraft.client.MinecraftClient;
import net.minecraft.client.network.ClientPlayerEntity;
import net.minecraft.entity.LivingEntity;
import net.minecraft.item.ItemStack;
import net.minecraft.registry.Registries;
import net.minecraft.util.hit.BlockHitResult;
import net.minecraft.util.hit.EntityHitResult;
import net.minecraft.util.math.Vec3d;

/**
 * M11.6 视线工具策略：按 crosshair 命中的第一个方块/实体切换手持工具。
 *
 * <p>解决的问题：此前工具只在「挖穿子模式 / Python 近身补挖 / kill 爆发前」四个
 * 时刻切换，正常行走/追击期间没有策略 → 挖完石头后一直持镐，追击猪时"镐子打猪"。
 *
 * <p>三档模式（WS {@code set_tool_mode} 下发，Python KitAgent 按任务设置）：
 * <ul>
 *   <li>{@code auto}（dig 任务）：crosshair 命中可挖方块且在触及范围内 → 按
 *       {@link BlockTraits#toolFor} 切对应工具；命中活体实体且在近战范围内 → 切剑。</li>
 *   <li>{@code melee}（kill 任务）：无条件确保持剑——追击全程持剑，不再只在爆发前选。
 *       客户端挖穿绕障时本策略跳过（NavExecutor busy），挖完准星对回猪自动换回剑。</li>
 *   <li>{@code none}（place 任务 / 默认）：不干预，技能自己选槽（放泥土用 dirt 槽）。</li>
 * </ul>
 *
 * <p>防抖：只在「当前手持不匹配」时切换；切换间隔 ≥ {@link #SWITCH_COOLDOWN_TICKS}；
 * 命中实体后进入 {@link #ENTITY_HOLD_TICKS} 保持窗口，期间忽略方块命中（防准星扫过
 * 草皮把剑换回铲）。全部状态仅客户端 tick 线程访问；crosshairTarget 由渲染线程逐帧
 * 更新（与 PillarExecutor 同源，轻微滞后可接受）。
 */
public final class ToolPolicy {

    /** 方块命中切换范围（米，交互触及距离）。 */
    private static final double BLOCK_REACH = 4.5;
    /** 实体命中切换范围（米，近战距离；超出视为"还没到打的时候"）。 */
    private static final double ENTITY_REACH = 3.0;
    /** 两次切换的最小间隔（tick，防准星扫过不同方块时乱切）。 */
    private static final int SWITCH_COOLDOWN_TICKS = 5;
    /** 命中实体后的保持窗口（tick）：期间方块命中不换工具。 */
    private static final int ENTITY_HOLD_TICKS = 20;

    /** 工具策略三档模式。 */
    public enum Mode { AUTO, MELEE, NONE }

    private Mode mode = Mode.AUTO;
    private int lastSwitchTick = Integer.MIN_VALUE;
    private int entityHoldLeft = 0;

    /** WS {@code set_tool_mode}：melee / none，其余一律 auto。 */
    public void setMode(String mode) {
        if ("melee".equalsIgnoreCase(mode)) {
            this.mode = Mode.MELEE;
        } else if ("none".equalsIgnoreCase(mode)) {
            this.mode = Mode.NONE;
        } else {
            this.mode = Mode.AUTO;
        }
        this.entityHoldLeft = 0;
    }

    /**
     * 每 tick 调用（客户端 tick 线程，注入前）。{@code busy}=true（挖穿/放置子模式或
     * pillar 进行中）时跳过——工具由技能/规划决定，本策略只覆盖普通行走/追击。
     */
    public void apply(MinecraftClient client, ClientPlayerEntity player, boolean busy) {
        if (client == null || player == null || mode == Mode.NONE || busy) {
            return;
        }
        if (mode == Mode.MELEE) {
            selectToolCategory(player, "sword");
            return;
        }
        // AUTO：优先实体 → 剑（带保持窗口），否则命中方块 → 对应工具
        if (entityHoldLeft > 0) {
            entityHoldLeft--;
        }
        String category = null;
        if (client.crosshairTarget instanceof EntityHitResult ehr
                && ehr.getEntity() instanceof LivingEntity) {
            if (player.getEyePos().distanceTo(ehr.getEntity().getEyePos()) <= ENTITY_REACH) {
                category = "sword";
                entityHoldLeft = ENTITY_HOLD_TICKS;
            }
        } else if (client.crosshairTarget instanceof BlockHitResult bhr) {
            Vec3d hit = bhr.getPos();
            if (player.getEyePos().distanceTo(hit) <= BLOCK_REACH) {
                BlockState state = player.getWorld().getBlockState(bhr.getBlockPos());
                category = BlockTraits.toolFor(state);
            }
        }
        if (category == null) {
            return;   // 无可挖/可击目标：保持现状
        }
        if (entityHoldLeft > 0 && !"sword".equals(category)) {
            return;   // 实体保持窗口内，方块命中不换
        }
        if (player.age - lastSwitchTick < SWITCH_COOLDOWN_TICKS) {
            return;
        }
        if (selectToolCategory(player, category)) {
            lastSwitchTick = player.age;
        }
    }

    /**
     * 按工具**类别**（pickaxe/axe/shovel/sword，来自 {@link BlockTraits#toolFor}）在快捷栏
     * 0-8 找注册名含该类别的物品并选中（如 diamond_pickaxe 匹配 "pickaxe"）。已手持匹配
     * 工具则跳过；找不到则不切（徒手挖，慢但不算错）。返回是否发生了切换。
     */
    public static boolean selectToolCategory(ClientPlayerEntity player, String category) {
        try {
            ItemStack held = player.getMainHandStack();
            if (!held.isEmpty()
                    && Registries.ITEM.getId(held.getItem()).getPath().contains(category)) {
                return false;   // 已在手
            }
            for (int i = 0; i < 9; i++) {
                ItemStack stack = player.getInventory().getStack(i);
                if (!stack.isEmpty()
                        && Registries.ITEM.getId(stack.getItem()).getPath().contains(category)) {
                    player.getInventory().selectedSlot = i;
                    return true;
                }
            }
        } catch (Exception e) {
            // 快捷栏无此类工具：徒手挖
        }
        return false;
    }
}
