package dev.vla.purpur.task;

import com.google.gson.Gson;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import java.io.File;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.logging.Logger;

/**
 * 任务注册表（DESIGN.md §10 Registry + §17.3 难点② data-driven 扩展）。
 *
 * <p>两个来源：
 * <ul>
 *   <li><b>内置任务</b>（static 块）：collect_wood / collect_stone / kill_animal /
 *       dig_dirt / place_dirt / craft_planks。</li>
 *   <li><b>JSON 任务</b>（M11.5）：{@code plugins/VlaPlugin/tasks/*.json}，onEnable 加载、
 *       {@code vla reloadtasks} 热重载。同 id 覆盖内置——无需改代码即可增删任务。</li>
 * </ul>
 *
 * <p>JSON schema（字段与 {@link TaskSpec} 一一对应）：
 * <pre>{@code
 * { "id": "collect_sand", "instruction": "Collect 4 sand.", "instruction_zh": "挖 4 个沙子。",
 *   "type": "COLLECT", "difficulty": 1,
 *   "predicate": "block_mined", "args": {"block": "minecraft:sand", "count": 4},
 *   "timeout_ticks": 6000, "items": ["minecraft:diamond_shovel", "minecraft:dirt@64"],
 *   "dig_penalty": 0.05 }
 * }</pre>
 */
public final class TaskRegistry {

    private static final Map<String, TaskSpec> TASKS = new LinkedHashMap<>();
    private static final Gson GSON = new Gson();

    /** 采集类内置任务的默认过度挖掘惩罚（每块非目标方块扣的 reward，§17.3 难点③）。 */
    public static final double DEFAULT_DIG_PENALTY = 0.05;

    /** 全部任务统一发放的完整钻石工具套装（顺序即 hotbar 槽位 0-4）。 */
    public static final List<String> DIAMOND_TOOL_SET = List.of(
            "minecraft:diamond_pickaxe",   // 0：挖石头/矿物
            "minecraft:diamond_axe",       // 1：砍原木
            "minecraft:diamond_shovel",    // 2：铲土/沙
            "minecraft:diamond_sword",     // 3：近战
            "minecraft:diamond_hoe");      // 4：锄地

    /** 统一补发的放置方块（hotbar 5，×64）——NavV2 PLACE_STEP 垫方块爬高用。 */
    public static final String PLACE_BLOCK = "minecraft:dirt@64";

    /** 全部任务初始物品 = 钻石工具 + 放置方块。 */
    private static final List<String> INITIAL_ITEMS = new ArrayList<>(DIAMOND_TOOL_SET) {{
        add(PLACE_BLOCK);
    }};

    /** M11 固定生存工具包（hotbar 0-3）：镐/剑/铲 + 泥土方块（人类演示录制用）。 */
    public static final List<String> SURVIVAL_KIT = List.of(
            "minecraft:diamond_pickaxe",   // 0：挖石头
            "minecraft:diamond_sword",     // 1：近战（杀猪）
            "minecraft:diamond_shovel",    // 2：铲泥土
            "minecraft:dirt@64");          // 3：放置方块

    private TaskRegistry() {
    }

    static {
        Map<String, Object> collectArgs = new LinkedHashMap<>();
        collectArgs.put("block", "minecraft:oak_log");
        collectArgs.put("count", 4);
        register(new TaskSpec(
                "collect_wood",
                "Break 4 oak logs.",
                "挖 4 个橡木原木。",
                TaskSpec.TaskType.COLLECT,
                1,
                "block_mined",
                collectArgs,
                6000,
                INITIAL_ITEMS,
                DEFAULT_DIG_PENALTY));

        // craft_planks 占位：predicate=inventory_contains，本回合（M5）不验收
        Map<String, Object> craftArgs = new LinkedHashMap<>();
        craftArgs.put("item", "minecraft:oak_planks");
        craftArgs.put("count", 4);
        register(new TaskSpec(
                "craft_planks",
                "Craft 4 oak planks.",
                "合成 4 个橡木木板。",
                TaskSpec.TaskType.CRAFT,
                1,
                "inventory_contains",
                craftArgs,
                6000,
                INITIAL_ITEMS));

        Map<String, Object> stoneArgs = new LinkedHashMap<>();
        stoneArgs.put("block", "minecraft:stone");
        stoneArgs.put("count", 8);
        register(new TaskSpec(
                "collect_stone",
                "Break 8 stone blocks.",
                "挖 8 个石头。",
                TaskSpec.TaskType.COLLECT,
                1,
                "block_mined",
                stoneArgs,
                6000,
                INITIAL_ITEMS,
                DEFAULT_DIG_PENALTY));

        Map<String, Object> killArgs = new LinkedHashMap<>();
        killArgs.put("entity", "minecraft:pig");
        killArgs.put("count", 2);
        register(new TaskSpec(
                "kill_animal",
                "Kill 2 pigs.",
                "杀 2 头猪。",
                TaskSpec.TaskType.COMBAT,
                1,
                "entity_killed",
                killArgs,
                6000,
                INITIAL_ITEMS));

        // M11 生存工具包任务（镐/剑/铲/泥土）：物品 = SURVIVAL_KIT（hotbar 0-3）
        Map<String, Object> dirtArgs = new LinkedHashMap<>();
        dirtArgs.put("block", "minecraft:dirt");
        dirtArgs.put("count", 6);
        register(new TaskSpec(
                "dig_dirt",
                "Break 6 dirt blocks with a shovel.",
                "用铲子挖 6 个泥土。",
                TaskSpec.TaskType.COLLECT,
                1,
                "block_mined",
                dirtArgs,
                6000,
                SURVIVAL_KIT,
                DEFAULT_DIG_PENALTY));

        Map<String, Object> placeArgs = new LinkedHashMap<>();
        placeArgs.put("block", "minecraft:dirt");
        placeArgs.put("count", 3);
        register(new TaskSpec(
                "place_dirt",
                "Place 3 dirt blocks.",
                "放置 3 个泥土方块。",
                TaskSpec.TaskType.BUILD,
                1,
                "block_placed",
                placeArgs,
                6000,
                SURVIVAL_KIT));
    }

