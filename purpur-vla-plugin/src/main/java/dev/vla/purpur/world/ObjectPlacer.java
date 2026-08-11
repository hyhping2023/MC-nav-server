package dev.vla.purpur.world;

import dev.vla.purpur.grpc.MainThreadDispatcher;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.UUID;
import org.bukkit.Material;
import org.bukkit.World;
import org.bukkit.block.data.BlockData;
import org.bukkit.entity.Player;
import org.bukkit.plugin.Plugin;

/**
 * 录制场景目标生成器：在玩家周围随机放置单个石头、泥土或树目标。
 *
 * <p>每个 episode 只有一个目标物体，位置由 seed 决定并落在玩家 6~12 格附近。石头和
 * 泥土从柱、矮墙、平台、阶梯等四块形状中确定性随机选择；树干高度为 3~7 格，带叶冠。
 * 高层目标留给客户端 {@code PillarExecutor} 用脚底垫块处理，绝不需要跳挖。
 *
 * <p>全部主线程执行 + {@code Block#setBlockData(data, false)}（关闭物理更新）。
 */
public final class ObjectPlacer {

    public static final int DEFAULT_MIN_DIST = 6;
    public static final int DEFAULT_MAX_DIST = 12;
    /** 单个物体候选位置最大尝试次数（去重失败重试上限，防死循环）。 */
    private static final int MAX_PLACE_ATTEMPTS = 400;
    /** 每个玩家当前 episode 的目标方块，切换任务时清除，确保一次任务只保留一个目标物体。 */
    private static final Map<UUID, List<org.bukkit.block.Block>> TASK_TARGETS = new HashMap<>();

    private ObjectPlacer() {
    }

    /**
     * 依照任务 id 放置唯一目标。
     *
     * <p>石头/泥土都恰好放置 4 个任务方块，匹配其固定成功计数；树高可以变化，任务仍
     * 只要求砍 4 根原木，因此演示会覆盖“目标不必全部挖完”的自然场景。
     *
     * @return 已识别并已排入放置任务时返回 {@code true}；非这三类任务返回 {@code false}
     */
    public static boolean placeForTask(Plugin plugin, Player player, String taskId, long seed) {
        Target target = Target.forTask(taskId);
        if (target == null) {
            return false;
        }
        MainThreadDispatcher.runSync(() -> startPlaceOne(plugin, player, target,
                DEFAULT_MIN_DIST, DEFAULT_MAX_DIST, seed));
        return true;
    }

    /** 主线程入口：放置单一任务目标。 */
    private static void startPlaceOne(Plugin plugin, Player player, Target target, int minDist,
            int maxDist, long seed) {
        World world = player.getWorld();
        int cx = player.getLocation().getBlockX();
        int cz = player.getLocation().getBlockZ();
        int minD = Math.max(1, minDist);
        int maxD = Math.max(minD + 1, maxDist);
        Random rng = new Random(seed ^ target.taskId.hashCode());
        int[] pos = findSpot(rng, cx, cz, minD, maxD);
        if (pos == null) {
            plugin.getLogger().warning("[placeobjects] no valid single target position for task="
                    + target.taskId);
            return;
        }
        clearPreviousTaskTarget(player);
        List<org.bukkit.block.Block> targetBlocks = new ArrayList<>();
        switch (target) {
            case TREE -> placeTree(world, pos[0], pos[1], 3 + rng.nextInt(5), targetBlocks);
            case STONE -> placeFourBlockShape(world, pos[0], pos[1], Material.STONE, rng, targetBlocks);
            case DIRT -> placeFourBlockShape(world, pos[0], pos[1], Material.DIRT, rng, targetBlocks);
        }
        TASK_TARGETS.put(player.getUniqueId(), targetBlocks);
        plugin.getLogger().info("[placeobjects] task=" + target.taskId + " target="
                + target.name().toLowerCase() + " at=(" + pos[0] + "," + pos[1] + ") seed="
                + seed);
    }

