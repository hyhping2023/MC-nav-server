package dev.vla.purpur;

import dev.vla.purpur.debug.PathVisualizer;
import dev.vla.purpur.grpc.MainThreadDispatcher;
import dev.vla.purpur.grpc.VlaGrpcService;
import dev.vla.purpur.path.DirectPathPlanner;
import dev.vla.purpur.player.AgentManager;
import dev.vla.purpur.reset.ResetEngine;
import dev.vla.purpur.task.TaskManager;
import dev.vla.purpur.task.TaskSpec;
import dev.vla.purpur.world.ControlledPlainsGenerator;
import dev.vla.purpur.world.SurfaceWorldManager;
import dev.vla.purpur.world.VoxelReader;
import io.grpc.Server;
import io.grpc.netty.shaded.io.grpc.netty.NettyServerBuilder;
import io.grpc.protobuf.services.ProtoReflectionService;
import java.net.InetSocketAddress;
import java.nio.ByteBuffer;
import org.bukkit.Bukkit;
import org.bukkit.Location;
import org.bukkit.World;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.Listener;
import org.bukkit.event.block.BlockBreakEvent;
import org.bukkit.event.block.BlockPlaceEvent;
import org.bukkit.event.entity.EntityDeathEvent;
import org.bukkit.event.player.PlayerMoveEvent;
import org.bukkit.plugin.java.JavaPlugin;
import org.bukkit.util.BlockVector;
import vla.Vla.VoxelReply;

/**
 * VLA research environment gRPC bridge（M1 通信底座 + M4 世界引擎 + M5 任务 + M6 状态/寻路）。
 *
 * <p>onEnable 启动 gRPC server（127.0.0.1:50051），注册 {@code /vla} 命令：
 * {@code status}（探测服务器状态）、{@code reset <player> [halfExtent]}、
 * {@code verify <player>}、{@code task <player> <task>}、{@code taskinfo <player>}、
 * {@code voxels <player> [r]}、{@code path <x> <y> <z>}；
 * 同时监听方块破坏/放置、实体死亡、玩家移动事件转发给 {@link TaskManager}（§4.7）。
 * onDisable 优雅关停 gRPC server。
 */
public class VlaPlugin extends JavaPlugin implements Listener {

    /** gRPC 监听地址。 */
    public static final String GRPC_HOST = "127.0.0.1";
    /** gRPC 监听端口（与 vla_env 客户端契约一致）。 */
    public static final int GRPC_PORT = 50051;

    /** M8：tick 广播插件消息频道（DESIGN.md §5.6）。 */
    public static final String TICK_CHANNEL = "vla:tick";

    private Server grpcServer;
    private AgentManager agentManager;
    private ResetEngine resetEngine;
    private TaskManager taskManager;
    private PathVisualizer pathVisualizer;
    private SurfaceWorldManager surfaceWorldManager;

    /** 单材质元世界的自然地面保护：y<=surfaceY 的方块不可破坏。 */
    private volatile boolean groundProtected = false;
    private volatile int protectedSeaLevel = ControlledPlainsGenerator.SURFACE_Y;
    /** 默认元世界完成创建前不接受任务目标投放。 */
    private volatile boolean worldReady = false;

    public void setGroundProtected(boolean v) {
        this.groundProtected = v;
    }

    public void setProtectedSeaLevel(int seaLevel) {
        this.protectedSeaLevel = seaLevel;
    }

    public boolean isWorldReady() {
        return worldReady;
    }

    public SurfaceWorldManager.Selection selectSurfaceWorld(Player player, String surface, long seed) {
        if (surfaceWorldManager == null) {
            throw new IllegalStateException("surface world manager is not initialized");
        }
        SurfaceWorldManager.Selection selected = surfaceWorldManager.select(player, surface, seed);
        // 新建并发 worker world 同样需要进入“受控环境就绪”状态。地面保护规则
        // 不依赖 primary world；所有 vla_surface_* 世界均使用相同 Y=63 地表契约。
        setGroundProtected(true);
        setProtectedSeaLevel(ControlledPlainsGenerator.SURFACE_Y);
        worldReady = true;
        return selected;
    }

