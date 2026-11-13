/**
 * 测正常路径下 window_click 的发包量与速率——判断熔断阈值 200 会不会误伤。
 *
 *   bash start_servers.sh --mc-only
 *   node test_window_click_normal_usage.js
 *
 * 测的最坏正常用法：
 *   ① bot.craft(planks, 10, null)  —— 徒手合 10 次木板（无工作台，2×2）
 *   ② bot.craft(sticks, 5, null)   —— 合 5 次木棍
 *   ③ chest.deposit 多种物品 各 32
 *   ④ chest.withdraw 取回
 *
 * 每条记 window_click 包数 + 耗时 + 速率（包/秒）。判据：
 *   单条 > 150 包 或 速率 > 200/秒 → 熔断阈值 200 有误伤风险，要调高或改速率判据
 *   全部 < 100 且速率 < 50/秒 → 200 阈值安全，正常用法碰不到
 */
const mineflayer = require("mineflayer");
const { pathfinder, Movements, goals } = require("mineflayer-pathfinder");
const { Vec3 } = require("vec3");
const bot = mineflayer.createBot({ host:"localhost", port:25567, username:"bot", version:"1.21.11" });
bot.loadPlugin(pathfinder);
const log=(...a)=>console.log(`[norm]`,...a);
const wait=(ms)=>new Promise(r=>setTimeout(r,ms));
const cmd=async(s,ms=700)=>{bot.chat(s);await wait(ms);};
const mcData=()=>require("minecraft-data")(bot.version);

let wcCount=0, wcStart=0, wcTimes=[];
const ow=bot._client.write.bind(bot._client);
bot._client.write=(n,p)=>{if(n==="window_click"){if(wcCount===0)wcStart=Date.now();wcCount++;wcTimes.push(Date.now());}return ow(n,p);};

function snap(label){ const t=wcTimes.length?Date.now()-wcStart:0; const rate=wcTimes.length&&t? (wcTimes.length*1000/t).toFixed(0):0; log(`${label}: window_click=${wcCount} 耗时=${t}ms 速率≈${rate}/秒`); }
function resetWc(){wcCount=0;wcTimes=[];wcStart=0;}

bot.once("spawn",async()=>{
  log("spawn");await wait(1500);
  await cmd("/time set 1000",600);await cmd("/gamerule doMobSpawning false",600);
  // 给一堆原木
  bot.inventory.items().forEach(it=>{try{bot.tossStack(it);}catch(e){}});await wait(400);
  await cmd("/give bot minecraft:oak_log 64",700);

  log("\n========== ① craft planks ×10（徒手 2×2）==========");
  resetWc();
  try{
    const rec=bot.recipesFor(mcData().itemsByName["oak_planks"].id,null,1,null)[0];
    await bot.craft(rec,10,null);
    snap("craft planks×10 完");
  }catch(e){log("craft err",e.message);snap("craft planks×10 失败");}

  log("\n========== ② craft sticks ×5 ==========");
  resetWc();
  try{
    const rec=bot.recipesFor(mcData().itemsByName["stick"].id,null,1,null)[0];
    await bot.craft(rec,5,null);
    snap("craft sticks×5 完");
  }catch(e){log("craft err",e.message);snap("craft sticks×5 失败");}

  // 放箱子
  const pos=bot.entity.position.floored();const cp=pos.offset(0,0,1);
  await cmd(`/setblock ${cp.x} ${cp.y} ${cp.z} minecraft:air`,400);
  await cmd(`/setblock ${cp.x} ${cp.y} ${cp.z} minecraft:chest`,700);await wait(300);
  const cb=bot.blockAt(cp);
  try{bot.pathfinder.setMovements(new Movements(bot,mcData()));await bot.pathfinder.goto(new goals.GoalNear(cp.x,cp.y,cp.z,1));}catch(e){}
  await wait(300);

  log("\n========== ③ deposit 多种物品 ==========");
  resetWc();
  try{
    const chest=await bot.openContainer(cb);
    const plank=mcData().itemsByName["oak_planks"];
    await chest.deposit(plank.id,null,32);
    const stick=mcData().itemsByName["stick"];
    await chest.deposit(stick.id,null,32);
    chest.close();
    snap("deposit 2种各32 完");
  }catch(e){log("deposit err",e.message);snap("deposit 失败");}

  log("\n========== ④ withdraw 取回 ==========");
  resetWc();
  try{
    const chest=await bot.openContainer(cb);
    const plank=mcData().itemsByName["oak_planks"];
    await chest.withdraw(plank.id,null,32);
    chest.close();
    snap("withdraw 32 完");
  }catch(e){log("withdraw err",e.message);snap("withdraw 失败");}

  log("\n================ 判据 ================");
  log("若每条 window_click < 100 且速率 < 50/秒 → 200 阈值安全");
  log("若有条 > 150 或速率 > 200/秒 → 阈值有误伤风险，需调高或改速率判据");
  setTimeout(()=>process.exit(0),300);
});
bot.on("kicked",r=>log("kicked",typeof r==="string"?r:JSON.stringify(r)));
bot.on("error",e=>log("err",e.message));
