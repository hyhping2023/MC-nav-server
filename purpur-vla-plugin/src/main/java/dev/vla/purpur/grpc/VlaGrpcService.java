package dev.vla.purpur.grpc;

import dev.vla.purpur.VlaPlugin;
import dev.vla.purpur.reset.ResetEngine;
import io.grpc.Status;
import io.grpc.stub.StreamObserver;
import java.util.List;
import org.bukkit.Bukkit;
import org.bukkit.Location;
import org.bukkit.World;
import org.bukkit.entity.Player;
import vla.Vla;
import vla.Vla.ClearRequest;
import vla.Vla.GenerateRequest;
import vla.Vla.PathRequest;
import vla.Vla.PathReply;
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
import vla.Vla.Void;
import vla.Vla.VoxelReply;
import vla.Vla.VoxelRequest;
import vla.VlaServerGrpc;

/**
 * gRPC 服务实现（M1 通信底座）。
 *
 * <p>Ping 为只读连通性检查，直接在 gRPC 线程构造回复（安全）；其余 RPC 属于 M2+，
 * 统一返回 UNIMPLEMENTED。
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

    // ---- M2+ RPC：resetWorld 已实现，其余尚未实现 ----

    @Override
    public void resetWorld(ResetRequest request, StreamObserver<ResetReply> responseObserver) {
        // 写操作：经 MainThreadDispatcher 调度到 Bukkit 主线程执行（§4.2）
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
    public void getStepResult(StepRequest request, StreamObserver<StepReply> responseObserver) {
        unimpl(responseObserver);
    }

    @Override
    public void getState(StateRequest request, StreamObserver<StateReply> responseObserver) {
        unimpl(responseObserver);
    }

    @Override
    public void getVoxels(VoxelRequest request, StreamObserver<VoxelReply> responseObserver) {
        unimpl(responseObserver);
    }

    @Override
    public void computePath(PathRequest request, StreamObserver<PathReply> responseObserver) {
        unimpl(responseObserver);
    }

    @Override
    public void setTask(TaskRequest request, StreamObserver<TaskReply> responseObserver) {
        unimpl(responseObserver);
    }

    @Override
    public void generateTask(GenerateRequest request, StreamObserver<TaskReply> responseObserver) {
        unimpl(responseObserver);
    }

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

    /** 统一 UNIMPLEMENTED 响应。 */
    private static <T> void unimpl(StreamObserver<T> responseObserver) {
        responseObserver.onError(Status.UNIMPLEMENTED
                .withDescription("not implemented yet (M2+)")
                .asRuntimeException());
    }
}