    /**
     * Bukkit 在创建 {@code bukkit.yml} 中配置为 {@code vla-purpur:controlled_plains}
     * 的世界时调用。生成器直接生成单材质平面，不做启动后地形修改。
     */
    @Override
    public ControlledPlainsGenerator getDefaultWorldGenerator(String worldName, String id) {
        if ("controlled_plains".equalsIgnoreCase(id)) {
            getLogger().info("[world] providing controlled_plains generator for " + worldName);
            return new ControlledPlainsGenerator(SurfaceWorldManager.surfaceForWorldName(worldName));
        }
        return null;
    }

    @Override
    public void onEnable() {
        MainThreadDispatcher.init(this);

        this.agentManager = new AgentManager(this);
        this.resetEngine = new ResetEngine();
        this.taskManager = new TaskManager(this);
        this.pathVisualizer = new PathVisualizer(this);
        this.surfaceWorldManager = new SurfaceWorldManager(this);
        saveDefaultConfig();
        getLogger().info("path visualizer enabled="
                + getConfig().getBoolean("path-visualizer.enabled", false));
        Bukkit.getPluginManager().registerEvents(agentManager, this);
        // M5：任务相关事件（方块破坏/放置、实体死亡、玩家移动 → TaskManager 判定）
        Bukkit.getPluginManager().registerEvents(this, this);

        // M11.5：data-driven 任务（难点②）——plugins/VlaPlugin/tasks/*.json，
        // 同 id 覆盖内置；`vla reloadtasks` 热重载。
        int loaded = dev.vla.purpur.task.TaskRegistry.loadFromDir(
                new java.io.File(getDataFolder(), "tasks"), getLogger());
        if (loaded > 0) {
            getLogger().info("[task] loaded " + loaded + " JSON task(s)");
        }

        // M8：vla:tick 频道（§5.6）——每 tick 向在线玩家广播权威 server_tick + 墙钟，
        // 客户端据此给帧打 last_server_tick，供 Python lockstep 对齐（§9.3）。
        Bukkit.getMessenger().registerOutgoingPluginChannel(this, TICK_CHANNEL);
        Bukkit.getScheduler().runTaskTimer(this, this::broadcastTick, 20L, 1L);

        // 受控环境由自定义 ChunkGenerator 首次生成：y=63 为固定单材质地表，y>=64
        // 为空气。插件只管理存档、元数据和地面保护，绝不启动后回填/削平地形。
        World primaryWorld = Bukkit.getWorlds().isEmpty() ? null : Bukkit.getWorlds().get(0);
        if (primaryWorld != null) {
            markControlledWorldReady(primaryWorld);
        } else {
            // load: STARTUP 时插件会早于默认世界启用；延后一 tick，等世界创建和自定义
            // generator 挂载完成后再开启任务与地面保护。
            Bukkit.getScheduler().runTask(this, () -> {
                World createdWorld = Bukkit.getWorlds().isEmpty() ? null : Bukkit.getWorlds().get(0);
                if (createdWorld == null) {
                    getLogger().warning("[world] primary world was not created after startup");
                    return;
                }
                markControlledWorldReady(createdWorld);
            });
        }

        try {
            grpcServer = NettyServerBuilder
                    .forAddress(new InetSocketAddress(GRPC_HOST, GRPC_PORT))
                    .addService(new VlaGrpcService(this))
                    // 反射服务：grpc_cli / grpcurl 可枚举接口，便于调试
                    .addService(ProtoReflectionService.newInstance())
                    .build()
                    .start();
            getLogger().info("gRPC server started on " + GRPC_HOST + ":" + GRPC_PORT);
        } catch (Exception e) {
            getLogger().severe("Failed to start gRPC server: " + e);
            e.printStackTrace();
            Bukkit.getPluginManager().disablePlugin(this);
            return;
        }

        getCommand("vla").setExecutor(this::onVlaCommand);
        getLogger().info("vla-purpur enabled (tick=" + Bukkit.getCurrentTick() + ")");
    }

    @Override
    public void onDisable() {
        if (grpcServer != null) {
            grpcServer.shutdown();
            try {
                if (!grpcServer.awaitTermination(2, java.util.concurrent.TimeUnit.SECONDS)) {
                    getLogger().warning("gRPC server did not terminate in 2s, forcing shutdown");
                    grpcServer.shutdownNow();
                }
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                getLogger().warning("Interrupted while shutting down gRPC server");
                grpcServer.shutdownNow();
            }
            grpcServer = null;
        }
        getLogger().info("vla-purpur disabled");
    }

