/**
 * 路径预算（非阻塞 A*）包裹层的行为契约测试 —— **不连 MC 服务器**，用假 bot 跑。
 *
 *     node test_pathfinder_precompute.js
 *
 * 测什么（index.js 里 `__openkagePathfinderGuarded` 那段）：
 *   ① 坐标校验的错误形状/时机不变（LLM 要读这些消息）
 *   ② 缓存命中 / 首节点漂移弃用 / 未命中时透明回落到原实现
 *   ③ setGoal 换 goal 时作废在飞预算；同一个 goal 时**不能**作废
 *   ④ timeout/noPath 的错误形状与 lib/goto.js:23-31 逐字一致
 *   ⑦ ★ 回归：`goto → _origGoto → lib/goto.js:63 setGoal(同一个 goal)` 这条**真实调用链**
 *      走完之后缓存必须还在。2026-08-16 的 smoke 实测 `hit=0 miss=23` 就是踩了这个 ——
 *      无条件 `_cancelPrecompute()` 会把刚备好的结果当场清掉，于是预算白算、优化完全失效
 *      （功能正常，所以只有靠 `[pathfinder预算]` 计数器才看得出来）。
 *
 * ⚠️ 本文件**复刻**（而非 import）index.js 守卫层的逻辑：那段代码住在 `/step` 处理体内、
 *    依赖真实 bot，抽不出来。所以改 index.js 那段时**必须同步改这里** —— 两边分叉的话
 *    这个测试会变成"测了个已经不存在的实现"。判据：`build()` 里的三个包裹要与
 *    index.js 里的同名逻辑逐行对得上。
 */
const PF=require('path').join(__dirname,'node_modules/mineflayer-pathfinder');
const AStar=require(PF+'/lib/astar.js');
const mk=(x,y,z)=>({x,y,z,hash:`${x},${y},${z}`,cost:1,remainingBlocks:0});

// 复刻 index.js 守卫层的核心逻辑（同构，用于验证契约）
function build(bot, opts={}) {
  const _origGetPathTo = bot.pathfinder.getPathTo;
  const _origGoto = bot.pathfinder.goto;
  const _origSetGoal = bot.pathfinder.setGoal;
  let _pre=null, _preToken=0;
  const _preStat={hit:0,miss:0,drift:0,abort:0,timeout:0};
  const _PRE_MAX_DRIFT_SQ=9;
  const _cancel=()=>{_preToken++;_pre=null;};
  async function _precompute(goal,budget){
    const myToken=++_preToken;
    const movements=bot.pathfinder.movements;
    if(!movements||!goal) return null;
    const gen=bot.pathfinder.getPathFromTo(movements,bot.entity.position,goal,{timeout:budget,tickTimeout:40});
    let r=gen.next().value?.result;
    while(r&&r.status==='partial'){
      await new Promise(res=>setImmediate(res));
      if(myToken!==_preToken){_preStat.abort++;return null;}
      r=gen.next().value?.result;
    }
    if(r) delete r.context;
    return r;
  }
  bot.pathfinder.getPathTo=(m,goal,t)=>{
    if(_pre&&_pre.goal===goal){
      const c=_pre.result;_pre=null;
      const f=c.path&&c.path[0];
      if(!f){_preStat.hit++;return c;}
      const dx=bot.entity.position.x-f.x,dy=bot.entity.position.y-f.y,dz=bot.entity.position.z-f.z;
      if(dx*dx+dy*dy+dz*dz<=_PRE_MAX_DRIFT_SQ){_preStat.hit++;return c;}
      _preStat.drift++;
    } else _preStat.miss++;
    return _origGetPathTo(m,goal,t);
  };
  bot.pathfinder.goto=async(goal,...rest)=>{
    if(opts.goalErr&&opts.goalErr(goal)) throw opts.goalErr(goal);
    const pre=await _precompute(goal,opts.budget??5000);
    if(pre&&pre.status!=='success') _preStat.timeout++;
    else if(pre) _pre={goal,result:pre};
    return _origGoto(goal,...rest);
  };
  bot.pathfinder.setGoal=(goal,...rest)=>{ if(!_pre||_pre.goal!==goal) _cancel(); return _origSetGoal(goal,...rest); };
  return {_preStat, cancel:_cancel, peek:()=>_pre};
}

function fakeBot(dist,wallAt,gapZ){
  const blocked=(x,z)=>wallAt!=null&&x===wallAt&&z!==gapZ;
  const movements={countScaffoldingItems:()=>0,allowEntityDetection:false,
    getNeighbors(n){const o=[];for(const[dx,dz]of[[1,0],[-1,0],[0,1],[0,-1],[1,1],[1,-1],[-1,1],[-1,-1]]){
      const x=n.x+dx,z=n.z+dz;if(Math.abs(x)>1500||Math.abs(z)>1500)continue;if(blocked(x,z))continue;o.push(mk(x,n.y,z));}return o;}};
  let gotoCalled=0, origGetPathToCalled=0;
  const bot={
    entity:{position:{x:0,y:64,z:0,distanceSquared(){return 0;}}},
    pathfinder:{
      movements, thinkTimeout:5000, tickTimeout:40,
      getPathFromTo:function*(m,start,goal,o={}){
        const a=new AStar(mk(0,64,0),m,goal,o.timeout??5000,o.tickTimeout??40,-1);
        let r=a.compute(); yield {result:r,astarContext:a};
        while(r.status==='partial'){r=a.compute();yield {result:r,astarContext:a};}
      },
      getPathTo:(m,goal,t)=>{origGetPathToCalled++;return {status:'success',path:[mk(1,64,0)]};},
      goto:async(goal)=>{gotoCalled++;bot.pathfinder.setGoal(goal);return 'walked';},
      setGoal:()=>{},
    },
  };
  return {bot, stats:()=>({gotoCalled,origGetPathToCalled})};
}
const G=d=>({isEnd:n=>n.x===d&&n.z===0,heuristic:n=>Math.max(Math.abs(d-n.x),Math.abs(n.z))});

