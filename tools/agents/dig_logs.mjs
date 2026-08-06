// M5 验收辅助脚本：离线 bot（默认 agent0）spawn 后扫描 12 格内 oak_log，
// 按可达距离排序挖 4 个，打印每次挖的坐标，结束退出。仅用于本次验证。
// 用法: node dig_logs.mjs [--host 127.0.0.1] [--port 25565] [--username agent0]
import { createBot } from "mineflayer";

const args = process.argv.slice(2);
const get = (flag, def) => {
  const i = args.indexOf(flag);
  return i >= 0 ? args[i + 1] : def;
};
const host = get("--host", "127.0.0.1");
const port = parseInt(get("--port", "25565"), 10);
const username = get("--username", "agent0");
const NEED = 4;

const bot = createBot({ host, port, username, auth: "offline" });

bot.once("spawn", async () => {
  const pos = bot.entity.position;
  console.log(`[dig_logs] ${username} spawned at ${pos}`);
  try {
    await bot.waitForChunksToLoad();
  } catch (err) {
    console.log(`[dig_logs] waitForChunksToLoad: ${err.message}`);
  }
  await new Promise((r) => setTimeout(r, 1500));

  // 扫描 12 格内 oak_log（找不到则扩大到 20 格）
  const logs = [];
  for (const radius of [12, 20]) {
    for (let dx = -radius; dx <= radius; dx++) {
      for (let dy = -radius; dy <= radius; dy++) {
        for (let dz = -radius; dz <= radius; dz++) {
          const p = bot.blockAt(pos.offset(dx, dy, dz));
          if (p && p.name === "oak_log") {
            logs.push(p);
          }
        }
      }
    }
    if (logs.length >= NEED) break;
  }
  // 按欧氏距离（到方块中心）排序，优先近的
  const dist = (b) =>
    bot.entity.position.distanceTo(b.position.offset(0.5, 0.5, 0.5));
  logs.sort((a, b) => dist(a) - dist(b));
  console.log(`[dig_logs] found ${logs.length} oak_log blocks (need ${NEED})`);

  let dug = 0;
  for (const block of logs) {
    if (dug >= NEED) break;
    try {
      await bot.dig(block);
      console.log(`[dig_logs] DUG ${block.name} at ${block.position} (${dug + 1}/${NEED})`);
      dug++;
    } catch (err) {
      console.log(`[dig_logs] dig failed ${block.position}: ${err.message}`);
    }
    await new Promise((r) => setTimeout(r, 200));
  }
  console.log(`[dig_logs] dug ${dug}/${NEED} oak logs, quitting`);
  bot.quit();
});

bot.on("kicked", (reason) => console.log(`[dig_logs] kicked: ${reason}`));
bot.on("error", (err) => console.error(`[dig_logs] error: ${err.message}`));
bot.on("end", (reason) => console.log(`[dig_logs] ended: ${reason}`));

const shutdown = () => {
  bot.quit();
  setTimeout(() => process.exit(0), 500);
};
process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