    public AgentManager getAgentManager() {
        return agentManager;
    }

    public ResetEngine getResetEngine() {
        return resetEngine;
    }

    public TaskManager getTaskManager() {
        return taskManager;
    }

    /** demo 路径可视化（A* 航点粒子特效，gRPC ShowPath）。 */
    public PathVisualizer getPathVisualizer() {
        return pathVisualizer;
    }

    /**
     * M8：每 tick 向所有在线玩家广播 `vla:tick` payload（主线程，runTaskTimer 1L）。
     *
     * <p>payload 12B：`[4B int serverTick=Bukkit.getCurrentTick()][8B long wallNanos=
     * System.nanoTime()]`（大端）。无玩家时不广播（省 CPU）。日志 debug 级。
     */
    private void broadcastTick() {
        if (Bukkit.getOnlinePlayers().isEmpty()) {
            return;
        }
        int tick = Bukkit.getCurrentTick();
        long wallNanos = System.nanoTime();
        byte[] payload = new byte[12];
        ByteBuffer.wrap(payload).putInt(tick).putLong(wallNanos);
        for (Player p : Bukkit.getOnlinePlayers()) {
            p.sendPluginMessage(this, TICK_CHANNEL, payload);
        }
    }

    private void markControlledWorldReady(World world) {
        setGroundProtected(true);
        setProtectedSeaLevel(ControlledPlainsGenerator.SURFACE_Y);
        worldReady = true;
        java.nio.file.Path metadata = surfaceWorldManager.registerExistingWorld(world);
        getLogger().info("[world] controlled single-surface plains ready at "
                + world.getSpawnLocation() + " metadata=" + metadata);
    }

    /** 供命令/日志探测 gRPC server 是否仍处于运行状态。 */
    public boolean isGrpcListening() {
        return grpcServer != null && !grpcServer.isShutdown() && !grpcServer.isTerminated();
    }

    /** 处理 {@code /vla} 子命令：status / reset / verify / task / taskinfo / voxels / path。 */
    private boolean onVlaCommand(CommandSender sender, Command command, String label, String[] args) {
        if (args.length == 0) {
            usage(sender);
            return true;
        }
        switch (args[0].toLowerCase(java.util.Locale.ROOT)) {
            case "status":
                return vlaStatus(sender);
            case "reset":
                return vlaReset(sender, args);
            case "verify":
                return vlaVerify(sender, args);
            case "task":
                return vlaTask(sender, args);
            case "taskinfo":
                return vlaTaskInfo(sender, args);
            case "voxels":
                return vlaVoxels(sender, args);
            case "path":
                return vlaPath(sender, args);
            case "surface":
                return vlaSurface(sender, args);
            case "reloadtasks": {
                int n = dev.vla.purpur.task.TaskRegistry.loadFromDir(
                        new java.io.File(getDataFolder(), "tasks"), getLogger());
                sender.sendMessage("[vla-purpur] reloadtasks: loaded " + n + " JSON task(s), "
                        + dev.vla.purpur.task.TaskRegistry.all().size() + " total");
                return true;
            }
            default:
                usage(sender);
                return true;
        }
    }

    private void usage(CommandSender sender) {
        sender.sendMessage("Usage: /vla status | /vla reset <player> [halfExtent] | /vla verify <player>"
                + " | /vla task <player> <task> | /vla taskinfo <player>"
                + " | /vla voxels <player> [r] | /vla path <x> <y> <z> [mode]"
                + " | /vla surface <player> <surface> [seed]"
                + " | /vla reloadtasks");
    }

    /** {@code /vla status}：输出 tick/tps/gRPC 端口（保留 M1）。 */
    private boolean vlaStatus(CommandSender sender) {
        sender.sendMessage("[vla-purpur] status:");
        sender.sendMessage("  server_tick = " + Bukkit.getCurrentTick());
        double[] tps = Bukkit.getTPS();
        sender.sendMessage(String.format("  tps         = %.2f (1m=%.2f 5m=%.2f)",
                tps[0], tps.length > 1 ? tps[1] : 0, tps.length > 2 ? tps[2] : 0));
        sender.sendMessage("  version     = " + Bukkit.getVersion());
        sender.sendMessage("  worlds      = " + Bukkit.getWorlds().size()
                + " (first=" + (Bukkit.getWorlds().isEmpty() ? "<none>" : Bukkit.getWorlds().get(0).getName()) + ")");
        sender.sendMessage("  grpc        = " + GRPC_HOST + ":" + GRPC_PORT
                + " (listening=" + isGrpcListening() + ")");
        return true;
    }

