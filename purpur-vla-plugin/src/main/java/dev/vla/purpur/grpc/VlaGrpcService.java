package dev.vla.purpur.grpc;

import dev.vla.purpur.VlaPlugin;
import dev.vla.purpur.path.CoarsePathPlanner;
import dev.vla.purpur.path.DirectPathPlanner;
import dev.vla.purpur.reset.ResetEngine;
import dev.vla.purpur.task.TaskManager;
import dev.vla.purpur.task.TaskRegistry;
import dev.vla.purpur.task.TaskSpec;
import dev.vla.purpur.world.ControlledPlainsGenerator;
import dev.vla.purpur.world.SurfaceWorldManager;
import dev.vla.purpur.world.VoxelReader;
import io.grpc.Status;
import io.grpc.stub.StreamObserver;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.ThreadLocalRandom;
import org.bukkit.Bukkit;
import org.bukkit.Location;
import org.bukkit.Material;
import org.bukkit.World;
import org.bukkit.entity.EntityType;
import org.bukkit.entity.Player;
import org.bukkit.inventory.ItemStack;
import org.bukkit.util.BlockVector;
import org.bukkit.util.Vector;
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
import vla.Vla.SelectSurfaceWorldReply;
import vla.Vla.SelectSurfaceWorldRequest;
import vla.Vla.ShowPathRequest;
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
 * 未实现的世界控制 RPC（clearRegion/teleport/setBlock）保留 UNIMPLEMENTED（后续 M7+）。
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
                // M11.5：自定义出生点（难点③）——显式 spawn 优先；区域未显式给出时
                // 以 spawn 为中心（保证基线快照覆盖出生环境）。
                if (request.getHasSpawn()) {
                    spec.spawn = new Location(world, request.getSpawnX(), request.getSpawnY(),
                            request.getSpawnZ(), request.getSpawnYaw(), 0f);
                }
                boolean explicitRegion = request.getRegionX() != 0
                        || request.getRegionY() != 0
                        || request.getRegionZ() != 0;
                if (explicitRegion) {
                    spec.setCenter(request.getRegionX(), request.getRegionY(), request.getRegionZ());
                } else if (spec.spawn != null) {
                    spec.setCenter(spec.spawn.getBlockX(), spec.spawn.getBlockY(),
                            spec.spawn.getBlockZ());
                } else {
                    Location loc = player.getLocation();
                    spec.setCenter(loc.getBlockX(), loc.getBlockY(), loc.getBlockZ());
                }
                if (request.getRegionHalfExtent() > 0) {
                    spec.halfExtent = request.getRegionHalfExtent();
                }
                // 任务初始物品（如 collect_stone 木镐 / kill_animal 木剑）：reset 时随背包下发
                // 键形如 "minecraft:oak_log@64"（数量缺省 1）。
                String taskId = request.getTask();
                if (taskId != null && !taskId.isEmpty()) {
                    TaskSpec tspec = TaskRegistry.get(taskId);
                    if (tspec != null) {
                        for (String key : tspec.initialItems()) {
                            int at = key.indexOf('@');
                            String id = at < 0 ? key : key.substring(0, at);
                            int count = at < 0 ? 1 : Integer.parseInt(key.substring(at + 1));
                            Material mat = Material.matchMaterial(id);
                            if (mat != null) {
                                spec.initialItems.add(new ItemStack(mat, count));
                            }
                        }
                    }
                }
                // M11：ResetRequest.items 覆盖任务默认初始物品（如固定生存工具包
                // 镐/剑/铲/泥土）。非空时清空任务默认物品、按显式列表发放。
                if (request.getItemsCount() > 0) {
                    spec.initialItems.clear();
                    for (String key : request.getItemsList()) {
                        int at = key.indexOf('@');
                        String id = at < 0 ? key : key.substring(0, at);
                        int count = at < 0 ? 1 : Integer.parseInt(key.substring(at + 1));
                        Material mat = Material.matchMaterial(id);
                        if (mat != null) {
                            spec.initialItems.add(new ItemStack(mat, count));
                        }
                    }
                }
                // M11：确定性种子（同 seed → 同一区域基线 + 任务初始态，可回放）
                spec.seed = request.getSeed();

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
                if (!plugin.isWorldReady()) {
                    responseObserver.onError(Status.UNAVAILABLE
                            .withDescription("controlled plains initialization is still running")
                            .asRuntimeException());
                    return;
                }
                TaskSpec spec = plugin.getTaskManager().setTask(player, request.getTask(),
                        request.getSeed());
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
    public void selectSurfaceWorld(SelectSurfaceWorldRequest request,
            StreamObserver<SelectSurfaceWorldReply> responseObserver) {
        MainThreadDispatcher.runSync(() -> {
            try {
                Player player = plugin.getAgentManager().resolve(request.getPlayer());
                if (player == null) {
                    responseObserver.onError(Status.FAILED_PRECONDITION
                            .withDescription("player not found: " + request.getPlayer())
                            .asRuntimeException());
                    return;
                }
                SurfaceWorldManager.Selection selected = plugin.selectSurfaceWorld(player,
                        request.getSurface(), request.getSeed());
                // 每个材质世界独立维护 ResetEngine 基线。让接下来的 ResetWorld 以新出生点为
                // 中心，而非继续使用旧世界/旧材质的 session 区域。
                plugin.getAgentManager().recordSessionRegion(player, selected.world().getSpawnLocation(),
                        player.getLocation().getBlockX(), player.getLocation().getBlockY(),
                        player.getLocation().getBlockZ(), 16);
                responseObserver.onNext(SelectSurfaceWorldReply.newBuilder()
                        .setWorldName(selected.world().getName())
                        .setSurfaceId(selected.surfaceId())
                        .setSurfaceMaterial(selected.material().getKey().toString())
                        .setCreated(selected.created())
                        .setWorkerId(selected.workerId())
                        .setMapSeed(selected.world().getSeed())
                        .setMetadataPath(selected.metadataPath().toString())
                        .setSurfaceY(ControlledPlainsGenerator.SURFACE_Y)
                        .build());
                responseObserver.onCompleted();
            } catch (IllegalArgumentException | IllegalStateException e) {
                responseObserver.onError(Status.INVALID_ARGUMENT
                        .withDescription(e.getMessage())
                        .withCause(e)
                        .asRuntimeException());
            } catch (Exception e) {
                plugin.getLogger().warning("SelectSurfaceWorld failed: " + e);
                responseObserver.onError(Status.INTERNAL
                        .withDescription("selectSurfaceWorld failed: " + e.getMessage())
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
        // getNearbyEntities 有 Paper 主线程守卫（抛 "Asynchronous getNearbyEntities!"），
        // 实体列表必须主线程读取 → 整个 getState 经 runSync 调度（§4.2，与 resetWorld 同模式）。
        MainThreadDispatcher.runSync(() -> {
            try {
                Player player = plugin.getAgentManager().resolve(request.getPlayer());
                if (player == null) {
                    responseObserver.onError(Status.FAILED_PRECONDITION
                            .withDescription("player not found: " + request.getPlayer())
                            .asRuntimeException());
                    return;
                }
                Location loc = player.getLocation();
                World world = player.getWorld();
                org.bukkit.util.Vector vel = player.getVelocity();
                ItemStack held = player.getInventory().getItemInMainHand();
                String heldItem = (held == null || held.getType().isAir())
                        ? "minecraft:air" : held.getType().getKey().toString();
                // 背包 main（36 格，非空气项）
                StringBuilder main = new StringBuilder();
                ItemStack[] contents = player.getInventory().getContents();
                for (int i = 0; i < 36; i++) {
                    ItemStack it = contents[i];
                    if (it != null && !it.getType().isAir()) {
                        if (main.length() > 0) {
                            main.append(",");
                        }
                        main.append("{\"slot\":").append(i)
                                .append(",\"item\":\"").append(it.getType().getKey()).append("\"")
                                .append(",\"count\":").append(it.getAmount()).append("}");
                    }
                }
                // 附近实体列表（半径 24 格，跳过玩家本体）
                StringBuilder entities = new StringBuilder();
                for (org.bukkit.entity.Entity e : world.getNearbyEntities(loc, 24, 24, 24)) {
                    if (e instanceof Player) {
                        continue;
                    }
                    if (entities.length() > 0) {
                        entities.append(",");
                    }
                    entities.append(String.format(Locale.ROOT,
                            "{\"type\":\"%s\",\"x\":%.2f,\"y\":%.2f,\"z\":%.2f}",
                            e.getType().getKey(), e.getLocation().getX(),
                            e.getLocation().getY(), e.getLocation().getZ()));
                }
                // 字段名对齐 DESIGN.md §8（player.pos/hp/hunger、inventory.selected_slot/held_item）
                String json = String.format(Locale.ROOT,
                        "{\"player\":{\"pos\":[%.2f,%.2f,%.2f],\"hp\":%.1f,\"hunger\":%d,"
                                + "\"yaw\":%.1f,\"pitch\":%.1f,\"on_ground\":%b,\"dimension\":\"%s\","
                                + "\"velocity\":[%.3f,%.3f,%.3f]},"
                                + "\"inventory\":{\"selected_slot\":%d,\"held_item\":\"%s\",\"main\":[%s]},"
                                + "\"stats\":{\"xp\":%d,\"level\":%d,\"playtime\":%.1f},"
                                + "\"entities\":[%s]}",
                        loc.getX(), loc.getY(), loc.getZ(),
                        player.getHealth(), player.getFoodLevel(),
                        loc.getYaw(), loc.getPitch(),
                        player.isOnGround(), player.getWorld().getName(),
                        vel.getX(), vel.getY(), vel.getZ(),
                        player.getInventory().getHeldItemSlot(), heldItem, main,
                        player.getTotalExperience(), player.getLevel(),
                        player.getStatistic(org.bukkit.Statistic.PLAY_ONE_MINUTE) / 20.0,
                        entities);
                responseObserver.onNext(StateReply.newBuilder().setJson(json).build());
                responseObserver.onCompleted();
            } catch (Exception e) {
                plugin.getLogger().warning("GetState failed: " + e);
                responseObserver.onError(Status.INTERNAL
                        .withDescription("getState failed: " + e.getMessage())
                        .withCause(e)
                        .asRuntimeException());
            }
        });
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

            // M11.5 两层导航（DESIGN.md §17.3 难点⑤）：直线可达 → [start, goal] 两航点；
            // 直线被挡 → CoarsePathPlanner 沿线采样 + 落地吸附输出途径点序列（间距 ≤8 格），
            // 客户端 LocalPathfinder 在相邻途径点间局部绕障/挖穿。终点邻域都不可站才
            // found=false（Python "no path → blacklist + wander" 兜底）。
            String mode = request.getCostMode();
            DirectPathPlanner.PathResult result = CoarsePathPlanner.findPath(world, start, goal, mode);

            PathReply.Builder b = PathReply.newBuilder().setFound(result.found);
            for (BlockVector wp : result.waypoints) {
                b.addWaypoints(Vec3.newBuilder()
                        .setX(wp.getBlockX()).setY(wp.getBlockY()).setZ(wp.getBlockZ()));
            }
            for (DirectPathPlanner.Waypoint w : result.details) {
                b.addDetails(Vla.Waypoint.newBuilder()
                        .setPos(Vec3.newBuilder()
                                .setX(w.pos.getBlockX()).setY(w.pos.getBlockY()).setZ(w.pos.getBlockZ()))
                        .setAction(w.action)
                        .setTarget(w.target == null ? Vec3.getDefaultInstance()
                                : Vec3.newBuilder()
                                        .setX(w.target.getBlockX()).setY(w.target.getBlockY())
                                        .setZ(w.target.getBlockZ()).build()));
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
        // 自动课程/LLM 生成：M12 完整实现；目前返回注册表任务（M11 起支持确定性 seed）。
        List<TaskSpec> all = TaskRegistry.all();
        if (all.isEmpty()) {
            responseObserver.onError(Status.NOT_FOUND
                    .withDescription("no tasks registered")
                    .asRuntimeException());
            return;
        }
        // M11：seed != 0 时用 java.util.Random(seed) 确定性选任务（同 seed → 同任务，
        // 支撑种子回放）；seed=0 回退随机。确定性选择与 Java 实现版本无关，可跨端复现。
        int seed = request.getSeed();
        java.util.Random rng = seed == 0
                ? ThreadLocalRandom.current()
                : new java.util.Random(seed);
        TaskSpec spec = all.get(rng.nextInt(all.size()));
        plugin.getLogger().info("[vla-plugin] GenerateTask seed=" + seed
                + " -> " + spec.id());
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
                Vec3 pos = request.getPos();
                boolean explicitPos = pos != null
                        && (pos.getX() != 0 || pos.getY() != 0 || pos.getZ() != 0);
                Location loc;
                if (explicitPos) {
                    loc = new Location(world, pos.getX(), pos.getY(), pos.getZ(),
                            request.getYaw(), request.getPitch());
                } else {
                    loc = world.getSpawnLocation();
                }
                player.teleport(loc);
                responseObserver.onNext(Void.getDefaultInstance());
                responseObserver.onCompleted();
            } catch (Exception e) {
                plugin.getLogger().warning("Teleport failed: " + e);
                responseObserver.onError(Status.INTERNAL
                        .withDescription("teleport failed: " + e.getMessage())
                        .withCause(e)
                        .asRuntimeException());
            }
        });
    }

    @Override
    public void spawnEntity(SpawnRequest request, StreamObserver<Void> responseObserver) {
        MainThreadDispatcher.runSync(() -> {
            try {
                Player player = plugin.getAgentManager().resolve(request.getPlayer());
                if (player == null) {
                    responseObserver.onError(Status.FAILED_PRECONDITION
                            .withDescription("player not found: " + request.getPlayer())
                            .asRuntimeException());
                    return;
                }
                EntityType type = parseEntityType(request.getEntityType());
                if (type == null) {
                    responseObserver.onError(Status.INVALID_ARGUMENT
                            .withDescription("unknown entity_type: " + request.getEntityType())
                            .asRuntimeException());
                    return;
                }
                World world = player.getWorld();
                Vec3 pos = request.getPos();
                boolean explicitPos = pos != null
                        && (pos.getX() != 0 || pos.getY() != 0 || pos.getZ() != 0);
                Location loc;
                if (explicitPos) {
                    loc = new Location(world, pos.getX(), pos.getY(), pos.getZ());
                } else {
                    // 玩家附近随机（半径 3~5 格），y 保持玩家所在高度（+0.5）
                    ThreadLocalRandom rnd = ThreadLocalRandom.current();
                    double angle = rnd.nextDouble(Math.PI * 2);
                    double dist = 3 + rnd.nextDouble(2);
                    Location p = player.getLocation();
                    loc = p.clone().add(
                            Math.cos(angle) * dist,
                            0.5,
                            Math.sin(angle) * dist);
                }
                int count = Math.max(1, request.getCount());
                for (int i = 0; i < count; i++) {
                    world.spawnEntity(loc, type);
                }
                responseObserver.onNext(Void.getDefaultInstance());
                responseObserver.onCompleted();
            } catch (Exception e) {
                plugin.getLogger().warning("SpawnEntity failed: " + e);
                responseObserver.onError(Status.INTERNAL
                        .withDescription("spawnEntity failed: " + e.getMessage())
                        .withCause(e)
                        .asRuntimeException());
            }
        });
    }

    @Override
    public void setBlock(SetBlockRequest request, StreamObserver<Void> responseObserver) {
        MainThreadDispatcher.runSync(() -> {
            try {
                Player player = plugin.getAgentManager().resolve(request.getPlayer());
                if (player == null) {
                    responseObserver.onError(Status.FAILED_PRECONDITION
                            .withDescription("player not found: " + request.getPlayer())
                            .asRuntimeException());
                    return;
                }
                if (!request.hasPos()) {
                    responseObserver.onError(Status.INVALID_ARGUMENT
                            .withDescription("setBlock requires pos")
                            .asRuntimeException());
                    return;
                }
                Material mat = Material.matchMaterial(request.getBlock());
                if (mat == null || !mat.isBlock()) {
                    responseObserver.onError(Status.INVALID_ARGUMENT
                            .withDescription("bad block: " + request.getBlock())
                            .asRuntimeException());
                    return;
                }
                Vec3 pos = request.getPos();
                World world = player.getWorld();
                org.bukkit.block.Block block = world.getBlockAt(
                        (int) Math.floor(pos.getX()), (int) Math.floor(pos.getY()),
                        (int) Math.floor(pos.getZ()));
                // 用 Block#setBlockData(data, applyPhysics)（与 RegionSnapshot 回滚同一 API；
                // 物理更新由调用方控制，demo 放置树默认关闭防连锁更新崩 TPS）
                block.setBlockData(mat.createBlockData(), request.getApplyPhysics());
                responseObserver.onNext(Void.getDefaultInstance());
                responseObserver.onCompleted();
            } catch (Exception e) {
                plugin.getLogger().warning("SetBlock failed: " + e);
                responseObserver.onError(Status.INTERNAL
                        .withDescription("setBlock failed: " + e.getMessage())
                        .withCause(e)
                        .asRuntimeException());
            }
        });
    }

    /**
     * ShowPath：路径可视化（两层导航 M10）——服务端长程航点刷黄色粒子，客户端局部路径刷白色粒子。
     * clear=true 清除该玩家全部特效。写操作，经主线程调度。
     */
    @Override
    public void showPath(ShowPathRequest request, StreamObserver<Void> responseObserver) {
        MainThreadDispatcher.runSync(() -> {
            try {
                Player player = plugin.getAgentManager().resolve(request.getPlayer());
                if (player == null) {
                    responseObserver.onError(Status.FAILED_PRECONDITION
                            .withDescription("player not found: " + request.getPlayer())
                            .asRuntimeException());
                    return;
                }
                if (request.getClear()) {
                    plugin.getPathVisualizer().clear(player);
                } else {
                    List<Vector> points = new ArrayList<>();
                    for (Vec3 v : request.getWaypointsList()) {
                        points.add(new Vector(v.getX(), v.getY(), v.getZ()));
                    }
                    Vector goal = request.hasGoal()
                            ? new Vector(request.getGoal().getX(),
                                    request.getGoal().getY(), request.getGoal().getZ())
                            : null;
                    String type = request.getPathType();
                    plugin.getPathVisualizer().show(
                            player, points, goal, request.getLifetimeTicks(), type);
                }
                responseObserver.onNext(Void.getDefaultInstance());
                responseObserver.onCompleted();
            } catch (Exception e) {
                plugin.getLogger().warning("ShowPath failed: " + e);
                responseObserver.onError(Status.INTERNAL
                        .withDescription("showPath failed: " + e.getMessage())
                        .withCause(e)
                        .asRuntimeException());
            }
        });
    }

    // ---- 工具 ----

    /** 解析实体类型：EntityType.fromName → 去前缀再 fromName → 大写枚举名；失败返回 null。 */
    private static EntityType parseEntityType(String key) {
        if (key == null || key.isEmpty()) {
            return null;
        }
        EntityType type = EntityType.fromName(key);
        if (type != null) {
            return type;
        }
        if (key.startsWith("minecraft:")) {
            type = EntityType.fromName(key.substring("minecraft:".length()));
            if (type != null) {
                return type;
            }
        }
        try {
            return EntityType.valueOf(key.toUpperCase(Locale.ROOT));
        } catch (IllegalArgumentException e) {
            return null;
        }
    }

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
