package dev.vla.purpur.player;

import java.nio.charset.StandardCharsets;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import org.bukkit.Bukkit;
import org.bukkit.Location;
import org.bukkit.OfflinePlayer;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.Listener;
import org.bukkit.event.entity.PlayerDeathEvent;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.event.player.PlayerQuitEvent;
import org.bukkit.plugin.Plugin;

/**
 * 多智能体玩家管理（DESIGN.md §4.8，任务 2.4）。
 *
 * <p>离线 UUID 稳定映射：mineflayer 以离线模式登录时，服务端实际使用的 UUID 就是
 * {@code UUID.nameUUIDFromBytes("OfflinePlayer:"+name)}；玩家会话记录上次 reset 的
 * 出生点与区域（供无参 reset/verify 复用，保证确定性），断线清理、死亡 2 秒后自动重生。
 */
public final class AgentManager implements Listener {

    /** 玩家会话（由上次 reset 记录出生点与区域）。 */
    public static final class AgentSession {
        public final String playerName;
        /** 出生点（死亡重生/复位目标）。 */
        public Location spawn;
        /** 上次 reset 的区域中心与半宽（无参 reset/verify 复用）。 */
        public Integer regionCenterX;
        public Integer regionCenterY;
        public Integer regionCenterZ;
        public Integer regionHalfExtent;

        AgentSession(String playerName) {
            this.playerName = playerName;
        }

        public boolean hasRegion() {
            return regionCenterX != null && regionCenterY != null && regionCenterZ != null
                    && regionHalfExtent != null;
        }
    }

    private final Plugin plugin;
    /** 在线连接会话：join 创建、quit 清理（死亡自动重生/join 日志用）。 */
    private final Map<UUID, AgentSession> sessions = new ConcurrentHashMap<>();
    /**
     * 持久化区域记录：记录上次 reset 的出生点与区域，玩家断线重连后仍保留，
     * 保证「无参 reset/verify 复用上次区域」的确定性（C2==C1）。
     */
    private final Map<UUID, AgentSession> persistent = new ConcurrentHashMap<>();

    public AgentManager(Plugin plugin) {
        this.plugin = plugin;
    }

    /** 离线 UUID 稳定映射（与服务端离线登录一致，§4.8）。 */
    public static UUID offlineUuid(String name) {
        return UUID.nameUUIDFromBytes(("OfflinePlayer:" + name).getBytes(StandardCharsets.UTF_8));
    }

    /** 解析在线玩家：优先 getPlayerExact，回退 getOfflinePlayer().getPlayer()，找不到返回 null。 */
    public Player resolve(String name) {
        Player exact = Bukkit.getPlayerExact(name);
        if (exact != null) {
            return exact;
        }
        OfflinePlayer offline = Bukkit.getOfflinePlayer(name);
        return offline != null ? offline.getPlayer() : null;
    }

    public AgentSession getSession(Player player) {
        AgentSession live = sessions.get(player.getUniqueId());
        if (live != null) {
            return live;
        }
        return persistent.get(player.getUniqueId());
    }

    public AgentSession getOrCreateSession(Player player) {
        return sessions.computeIfAbsent(player.getUniqueId(),
                uuid -> new AgentSession(player.getName()));
    }

    /** 记录上次 reset 使用的出生点与区域（写入持久区，重连后仍可用）。 */
    public void recordSessionRegion(Player player, Location spawn,
                                    int cx, int cy, int cz, int halfExtent) {
        AgentSession record = persistent.computeIfAbsent(player.getUniqueId(),
                uuid -> new AgentSession(player.getName()));
        record.spawn = spawn.clone();
        record.regionCenterX = cx;
        record.regionCenterY = cy;
        record.regionCenterZ = cz;
        record.regionHalfExtent = halfExtent;
        AgentSession live = sessions.get(player.getUniqueId());
        if (live != null) {
            live.spawn = spawn.clone();
            live.regionCenterX = cx;
            live.regionCenterY = cy;
            live.regionCenterZ = cz;
            live.regionHalfExtent = halfExtent;
        }
    }

    @EventHandler
    public void onJoin(PlayerJoinEvent event) {
        Player player = event.getPlayer();
        AgentSession session = getOrCreateSession(player);
        // 重连玩家从持久区恢复上次 reset 的出生点/区域
        AgentSession record = persistent.get(player.getUniqueId());
        if (record != null) {
            session.spawn = record.spawn != null ? record.spawn.clone() : null;
            session.regionCenterX = record.regionCenterX;
            session.regionCenterY = record.regionCenterY;
            session.regionCenterZ = record.regionCenterZ;
            session.regionHalfExtent = record.regionHalfExtent;
        }
        if (session.spawn == null) {
            session.spawn = player.getLocation().clone();
        }
        plugin.getLogger().info("[vla-purpur] agent join: " + player.getName()
                + " uuid=" + player.getUniqueId()
                + " offlineUuid=" + offlineUuid(player.getName())
                + " at " + player.getLocation());
    }

    @EventHandler
    public void onQuit(PlayerQuitEvent event) {
        sessions.remove(event.getPlayer().getUniqueId());
        plugin.getLogger().info("[vla-purpur] agent quit: " + event.getPlayer().getName());
    }

    @EventHandler
    public void onDeath(PlayerDeathEvent event) {
        Player player = event.getEntity();
        AgentSession session = sessions.get(player.getUniqueId());
        if (session == null) {
            return;
        }
        plugin.getLogger().info("[vla-purpur] agent death: " + player.getName()
                + ", scheduling auto respawn in 2s");
        Bukkit.getScheduler().runTaskLater(plugin, () -> {
            if (!player.isOnline()) {
                return;
            }
            if (player.isDead()) {
                player.spigot().respawn();
            }
            player.setHealth(20.0);
            Location spawn = session.spawn != null
                    ? session.spawn : player.getWorld().getSpawnLocation();
            player.teleport(spawn);
            plugin.getLogger().info("[vla-purpur] agent respawned: " + player.getName()
                    + " at " + spawn);
        }, 40L);
    }
}