    /**
     * {@code /vla reset <player> [halfExtent]}：世界重置。
     *
     * <p>区域缺省复用该玩家上次 reset 的区域（保证二次 reset 确定性，C2==C1）；
     * 无历史则用玩家当前位置。
     */
    private boolean vlaReset(CommandSender sender, String[] args) {
        if (args.length < 2) {
            sender.sendMessage("Usage: /vla reset <player> [halfExtent]");
            return true;
        }
        Player player = agentManager.resolve(args[1]);
        if (player == null) {
            sender.sendMessage("[vla-purpur] reset: player not found: " + args[1]);
            return true;
        }
        ResetEngine.ResetSpec spec = new ResetEngine.ResetSpec();
        AgentManager.AgentSession session = agentManager.getSession(player);
        if (session != null && session.hasRegion()) {
            spec.setCenter(session.regionCenterX, session.regionCenterY, session.regionCenterZ);
            spec.halfExtent = session.regionHalfExtent;
        } else {
            Location loc = player.getLocation();
            spec.setCenter(loc.getBlockX(), loc.getBlockY(), loc.getBlockZ());
        }
        if (args.length >= 3) {
            try {
                spec.halfExtent = Integer.parseInt(args[2]);
            } catch (NumberFormatException e) {
                sender.sendMessage("[vla-purpur] reset: bad halfExtent: " + args[2]);
                return true;
            }
        }

        ResetEngine.ResetResult result = resetEngine.reset(player, spec);
        Location spawn = player.getWorld().getSpawnLocation();
        agentManager.recordSessionRegion(player, spawn,
                spec.centerX, spec.centerY, spec.centerZ, spec.halfExtent);

        Location p = player.getLocation();
        sender.sendMessage(String.format(
                "[vla-purpur] reset %s ok=%b checksum=%s entities=%d pos=(%.1f,%.1f,%.1f) time=%d tick=%d",
                args[1], result.ok, result.checksum, result.entityCount,
                p.getX(), p.getY(), p.getZ(),
                player.getWorld().getTime(), result.serverTick));
        return true;
    }

    /** {@code /vla verify <player>}：实时区域 checksum/实体数/时间/玩家摘要。 */
    private boolean vlaVerify(CommandSender sender, String[] args) {
        if (args.length < 2) {
            sender.sendMessage("Usage: /vla verify <player>");
            return true;
        }
        Player player = agentManager.resolve(args[1]);
        if (player == null) {
            sender.sendMessage("[vla-purpur] verify: player not found: " + args[1]);
            return true;
        }
        World world = player.getWorld();
        AgentManager.AgentSession session = agentManager.getSession(player);
        int cx;
        int cy;
        int cz;
        int extent;
        if (session != null && session.hasRegion()) {
            cx = session.regionCenterX;
            cy = session.regionCenterY;
            cz = session.regionCenterZ;
            extent = session.regionHalfExtent;
        } else {
            Location loc = player.getLocation();
            cx = loc.getBlockX();
            cy = loc.getBlockY();
            cz = loc.getBlockZ();
            extent = 16;
        }
        ResetEngine.RegionVerify v = resetEngine.verify(world, cx, cy, cz, extent, player);
        sender.sendMessage(String.format(
                "[vla-purpur] verify %s region=(%d,%d,%d) half=%d checksum=%s entities=%d time=%d player:%s",
                args[1], cx, cy, cz, extent, v.checksum, v.entityCount, v.time, v.playerSummary));
        return true;
    }

    /** {@code /vla task <player> <task>}：为该玩家设置任务（重置 episode 状态）。 */
    private boolean vlaTask(CommandSender sender, String[] args) {
        if (args.length < 3) {
            sender.sendMessage("Usage: /vla task <player> <task>");
            return true;
        }
        Player player = agentManager.resolve(args[1]);
        if (player == null) {
            sender.sendMessage("[vla-purpur] task: player not found: " + args[1]);
            return true;
        }
        if (!worldReady) {
            sender.sendMessage("[vla-purpur] task: world initialization is still running");
            return true;
        }
        TaskSpec spec = taskManager.setTask(player, args[2], 0L);
        if (spec == null) {
            sender.sendMessage("[vla-purpur] task: unknown task: " + args[2]
                    + " (known: collect_wood, craft_planks, collect_stone, kill_animal)");
            return true;
        }
        sender.sendMessage(String.format("[vla-purpur] task %s set: id=%s instruction=\"%s\" timeout=%d",
                args[1], spec.id(), spec.instruction(), spec.timeoutTicks()));
        return true;
    }

