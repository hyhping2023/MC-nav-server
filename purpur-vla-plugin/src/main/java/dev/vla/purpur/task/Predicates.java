package dev.vla.purpur.task;

import java.util.List;
import org.bukkit.Location;
import org.bukkit.entity.Player;
import org.bukkit.inventory.ItemStack;

/**
 * 成功判定器（DESIGN.md §4.7 内置判定器）。
 *
 * <p>判定以服务端事件/状态为准（§14.2 server-authoritative）：
 * block_mined/block_placed 由 {@link TaskManager} 事件回调维护 counter；
 * entity_killed 同理；inventory_contains 实时扫背包；player_at 实时算距离。
 */
public final class Predicates {

    private Predicates() {
    }

    /** 判定任务是否成功。 */
    public static boolean evaluate(TaskSpec task, Player player, TaskManager.EpisodeState state) {
        if (task == null || task.successPredicate() == null) {
            return false;
        }
        switch (task.successPredicate()) {
            case "block_mined":
            case "block_placed": {
                String block = TaskSpec.argStr(task.successArgs(), "block", "");
                int count = TaskSpec.argInt(task.successArgs(), "count", 1);
                return state.counter(task.successPredicate(), block) >= count;
            }
            case "entity_killed": {
                String entity = TaskSpec.argStr(task.successArgs(), "entity", "");
                int count = TaskSpec.argInt(task.successArgs(), "count", 1);
                return state.counter("entity_killed", entity) >= count;
            }
            case "inventory_contains": {
                String item = TaskSpec.argStr(task.successArgs(), "item", "");
                int count = TaskSpec.argInt(task.successArgs(), "count", 1);
                return countInventory(player, item) >= count;
            }
            case "player_at": {
                double[] pos = parsePos(task.successArgs().get("pos"));
                if (pos == null) {
                    return false;
                }
                double tolerance = TaskSpec.argInt(task.successArgs(), "tolerance", 2);
                Location loc = player.getLocation();
                double dx = loc.getX() - pos[0];
                double dy = loc.getY() - pos[1];
                double dz = loc.getZ() - pos[2];
                return Math.sqrt(dx * dx + dy * dy + dz * dz) < tolerance;
            }
            default:
                return false;
        }
    }

    /** 统计背包中匹配物品的总数（含快捷栏；不统计盔甲/副手外槽位以外的槽）。 */
    public static int countInventory(Player player, String itemKey) {
        int n = 0;
        for (ItemStack it : player.getInventory().getContents()) {
            if (it != null && !it.getType().isAir()
                    && it.getType().getKey().toString().equals(itemKey)) {
                n += it.getAmount();
            }
        }
        return n;
    }

    /**
     * 解析 args 中的 pos：支持 {@code List<Number>}、{@code Number[]}、{@code double[]}、
     * 或 {@code "x,y,z"} 字符串；无法解析返回 null。
     */
    private static double[] parsePos(Object raw) {
        if (raw == null) {
            return null;
        }
        if (raw instanceof List<?> list && list.size() >= 3) {
            double[] p = new double[3];
            for (int i = 0; i < 3; i++) {
                if (!(list.get(i) instanceof Number n)) {
                    return null;
                }
                p[i] = n.doubleValue();
            }
            return p;
        }
        if (raw instanceof double[] d && d.length >= 3) {
            return new double[]{d[0], d[1], d[2]};
        }
        if (raw instanceof Number[] arr && arr.length >= 3) {
            return new double[]{arr[0].doubleValue(), arr[1].doubleValue(), arr[2].doubleValue()};
        }
        String s = raw.toString().trim();
        String[] parts = s.split(",");
        if (parts.length >= 3) {
            try {
                return new double[]{
                        Double.parseDouble(parts[0].trim()),
                        Double.parseDouble(parts[1].trim()),
                        Double.parseDouble(parts[2].trim())};
            } catch (NumberFormatException e) {
                return null;
            }
        }
        return null;
    }
}
