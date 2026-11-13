const Observation = require("./base.js").Observation;

class Status extends Observation {
    constructor(bot) {
        super(bot);
        this.name = "status";
    }

    observe() {
        return {
            health: this.bot.health,
            food: this.bot.food,
            saturation: this.bot.foodSaturation,
            oxygen: this.bot.oxygenLevel,
            position: this.bot.entity.position,
            velocity: this.bot.entity.velocity,
            yaw: this.bot.entity.yaw,
            pitch: this.bot.entity.pitch,
            onGround: this.bot.entity.onGround,
            equipment: this.getEquipment(),
            name: this.bot.entity.username,
            timeSinceOnGround: this.bot.entity.timeSinceOnGround,
            isInWater: this.bot.entity.isInWater,
            isInLava: this.bot.entity.isInLava,
            isInWeb: this.bot.entity.isInWeb,
            isCollidedHorizontally: this.bot.entity.isCollidedHorizontally,
            isCollidedVertically: this.bot.entity.isCollidedVertically,
            biome: this.bot.blockAt(this.bot.entity.position)
                ? this.bot.blockAt(this.bot.entity.position).biome.name
                : "None",
            entities: this.getEntities(),
            timeOfDay: this.getTime(),
            // 原始游戏刻（0-23999）。**与 timeOfDay 标签并存、不替换它** —— NIGHT_PHASES 的
            // 夜晚判定、昼夜跳变触发反思全靠那个标签，动它风险太大（见 env.py NIGHT_PHASES）。
            // 加它的理由：标签只说"现在是 night"，说不出**还有多久天亮**。而 LLM 自己反思出的
            // 关键策略正是「日落(12000)前必须建好庇护所」—— 那条信息环境有、却没给它。
            timeTicks: this.bot.time.timeOfDay,
            // 服务器权威累计天数（mineflayer time.js：day = floor(time/24000)，time 来自
            // update_time 包的累计刻，单调递增）。与 timeTicks 同源但语义不同：timeTicks 是
            // 一天内 0-23999 的时刻，worldDay 是过了几个完整昼夜。
            //
            // **为什么加它**：sleep 跳夜会把「夜」从步末 phase 采样里抹掉（erin 第10天事故）——
            // sleep 瞬间把 timeOfDay 从 ~14000 跳到 ~10，步末永远采到白天，Python 侧
            // `was_night and not now_night` 判据永不触发 → 不进反思 → 续跑死循环 retire。
            // sleep 跳夜时服务器把累计 time 推进到下一个 24000 倍数（实测：14005→24010，
            // day 0→1），worldDay 必然 +1，不依赖夜的 phase 是否出现在采样点。Python 侧改用
            // worldDay 相对增量判穿过午夜即可补这个盲区（见 env.py last_day）。
            worldDay: this.bot.time.day,
            inventoryUsed: this.bot.inventoryUsed(),
            elapsedTime: this.bot.globalTickCounter,
            // 经验值(XP)。mineflayer experience 插件默认加载，`bot.experience` 暴露
            // points(总XP点数)/level/progress。points 在死亡后会被 MC 服务器压低(只留少量)，
            // 故 Python 侧用「只累加正增量」实现跨命累计(见 env.py _xp_total)。
            // ★ score 已改为累计 XP(2026-08-25)，这三项是它的数据源，缺失则 score 恒 0。
            experience: {
                points: this.bot.experience.points,
                level: this.bot.experience.level,
                progress: this.bot.experience.progress,
            },
            ...this.getLight(),
        };
    }

