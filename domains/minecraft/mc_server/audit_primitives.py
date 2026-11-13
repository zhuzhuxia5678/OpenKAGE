#!/usr/bin/env python3
"""原语边界审计（回归测试用）

用法：先起 MC + mineflayer（默认 :3205），再 `python3 audit_primitives.py`。
判据见下。任何一次改原语/换 mineflayer 版本/npm ci 之后都该跑一遍。

把每个 LLM 可调函数的坏输入/边界输入都过一遍真实 /step。

判据（对每个用例）：
  · 进程存活？   —— 死了就是致命 bug（PID 变化 / 端口不通）
  · 是否卡死？   —— 墙钟远超申请时限 = 挂住
  · 反馈可执行？ —— 有 onChat/onError 说清问题，而不是静默返回

⚠️ 本审计覆盖 control_primitives/ 下**全部**原语，与「哪些注入给 Agent」正交：0827 起生产
路径只注入 waitUntil，其余能力原语都不注入。审计仍把它们列进来，是因为它测的是
「函数本身在真服上对坏输入健壮否」，改原语/换 mineflayer 时回归——与是否注入无关。
"""
import json, subprocess, sys, time, io, os

URL = "http://127.0.0.1:3205"
PRIMS = ["craftHelper", "craftItem", "mineBlock", "placeItem", "givePlacedItemBack",
         "killMob", "shoot", "smeltItem", "useChest", "exploreUntil",
         "waitUntil", "waitForMobRemoved",
         "collectDroppedItems"]


def programs():
    s = ""
    for f in PRIMS:
        p = f"control_primitives/{f}.js"
        if os.path.exists(p):
            s += io.open(p, encoding="utf-8").read() + "\n"
    return s


def pid():
    r = subprocess.run(["pgrep", "-f", "node index.js 3205"],
                       capture_output=True, text=True)
    return (r.stdout.split() or [None])[0]


def step(code, timeout_ticks=300, http_timeout=None):
    # ★ timeout_ticks 单位是游戏刻（与 index.js 的 STEP_CODE_TIMEOUT_TICKS 同口径）。
    #   HTTP 超时按 1x 折算成墙钟秒（audit 在自然流速下跑）：刻/20 + 30s 余量。
    body = {"code": code, "programs": programs(), "timeoutTicks": timeout_ticks}
    io.open("/tmp/_a.json", "w", encoding="utf-8").write(json.dumps(body))
    ht = http_timeout or (timeout_ticks / 20 + 30)
    t0 = time.time()
    r = subprocess.run(["curl", "-s", "-m", str(int(ht)), "-X", "POST",
                        f"{URL}/step", "-H", "Content-Type: application/json",
                        "-d", "@/tmp/_a.json"], capture_output=True, text=True)
    dt = (time.time() - t0) * 1000
    chats, logs, errs = [], [], []
    try:
        raw = json.loads(r.stdout)
        if isinstance(raw, str):
            raw = json.loads(raw)
        for e in raw:
            if isinstance(e, list) and len(e) >= 2:
                k, v = e[0], e[1]
                val = v.get(k) if isinstance(v, dict) else v
                if k == "onChat":
                    chats.append(str(val))
                elif k == "onLog":
                    logs.append(str(val))
                elif k == "onError":
                    errs.append(str(val))
    except Exception:
        pass
    return {"ms": dt, "http_ok": r.returncode == 0 and bool(r.stdout),
            "chat": " | ".join(chats), "log": " | ".join(logs), "err": " | ".join(errs)}


