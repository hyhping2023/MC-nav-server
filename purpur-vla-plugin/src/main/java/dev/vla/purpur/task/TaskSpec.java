package dev.vla.purpur.task;

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
 */
public record TaskSpec(
        String id,
        String instruction,
        String instructionZh,
        TaskType type,
        int difficulty,
        String successPredicate,
        Map<String, Object> successArgs,
        int timeoutTicks) {

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
}
