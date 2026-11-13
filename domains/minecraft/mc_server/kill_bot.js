// 在手工服务器上制造真实死亡 + tier 突破，用来验证录制链路的完整效果
const mineflayer=require('mineflayer');
const bot=mineflayer.createBot({host:'127.0.0.1',port:25567,username:'bot',disableChatSigning:true,checkTimeoutInterval:600000});
bot.on('error',e=>console.log('ERR',e.message));
async function w(t){ try{ await bot.waitForTicks(t); }catch(e){} }
bot.once('spawn',async()=>{
  bot.chat('/op spec_25567');
  bot.chat('/time set day'); bot.chat('/weather clear');
  await w(20);
  console.log('READY', bot.entity.position.toString());
  console.log('CMD_WAIT');   // 录制器就绪后由外部触发
});
process.on('SIGUSR1', async ()=>{
  // tier 突破：给一把石镐
  bot.chat('/give bot stone_pickaxe');
  await w(40);
  console.log('TIER_GIVEN');
  await w(40);
  // 真实死亡
  bot.chat('/damage bot 100');
  console.log('KILLED');
});
setTimeout(()=>process.exit(0), 300000);
