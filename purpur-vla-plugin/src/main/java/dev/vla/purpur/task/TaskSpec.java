package dev.vla.purpur.task;

import java.util.List;
import java.util.Map;

/**
 * 任务定义（DESIGN.md §4.7 TaskSpec 子集）。
 *
 * @param id               注册表任务 id（如 collect_wood）
 * @param instruction      英文指令文案（VLA 语言指令）
 * @param instructionZh    中文指令（可选）
 * @param type             任务类型（COLLECT/CRAFT/...）
 * @param difficulty       难度（课程/自动课程用）
 * @param successPredicate 成功判定器名（block_mined / inventory_contains / entity_killed / player_at）
 * @param successArgs      判定器参数（如 {block:"minecraft:oak_log", count:4}）
 * @param timeoutTicks     超时刻数（默认 6000 = 5min @20tps），steps 累计超过即 truncated
 * @param initialItems     reset 时发给玩家的初始物品（如 collect_stone 的木镐，键形如 "minecraft:wooden_pickaxe"）
 * @param digPenalty       过度挖掘惩罚（M11.5 难点③）：每挖一块**非目标**方块扣的 reward
 *                         （0 = 不惩罚）。挖目标块永不惩罚；非 block_mined 任务对一切挖掘生效
 */
public record TaskSpec(
        String id,
        String instruction,
        String instructionZh,
        TaskType type,
        int difficulty,
        String successPredicate,
        Map<String, Object> successArgs,
        int timeoutTicks,
        List<String> initialItems,
        double digPenalty) {

    /** 兼容构造：digPenalty = 0（老调用点不惩罚过度挖掘）。 */
    public TaskSpec(String id, String instruction, String instructionZh, TaskType type,
                    int difficulty, String successPredicate, Map<String, Object> successArgs,
                    int timeoutTicks, List<String> initialItems) {
        this(id, instruction, instructionZh, type, difficulty, successPredicate, successArgs,
                timeoutTicks, initialItems, 0.0);
    }

    /** 任务类型（DESIGN.md §4.7）。 */
    public enum TaskType {
        COLLECT, CRAFT, BUILD, COMBAT, NAV, INTERACT
    }

    /** 从 args 读 int（兼容 Integer/Long/Double/String）。 */
    public static int argInt(Map<String, Object> args, String key, int def) {
        Object v = args.get(key);
        if (v == null) {
            return def;
        }
        if (v instanceof Number n) {
            return n.intValue();
        }
        try {
            return Integer.parseInt(v.toString().trim());
        } catch (NumberFormatException e) {
            return def;
        }
    }

    /** 从 args 读字符串。 */
    public static String argStr(Map<String, Object> args, String key, String def) {
        Object v = args.get(key);
        return v == null ? def : v.toString();
    }

    /** 从 args 读 double（兼容 Number/String）。 */
    public static double argDouble(Map<String, Object> args, String key, double def) {
        Object v = args.get(key);
        if (v == null) {
            return def;
        }
        if (v instanceof Number n) {
            return n.doubleValue();
        }
        try {
            return Double.parseDouble(v.toString().trim());
        } catch (NumberFormatException e) {
            return def;
        }
    }
}