    /** {@code /vla taskinfo <player>}：任务状态（计数/进度/成功）。 */
    private boolean vlaTaskInfo(CommandSender sender, String[] args) {
        if (args.length < 2) {
            sender.sendMessage("Usage: /vla taskinfo <player>");
            return true;
        }
        Player player = agentManager.resolve(args[1]);
        if (player == null) {
            sender.sendMessage("[vla-purpur] taskinfo: player not found: " + args[1]);
            return true;
        }
        TaskManager.EpisodeState st = taskManager.getState(player);
        if (st == null || st.task == null) {
            sender.sendMessage("[vla-purpur] taskinfo " + args[1] + ": no task set");
            return true;
        }
        sender.sendMessage(String.format(
                "[vla-purpur] taskinfo %s: id=%s instruction=\"%s\" progress=%.2f success=%b "
                        + "steps=%d timeout=%d counters=%s",
                args[1], st.task.id(), st.task.instruction(),
                taskManager.progress(player, st), st.success,
                st.steps, st.task.timeoutTicks(), st.counters));
        return true;
    }

    /** {@code /vla voxels <player> [r]}：读取并打印局部体素摘要（palette/data）。 */
    private boolean vlaVoxels(CommandSender sender, String[] args) {
        if (args.length < 2) {
            sender.sendMessage("Usage: /vla voxels <player> [r]");
            return true;
        }
        Player player = agentManager.resolve(args[1]);
        if (player == null) {
            sender.sendMessage("[vla-purpur] voxels: player not found: " + args[1]);
            return true;
        }
        int r = 16;
        if (args.length >= 3) {
            try {
                r = Integer.parseInt(args[2]);
            } catch (NumberFormatException e) {
                sender.sendMessage("[vla-purpur] voxels: bad radius: " + args[2]);
                return true;
            }
        }
        Location loc = player.getLocation();
        VoxelReply vr = VoxelReader.read(player.getWorld(),
                new BlockVector(loc.getBlockX(), loc.getBlockY(), loc.getBlockZ()), r);
        sender.sendMessage(String.format(
                "[vla-purpur] voxels %s r=%d origin=(%d,%d,%d) size=%d palette=%d data=%d",
                args[1], r, vr.getOriginX(), vr.getOriginY(), vr.getOriginZ(),
                vr.getSize(), vr.getPaletteCount(), vr.getDataCount()));
        StringBuilder sample = new StringBuilder("[vla-purpur]   palette: ");
        int show = Math.min(5, vr.getPaletteCount());
        for (int i = 0; i < show; i++) {
            sample.append('[').append(i).append("]=").append(vr.getPalette(i)).append(' ');
        }
        sender.sendMessage(sample.toString());
        return true;
    }

