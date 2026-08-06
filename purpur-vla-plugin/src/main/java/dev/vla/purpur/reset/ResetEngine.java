package dev.vla.purpur.reset;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import org.bukkit.Bukkit;
import org.bukkit.GameMode;
import org.bukkit.GameRule;
import org.bukkit.Location;
import org.bukkit.World;
import org.bukkit.entity.Entity;
import org.bukkit.entity.Player;
import org.bukkit.inventory.ItemStack;
import org.bukkit.potion.PotionEffect;
import org.bukkit.util.Vector;

/**
 * 世界控制与重置引擎（DESIGN.md §4.3/§4.4，M4 交付物，任务 2.3）。
 *
 * <p>L1 内存快照回滚：首次 reset 对目标区域 capture 并缓存基线（保证确定性，
 * 后续 reset 从缓存恢复、不重复捕获）。reset 流程固定（语义不可缩水）：
 * 确保区块加载 → 清非玩家实体 → 回滚方块 → 玩家态重置 → gamerule 冻结 →
 * 固定时间/天气 → 发初始物品。全部方法必须在 Bukkit 主线程调用。
 */
public final class ResetEngine {

    /** 重置参数（DESIGN.md §4.7 TaskSpec.reset 的子集）。 */
    public static final class ResetSpec {
        /** 区域中心（未 setCenter 时由调用方回退到玩家当前位置）。 */
        public int centerX;
        public int centerY;
        public int centerZ;
        public boolean centerSet = false;
        /** 半宽，默认 16 → 33×33×33。 */
        public int halfExtent = 16;
        /** 出生点；null → world.getSpawnLocation()。 */
        public Location spawn;
        public boolean clearInventory = true;
        /** 初始物品（reset 前先清空背包再按序给予）。 */
        public final List<ItemStack> initialItems = new ArrayList<>();
        /** 固定时间（time of day，默认 6000 = 正午）。 */
        public long time = 6000;

        public void setCenter(int x, int y, int z) {
            this.centerX = x;
            this.centerY = y;
            this.centerZ = z;
            this.centerSet = true;
        }
    }

    /** reset 结果（ResetReply.message = checksum）。 */
    public static final class ResetResult {
        public final boolean ok;
        public final String checksum;
        public final int serverTick;
        /** reset 完成后区域内非玩家实体数。 */
        public final int entityCount;

        ResetResult(boolean ok, String checksum, int serverTick, int entityCount) {
            this.ok = ok;
            this.checksum = checksum;
            this.serverTick = serverTick;
            this.entityCount = entityCount;
        }
    }

    /** verify 结果（/vla verify 用）。 */
    public static final class RegionVerify {
        public final String checksum;
        public final int entityCount;
        public final long time;
        public final String playerSummary;

        RegionVerify(String checksum, int entityCount, long time, String playerSummary) {
            this.checksum = checksum;
            this.entityCount = entityCount;
            this.time = time;
            this.playerSummary = playerSummary;
        }
    }

    /** 基线缓存：key = worldName + center + extent。 */
    private final Map<String, RegionSnapshot> baselineCache = new HashMap<>();

    private static String cacheKey(String worldName, int cx, int cy, int cz, int extent) {
        return worldName + "@" + cx + "," + cy + "," + cz + "@" + extent;
    }

