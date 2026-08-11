package dev.vla.purpur.world;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Instant;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;
import org.bukkit.Bukkit;
import org.bukkit.Location;
import org.bukkit.Material;
import org.bukkit.World;
import org.bukkit.WorldCreator;
import org.bukkit.entity.Player;
import org.bukkit.plugin.Plugin;

/**
 * 单地表材质元世界管理器。
 *
 * <p>并发录制时，每个 worker（由玩家身份标识）拥有每种材质的一张独立地图：
 * {@code server/vla_surface_<surface-id>__<worker-id>/}。首次选择时才创建，之后同一
 * worker 反复选择同一材质会复用该存档；不同 worker 即使录制同一材质也绝不共享世界。
 * 同时将其不可变生成契约写入插件数据目录的
 * {@code surface-worlds/<worker-id>/<surface-id>/world-meta.json}。
 *
 * <p>因此任务不是在“混合材质地图”上执行：一次 episode 始终绑定到一个表面材质世界，
 * 例如 {@code sand}、{@code stone} 或 {@code grass_block}。
 */
public final class SurfaceWorldManager {

    public static final String WORLD_PREFIX = "vla_surface_";
    /** surface 与 worker 之间的世界名分隔符；surface id 自身不包含该串。 */
    private static final String WORKER_SEPARATOR = "__";
    /** 旧的无 worker world / server.properties 默认世界归入 shared 作用域。 */
    public static final String SHARED_WORKER_ID = "shared";

    private static final Map<String, Material> SURFACES;

    static {
        Map<String, Material> surfaces = new LinkedHashMap<>();
        surfaces.put("grass_block", Material.GRASS_BLOCK);
        surfaces.put("dirt", Material.DIRT);
        surfaces.put("coarse_dirt", Material.COARSE_DIRT);
        surfaces.put("sand", Material.SAND);
        surfaces.put("red_sand", Material.RED_SAND);
        surfaces.put("stone", Material.STONE);
        surfaces.put("granite", Material.GRANITE);
        surfaces.put("diorite", Material.DIORITE);
        surfaces.put("andesite", Material.ANDESITE);
        surfaces.put("clay", Material.CLAY);
        SURFACES = Collections.unmodifiableMap(surfaces);
    }

    /** 一次选择结果，供命令和 gRPC 透传。 */
    public record Selection(World world, String surfaceId, Material material, boolean created,
                            String workerId, Path metadataPath) {
    }

    private final Plugin plugin;
    private final Path metadataRoot;

    public SurfaceWorldManager(Plugin plugin) {
        this.plugin = plugin;
        this.metadataRoot = plugin.getDataFolder().toPath().resolve("surface-worlds");
    }

    public static Map<String, Material> availableSurfaces() {
        return SURFACES;
    }

    public static Material resolveSurface(String id) {
        if (id == null) {
            return null;
        }
        return SURFACES.get(id.trim().toLowerCase(Locale.ROOT));
    }

    /**
     * 返回 worker 专属世界名。world 名会落到文件系统，worker id 必须先正规化。
     */
    public static String worldName(String surfaceId, String workerId) {
        return WORLD_PREFIX + surfaceId + WORKER_SEPARATOR + normalizeWorkerId(workerId);
    }

    /** 从受控世界名恢复其表面材质；未知/旧默认世界回退草方块。 */
    public static Material surfaceForWorldName(String worldName) {
        if (worldName != null && worldName.startsWith(WORLD_PREFIX)) {
            String remainder = worldName.substring(WORLD_PREFIX.length());
            int workerAt = remainder.indexOf(WORKER_SEPARATOR);
            String surfaceId = workerAt >= 0 ? remainder.substring(0, workerAt) : remainder;
            Material material = SURFACES.get(surfaceId);
            if (material != null) {
                return material;
            }
        }
        return Material.GRASS_BLOCK;
    }

    /** 从世界名恢复 worker 作用域；兼容旧的 {@code vla_surface_sand} 世界。 */
    public static String workerForWorldName(String worldName) {
        if (worldName != null && worldName.startsWith(WORLD_PREFIX)) {
            String remainder = worldName.substring(WORLD_PREFIX.length());
            int workerAt = remainder.indexOf(WORKER_SEPARATOR);
            if (workerAt >= 0 && workerAt + WORKER_SEPARATOR.length() < remainder.length()) {
                return normalizeWorkerId(remainder.substring(workerAt + WORKER_SEPARATOR.length()));
            }
        }
        return SHARED_WORKER_ID;
    }

