// M0 多玩家连通性冒烟测试：启动 N 个无头 mineflayer bot 连入 Purpur 服务端
// 用法: node bot.mjs --host 127.0.0.1 --port 25565 --count 2 --prefix agent
import { createBot } from "mineflayer";

const args = process.argv.slice(2);
const get = (flag, def) => {
  const i = args.indexOf(flag);
  return i >= 0 ? args[i + 1] : def;
};
const host = get("--host", "127.0.0.1");
const port = parseInt(get("--port", "25565"), 10);
const count = parseInt(get("--count", "2"), 10);
const prefix = get("--prefix", "agent");

const bots = [];
let joined = 0;

for (let i = 0; i < count; i++) {
  const username = `${prefix}${i}`;
  const bot = createBot({ host, port, username, auth: "offline" });
  bots.push(bot);

  bot.on("login", () => console.log(`[${username}] login OK`));
  bot.on("spawn", () => {
    joined++;
    console.log(`[${username}] spawned at ${bot.entity.position}`);
    if (joined === count) {
      console.log(`SMOKE_OK: ${joined}/${count} bots joined ${host}:${port}`);
    }
  });
  bot.on("kicked", (reason) => console.log(`[${username}] kicked: ${reason}`));
  bot.on("error", (err) => {
    console.error(`[${username}] error: ${err.message}`);
  });
  bot.on("end", (reason) => console.log(`[${username}] connection ended: ${reason}`));
}

// 保持进程存活；Ctrl+C 时断开所有 bot
const shutdown = () => {
  console.log("disconnecting bots...");
  bots.forEach((b) => b.quit());
  setTimeout(() => process.exit(0), 1500);
};
process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
