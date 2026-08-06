package dev.vla.purpur.task;

import io.grpc.stub.StreamObserver;
import java.util.HashMap;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import org.bukkit.Bukkit;
import org.bukkit.entity.Player;
import org.bukkit.plugin.Plugin;
import vla.Vla.StepReply;

/**
 * 任务管理器（DESIGN.md §4.7，M5 交付物，任务 2.5）。
 *
 * <p>任务状态按玩家隔离（§4.8）：{@link #states} 为 {@code Map<UUID, EpisodeState>}。
 * 事件回调（由 VlaPlugin 监听的 BlockBreak/BlockPlace/EntityDeath/PlayerMove 转发）
 * 更新计数并判定 success；判定通过即打日志并加奖励。
 * {@link #step} 由 gRPC GetStepResult 触发：延迟 {@code awaitTicks} 后在主线程
 * 组装并回传 server-authoritative 的 StepReply。
 */
public final class TaskManager {

    /** 单次成功奖励（DESIGN.md 奖励：success_bonus）。 */
    public static final double SUCCESS_REWARD = 10.0;

    /** 单玩家 episode 状态。 */
    public static final class EpisodeState {
        public TaskSpec task;
        /** 计数器：key = 判定器名:目标键（如 "block_mined:minecraft:oak_log"）。 */
        public final Map<String, Integer> counters = new HashMap<>();
        /** 累计步数（每次 step() 增加 awaitTicks 折算的刻数，简化口径）。 */
        public long steps = 0;
        /** 自上次 GetStepResult 结算以来累积的奖励，返回后清零。 */
        public double rewardSinceLastStep = 0;
        public boolean success = false;
        public boolean timeout = false;
        public int lastStepTick = 0;

        public int counter(String predicate, String key) {
            return counters.getOrDefault(counterKey(predicate, key), 0);
        }

        public void bump(String predicate, String key) {
            counters.merge(counterKey(predicate, key), 1, Integer::sum);
        }

        @Override
        public String toString() {
            return "EpisodeState{task=" + (task == null ? "null" : task.id())
                    + " counters=" + counters + " steps=" + steps
                    + " reward=" + rewardSinceLastStep + " success=" + success + "}";
        }
    }

    private final Plugin plugin;
    private final Map<UUID, EpisodeState> states = new ConcurrentHashMap<>();

    public TaskManager(Plugin plugin) {
        this.plugin = plugin;
    }

    /** counter key：predicate 与目标键拼接（Predicates 与 progress 共用）。 */
    public static String counterKey(String predicate, String key) {
        return predicate + ":" + key;
    }

    /**
     * 设置任务：重置该玩家 episode 状态。任务不存在返回 null。
     *
     * @return 成功设置的任务 spec；未知任务返回 null
     */
    public TaskSpec setTask(Player player, String taskId) {
        TaskSpec spec = TaskRegistry.get(taskId);
        if (spec == null) {
            return null;
        }
        EpisodeState state = new EpisodeState();
        state.task = spec;
        state.steps = 0;
        states.put(player.getUniqueId(), state);
        plugin.getLogger().info("[task] " + player.getName() + " set task: " + taskId);
        return spec;
    }

    public EpisodeState getState(Player player) {
        return states.get(player.getUniqueId());
    }

    // ---- 事件回调（VlaPlugin 监听转发；主线程）----

    public void onBlockBreak(Player player, String blockKey) {
        EpisodeState st = states.get(player.getUniqueId());
        if (st == null || st.task == null) {
            return;
        }
        if ("block_mined".equals(st.task.successPredicate())
                && blockKey.equals(TaskSpec.argStr(st.task.successArgs(), "block", ""))) {
            st.bump("block_mined", blockKey);
            checkSuccess(player, st);
        }
    }

