package dev.vla.purpur.grpc;

import dev.vla.purpur.VlaPlugin;
import dev.vla.purpur.path.AStar;
import dev.vla.purpur.reset.ResetEngine;
import dev.vla.purpur.task.TaskManager;
import dev.vla.purpur.task.TaskRegistry;
import dev.vla.purpur.task.TaskSpec;
import dev.vla.purpur.world.VoxelReader;
import io.grpc.Status;
import io.grpc.stub.StreamObserver;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.ThreadLocalRandom;
import org.bukkit.Bukkit;
import org.bukkit.Location;
import org.bukkit.World;
import org.bukkit.entity.Player;
import org.bukkit.inventory.ItemStack;
import org.bukkit.util.BlockVector;
import vla.Vla;
import vla.Vla.ClearRequest;
import vla.Vla.GenerateRequest;
import vla.Vla.PathReply;
import vla.Vla.PathRequest;
import vla.Vla.PingReply;
import vla.Vla.PingRequest;
import vla.Vla.ResetReply;
import vla.Vla.ResetRequest;
import vla.Vla.SetBlockRequest;
import vla.Vla.SpawnRequest;
import vla.Vla.StateReply;
import vla.Vla.StateRequest;
import vla.Vla.StepReply;
import vla.Vla.StepRequest;
import vla.Vla.TaskReply;
import vla.Vla.TaskRequest;
import vla.Vla.TeleportRequest;
import vla.Vla.Vec3;
import vla.Vla.Void;
import vla.Vla.VoxelReply;
import vla.Vla.VoxelRequest;
import vla.VlaServerGrpc;

/**
 * gRPC 服务实现（M1 通信底座 + M4 世界引擎 + M5 任务 + M6 状态/寻路）。
 *
 * <p>写操作（resetWorld/setTask/getStepResult）经 {@link MainThreadDispatcher} 调度到
 * Bukkit 主线程；只读查询（getVoxels/computePath/getState/generateTask/ping）gRPC 线程直跑（§4.2）。
 * 未实现的世界控制 RPC（clearRegion/teleport/spawnEntity/setBlock）保留 UNIMPLEMENTED（后续 M7+）。
 */
public class VlaGrpcService extends VlaServerGrpc.VlaServerImplBase {

    private final VlaPlugin plugin;

    public VlaGrpcService(VlaPlugin plugin) {
        this.plugin = plugin;
    }

    @Override
    public void ping(PingRequest request, StreamObserver<PingReply> responseObserver) {
        List<World> worlds = Bukkit.getWorlds();
        PingReply reply = PingReply.newBuilder()
                .setServerTick(Bukkit.getCurrentTick())
                .setTps(Bukkit.getTPS()[0])
                .setVersion(Bukkit.getVersion())
                .setWorldName(worlds.isEmpty() ? "" : worlds.get(0).getName())
                .build();
        responseObserver.onNext(reply);
        responseObserver.onCompleted();
    }

    // ---- 写操作（主线程调度）----

    @Override
    public void resetWorld(ResetRequest request, StreamObserver<ResetReply> responseObserver) {
        MainThreadDispatcher.runSync(() -> {
            try {
                Player player = plugin.getAgentManager().resolve(request.getPlayer());
                if (player == null) {
                    responseObserver.onError(Status.FAILED_PRECONDITION
                            .withDescription("player not found: " + request.getPlayer())
                            .asRuntimeException());
                    return;
                }
                World world = player.getWorld();
                ResetEngine.ResetSpec spec = new ResetEngine.ResetSpec();
                boolean explicitRegion = request.getRegionX() != 0
                        || request.getRegionY() != 0
                        || request.getRegionZ() != 0;
                if (explicitRegion) {
                    spec.setCenter(request.getRegionX(), request.getRegionY(), request.getRegionZ());
                } else {
                    Location loc = player.getLocation();
                    spec.setCenter(loc.getBlockX(), loc.getBlockY(), loc.getBlockZ());
                }
                if (request.getRegionHalfExtent() > 0) {
                    spec.halfExtent = request.getRegionHalfExtent();
                }

                ResetEngine.ResetResult result = plugin.getResetEngine().reset(player, spec);
                plugin.getAgentManager().recordSessionRegion(player, world.getSpawnLocation(),
                        spec.centerX, spec.centerY, spec.centerZ, spec.halfExtent);

                ResetReply reply = ResetReply.newBuilder()
                        .setServerTick(result.serverTick)
                        .setOk(result.ok)
                        .setMessage(result.checksum)
                        .build();
                responseObserver.onNext(reply);
                responseObserver.onCompleted();
            } catch (Exception e) {
                plugin.getLogger().warning("ResetWorld failed: " + e);
                responseObserver.onError(Status.INTERNAL
                        .withDescription("reset failed: " + e.getMessage())
                        .withCause(e)
                        .asRuntimeException());
            }
        });
    }