    // bot 所在格的光照：`{light, skyLight}`（0-15），读不到时两者为 null。
    //
    // **为什么补这个**：本域主要死因是夜间被围攻，而 1.18 起敌对生物的刷新条件
    // 就是这两个数 —— `light === 0 && skyLight <= 7`（1.18 前是 `light <= 7`，那次改动只收紧了
    // 方块光、并没有移除天光项）。也就是说**「这里会不会刷怪」在环境里是个可精确读出的事实**，
    // 而观察若不给这两个数，bot 只能靠"天黑了"这种阶段标签猜。它挖完地下掩体、封好口，
    // 从观察看信息是完备的 —— 却不知道自己正坐在 light=0 的刷怪点上，于是"躲进掩体还是被打"
    // 在它看来毫无因果，只能编一个。这与 timeTicks 是同一类「环境知道但不说」的漏洞。
    //
    // 两个数**必须都给、不能合并成一个"暗不暗"**：语义不同且都不可省 ——
    // · `light`（方块光）来自火把/熔炉等光源，是 bot **能主动改变**的那一项（插火把即可压掉刷怪）；
    // · `skyLight`（天光）是「这一格有多见天」的暴露度，**不随昼夜衰减** —— 午夜地表空地
    //   仍是 15。所以它不能用来判断"现在亮不亮"，只能判断"室内/地下 or 露天"。
    //   把它当亮度用会得出"地表午夜很亮"的错误结论。
    //
    // ★ 只报事实、不下判断（与 env.py 各 render_* 一致）：不标「⚠会刷怪」之类。刷怪还受难度 /
    //   生物群系 / 方块类型 / 上方空间等影响，凭这两个数下断言是拿我没有的信息说话；
    //   规则本身写进 system.md，让模型自己套。
    getLight() {
        const b = this.bot.blockAt(this.bot.entity.position);
        if (!b) return { light: null, skyLight: null };
        const num = (v) => (typeof v === "number" ? v : null);
        return { light: num(b.light), skyLight: num(b.skyLight) };
    }

    // 装备槽的物品：`{name, durability?: [剩余, 上限]}`，空槽为 null。
    //
    // **name 与 durability 分开两个字段、不拼成一个字符串**：name 要被 Python 侧当物品名用
    // （并入 tech_tier / unique_items 的物品集，见 env.tech_tier），拼进字符串就得再正则拆回来。
    // 耐久本身是必要的：工具快断时该提前补一把，不给这个数 LLM 只能等它突然消失。
    // 石头/木板之类无耐久的物品没有 durability 字段（`maxDurability` 为 0/undefined）。
    itemToEquipObs(item) {
        if (!item) return null;
        if (!item.maxDurability) return { name: item.name };
        return {
            name: item.name,
            durability: [
                item.maxDurability - (item.durabilityUsed || 0),
                item.maxDurability,
            ],
        };
    }

    getTime() {
        // 官方四阶段（time.md）：day[0,12000) sunset[12000,13800) night[13800,22200) sunrise[22200,24000)。
        // 保留 6 段细分（day→day+noon、night→night+midnight），边界严格对齐官方：
        //   0-1000 不再误标 sunrise —— 24000=0 是 day 起点（一天开始），sunrise 段是 22200-23999。
        const timeOfDay = this.bot.time.timeOfDay;
        let time = "";
        if (timeOfDay < 6000) {
            time = "day";            // 06:00-12:00（含 0 刻一天开始）
        } else if (timeOfDay < 12000) {
            time = "noon";           // 12:00-18:00
        } else if (timeOfDay < 13800) {
            time = "sunset";         // 18:00-19:48 官方日落段
        } else if (timeOfDay < 18000) {
            time = "night";          // 19:48-00:00 官方夜晚段前半
        } else if (timeOfDay < 22200) {
            time = "midnight";       // 00:00-04:12 官方夜晚段后半
        } else {
            time = "sunrise";        // 04:12-06:00 官方日出段
        }
        return time;
    }

