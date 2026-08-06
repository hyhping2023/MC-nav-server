"""优先级行为树：存活（进食）> 战斗（击杀/拉扯/逃跑）> 任务。

参考 mineflayer-auto-eat（hunger<14 进食）与 mineflayer-pvp（engage/kite/FLEE
状态机，冷却节流）。决策结果下发 Lua 语义动作（attack/eat/goto），由
Lua skills 负责实际执行质量。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from mcl2_env.brain.observation import MobEntity, Observation, Vec3

if TYPE_CHECKING:
    from mcl2_env.brain.policy import Context

# 进食阈值（饥饿或血量低于该值且背包有食物 -> 吃）
EAT_HUNGER_THRESHOLD = 8
EAT_HP_THRESHOLD = 8
# 战斗阈值
HOSTILE_AGGRO_RANGE = 16.0   # 警戒半径（与 mcl_mobs view_range 一致）
FLEE_HP = 4                  # 血量低于此值 -> 逃跑（打不过且无食物）
KITE_HP = 12                 # 血量低于此值 -> 拉扯风筝，不硬刚


class EatBehavior:
    """存活：饥饿/血量低且有食物 -> 吃（每 3 步最多下发一次，避免灌队列）。"""

    name = "eat"

    def should_run(self, obs: Observation) -> bool:
        return (
            obs.player.hunger <= EAT_HUNGER_THRESHOLD
            or obs.player.hp <= EAT_HP_THRESHOLD
        ) and obs.has_food()

    def act(self, obs: Observation, ctx: "Context") -> list[tuple[str, dict]]:
        if ctx.step < ctx.eat_cooldown_until:
            return []
        ctx.eat_cooldown_until = ctx.step + 3
        return [("eat", {})]


class CombatBehavior:
    """战斗：16 格内出现敌对 -> 击杀/拉扯/逃跑。"""

    name = "combat"

    def should_run(self, obs: Observation) -> bool:
        return obs.nearest_hostile(HOSTILE_AGGRO_RANGE) is not None

    def act(self, obs: Observation, ctx: "Context") -> list[tuple[str, dict]]:
        mob = obs.nearest_hostile(HOSTILE_AGGRO_RANGE)
        if mob is None:
            return []
        hp = obs.player.hp
        can_win = self._can_win(obs, mob)

        # 打不过 + 残血 -> 逃跑（向远离目标的方向 goto）
        if hp <= FLEE_HP and (not obs.has_food() or not can_win):
            if ctx.step < ctx.flee_until_step:
                return []
            ctx.flee_until_step = ctx.step + 10
            away = Vec3(
                obs.player.pos.x + (obs.player.pos.x - mob.pos.x) * 3,
                obs.player.pos.y,
                obs.player.pos.z + (obs.player.pos.z - mob.pos.z) * 3,
            )
            return [("goto", {"pos": away.to_dict()})]

        # 低血 -> 拉扯风筝；健康 -> 正面击杀
        mode = "kite" if hp <= KITE_HP else "melee"
        # 同一目标 5 步内不重复下发（Lua attack 会在队列里持续执行）
        if ctx.fight_target_id == mob.id and ctx.step - ctx.fight_issued_step < 5:
            return []
        ctx.fight_target_id = mob.id
        ctx.fight_issued_step = ctx.step
        return [("attack", {"target": "auto", "mode": mode})]

    @staticmethod
    def _can_win(obs: Observation, mob: MobEntity) -> bool:
        """粗判胜负：有武器或有血量优势即视为可打。"""
        if mob.health is None:
            return True
        if "sword" in obs.player.held_item:
            return True
        return mob.health <= obs.player.hp
