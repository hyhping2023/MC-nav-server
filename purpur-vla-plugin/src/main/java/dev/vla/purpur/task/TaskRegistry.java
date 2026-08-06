package dev.vla.purpur.task;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 内置任务注册表（DESIGN.md §10 Registry）。
 *
 * <p>手工定义基础任务；M12 自动课程将在此基础上动态调整难度/推进。
 */
public final class TaskRegistry {

    private static final Map<String, TaskSpec> TASKS = new LinkedHashMap<>();

    private TaskRegistry() {
    }

    static {
        Map<String, Object> collectArgs = new LinkedHashMap<>();
        collectArgs.put("block", "minecraft:oak_log");
        collectArgs.put("count", 4);
        TASKS.put("collect_wood", new TaskSpec(
                "collect_wood",
                "Break 4 oak logs.",
                "挖 4 个橡木原木。",
                TaskSpec.TaskType.COLLECT,
                1,
                "block_mined",
                collectArgs,
                6000));

        // craft_planks 占位：predicate=inventory_contains，本回合（M5）不验收
        Map<String, Object> craftArgs = new LinkedHashMap<>();
        craftArgs.put("item", "minecraft:oak_planks");
        craftArgs.put("count", 4);
        TASKS.put("craft_planks", new TaskSpec(
                "craft_planks",
                "Craft 4 oak planks.",
                "合成 4 个橡木木板。",
                TaskSpec.TaskType.CRAFT,
                1,
                "inventory_contains",
                craftArgs,
                6000));
    }

    public static TaskSpec get(String id) {
        return TASKS.get(id);
    }

    public static List<TaskSpec> all() {
        return new ArrayList<>(TASKS.values());
    }
}
