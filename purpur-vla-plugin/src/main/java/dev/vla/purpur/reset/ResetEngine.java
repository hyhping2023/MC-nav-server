package dev.vla.purpur.reset;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;
import org.bukkit.Bukkit;
import org.bukkit.Difficulty;
import org.bukkit.GameMode;
import org.bukkit.GameRule;
import org.bukkit.Location;
import org.bukkit.Material;
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
        /** M11 确定性种子（ResetRequest.seed）：决定 episode 的时间、天气与目标位置。 */
        public int seed = 0;

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

        // 3. 清理上一个 episode 的柱/树，再首次 capture 干净基线并缓存。服务端重启时
        // ObjectPlacer 的内存索引会丢失，不能依赖它来清理持久化的旧目标。
        clearAboveSurface(world, cx, cy, cz, extent);

        // 4. 回滚方块：首次 capture 基线并缓存；后续从缓存恢复（保证确定性）
        String key = cacheKey(world.getName(), cx, cy, cz, extent);
        RegionSnapshot snapshot = baselineCache.get(key);
        if (snapshot == null) {
            snapshot = RegionSnapshot.capture(world, new Vector(cx, cy, cz), extent);
            baselineCache.put(key, snapshot);
        }
        snapshot.restore(world);

        // 5. 玩家态重置
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

        // 6. gamerule 冻结（幂等，§4.3 确定性配置）
        freezeGamerules(world);

        // 7. Episode 环境随机化（同 seed 可复现）：白昼系 80%（日间/黎明/黄昏），
        // 夜间 20%；天气在晴天/雨/雷雨间采样。困难度固定和平，昼夜/天气循环冻结。
        applyEpisodeEnvironment(world, spec.seed);

        // 8. 初始物品（先 clear 再加）：前 9 个显式放入 hotbar 0-8（确定性槽位），
        //    超出部分 addItem；存在初始物品时选中槽归零（镐 0），避免上一 episode 选中槽残留。
        ItemStack[] initial = spec.initialItems.toArray(new ItemStack[0]);
        for (int i = 0; i < initial.length && i < 9; i++) {
            if (initial[i] != null) {
                player.getInventory().setItem(i, initial[i]);
            }
        }
        if (initial.length > 9) {
            for (int i = 9; i < initial.length; i++) {
                if (initial[i] != null) {
                    player.getInventory().addItem(initial[i]);
                }
            }
        }
        if (initial.length > 0) {
            player.getInventory().setHeldItemSlot(0);
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

    /** 清理受控平面上方的遗留人工方块，保留 y<=63 的单材质地表及其地下基底。 */
    private void clearAboveSurface(World world, int cx, int cy, int cz, int extent) {
        int minY = Math.max(ControlledSurface.Y + 1, world.getMinHeight());
        int maxY = Math.min(cy + extent, world.getMaxHeight() - 1);
        if (minY > maxY) {
            return;
        }
        for (int x = cx - extent; x <= cx + extent; x++) {
            for (int z = cz - extent; z <= cz + extent; z++) {
                for (int y = minY; y <= maxY; y++) {
                    if (!world.getBlockAt(x, y, z).getType().isAir()) {
                        world.getBlockAt(x, y, z).setType(Material.AIR, false);
                    }
                }
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

    /**
     * 初始化一个可复现的录制场景。
     *
     * <p>时间分布满足「白昼系 : 夜间 = 8 : 2」：
     * 明亮白天 50%、黎明 15%、黄昏 15%、夜间 20%。天气为晴 60%、雨 32%、雷雨 8%。
     * 世界规则已冻结，故录制过程中光照和天气不会漂移；和平难度保证没有敌对生物干扰构造。
     */
    private void applyEpisodeEnvironment(World world, int seed) {
        Random rng = new Random(0x564c415f454e564cL ^ seed);
        int slot = rng.nextInt(100);
        long time;
        if (slot < 50) {
            time = 3_000L + rng.nextInt(6_000);     // 明亮白天
        } else if (slot < 65) {
            time = 23_000L + rng.nextInt(1_000);    // 黎明
        } else if (slot < 80) {
            time = 12_000L + rng.nextInt(1_000);    // 黄昏
        } else {
            time = 16_000L + rng.nextInt(6_000);    // 夜间
        }
        int weather = rng.nextInt(100);
        boolean storm = weather >= 60;
        boolean thunder = weather >= 92;
        world.setTime(time);
        world.setStorm(storm);
        world.setThundering(thunder);
        world.setWeatherDuration(1_000_000);
        world.setThunderDuration(1_000_000);
        world.setDifficulty(Difficulty.PEACEFUL);
    }

    private String playerSummary(Player player) {
        Location loc = player.getLocation();
        return String.format("pos=(%.1f,%.1f,%.1f) hp=%.0f food=%d sat=%.0f",
                loc.getX(), loc.getY(), loc.getZ(),
                player.getHealth(), player.getFoodLevel(), player.getSaturation());
    }

    /** 避免 reset 包依赖插件实现类，仅集中定义受控地表高度常量。 */
    private static final class ControlledSurface {
        static final int Y = 63;
    }
}
