package dev.vla.client.input;

import net.minecraft.client.MinecraftClient;
import net.minecraft.client.network.ClientPlayerEntity;
import net.minecraft.client.option.KeyBinding;
import net.minecraft.util.math.MathHelper;

/**
 * M2 动作注入（DESIGN.md §5.3 / docs/p1_protocol.md §3）。
 *
 * <ul>
 *   <li>视角：{@code setPitch/setYaw} 增量（pitch 夹紧 ±90°，规避万向节死锁）</li>
 *   <li>攻击/使用：{@link KeyBinding#setPressed}（长按驱动原版挖掘进度 / 使用物品）</li>
 *   <li>丢弃/物品栏/快捷栏：原版用 {@code wasPressed()} 消费，需经
 *       {@link KeyBinding#onKeyPressed} 补一次 timesPressed，否则 {@code setPressed} 无效</li>
 *   <li>快捷栏选中后由 {@link #resetKeys} 在下一 tick 释放（模拟按键按下-抬起）</li>
 *   <li>移动由 {@code KeyboardInputMixin} 处理，本类不管 movement</li>
 * </ul>
 */
public final class ActionApplier {

    private ActionApplier() {
    }

    /**
     * 每 tick（END_CLIENT_TICK 开头）释放上一 tick 注入的按键，防止粘键。
     */
    public static void resetKeys(MinecraftClient client) {
        if (client == null || client.options == null) {
            return;
        }
        client.options.attackKey.setPressed(false);
        client.options.useKey.setPressed(false);
        client.options.dropKey.setPressed(false);
        client.options.inventoryKey.setPressed(false);
        for (KeyBinding hotbarKey : client.options.hotbarKeys) {
            hotbarKey.setPressed(false);
        }
    }

    public static void apply(MinecraftClient client, ClientPlayerEntity player, ActionCmd cmd) {
        if (client == null || player == null || cmd == null) {
            return;
        }

        // 1) 视角增量：一次性字段，应用后清零（动作电平保持时防止每 tick 重复累加）
        if (cmd.camera != null) {
            player.setPitch(MathHelper.clamp(player.getPitch() + cmd.camera[0], -90.0f, 90.0f));
            player.setYaw(player.getYaw() + cmd.camera[1]);
            cmd.camera[0] = 0.0f;
            cmd.camera[1] = 0.0f;
        }

        // 2) 长按型按钮（isPressed() 驱动，电平保持：持续按住直到新动作置 false）
        client.options.attackKey.setPressed(cmd.attack);
        client.options.useKey.setPressed(cmd.use);

        // 3) 离散型按钮（wasPressed() 驱动，需补 timesPressed）；一次性，应用后清零
        if (cmd.drop) {
            pressDiscrete(client.options.dropKey);
            cmd.drop = false;
        }
        if (cmd.inventory) {
            pressDiscrete(client.options.inventoryKey);
            cmd.inventory = false;
        }

        // 4) 快捷栏 0-8：模拟按键选择；一次性，应用后清零（下一 tick resetKeys 释放）
        if (cmd.hotbar >= 0 && cmd.hotbar < client.options.hotbarKeys.length) {
            pressDiscrete(client.options.hotbarKeys[cmd.hotbar]);
            cmd.hotbar = -1;
        }
    }

    /**
     * 模拟一次按键按下：既置 {@code pressed}（让 {@code isPressed()} 生效），
     * 又经 {@code onKeyPressed} 补 timesPressed（让 {@code wasPressed()} 生效）。
     * {@code getDefaultKey()} 与注册表匹配依赖默认键位绑定。
     */
    private static void pressDiscrete(KeyBinding keyBinding) {
        keyBinding.setPressed(true);
        KeyBinding.onKeyPressed(keyBinding.getDefaultKey());
    }
}