CASES = [
    # ---- mineBlock ----
    ("mineBlock 方块名不存在",      "await mineBlock(bot,'notarealblock',1)", 15000),
    ("mineBlock name 传数字",       "await mineBlock(bot,123,1)", 15000),
    ("mineBlock count 传字符串",     "await mineBlock(bot,'dirt','3')", 15000),
    ("mineBlock count=0",           "await mineBlock(bot,'dirt',0)", 15000),
    ("mineBlock count 负数",         "await mineBlock(bot,'dirt',-5)", 20000),
    ("mineBlock count 超大",         "await mineBlock(bot,'dirt',99999)", 25000),
    ("mineBlock name=null",         "await mineBlock(bot,null,1)", 15000),
    ("mineBlock 空手采石头(护栏)",    "await mineBlock(bot,'stone',1)", 15000),
    # ---- craftItem ----
    ("craftItem 物品不存在",         "await craftItem(bot,'notarealitem',1)", 15000),
    ("craftItem count=0",           "await craftItem(bot,'oak_planks',0)", 15000),
    ("craftItem count 负数",         "await craftItem(bot,'oak_planks',-3)", 15000),
    ("craftItem name 传对象",        "await craftItem(bot,{a:1},1)", 15000),
    ("craftItem 无材料",             "await craftItem(bot,'diamond_sword',1)", 15000),
    ("craftItem 不可合成物",          "await craftItem(bot,'bedrock',1)", 15000),
    # ---- smeltItem ----
    ("smeltItem 无熔炉",             "await smeltItem(bot,'raw_iron','coal',1)", 20000),
    ("smeltItem 燃料名错",            "await smeltItem(bot,'raw_iron','notafuel',1)", 20000),
    ("smeltItem 不可熔炼物",          "await smeltItem(bot,'dirt','coal',1)", 20000),
    ("smeltItem count=0",           "await smeltItem(bot,'raw_iron','coal',0)", 20000),
    # ---- placeItem ----
    ("placeItem 库存没有该物",        "await placeItem(bot,'crafting_table',bot.entity.position.offset(1,0,0))", 20000),
    ("placeItem position 传数组",     "await placeItem(bot,'dirt',[1,2,3])", 15000),
    ("placeItem position=null",     "await placeItem(bot,'dirt',null)", 15000),
    ("placeItem NaN 坐标",           "const {Vec3}=require('vec3'); await placeItem(bot,'dirt',new Vec3(NaN,NaN,NaN))", 20000),
    ("placeItem 物品名不存在",        "const {Vec3}=require('vec3'); await placeItem(bot,'notareal',new Vec3(0,64,0))", 15000),
    # 作用域遮蔽回归：LLM 若写 `const Vec3 = require('vec3')`（不解构）会拿到工厂函数而非类，
    #   可能遮蔽掉 programs 里原语看到的 Vec3 绑定，导致 placeItem 的 instanceof 校验恒 false。
    #   这两条故意用不解构的写法（且覆盖 mcData，供依赖它的原语用），
    #   判据是「不能报 must be a Vec3」：报缺料/悬空都算通过（说明已进到真实逻辑）。
    ("遮蔽Vec3后placeItem仍认坐标",
     "const Vec3=require('vec3');"
     "try{ await placeItem(bot,'crafting_table',new Vec3(0,64,0)); console.log('OK 放下了'); }"
     "catch(e){ if(/must be a Vec3/.test(e.message)) throw new Error('回归: Vec3 被遮蔽 → '+e.message);"
     " console.log('OK 进到真实逻辑: '+e.message.slice(0,50)); }", 20000),
    ("遮蔽mcData后mineBlock仍工作",
     "const mcData=require('minecraft-data'); const Vec3=require('vec3');"
     "try{ await mineBlock(bot,'dirt',1); console.log('OK 采到了'); }"
     "catch(e){ if(/is not defined|is not a function|undefined/.test(e.message))"
     " throw new Error('回归: mcData 被遮蔽 → '+e.message);"
     " console.log('OK 进到真实逻辑: '+e.message.slice(0,50)); }", 25000),
    # ---- givePlacedItemBack (0 校验点) ----
    ("givePlacedItemBack 传字符串而非数组", "await givePlacedItemBack(bot,'crafting_table',bot.entity.position)", 20000),
    ("givePlacedItemBack 空数组",     "await givePlacedItemBack(bot,[],[])", 15000),
    ("givePlacedItemBack 长度不匹配",  "await givePlacedItemBack(bot,['dirt','stone'],[bot.entity.position])", 20000),
    ("givePlacedItemBack null",      "await givePlacedItemBack(bot,null,null)", 15000),
    # ---- collectDroppedItems ----
    ("collectDroppedItems radius 传字符串", "await collectDroppedItems(bot,'8')", 15000),
    ("collectDroppedItems maxTicks 传字符串", "await collectDroppedItems(bot,8,'200')", 15000),
    ("collectDroppedItems radius 负数", "await collectDroppedItems(bot,-1)", 15000),
    ("collectDroppedItems 附近无掉落物", "await collectDroppedItems(bot,8)", 15000),
    # ---- killMob ----
    ("killMob 生物不存在",            "await killMob(bot,'notarealmob',5)", 20000),
    ("killMob mobName 传数字",        "await killMob(bot,42,5)", 15000),
    ("killMob timeout=0",           "await killMob(bot,'zombie',0)", 15000),
    # ---- shoot (0 校验点) ----
    ("shoot 武器不合法",              "await shoot(bot,'banana','zombie')", 15000),
    ("shoot 库存无武器",              "await shoot(bot,'bow','zombie')", 20000),
    ("shoot target 传对象",           "await shoot(bot,'bow',{a:1})", 15000),
    ("shoot weapon=null",           "await shoot(bot,null,'zombie')", 15000),
    # ---- exploreUntil ----
    ("exploreUntil direction 传数组", "await exploreUntil(bot,[1,0,0],3,()=>true)", 15000),
    ("exploreUntil direction 全0",   "const {Vec3}=require('vec3'); await exploreUntil(bot,new Vec3(0,0,0),3,()=>true)", 15000),
    ("exploreUntil 分量非-1/0/1",     "const {Vec3}=require('vec3'); await exploreUntil(bot,new Vec3(5,0,0),3,()=>true)", 15000),
    ("exploreUntil maxTime 传字符串", "const {Vec3}=require('vec3'); await exploreUntil(bot,new Vec3(1,0,0),'3',()=>true)", 15000),
    ("exploreUntil callback 非函数",  "const {Vec3}=require('vec3'); await exploreUntil(bot,new Vec3(1,0,0),3,'notafn')", 15000),
    ("exploreUntil callback 抛错",    "const {Vec3}=require('vec3'); await exploreUntil(bot,new Vec3(1,0,0),3,()=>{throw new Error('cb boom')})", 15000),
    # ---- waitUntil（新签名 waitUntil(bot, ticks, reason, conditions)）----
    ("waitUntil 条件无法识别",        "await waitUntil(bot,3,'test',['bogus_cond'])", 15000),
    ("waitUntil conditions 传字符串", "await waitUntil(bot,3,'test','dawn')", 15000),
    ("waitUntil conditions=null",   "await waitUntil(bot,3,'test',null)", 15000),
    ("waitUntil ticks 负数",          "await waitUntil(bot,-5,'test',['dawn'])", 15000),
    ("waitUntil ticks 传字符串",      "await waitUntil(bot,'3','test',['dawn'])", 15000),
    # 验证两点：① 返回值不能是 undefined（否则 Agent 拿不到任何等待结果的信号）
    # ② already_satisfied 分支必须明说「没等、时间没推进」。
    # 白天跑这两条即命中该分支；夜里跑则走 timeout，reason 断言仍成立。
    ("waitUntil 返回值不是 undefined",
     "const r=await waitUntil(bot,3,'test',['dawn']);"
     "if(r===undefined) throw new Error('返回 undefined：Agent 无法判断等到了没有');"
     "if(!r.reason) throw new Error('返回值缺 reason 字段');"
     "console.log('reason='+r.reason+' waited='+r.waited)", 15000),
    ("waitUntil 白天等dawn要说没等",
     "const r=await waitUntil(bot,3,'test',['dawn']);"
     "if(r.reason==='already_satisfied' && r.waited!==0)"
     "  throw new Error('already_satisfied 却报等了 '+r.waited+'s')", 15000),
    # ---- useChest ----
    ("chest 位置无箱子",              "const {Vec3}=require('vec3'); await checkItemInsideChest(bot,new Vec3(0,64,0))", 20000),
    ("chest position 传数组",         "await checkItemInsideChest(bot,[1,2,3])", 15000),
    ("chest NaN 坐标",               "const {Vec3}=require('vec3'); await checkItemInsideChest(bot,new Vec3(NaN,NaN,NaN))", 20000),
    ("getItemFromChest itemsToGet 传数组", "const {Vec3}=require('vec3'); await getItemFromChest(bot,new Vec3(0,64,0),['dirt'])", 20000),
    # 回归：getItemFromChest 非 Vec3 必须**抛错**（旧实现是 chat+return 静默放过，Agent 误以为取物成功）。
    #   抛错 → onError 有反馈；若哪天改回 return，这条会在 audit 里报"静默无反馈"。
    ("getItemFromChest 非Vec3必须抛错", "try { await getItemFromChest(bot,[1,2,3],{dirt:5}); console.log('BAD: 没抛错'); } catch(e){ console.log('OK 抛错: '+e.message.slice(0,60)); }", 15000),
    ("depositItemIntoChest 非Vec3必须抛错", "try { await depositItemIntoChest(bot,[1,2,3],{dirt:5}); console.log('BAD: 没抛错'); } catch(e){ console.log('OK 抛错: '+e.message.slice(0,60)); }", 15000),
    # ---- pathfinder 直接调用（LLM 可见）----
    ("goto NaN 坐标(护栏)",           "const {goals}=require('mineflayer-pathfinder'); await bot.pathfinder.goto(new goals.GoalNear(NaN,NaN,NaN,1))", 20000),
    ("goto 少传 range(rangeSq NaN)",  "const {goals}=require('mineflayer-pathfinder'); await bot.pathfinder.goto(new goals.GoalNear(0,64,0))", 20000),
    ("setGoal 传裸 Vec3",             "const {Vec3}=require('vec3'); bot.pathfinder.setGoal(new Vec3(1,2,3))", 15000),
    ("GoalLookAtBlock NaN",          "const {goals}=require('mineflayer-pathfinder'); const {Vec3}=require('vec3'); await bot.pathfinder.goto(new goals.GoalLookAtBlock(new Vec3(NaN,NaN,NaN), bot.world))", 20000),
    # ---- process / 逃逸 ----
    ("process.exit 拦截",             "process.exit(1)", 15000),
    ("process.kill 拦截",             "process.kill(process.pid)", 15000),
    ("裸抛错",                        "throw new Error('plain throw')", 15000),
    ("语法就绪但引用未定义函数",        "await noSuchFunction(bot)", 15000),
]


def main():
    print(f"{'用例':<40} {'耗时':>8}  {'进程':<6} {'反馈'}")
    print("-" * 130)
    dead, silent, slow = [], [], []
    p0 = pid()
    for name, code, tmo in CASES:
        r = step(code, tmo)
        p1 = pid()
        alive = (p1 == p0 and p1 is not None)
        if not alive:
            dead.append(name)
            p0 = p1  # resync after self-heal
        fb = (r["err"] or r["chat"] or r["log"] or "").replace("\n", " ")
        if not fb.strip():
            silent.append(name)
            fb = "⚠️ 无任何反馈（静默）"
        if r["ms"] > tmo + 20000:
            slow.append((name, r["ms"]))
        print(f"{name:<40} {r['ms']:>7.0f}ms  {'OK' if alive else '☠死':<6} {fb[:70]}")
    print("\n" + "=" * 60)
    print(f"进程死亡: {len(dead)}  {dead}")
    print(f"静默无反馈: {len(silent)}  {silent}")
    print(f"疑似挂住: {len(slow)}  {slow}")


if __name__ == "__main__":
    main()
