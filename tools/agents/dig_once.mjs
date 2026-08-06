// M4 验收辅助脚本：离线 bot（默认 agent0）spawn 后找 4 格内最近的可挖掘非空气方块并挖掘，
// 打印挖掉坐标后退出。仅用于本次验证，不修改既有工具目录文件。
// 用法: node dig_once.mjs [--host 127.0.0.1] [--port 25565] [--username agent0]
import { createBot } from "mineflayer";

const args = process.argv.slice(2);
const get = (flag, def) => {
  const i = args.indexOf(flag);
  return i >= 0 ? args[i + 1] : def;
};
const host = get("--host", "127.0.0.1");
const port = parseInt(get("--port", "25565"), 10);
const username = get("--username", "agent0");

const bot = createBot({ host, port, username, auth: "offline" });

// 排除不可挖掘 / 非固体 / 危险方块
const EXCLUDE = new Set([
  "bedrock",
  "water",
  "flowing_water",
  "lava",
  "flowing_lava",
  "barrier",
  "command_block",
  "structure_block",
]);

bot.once("spawn", async () => {
  const pos = bot.entity.position;
  console.log(`[dig_once] ${username} spawned at ${pos}`);
  // 等待客户端区块加载完成，否则 blockAt 可能返回 air/undefined
  try {
    await bot.waitForChunksToLoad();
  } catch (err) {
    console.log(`[dig_once] waitForChunksToLoad: ${err.message}`);
  }
  await new Promise((r) => setTimeout(r, 1500));

  let target = null;
  let bestDist = Infinity;
  for (let attempt = 0; attempt < 3 && !target; attempt++) {
    for (let dx = -4; dx <= 4; dx++) {
      for (let dy = -4; dy <= 4; dy++) {
        for (let dz = -4; dz <= 4; dz++) {
          const p = bot.blockAt(pos.offset(dx, dy, dz));
          if (!p || p.type === 0) continue; // air
          if (EXCLUDE.has(p.name)) continue;
          const d = Math.abs(dx) + Math.abs(dy) + Math.abs(dz);
          if (d < bestDist) {
            bestDist = d;
            target = p;
          }
        }
      }
    }
    if (!target) {
      console.log(`[dig_once] attempt ${attempt + 1}: no diggable block, retrying...`);
      await new Promise((r) => setTimeout(r, 1500));
    }
  }
  if (!target) {
    console.log("[dig_once] no diggable block within 4 blocks");
    bot.quit();
    return;
  }
  console.log(`[dig_once] digging ${target.name} at ${target.position}`);
  try {
    await bot.dig(target);
    console.log(`[dig_once] DUG ${target.name} at ${target.position}`);
  } catch (err) {
    console.log(`[dig_once] dig error: ${err.message}`);
  }
  bot.quit();
});

bot.on("kicked", (reason) => console.log(`[dig_once] kicked: ${reason}`));
bot.on("error", (err) => console.error(`[dig_once] error: ${err.message}`));
bot.on("end", (reason) => console.log(`[dig_once] ended: ${reason}`));

const shutdown = () => {
  bot.quit();
  setTimeout(() => process.exit(0), 500);
};
process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
