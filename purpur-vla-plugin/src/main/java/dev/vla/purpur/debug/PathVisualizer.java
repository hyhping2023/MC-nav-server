package dev.vla.purpur.debug;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import org.bukkit.Bukkit;
import org.bukkit.Color;
import org.bukkit.Particle;
import org.bukkit.World;
import org.bukkit.entity.Player;
import org.bukkit.plugin.Plugin;
import org.bukkit.util.Vector;

/**
 * 寻路演示特效（两层导航 M10）：服务端长程航点=黄色 Dust，客户端局部路径=白色 Dust。
 *
 * <p>每个玩家最多两条活动路径（serverPath + clientPath），互不覆盖。
 * show() / showServerPath() = 黄色（服务端全局航点），showClientPath() = 白色（客户端局部路径）。
 * clear() 清除全部，超时自动消失。
 */
public final class PathVisualizer {

    private static final long REFRESH_TICKS = 10;
    private static final long DEFAULT_LIFETIME_TICKS = 1200;

    /** 黄色 Dust（服务端长程航点，1.2f 大粒度）。 */
    private static final Particle.DustOptions YELLOW_DUST = new Particle.DustOptions(Color.fromRGB(255, 255, 0), 1.2f);
    /** 白色 Dust（客户端局部路径，0.8f 小粒度）。 */
    private static final Particle.DustOptions WHITE_DUST = new Particle.DustOptions(Color.fromRGB(255, 255, 255), 0.8f);

    private final Plugin plugin;
    private final Map<UUID, ActivePath> serverPaths = new ConcurrentHashMap<>();
    private final Map<UUID, ActivePath> clientPaths = new ConcurrentHashMap<>();

    /** 一条活动路径。 */
    public record ActivePath(List<Vector> points, Vector goal, long expireTick) {
        public boolean expired(long now) {
            return expireTick <= now;
        }
    }

    public PathVisualizer(Plugin plugin) {
        this.plugin = plugin;
        plugin.getServer().getScheduler().runTaskTimer(plugin, this::refresh, 20L, REFRESH_TICKS);
    }

    /**
     * 按 path_type 设置路径特效：
     * <ul>
     *   <li>"client" → 白色 Dust（客户端局部路径）</li>
     *   <li>其他（含 null/空/"server"）→ 黄色 Dust（服务端长程航点）</li>
     * </ul>
     */
    public void show(Player player, List<Vector> points, Vector goal,
                     int lifetimeTicks, String pathType) {
        if ("client".equals(pathType)) {
            showClientPath(player, points, lifetimeTicks);
        } else {
            showServerPath(player, points, goal, lifetimeTicks);
        }
    }

    /** 向后兼容：无 path_type 时默认显示服务端黄色航点。 */
    public void show(Player player, List<Vector> points, Vector goal, int lifetimeTicks) {
        showServerPath(player, points, goal, lifetimeTicks);
    }

    /** 设置服务端长程航点：黄色 Dust + 目标红色高亮。 */
    public void showServerPath(Player player, List<Vector> points, Vector goal, int lifetimeTicks) {
        long life = lifetimeTicks > 0 ? lifetimeTicks : DEFAULT_LIFETIME_TICKS;
        List<Vector> centered = centerPoints(points);
        Vector centeredGoal = goal == null ? null
                : new Vector(goal.getX() + 0.5, goal.getY() + 0.5, goal.getZ() + 0.5);
        serverPaths.put(player.getUniqueId(),
                new ActivePath(centered, centeredGoal, Bukkit.getCurrentTick() + life));
    }

    /** 设置客户端局部路径：白色 Dust（更小更淡，无目标高亮）。 */
    public void showClientPath(Player player, List<Vector> points, int lifetimeTicks) {
        long life = lifetimeTicks > 0 ? lifetimeTicks : DEFAULT_LIFETIME_TICKS;
        clientPaths.put(player.getUniqueId(),
                new ActivePath(centerPoints(points), null, Bukkit.getCurrentTick() + life));
    }

    /** 清除玩家全部路径特效（服务器 + 客户端）。 */
    public void clear(Player player) {
        UUID id = player.getUniqueId();
        serverPaths.remove(id);
        clientPaths.remove(id);
    }

    public void clearAll() {
        serverPaths.clear();
        clientPaths.clear();
    }

    /** 周期刷新：黄色=服务器航点，白色=客户端局部路径，目标红=高亮。 */
    private void refresh() {
        if (serverPaths.isEmpty() && clientPaths.isEmpty()) return;
        long now = Bukkit.getCurrentTick();
        refreshMap(serverPaths, now, YELLOW_DUST, true);
        refreshMap(clientPaths, now, WHITE_DUST, false);
    }

    private void refreshMap(Map<UUID, ActivePath> paths, long now,
                            Particle.DustOptions dust, boolean showGoal) {
        if (paths.isEmpty()) return;
        paths.entrySet().removeIf(e -> {
            ActivePath p = e.getValue();
            Player player = Bukkit.getPlayer(e.getKey());
            if (player == null || p.expired(now)) return true;
            World world = player.getWorld();
            for (Vector pt : p.points) {
                world.spawnParticle(Particle.REDSTONE, pt.getX(), pt.getY(), pt.getZ(),
                        1, 0, 0, 0, 0, dust);
            }
            if (showGoal && p.goal != null) {
                // 目标高亮（M11.6）：方块顶上方红色粒子柱（beacon 效果）——粒子刷在
                // 方块内部会被深度遮挡、录制里几乎不可见；改为从块顶往上的竖柱，开阔
                // 地面/墙后目标都清晰可见（配合编排器 show_path(goal=…) 定位目标块）。
                double gx = p.goal.getX(), gy = p.goal.getY(), gz = p.goal.getZ();
                Particle.DustOptions goalDust = new Particle.DustOptions(Color.RED, 1.4f);
                for (int k = 0; k < 4; k++) {
                    world.spawnParticle(Particle.REDSTONE, gx, gy + 1.05 + k * 0.4, gz,
                            1, 0.1, 0, 0.1, 0, goalDust);
                }
                world.spawnParticle(Particle.END_ROD, gx, gy + 1.05, gz, 4, 0.2, 0.05, 0.2, 0);
            }
            return false;
        });
    }

    private static List<Vector> centerPoints(List<Vector> points) {
        List<Vector> centered = new ArrayList<>(points.size());
        for (Vector p : points) {
            centered.add(new Vector(p.getX() + 0.5, p.getY() + 0.5, p.getZ() + 0.5));
        }
        return centered;
    }
}