    /** 在 [minD,maxD] 环带随机取目标锚点，避开玩家脚下一圈。 */
    private static int[] findSpot(Random rng, int cx, int cz, int minD, int maxD) {
        for (int attempt = 0; attempt < MAX_PLACE_ATTEMPTS; attempt++) {
            double angle = rng.nextDouble() * Math.PI * 2;
            double dist = minD + rng.nextDouble() * (maxD - minD);
            int x = cx + (int) Math.round(Math.cos(angle) * dist);
            int z = cz + (int) Math.round(Math.sin(angle) * dist);
            if (Math.hypot(x - cx, z - cz) < 3) {
                continue;
            }
            return new int[]{x, z};
        }
        return null;
    }

    private static void placeTree(World world, int x, int z, int trunkH,
            List<org.bukkit.block.Block> targetBlocks) {
        int ground = groundY(world, x, z);
        BlockData log = Material.OAK_LOG.createBlockData();
        BlockData leaves = Material.OAK_LEAVES.createBlockData();
        for (int dy = 1; dy <= trunkH; dy++) {
            org.bukkit.block.Block block = world.getBlockAt(x, ground + dy, z);
            block.setBlockData(log, false);
            recordTargetBlock(targetBlocks, block);
        }
        int canopyBase = ground + trunkH + 1; // 叶冠在树干顶部之上（2×2×3）
        for (int dy = 0; dy < 3; dy++) {
            for (int dx = 0; dx < 2; dx++) {
                for (int dz = 0; dz < 2; dz++) {
                    org.bukkit.block.Block block = world.getBlockAt(
                            x + dx, canopyBase + dy, z + dz);
                    block.setBlockData(leaves, false);
                    recordTargetBlock(targetBlocks, block);
                }
            }
        }
    }

    /** 随机放置四块石/土：柱、矮墙、平台或上行阶梯。 */
    private static void placeFourBlockShape(World world, int x, int z, Material material,
            Random rng, List<org.bukkit.block.Block> targetBlocks) {
        int ground = groundY(world, x, z);
        BlockData data = material.createBlockData();
        int[][] offsets = switch (rng.nextInt(4)) {
            case 0 -> new int[][]{{0, 1, 0}, {0, 2, 0}, {0, 3, 0}, {0, 4, 0}}; // 柱
            case 1 -> new int[][]{{0, 1, 0}, {1, 1, 0}, {2, 1, 0}, {3, 1, 0}}; // 矮墙
            case 2 -> new int[][]{{0, 1, 0}, {1, 1, 0}, {0, 1, 1}, {1, 1, 1}}; // 平台
            default -> new int[][]{{0, 1, 0}, {1, 2, 0}, {2, 3, 0}, {3, 4, 0}}; // 阶梯
        };
        for (int[] o : offsets) {
            org.bukkit.block.Block block = world.getBlockAt(x + o[0], ground + o[1], z + o[2]);
            block.setBlockData(data, false);
            recordTargetBlock(targetBlocks, block);
        }
    }

    /** 从当前列顶端向下找第一个实心块。受控任务在平原 reset 后调用，故结果为草地方块。 */
    private static int groundY(World world, int x, int z) {
        for (int y = world.getMaxHeight() - 1; y >= world.getMinHeight(); y--) {
            if (world.getBlockAt(x, y, z).getType().isSolid()) {
                return y;
            }
        }
        return world.getMinHeight();
    }

    private static void clearPreviousTaskTarget(Player player) {
        List<org.bukkit.block.Block> old = TASK_TARGETS.remove(player.getUniqueId());
        if (old == null) {
            return;
        }
        BlockData air = Material.AIR.createBlockData();
        for (org.bukkit.block.Block block : old) {
            block.setBlockData(air, false);
        }
    }

    private static void recordTargetBlock(List<org.bukkit.block.Block> targetBlocks,
            org.bukkit.block.Block block) {
        if (targetBlocks != null) {
            targetBlocks.add(block);
        }
    }

    /** 三个受控采集任务到唯一目标类型的映射。 */
    private enum Target {
        STONE("collect_stone"),
        DIRT("dig_dirt"),
        TREE("collect_wood");

        final String taskId;

        Target(String taskId) {
            this.taskId = taskId;
        }

        static Target forTask(String taskId) {
            for (Target target : values()) {
                if (target.taskId.equals(taskId)) {
                    return target;
                }
            }
            return null;
        }
    }
}
