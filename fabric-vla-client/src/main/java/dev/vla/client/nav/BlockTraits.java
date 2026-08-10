package dev.vla.client.nav;

import net.minecraft.block.BlockState;
import net.minecraft.block.Blocks;
import net.minecraft.registry.tag.BlockTags;
import net.minecraft.registry.tag.FluidTags;

/**
 * 方块通过性四态判定——寻路/跟随/技能三方共用的唯一口径。
 *
 * <p>存在的原因是一个会污染训练数据的 bug：老代码统一用 {@code BlockState#isSolid()}
 * 判通过性，而**岩浆和水的 {@code isSolid()} 都是 false** → 被当成可通行。
 * {@code hasGround} 要求脚下 SOLID，所以岩浆本身当不了地面，但「岩浆铺在石头上」这种
 * 格子会被判成「可站」，A* 于是主动把 agent 路由进岩浆池。
 * ({@code LocalPathfinder#isDiggable} 当时排除了 water/lava，说明想到了「挖」的一面，
 * 漏了「走」的一面。)
 *
 * <p>后果不只是死一次：agent 烧死的那段轨迹会被 recorder 原样写进数据集。
 *
 * <h2>四态</h2>
 * <ul>
 *   <li>{@link Kind#SOLID}：实心可站立面。只有它能当 {@code hasGround}。</li>
 *   <li>{@link Kind#OPEN}：空气/草丛/花等可自由通过且无伤。</li>
 *   <li>{@link Kind#WATER}：可通过但**加成本**（不硬禁：浅溪/齐膝水该能走；
 *       加价让 A* 只在没有旱路时才下水，从源头减少溺水自救的触发频率）。</li>
 *   <li>{@link Kind#HAZARD}：接触即掉血/陷入，**一律不可通行、也不可当地面**
 *       （岩浆块和仙人掌本身是实心的，仍归 HAZARD → 不能踩）。</li>
 * </ul>
 *
 * <p>危险判定优先用 tag（{@code FluidTags.LAVA} / {@code BlockTags.FIRE} /
 * {@code BlockTags.CAMPFIRES}）而不是硬编码方块名，这样流动岩浆、灵魂火、
 * 各类营火变体自动覆盖；tag 覆盖不到的（岩浆块/仙人掌/细雪/浆果丛/凋灵玫瑰/
 * 岩浆炼药锅）再点名补齐。
 */
public final class BlockTraits {

    /** 通过性四态。 */
    public enum Kind { SOLID, OPEN, WATER, HAZARD }

    private BlockTraits() {
    }

    public static Kind of(BlockState state) {
        if (state == null) {
            return Kind.OPEN;
        }
        if (isHazard(state)) {
            return Kind.HAZARD;   // 必须先判：岩浆块/仙人掌 isSolid()==true
        }
        if (isWater(state)) {
            return Kind.WATER;
        }
        if (state.isAir()) {
            return Kind.OPEN;
        }
        return state.isSolid() ? Kind.SOLID : Kind.OPEN;
    }

    /** 接触即掉血/陷入 → 既不可通行也不可当地面。 */
    public static boolean isHazard(BlockState state) {
        if (state == null) {
            return false;
        }
        if (state.getFluidState().isIn(FluidTags.LAVA)) {
            return true;   // 静止+流动岩浆
        }
        if (state.isIn(BlockTags.FIRE) || state.isIn(BlockTags.CAMPFIRES)) {
            return true;   // 火/灵魂火/各类营火
        }
        return state.isOf(Blocks.MAGMA_BLOCK)
                || state.isOf(Blocks.CACTUS)
                || state.isOf(Blocks.POWDER_SNOW)
                || state.isOf(Blocks.SWEET_BERRY_BUSH)
                || state.isOf(Blocks.WITHER_ROSE)
                || state.isOf(Blocks.LAVA_CAULDRON);
    }

    /** 水（含流动水）；不含岩浆——岩浆先被 isHazard 截走。 */
    public static boolean isWater(BlockState state) {
        return state != null && state.getFluidState().isIn(FluidTags.WATER);
    }

    /** 玩家身体不能占据该格（实心或危险）。 */
    public static boolean blocksBody(BlockState state) {
        Kind k = of(state);
        return k == Kind.SOLID || k == Kind.HAZARD;
    }

    /** 该格能当站立面（只有 SOLID；岩浆块/仙人掌虽实心但归 HAZARD，踩不得）。 */
    public static boolean isGround(BlockState state) {
        return of(state) == Kind.SOLID;
    }

    // ---- M11.5 方块 → 首选工具（难点④：挖对应方块用对应工具） ----

    /**
     * 该方块的首选工具类别；null = 徒手/无所谓（树叶/草丛等）。
     *
     * <p>用原版挖掘 tag（{@code mineable/pickaxe|axe|shovel} = {@code BlockTags.*_MINEABLE}）
     * 判定——新方块/模组方块自动覆盖，无需维护方块名清单。返回值是工具**类别名**
     * （物品注册名的后缀匹配用，如 {@code diamond_pickaxe} 含 {@code pickaxe}）。
     */
    public static String toolFor(BlockState state) {
        if (state == null || state.isAir()) {
            return null;
        }
        if (state.isIn(BlockTags.PICKAXE_MINEABLE)) {
            return "pickaxe";
        }
        if (state.isIn(BlockTags.SHOVEL_MINEABLE)) {
            return "shovel";
        }
        if (state.isIn(BlockTags.AXE_MINEABLE)) {
            return "axe";
        }
        if (state.isIn(BlockTags.SWORD_EFFICIENT)) {
            return "sword";   // 竹子/蜘蛛网等剑效方块
        }
        return null;
    }
}