    /** 主线程重置。返回 ok/checksum/serverTick/entityCount。 */
    public ResetResult reset(Player player, ResetSpec spec) {
        World world = player.getWorld();
        int extent = spec.halfExtent;
        int cx = spec.centerX;
        int cy = spec.centerY;
        int cz = spec.centerZ;

        // 1. 确保区域所有 chunk 已加载（未加载则 force load）
        ensureChunksLoaded(world, cx, cz, extent);

        // 2. 清实体：区域内非 Player 全部 remove（掉落物也是实体，一并清）
        clearNonPlayerEntities(world, cx, cy, cz, extent);

        // 3. 回滚方块：首次 capture 基线并缓存；后续从缓存恢复（保证确定性）
        String key = cacheKey(world.getName(), cx, cy, cz, extent);
        RegionSnapshot snapshot = baselineCache.get(key);
        if (snapshot == null) {
            snapshot = RegionSnapshot.capture(world, new Vector(cx, cy, cz), extent);
            baselineCache.put(key, snapshot);
        }
        snapshot.restore(world);

        // 4. 玩家态重置
        Location spawn = spec.spawn != null ? spec.spawn : world.getSpawnLocation();
        player.teleport(spawn);
        if (spec.clearInventory) {
            player.getInventory().clear();
            player.getEquipment().clear();
            player.getInventory().setItemInMainHand(null);
            player.getInventory().setItemInOffHand(null);
        }
        player.setHealth(20.0);
        player.setFoodLevel(20);
        player.setSaturation(20f);
        for (PotionEffect effect : player.getActivePotionEffects()) {
            player.removePotionEffect(effect.getType());
        }
        player.setFireTicks(0);
        player.setFallDistance(0f);
        player.setGameMode(GameMode.SURVIVAL);

        // 5. gamerule 冻结（幂等，§4.3 确定性配置）
        freezeGamerules(world);

        // 6. 时间/天气
        world.setTime(spec.time);
        world.setStorm(false);
        world.setThundering(false);

        // 7. 初始物品（先 clear 再加）
        for (ItemStack item : spec.initialItems) {
            if (item != null) {
                player.getInventory().addItem(item);
            }
        }

        return new ResetResult(true, snapshot.checksum(), Bukkit.getCurrentTick(),
                countNonPlayerEntities(world, cx, cy, cz, extent));
    }

    /** 实时区域校验（/vla verify 用）：重算 checksum + 区域内实体数 + 时间 + 玩家摘要。 */
    public RegionVerify verify(World world, int cx, int cy, int cz, int extent, Player player) {
        String checksum = RegionSnapshot.capture(world, new Vector(cx, cy, cz), extent).checksum();
        int entities = countNonPlayerEntities(world, cx, cy, cz, extent);
        long time = world.getTime();
        return new RegionVerify(checksum, entities, time, playerSummary(player));
    }

    private void ensureChunksLoaded(World world, int cx, int cz, int extent) {
        int minX = (cx - extent) >> 4;
        int maxX = (cx + extent) >> 4;
        int minZ = (cz - extent) >> 4;
        int maxZ = (cz + extent) >> 4;
        for (int x = minX; x <= maxX; x++) {
            for (int z = minZ; z <= maxZ; z++) {
                if (!world.isChunkLoaded(x, z)) {
                    world.loadChunk(x, z);
                }
            }
        }
    }

    private void clearNonPlayerEntities(World world, int cx, int cy, int cz, int extent) {
        for (Entity e : regionEntities(world, cx, cy, cz, extent)) {
            if (!(e instanceof Player)) {
                e.remove();
            }
        }
    }

    private int countNonPlayerEntities(World world, int cx, int cy, int cz, int extent) {
        int n = 0;
        for (Entity e : regionEntities(world, cx, cy, cz, extent)) {
            if (!(e instanceof Player)) {
                n++;
            }
        }
        return n;
    }

    private java.util.Collection<Entity> regionEntities(World world, int cx, int cy, int cz, int extent) {
        Location center = new Location(world, cx + 0.5, cy + 0.5, cz + 0.5);
        return world.getNearbyEntities(center, extent, extent, extent);
    }

    private void freezeGamerules(World world) {
        world.setGameRule(GameRule.DO_DAYLIGHT_CYCLE, false);
        world.setGameRule(GameRule.DO_WEATHER_CYCLE, false);
        world.setGameRule(GameRule.DO_MOB_SPAWNING, false);
        world.setGameRule(GameRule.MOB_GRIEFING, false);
        world.setGameRule(GameRule.KEEP_INVENTORY, true);
        world.setGameRule(GameRule.RANDOM_TICK_SPEED, 0);
    }

    private String playerSummary(Player player) {
        Location loc = player.getLocation();
        return String.format("pos=(%.1f,%.1f,%.1f) hp=%.0f food=%d sat=%.0f",
                loc.getX(), loc.getY(), loc.getZ(),
                player.getHealth(), player.getFoodLevel(), player.getSaturation());
    }
}
