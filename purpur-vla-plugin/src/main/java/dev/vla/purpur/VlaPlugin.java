package dev.vla.purpur;

import dev.vla.purpur.grpc.MainThreadDispatcher;
import dev.vla.purpur.grpc.VlaGrpcService;
import io.grpc.Server;
import io.grpc.netty.shaded.io.grpc.netty.NettyServerBuilder;
import io.grpc.protobuf.services.ProtoReflectionService;
import java.net.InetSocketAddress;
import org.bukkit.Bukkit;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.plugin.java.JavaPlugin;

/**
 * VLA research environment gRPC bridge（M1 通信底座）。
 *
 * <p>onEnable 启动 gRPC server（127.0.0.1:50051），注册 {@code /vla} 命令用于探测
 * 服务器状态；onDisable 优雅关停 gRPC server。
 */
public class VlaPlugin extends JavaPlugin {

    /** gRPC 监听地址。 */
    public static final String GRPC_HOST = "127.0.0.1";
    /** gRPC 监听端口（与 vla_env 客户端契约一致）。 */
    public static final int GRPC_PORT = 50051;

    private Server grpcServer;

    @Override
    public void onEnable() {
        MainThreadDispatcher.init(this);

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

    /** 处理 {@code /vla status}：输出 tick/tps/gRPC 端口。 */
    private boolean onVlaCommand(CommandSender sender, Command command, String label, String[] args) {
        if (args.length == 0 || !"status".equalsIgnoreCase(args[0])) {
            sender.sendMessage("Usage: /vla status");
            return true;
        }
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

    /** 供命令/日志探测 gRPC server 是否仍处于运行状态。 */
    public boolean isGrpcListening() {
        return grpcServer != null && !grpcServer.isShutdown() && !grpcServer.isTerminated();
    }
}