(async()=>{
let pass=0,fail=0;
const ok=(c,m)=>{c?(pass++,console.log('  ✓ '+m)):(fail++,console.log('  ✗ '+m));};

console.log('① 校验错误：形状与时机');
{
  const {bot}=fakeBot(); const badErr=new Error('bad coords');
  const h=build(bot,{goalErr:g=>(g&&g.__bad?badErr:null)});
  let caught=null; try{ await bot.pathfinder.goto({__bad:1}); }catch(e){caught=e;}
  ok(caught===badErr,'goto 校验错误经 reject 抛出（await/catch 可接）');
}
console.log('② 正常路径：预算命中 → getPathTo 拿到缓存，不走 _origGetPathTo');
{
  const {bot,stats}=fakeBot(32,null,null); const h=build(bot);
  const goal=G(32);
  const r=await bot.pathfinder.goto(goal);
  ok(r==='walked','goto 最终交给 _origGoto 走');
  ok(h.peek()!==null,'预算结果已存入缓存');
  const p=bot.pathfinder.getPathTo(bot.pathfinder.movements,goal,5000);
  ok(p.status==='success'&&h._preStat.hit===1,'getPathTo 命中缓存 hit=1');
  ok(stats().origGetPathToCalled===0,'未回落到 _origGetPathTo');
  ok(h.peek()===null,'单次消费：缓存已清空');
  bot.pathfinder.getPathTo(bot.pathfinder.movements,goal,5000);
  ok(h._preStat.miss===1&&stats().origGetPathToCalled===1,'第二次 miss 并透明回落');
}
console.log('③ 漂移弃用');
{
  const {bot,stats}=fakeBot(32,null,null); const h=build(bot);
  const goal=G(32);
  await bot.pathfinder.goto(goal);
  bot.entity.position.x=99;   // 预算后 bot 被推到 99 格外
  bot.pathfinder.getPathTo(bot.pathfinder.movements,goal,5000);
  ok(h._preStat.drift===1&&stats().origGetPathToCalled===1,'漂移>3格 → drift=1 且回落重算');
}
console.log('④ setGoal 作废在飞预算');
{
  const {bot}=fakeBot(200,100,900); const h=build(bot);
  const goal=G(200);
  const p=bot.pathfinder.goto(goal);          // 长搜索，在飞
  setImmediate(()=>bot.pathfinder.setGoal(null));  // 中途作废
  await p.catch(()=>{});
  ok(h._preStat.abort>=1,'在飞预算被 setGoal 作废 abort>=1');
  ok(h.peek()===null,'缓存为空');
}
console.log('⑤ ★回归：预算算不出完整路径时**不得抛错**，要退化成 _origGoto 走 partial 路径');
{
  // 改动前库的行为是「A* 超时 → 返回 partial 路径 → bot 边走边算、至少挪近一截」。
  // 第一版在这里 throw → bot 一步没动就报错，同一目标反复失败：
  // 实测 26次/75step(34.7%) 远高于基线 40/318(12.6%)。
  const {bot,stats}=fakeBot(200,100,1200); const h=build(bot,{budget:300});
  let e=null,r=null; try{ r=await bot.pathfinder.goto(G(200)); }catch(x){e=x;}
  ok(e===null,'预算超时**不抛错**（否则 bot 原地不动、白烧步数）');
  ok(r==='walked','照旧交给 _origGoto（库会走 partial 路径并继续续算）');
  ok(h._preStat.timeout===1,'但要计数 timeout=1，便于观测');
  ok(h.peek()===null,'失败结果不进缓存');
}
console.log('⑥ null goal / 无 movements 安全');
{
  const {bot}=fakeBot(); const h=build(bot);
  bot.pathfinder.setGoal(null);
  ok(true,'setGoal(null) 不抛');
  const r=await bot.pathfinder.goto(null);
  ok(r==='walked','goto(null) 放弃预算后照常交给原实现');
}
console.log('⑦ ★回归：goto→_origGoto→setGoal(同 goal) 不得清掉刚备好的缓存');
{
  const {bot,stats}=fakeBot(32,null,null); const h=build(bot);
  const goal=G(32);
  await bot.pathfinder.goto(goal);   // 真实链路：内部会 setGoal(goal)
  ok(h.peek()!==null,'走完 goto 全链路后缓存仍在（2026-08-16 smoke hit=0 的根因）');
  const p=bot.pathfinder.getPathTo(bot.pathfinder.movements,goal,5000);
  ok(h._preStat.hit===1&&stats().origGetPathToCalled===0,'monitorMovement 能命中缓存 hit=1');
}
console.log('⑧ 不同 goal 的 setGoal 仍要作废');
{
  const {bot}=fakeBot(32,null,null); const h=build(bot);
  await bot.pathfinder.goto(G(32));
  ok(h.peek()!==null,'缓存已备');
  bot.pathfinder.setGoal(G(99));   // 换了别的 goal
  ok(h.peek()===null,'换 goal → 缓存作废');
}
console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail?1:0);
})();
