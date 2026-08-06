package dev.vla.purpur;

import dev.vla.purpur.grpc.MainThreadDispatcher;
import dev.vla.purpur.grpc.VlaGrpcService;
import dev.vla.purpur.player.AgentManager;
import dev.vla.purpur.reset.ResetEngine;
import io.grpc.Server;
import io.grpc.netty.shaded.io.grpc.netty.NettyServerBuilder;
import io.grpc.protobuf.services.ProtoReflectionService;
import java.net.InetSocketAddress;
import org.bukkit.Bukkit;
import org.bukkit.Location;
import org.bukkit.World;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.entity.Player;
import org.bukkit.plugin.java.JavaPlugin;

/**
 * VLA research environment gRPC bridge（M1 通信底座 + M4 世界引擎）。
 *
 * <p>onEnable 启动 gRPC server（127.0.0.1:50051），注册 {@code /vla} 命令：
 * {@code status}（探测服务器状态）、{@code reset <player> [halfExtent]}、
 * {@code verify <player>}；onDisable 优雅关停 gRPC server。
 */
public class VlaPlugin extends JavaPlugin {

    /** gRPC 监听地址。 */
    public static final String GRPC_HOST = "127.0.0.1";
    /** gRPC 监听端口（与 vla_env 客户端契约一致）。 */
    public static final int GRPC_PORT = 50051;

    private Server grpcServer;
    private AgentManager agentManager;
    private ResetEngine resetEngine;

    @Override
    public void onEnable() {
        MainThreadDispatcher.init(this);

        this.agentManager = new AgentManager(this);
        this.resetEngine = new ResetEngine();
        Bukkit.getPluginManager().registerEvents(agentManager, this);

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

    /** 供命令/日志探测 gRPC server 是否仍处于运行状态。 */
    public boolean isGrpcListening() {
        return grpcServer != null && !grpcServer.isShutdown() && !grpcServer.isTerminated();
    }

    /** 处理 {@code /vla} 子命令：status / reset / verify。 */
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
            default:
                usage(sender);
                return true;
        }
    }

    private void usage(CommandSender sender) {
        sender.sendMessage("Usage: /vla status | /vla reset <player> [halfExtent] | /vla verify <player>");
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
}