    @Override
    public void setTask(TaskRequest request, StreamObserver<TaskReply> responseObserver) {
        MainThreadDispatcher.runSync(() -> {
            try {
                Player player = plugin.getAgentManager().resolve(request.getPlayer());
                if (player == null) {
                    responseObserver.onError(Status.FAILED_PRECONDITION
                            .withDescription("player not found: " + request.getPlayer())
                            .asRuntimeException());
                    return;
                }
                TaskSpec spec = plugin.getTaskManager().setTask(player, request.getTask());
                if (spec == null) {
                    responseObserver.onError(Status.NOT_FOUND
                            .withDescription("unknown task: " + request.getTask())
                            .asRuntimeException());
                    return;
                }
                responseObserver.onNext(taskReply(spec, 0f, false));
                responseObserver.onCompleted();
            } catch (Exception e) {
                plugin.getLogger().warning("SetTask failed: " + e);
                responseObserver.onError(Status.INTERNAL
                        .withDescription("setTask failed: " + e.getMessage())
                        .withCause(e)
                        .asRuntimeException());
            }
        });
    }

    @Override
    public void getStepResult(StepRequest request, StreamObserver<StepReply> responseObserver) {
        Player player = plugin.getAgentManager().resolve(request.getPlayer());
        if (player == null) {
            responseObserver.onError(Status.FAILED_PRECONDITION
                    .withDescription("player not found: " + request.getPlayer())
                    .asRuntimeException());
            return;
        }
        // step() 内部经 runTaskLater 在主线程结算并回传（server-authoritative，§14.2）
        plugin.getTaskManager().step(player, request.getAwaitTicks(), responseObserver);
    }

    // ---- 只读查询（gRPC 线程直跑，§4.2）----

    @Override
    public void getState(StateRequest request, StreamObserver<StateReply> responseObserver) {
        try {
            Player player = plugin.getAgentManager().resolve(request.getPlayer());
            if (player == null) {
                responseObserver.onError(Status.FAILED_PRECONDITION
                        .withDescription("player not found: " + request.getPlayer())
                        .asRuntimeException());
                return;
            }
            Location loc = player.getLocation();
            ItemStack held = player.getInventory().getItemInMainHand();
            String heldItem = (held == null || held.getType().isAir())
                    ? "minecraft:air" : held.getType().getKey().toString();
            // 字段名对齐 DESIGN.md §8（player.pos/hp/hunger、inventory.selected_slot/held_item）
            String json = String.format(Locale.ROOT,
                    "{\"player\":{\"pos\":[%.2f,%.2f,%.2f],\"hp\":%.1f,\"hunger\":%d},"
                            + "\"inventory\":{\"selected_slot\":%d,\"held_item\":\"%s\"}}",
                    loc.getX(), loc.getY(), loc.getZ(),
                    player.getHealth(), player.getFoodLevel(),
                    player.getInventory().getHeldItemSlot(), heldItem);
            responseObserver.onNext(StateReply.newBuilder().setJson(json).build());
            responseObserver.onCompleted();
        } catch (Exception e) {
            plugin.getLogger().warning("GetState failed: " + e);
            responseObserver.onError(Status.INTERNAL
                    .withDescription("getState failed: " + e.getMessage())
                    .withCause(e)
                    .asRuntimeException());
        }
    }

    @Override
    public void getVoxels(VoxelRequest request, StreamObserver<VoxelReply> responseObserver) {
        try {
            Player player = plugin.getAgentManager().resolve(request.getPlayer());
            if (player == null) {
                responseObserver.onError(Status.FAILED_PRECONDITION
                        .withDescription("player not found: " + request.getPlayer())
                        .asRuntimeException());
                return;
            }
            World world = player.getWorld();
            boolean explicitCenter = request.getCenterX() != 0
                    || request.getCenterY() != 0
                    || request.getCenterZ() != 0;
            BlockVector center;
            if (explicitCenter) {
                center = new BlockVector(request.getCenterX(), request.getCenterY(), request.getCenterZ());
            } else {
                Location loc = player.getLocation();
                center = new BlockVector(loc.getBlockX(), loc.getBlockY(), loc.getBlockZ());
            }
            int extent = request.getHalfExtent() > 0
                    ? request.getHalfExtent() : VoxelReader.DEFAULT_HALF_EXTENT;
            responseObserver.onNext(VoxelReader.read(world, center, extent));
            responseObserver.onCompleted();
        } catch (Exception e) {
            plugin.getLogger().warning("GetVoxels failed: " + e);
            responseObserver.onError(Status.INTERNAL
                    .withDescription("getVoxels failed: " + e.getMessage())
                    .withCause(e)
                    .asRuntimeException());
        }
    }