    /**
     * 选择/创建指定单材质世界并传送玩家至固定出生点。此方法必须在 Bukkit 主线程调用。
     */
    public Selection select(Player player, String requestedSurface, long seed) {
        String surfaceId = normalizeSurface(requestedSurface);
        Material material = SURFACES.get(surfaceId);
        if (material == null) {
            throw new IllegalArgumentException("unknown surface: " + requestedSurface
                    + " (available: " + String.join(",", SURFACES.keySet()) + ")");
        }

        // 玩家名在 offline-mode 下是稳定身份；一个 worker 固定使用一个 player 名，
        // 因而能把世界、时间天气、reset 基线与任务目标完全隔离开。
        String workerId = normalizeWorkerId(player.getName());
        String worldName = worldName(surfaceId, workerId);
        World world = Bukkit.getWorld(worldName);
        boolean created = false;
        if (world == null) {
            WorldCreator creator = new WorldCreator(worldName)
                    .environment(World.Environment.NORMAL)
                    .seed(seed)
                    .generateStructures(false)
                    .generator(new ControlledPlainsGenerator(material));
            world = Bukkit.createWorld(creator);
            if (world == null) {
                throw new IllegalStateException("failed to create world: " + worldName);
            }
            created = true;
        }

        // 只设置出生点/传送，不写任何地形方块。生成器保证脚下是 y=63 的固定表面。
        world.setSpawnLocation(0, ControlledPlainsGenerator.SURFACE_Y + 1, 0);
        player.teleport(new Location(world, 0.5, ControlledPlainsGenerator.SURFACE_Y + 1,
                0.5, 0f, 0f));

        Path metadata = writeMetadata(surfaceId, workerId, material, world, seed);
        plugin.getLogger().info("[surface-world] selected " + worldName + " surface="
                + material.getKey() + " worker=" + workerId + " created=" + created
                + " metadata=" + metadata);
        return new Selection(world, surfaceId, material, created, workerId, metadata);
    }

    /**
     * 为已由 server.properties 创建的默认元世界补写元数据。默认世界本身也是一个可复用
     * 的单材质存档，只是它不经过 {@link #select(Player, String, long)} 的首次创建流程。
     */
    public Path registerExistingWorld(World world) {
        String name = world.getName();
        String surfaceId = surfaceIdForWorldName(name);
        String workerId = workerForWorldName(name);
        Material material = surfaceForWorldName(name);
        return writeMetadata(surfaceId, workerId, material, world, world.getSeed());
    }

    private Path writeMetadata(String surfaceId, String workerId, Material material, World world,
                               long requestedSeed) {
        Path path = metadataRoot.resolve(workerId).resolve(surfaceId).resolve("world-meta.json");
        String json = "{\n"
                + "  \"schema\": \"vla.surface-world.v2\",\n"
                + "  \"world_name\": \"" + world.getName() + "\",\n"
                + "  \"worker_id\": \"" + workerId + "\",\n"
                + "  \"surface_id\": \"" + surfaceId + "\",\n"
                + "  \"surface_material\": \"" + material.getKey() + "\",\n"
                + "  \"map_seed\": " + world.getSeed() + ",\n"
                + "  \"world_seed\": " + world.getSeed() + ",\n"
                + "  \"requested_seed\": " + requestedSeed + ",\n"
                + "  \"surface_y\": " + ControlledPlainsGenerator.SURFACE_Y + ",\n"
                + "  \"air_from_y\": " + (ControlledPlainsGenerator.SURFACE_Y + 1) + ",\n"
                + "  \"generator\": \"vla-purpur:single_surface\",\n"
                + "  \"created_or_selected_at\": \"" + Instant.now() + "\"\n"
                + "}\n";
        try {
            Files.createDirectories(path.getParent());
            Files.writeString(path, json, StandardCharsets.UTF_8);
        } catch (IOException e) {
            throw new IllegalStateException("cannot write surface world metadata: " + path, e);
        }
        return path;
    }

    private static String normalizeSurface(String requestedSurface) {
        if (requestedSurface == null || requestedSurface.trim().isEmpty()) {
            return "grass_block";
        }
        String id = requestedSurface.trim().toLowerCase(Locale.ROOT);
        if (id.startsWith("minecraft:")) {
            id = id.substring("minecraft:".length());
        }
        return id;
    }

    private static String surfaceIdForWorldName(String worldName) {
        if (worldName != null && worldName.startsWith(WORLD_PREFIX)) {
            String remainder = worldName.substring(WORLD_PREFIX.length());
            int workerAt = remainder.indexOf(WORKER_SEPARATOR);
            String surfaceId = workerAt >= 0 ? remainder.substring(0, workerAt) : remainder;
            if (SURFACES.containsKey(surfaceId)) {
                return surfaceId;
            }
        }
        return "grass_block";
    }

    /**
     * 世界名和元数据目录只能使用保守字符。Minecraft 玩家名通常已经满足此规则；
     * 此处仍做防御性正规化，使命令/gRPC 的异常输入不会写出路径分隔符。
     */
    public static String normalizeWorkerId(String requestedWorkerId) {
        if (requestedWorkerId == null || requestedWorkerId.trim().isEmpty()) {
            return SHARED_WORKER_ID;
        }
        String normalized = requestedWorkerId.trim().toLowerCase(Locale.ROOT)
                .replaceAll("[^a-z0-9_-]", "_");
        if (normalized.isEmpty()) {
            return SHARED_WORKER_ID;
        }
        // 保持 Bukkit world 名和文件路径简洁；worker 玩家名本身最大 16 字符。
        return normalized.length() <= 32 ? normalized : normalized.substring(0, 32);
    }
}