    // 穿在身上/握在手上的东西：护甲四件 + 主手 + 副手。空槽为 null。
    //
    // ⚠️ **这六格不在 `inventory` 观察里**：inventory.js 走 `bot.inventory.items()`，而
    // prismarine-windows 给 `minecraft:inventory` 定的槽位区间是 **9-44**（见
    // node_modules/prismarine-windows/index.js）—— 护甲槽 5-8 与副手槽 45 都在区间外。
    // 所以 bot 一穿上护甲，那件护甲就从 `库存` 里彻底消失，不靠本字段它无从知道自己穿着什么
    // （风险：以为护甲丢了、重复再造一件）。
    //
    // 形态为**带槽位名的对象**：键名与 mineflayer `bot.equip` 的
    // destination 参数**故意取同一套**（head/torso/legs/feet + hand/off-hand，见
    // node_modules/mineflayer/lib/plugins/simple_inventory.js 的 armorSlots），
    // 于是观察里的键可以直接照抄进 equip 调用，不需要第二张对照表。
    getEquipment() {
        const slots = this.bot.inventory.slots;
        const toObs = (it) => this.itemToEquipObs(it);
        return {
            head: toObs(slots[5]),
            torso: toObs(slots[6]),
            legs: toObs(slots[7]),
            feet: toObs(slots[8]),
            hand: toObs(this.bot.heldItem),
            "off-hand": toObs(slots[45]),
        };
    }

    // **报数量与方位，不再只报「每种最近一只」**。
    //
    // 若把同名生物压成一个「最近距离」：3 只僵尸在 5/8/10 格 → LLM 只看到 `zombie: 5`。
    // 但**夜间被围攻是本域主要死因**，而「被几只围住」这个决定性事实恰恰被这个数据结构抹平了；
    // 也没有方位 —— 只知道 5 格外有僵尸，不知道该往哪躲。这属于本域反复出现的病根
    // 「观察缺失 → LLM 编因果/瞎决策」。
    //
    // 输出 {name: {n, min, dirs}}：n=32 格内总只数，min=最近距离，dirs=最近 3 只的水平方位。
    // ★ 方位用**绝对轴向**（8 向，+x/-x/+z/-z 及对角），与 nearbyGrid / vision 同一坐标系 ——
    //   那两处已明确用绝对方位，混入「左/右」会让 LLM 换算出错（见 env.py render_nearby_grid 注释）。
    getEntities() {
        const entities = this.bot.entities;
        if (!entities) return {};
        const self = this.bot.entity.position;
        const acc = {};
        for (const id in entities) {
            const entity = entities[id];
            if (!entity.displayName) continue;
            if (entity.name === "player" || entity.name === "item") continue;
            const d = entity.position.distanceTo(self);
            if (d >= 32) continue;
            if (!acc[entity.name]) acc[entity.name] = [];
            acc[entity.name].push({
                d,
                dir: horizontalDir(
                    entity.position.x - self.x,
                    entity.position.z - self.z
                ),
            });
        }
        const mobs = {};
        for (const name in acc) {
            const list = acc[name].sort((a, b) => a.d - b.d);
            mobs[name] = {
                n: list.length,
                min: list[0].d,
                // 只列最近 3 只：再多对决策没有增量，而围攻时同名生物可能十几只
                dirs: list.slice(0, 3).map((e) => e.dir),
            };
        }
        return mobs;
    }
}

/**
 * 水平方位 → 8 向绝对轴向标签。
 * ★ 标签**必须与 env.py 的 `_GRID_DIRS` 完全一致**（"-x" "+x" "-z" "+z" "-x-z" "-x+z" "+x-z" "+x+z"）：
 *   nearbyGrid 与 vision 都用这套词，生物方位若另造一套（东南西北/左右）会逼 LLM 做两次换算，
 *   而坐标系换算出错正是这个域踩过的坑。
 * 判定：某轴分量的绝对值不足另一轴的 ~41%（tan 22.5°）时视为「基本不偏」，只报主轴；否则报对角。
 */
function horizontalDir(dx, dz) {
    const ax = Math.abs(dx);
    const az = Math.abs(dz);
    const sx = dx >= 0 ? "+x" : "-x";
    const sz = dz >= 0 ? "+z" : "-z";
    const TAN22 = 0.4142;
    if (az < ax * TAN22) return sx; // 几乎纯 x 向
    if (ax < az * TAN22) return sz; // 几乎纯 z 向
    return sx + sz; // 对角，拼成 "+x-z" 这种形态
}

module.exports = Status;