    @Override
    public void computePath(PathRequest request, StreamObserver<PathReply> responseObserver) {
        try {
            Player player = plugin.getAgentManager().resolve(request.getPlayer());
            if (player == null) {
                responseObserver.onError(Status.FAILED_PRECONDITION
                        .withDescription("player not found: " + request.getPlayer())
                        .asRuntimeException());
                return;
            }
            World world = player.getWorld();
            Location loc = player.getLocation();
            BlockVector feet = new BlockVector(loc.getBlockX(), loc.getBlockY(), loc.getBlockZ());

            boolean startExplicit = request.hasStart()
                    && !(request.getStart().getX() == 0 && request.getStart().getY() == 0
                    && request.getStart().getZ() == 0);
            BlockVector start = startExplicit ? toBlock(request.getStart()) : feet;

            boolean goalExplicit = request.hasGoal()
                    && !(request.getGoal().getX() == 0 && request.getGoal().getY() == 0
                    && request.getGoal().getZ() == 0);
            // goal 缺省：玩家位置 +X 30 格（"向前 30 格"的合理默认）
            BlockVector goal = goalExplicit ? toBlock(request.getGoal())
                    : new BlockVector(feet.getBlockX() + 30, feet.getBlockY(), feet.getBlockZ());

            String mode = request.getCostMode();
            AStar.PathResult result = AStar.findPath(world, start, goal, mode);

            PathReply.Builder b = PathReply.newBuilder().setFound(result.found);
            for (BlockVector wp : result.waypoints) {
                b.addWaypoints(Vec3.newBuilder()
                        .setX(wp.getBlockX()).setY(wp.getBlockY()).setZ(wp.getBlockZ()));
            }
            responseObserver.onNext(b.build());
            responseObserver.onCompleted();
        } catch (Exception e) {
            plugin.getLogger().warning("ComputePath failed: " + e);
            responseObserver.onError(Status.INTERNAL
                    .withDescription("computePath failed: " + e.getMessage())
                    .withCause(e)
                    .asRuntimeException());
        }
    }

    @Override
    public void generateTask(GenerateRequest request, StreamObserver<TaskReply> responseObserver) {
        // 自动课程/LLM 生成：M12 实现；目前返回注册表随机任务作为占位
        List<TaskSpec> all = TaskRegistry.all();
        if (all.isEmpty()) {
            responseObserver.onError(Status.NOT_FOUND
                    .withDescription("no tasks registered")
                    .asRuntimeException());
            return;
        }
        TaskSpec spec = all.get(ThreadLocalRandom.current().nextInt(all.size()));
        responseObserver.onNext(taskReply(spec, 0f, false));
        responseObserver.onCompleted();
    }

    // ---- M7+ 世界控制（未实现）----

    @Override
    public void clearRegion(ClearRequest request, StreamObserver<Void> responseObserver) {
        unimpl(responseObserver);
    }

    @Override
    public void teleport(TeleportRequest request, StreamObserver<Void> responseObserver) {
        unimpl(responseObserver);
    }

    @Override
    public void spawnEntity(SpawnRequest request, StreamObserver<Void> responseObserver) {
        unimpl(responseObserver);
    }

    @Override
    public void setBlock(SetBlockRequest request, StreamObserver<Void> responseObserver) {
        unimpl(responseObserver);
    }

    // ---- 工具 ----

    private static TaskReply taskReply(TaskSpec spec, float progress, boolean success) {
        TaskReply.Builder b = TaskReply.newBuilder()
                .setId(spec.id())
                .setInstruction(spec.instruction())
                .setInstructionZh(spec.instructionZh())
                .setDifficulty(spec.difficulty())
                .setProgress(progress)
                .setSuccess(success)
                .setTimeoutTicks(spec.timeoutTicks());
        return b.build();
    }

    private static BlockVector toBlock(Vec3 v) {
        return new BlockVector(
                (int) Math.floor(v.getX()),
                (int) Math.floor(v.getY()),
                (int) Math.floor(v.getZ()));
    }

    /** 统一 UNIMPLEMENTED 响应。 */
    private static <T> void unimpl(StreamObserver<T> responseObserver) {
        responseObserver.onError(Status.UNIMPLEMENTED
                .withDescription("not implemented yet (M7+)")
                .asRuntimeException());
    }
}
