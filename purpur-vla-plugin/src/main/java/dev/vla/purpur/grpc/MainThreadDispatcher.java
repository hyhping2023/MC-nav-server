package dev.vla.purpur.grpc;

import org.bukkit.Bukkit;
import org.bukkit.plugin.Plugin;

/**
 * 主线程（Bukkit Server thread）调度工具。
 *
 * <p>gRPC 线程不能直接触碰 Bukkit 可变状态（§4.2：写操作一律经主线程调度）。
 * M2+ 的 ResetWorld/GetStepResult 等 RPC 实现将使用 {@link #runSync(Runnable)}
 * 把工作切到主线程执行；M1 仅提供骨架（Ping 只读，gRPC 线程直读安全）。
 */
public final class MainThreadDispatcher {

    private static Plugin plugin;

    private MainThreadDispatcher() {
    }

    /** 插件 onEnable 时调用一次。 */
    public static void init(Plugin owningPlugin) {
        plugin = owningPlugin;
    }

    /**
     * 在 Bukkit 主线程执行 {@code task}。
     *
     * <ul>
     *   <li>调用方已在主线程 → 直接执行（同步）；
     *   <li>否则 → 经 {@code Bukkit.getScheduler().runTask} 异步调度到主线程。
     * </ul>
     */
    public static void runSync(Runnable task) {
        if (plugin == null) {
            throw new IllegalStateException("MainThreadDispatcher not initialized");
        }
        if (Bukkit.isPrimaryThread()) {
            task.run();
        } else {
            Bukkit.getScheduler().runTask(plugin, task);
        }
    }
}