    public void onBlockPlace(Player player, String blockKey) {
        EpisodeState st = states.get(player.getUniqueId());
        if (st == null || st.task == null) {
            return;
        }
        if ("block_placed".equals(st.task.successPredicate())
                && blockKey.equals(TaskSpec.argStr(st.task.successArgs(), "block", ""))) {
            st.bump("block_placed", blockKey);
            checkSuccess(player, st);
        }
    }

    public void onEntityDeath(Player killer, String entityKey) {
        EpisodeState st = states.get(killer.getUniqueId());
        if (st == null || st.task == null) {
            return;
        }
        if ("entity_killed".equals(st.task.successPredicate())
                && entityKey.equals(TaskSpec.argStr(st.task.successArgs(), "entity", ""))) {
            st.bump("entity_killed", entityKey);
            checkSuccess(killer, st);
        }
    }

    public void onPlayerMove(Player player) {
        EpisodeState st = states.get(player.getUniqueId());
        if (st == null || st.task == null) {
            return;
        }
        if ("player_at".equals(st.task.successPredicate())) {
            checkSuccess(player, st);
        }
    }

    /** 判定成功（仅一次）：置 success、累加奖励、打日志。 */
    private void checkSuccess(Player player, EpisodeState st) {
        if (st.success) {
            return;
        }
        if (Predicates.evaluate(st.task, player, st)) {
            st.success = true;
            st.rewardSinceLastStep += SUCCESS_REWARD;
            plugin.getLogger().info("[task] " + player.getName() + " success: " + st.task.id());
        }
    }

    /** 任务进度 [0,1]（用于 TaskReply.progress / StepReply.progress / taskinfo）。 */
    public float progress(Player player, EpisodeState st) {
        if (st == null || st.task == null) {
            return 0f;
        }
        if (st.success) {
            return 1f;
        }
        TaskSpec t = st.task;
        int count = TaskSpec.argInt(t.successArgs(), "count", 1);
        if (count <= 0) {
            return 0f;
        }
        int got;
        switch (t.successPredicate()) {
            case "block_mined":
            case "block_placed":
                got = st.counter(t.successPredicate(), TaskSpec.argStr(t.successArgs(), "block", ""));
                break;
            case "entity_killed":
                got = st.counter("entity_killed", TaskSpec.argStr(t.successArgs(), "entity", ""));
                break;
            case "inventory_contains":
                got = Predicates.countInventory(player, TaskSpec.argStr(t.successArgs(), "item", ""));
                break;
            case "player_at":
                return Predicates.evaluate(t, player, st) ? 1f : 0f;
            default:
                return 0f;
        }
        return Math.min(1f, (float) got / count);
    }

    /**
     * gRPC GetStepResult：延迟 {@code awaitTicks} 刻后在主线程结算并回传 StepReply。
     *
     * <p>steps 口径：每次 step() 自增 awaitTicks 折算的步数（简化、明确记录）；
     * truncated = steps ≥ timeoutTicks。reward 返回 {@code rewardSinceLastStep} 并清零。
     */
    public void step(Player player, int awaitTicks, StreamObserver<StepReply> observer) {
        UUID uuid = player.getUniqueId();
        int delay = Math.max(0, awaitTicks);
        Bukkit.getScheduler().runTaskLater(plugin, () -> {
            EpisodeState st = states.get(uuid);
            StepReply.Builder b = StepReply.newBuilder()
                    .setServerTick(Bukkit.getCurrentTick());
            if (st == null || st.task == null) {
                b.setReward(0f)
                        .setTerminated(false)
                        .setTruncated(false)
                        .setProgress(0f)
                        .putInfo("task", "none");
            } else {
                st.steps += delay;
                st.lastStepTick = Bukkit.getCurrentTick();
                boolean truncated = st.steps >= st.task.timeoutTicks();
                b.setReward((float) st.rewardSinceLastStep);
                st.rewardSinceLastStep = 0;
                b.setTerminated(st.success)
                        .setTruncated(truncated)
                        .setProgress(progress(player, st))
                        .putInfo("task", st.task.id());
            }
            observer.onNext(b.build());
            observer.onCompleted();
        }, delay);
    }
}
