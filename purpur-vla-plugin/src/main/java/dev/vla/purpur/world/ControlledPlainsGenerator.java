package dev.vla.purpur.world;

import java.util.List;
import java.util.Random;
import org.bukkit.HeightMap;
import org.bukkit.Material;
import org.bukkit.World;
import org.bukkit.block.Biome;
import org.bukkit.generator.BiomeProvider;
import org.bukkit.generator.ChunkGenerator;
import org.bukkit.generator.WorldInfo;

/**
 * 受控平面世界生成器。
 *
 * <p>它不在世界启动后修地形，而是在区块首次生成时直接写出固定高度的地面：
 *
 * <ul>
 *   <li>{@code y=-64}：基岩；</li>
 *   <li>{@code -63..62}：石头；</li>
 *   <li>{@code y=63}：该世界固定的一种完整表面方块；</li>
 *   <li>{@code y>=64}：不写任何方块，即保持空气。</li>
 * </ul>
 *
 * <p>每个 {@link SurfaceWorldManager} 世界实例只使用一种表面材质。这样可将草地、沙地、
 * 石地等作为独立、可持久化的元世界存档，而不会在同一 episode 混入不同地表。
 *
 * <p>表面仅使用完整、稳定的方块，避免水、草丛、雪层、半砖或会伤害/阻碍导航的方块。
 * 生成器关闭洞穴、结构、装饰和生物生成，因此不会在海平面以上自然生成任何方块。
 */
public final class ControlledPlainsGenerator extends ChunkGenerator {

    public static final int SURFACE_Y = 63;
    private static final int MIN_WORLD_Y = -64;

    private final Material surfaceMaterial;

    public ControlledPlainsGenerator(Material surfaceMaterial) {
        if (surfaceMaterial == null || !surfaceMaterial.isSolid()) {
            throw new IllegalArgumentException("surface material must be a solid block");
        }
        this.surfaceMaterial = surfaceMaterial;
    }

    private static final BiomeProvider PLAINS_BIOME = new BiomeProvider() {
        @Override
        public Biome getBiome(WorldInfo worldInfo, int x, int y, int z) {
            return Biome.PLAINS;
        }

        @Override
        public List<Biome> getBiomes(WorldInfo worldInfo) {
            return List.of(Biome.PLAINS);
        }
    };

    @Override
    public void generateNoise(WorldInfo worldInfo, Random random, int chunkX, int chunkZ,
            ChunkData chunkData) {
        int minY = Math.max(worldInfo.getMinHeight(), MIN_WORLD_Y);
        int maxY = Math.min(worldInfo.getMaxHeight() - 1, SURFACE_Y);
        if (minY > maxY) {
            return;
        }

        chunkData.setRegion(0, minY, 0, 16, SURFACE_Y, 16, Material.STONE);
        if (minY <= MIN_WORLD_Y) {
            chunkData.setRegion(0, MIN_WORLD_Y, 0, 16, MIN_WORLD_Y + 1, 16,
                    Material.BEDROCK);
        }
        for (int localX = 0; localX < 16; localX++) {
            for (int localZ = 0; localZ < 16; localZ++) {
                chunkData.setBlock(localX, SURFACE_Y, localZ, surfaceMaterial);
            }
        }
    }

    @Override
    public BiomeProvider getDefaultBiomeProvider(WorldInfo worldInfo) {
        return PLAINS_BIOME;
    }

    @Override
    public int getBaseHeight(WorldInfo worldInfo, Random random, int x, int z,
            HeightMap heightMap) {
        return SURFACE_Y + 1;
    }

    @Override
    public boolean canSpawn(World world, int x, int z) {
        return true;
    }

    @Override
    public boolean isParallelCapable() {
        return true;
    }

    @Override
    public boolean shouldGenerateNoise() {
        return false;
    }

    @Override
    public boolean shouldGenerateSurface() {
        return false;
    }

    @Override
    public boolean shouldGenerateBedrock() {
        return false;
    }

    @Override
    public boolean shouldGenerateCaves() {
        return false;
    }

    @Override
    public boolean shouldGenerateDecorations() {
        return false;
    }

    @Override
    public boolean shouldGenerateMobs() {
        return false;
    }

    @Override
    public boolean shouldGenerateStructures() {
        return false;
    }

    public Material surfaceMaterial() {
        return surfaceMaterial;
    }
}