    /** {@code /vla path <x> <y> <z> [mode]}：以执行者为起点寻路到目标，打印航点数/动作序列。 */
    private boolean vlaPath(CommandSender sender, String[] args) {
        if (args.length < 4) {
            sender.sendMessage("Usage: /vla path <x> <y> <z> [mode: default|dig|place]");
            return true;
        }
        Player player;
        if (sender instanceof Player p) {
            player = p;
        } else {
            player = Bukkit.getOnlinePlayers().stream().findFirst().orElse(null);
            if (player == null) {
                sender.sendMessage("[vla-purpur] path: no online player to start from");
                return true;
            }
        }
        try {
            int x = Integer.parseInt(args[1]);
            int y = Integer.parseInt(args[2]);
            int z = Integer.parseInt(args[3]);
            String mode = args.length >= 5 ? args[4] : "default";
            Location loc = player.getLocation();
            DirectPathPlanner.PathResult result = DirectPathPlanner.findPath(player.getWorld(),
                    new BlockVector(loc.getBlockX(), loc.getBlockY(), loc.getBlockZ()),
                    new BlockVector(x, y, z), mode);
            if (!result.found) {
                sender.sendMessage("[vla-purpur] path: not found (expanded=" + result.expanded + ")");
                return true;
            }
            String first = result.waypoints.isEmpty() ? "<none>" : result.waypoints.get(0).toString();
            String last = result.waypoints.isEmpty()
                    ? "<none>" : result.waypoints.get(result.waypoints.size() - 1).toString();
            sender.sendMessage(String.format("[vla-purpur] path found=%b waypoints=%d first=%s last=%s expanded=%d",
                    result.found, result.waypoints.size(), first, last, result.expanded));
            // Oracle 改造：动作级航点恒为空（details 空列表），此行保留兼容循环
            StringBuilder sb = new StringBuilder("[vla-purpur] actions: ");
            for (DirectPathPlanner.Waypoint w : result.details) {
                if (w.target != null) {
                    sb.append(w.action).append('(').append(w.target.getBlockX())
                            .append(',').append(w.target.getBlockY()).append(',')
                            .append(w.target.getBlockZ()).append(") ");
                } else {
                    sb.append(w.action).append(' ').append(w.pos.getBlockX())
                            .append(',').append(w.pos.getBlockY()).append(',')
                            .append(w.pos.getBlockZ()).append("  ");
                }
            }
            sender.sendMessage(sb.toString());
            return true;
        } catch (NumberFormatException e) {
            sender.sendMessage("[vla-purpur] path: bad coords");
            return true;
        }
    }

    /** 选择/创建单材质元世界，并将玩家传送到该世界的固定平面出生点。 */
    private boolean vlaSurface(CommandSender sender, String[] args) {
        if (args.length < 3) {
            sender.sendMessage("Usage: /vla surface <player> <surface> [seed]");
            sender.sendMessage("  surfaces: "
                    + String.join(", ", SurfaceWorldManager.availableSurfaces().keySet()));
            return true;
        }
        Player player = agentManager.resolve(args[1]);
        if (player == null) {
            sender.sendMessage("[vla-purpur] surface: player not found: " + args[1]);
            return true;
        }
        long seed = 0L;
        if (args.length >= 4) {
            try {
                seed = Long.parseLong(args[3]);
            } catch (NumberFormatException e) {
                sender.sendMessage("[vla-purpur] surface: bad seed: " + args[3]);
                return true;
            }
        }
        try {
            SurfaceWorldManager.Selection selection = selectSurfaceWorld(player, args[2], seed);
            sender.sendMessage("[vla-purpur] surface selected: world="
                    + selection.world().getName() + " material=" + selection.material().getKey()
                    + " created=" + selection.created() + " meta=" + selection.metadataPath());
        } catch (IllegalArgumentException | IllegalStateException e) {
            sender.sendMessage("[vla-purpur] surface: " + e.getMessage());
        }
        return true;
    }

    // ---- M5 事件监听（→ TaskManager 判定，§4.7）----

    @EventHandler
    public void onBlockBreak(BlockBreakEvent event) {
        // 单材质元世界的自然地面受保护；只有任务生成的柱子/树（y>surfaceY）可挖。
        // 取消的 break 不计入任务判定。
        if (groundProtected && event.getBlock().getY() <= protectedSeaLevel) {
            event.setCancelled(true);
            return;
        }
        taskManager.onBlockBreak(event.getPlayer(),
                event.getBlock().getType().getKey().toString());
    }

    @EventHandler
    public void onBlockPlace(BlockPlaceEvent event) {
        taskManager.onBlockPlace(event.getPlayer(),
                event.getBlock().getType().getKey().toString());
    }

    @EventHandler
    public void onEntityDeath(EntityDeathEvent event) {
        if (event.getEntity().getKiller() instanceof Player killer) {
            taskManager.onEntityDeath(killer,
                    event.getEntity().getType().getKey().toString());
        }
    }

    @EventHandler
    public void onPlayerMove(PlayerMoveEvent event) {
        // 仅方块级位移才触发判定（避免纯转身/微小位移高频调用）
        if (event.getFrom().getBlockX() != event.getTo().getBlockX()
                || event.getFrom().getBlockY() != event.getTo().getBlockY()
                || event.getFrom().getBlockZ() != event.getTo().getBlockZ()) {
            taskManager.onPlayerMove(event.getPlayer());
        }
    }
}