    public static synchronized TaskSpec get(String id) {
        return TASKS.get(id);
    }

    public static synchronized List<TaskSpec> all() {
        return new ArrayList<>(TASKS.values());
    }

    /** 注册/覆盖一个任务（内置 static 块与 JSON 加载共用）。 */
    public static synchronized void register(TaskSpec spec) {
        TASKS.put(spec.id(), spec);
    }

    // ---- M11.5 JSON 任务加载（难点②：插件式任务扩展） ----

    /**
     * 从目录加载全部 {@code *.json} 任务定义（同 id 覆盖）。目录不存在时创建（便于用户
     * 发现放置点）。坏文件跳过并打 WARN——加载不因单个文件失败中断。
     *
     * @return 成功加载的任务数
     */
    public static int loadFromDir(File dir, Logger log) {
        if (!dir.exists() && !dir.mkdirs()) {
            log.warning("[task] 无法创建任务目录: " + dir);
            return 0;
        }
        File[] files = dir.listFiles((d, name) -> name.endsWith(".json"));
        if (files == null || files.length == 0) {
            return 0;
        }
        int loaded = 0;
        for (File f : files) {
            try {
                String text = new String(Files.readAllBytes(f.toPath()), StandardCharsets.UTF_8);
                TaskSpec spec = fromJson(GSON.fromJson(text, JsonObject.class));
                register(spec);
                loaded++;
                log.info("[task] loaded " + spec.id() + " from " + f.getName());
            } catch (Exception e) {   // 单文件失败不中断
                log.warning("[task] 加载失败 " + f.getName() + ": " + e.getMessage());
            }
        }
        return loaded;
    }

    /** JSON → TaskSpec（schema 见类注释；id/instruction/predicate 必填，其余有缺省）。 */
    public static TaskSpec fromJson(JsonObject o) {
        String id = req(o, "id");
        String instruction = req(o, "instruction");
        String zh = o.has("instruction_zh") ? o.get("instruction_zh").getAsString() : "";
        TaskSpec.TaskType type = TaskSpec.TaskType.valueOf(
                o.has("type") ? o.get("type").getAsString().toUpperCase() : "COLLECT");
        int difficulty = o.has("difficulty") ? o.get("difficulty").getAsInt() : 1;
        String predicate = req(o, "predicate");
        Map<String, Object> args = new LinkedHashMap<>();
        if (o.has("args")) {
            for (Map.Entry<String, JsonElement> e : o.getAsJsonObject("args").entrySet()) {
                JsonElement v = e.getValue();
                if (v.isJsonPrimitive() && v.getAsJsonPrimitive().isNumber()) {
                    args.put(e.getKey(), v.getAsNumber());
                } else {
                    args.put(e.getKey(), v.getAsString());
                }
            }
        }
        int timeout = o.has("timeout_ticks") ? o.get("timeout_ticks").getAsInt() : 6000;
        List<String> items = new ArrayList<>();
        if (o.has("items")) {
            for (JsonElement e : o.getAsJsonArray("items")) {
                items.add(e.getAsString());
            }
        } else {
            items.addAll(SURVIVAL_KIT);
        }
        double digPenalty = o.has("dig_penalty") ? o.get("dig_penalty").getAsDouble() : 0.0;
        return new TaskSpec(id, instruction, zh, type, difficulty, predicate, args,
                timeout, items, digPenalty);
    }

    private static String req(JsonObject o, String key) {
        if (!o.has(key)) {
            throw new IllegalArgumentException("缺少必填字段: " + key);
        }
        return o.get(key).getAsString();
    }
}
