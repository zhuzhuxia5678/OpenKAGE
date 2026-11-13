// Blocks = require("./blocks")
const { Observation } = require("./base");

class Voxels extends Observation {
    constructor(bot) {
        super(bot);
        this.name = "voxels";
    }

    observe() {
        return Array.from(getSurroundingBlocks(this.bot, 8, 2, 8));
    }
}

// 以 bot 为中心的 3×3×3 邻域（25 格 = 27 - bot 自身占的 2 格）——**带方位**的近身空间认知。
// voxels（下面那个类）把 17×5×17 的方块塞进 Set 去重、只留名字，方位信息全丢，bot 因此
// 不知道「哪边能走、脚下是什么、头顶有没有封顶」；而 `block.type !== 0` 还把**空气过滤掉了**，
// 偏偏空气才是「能不能走/能不能站」的答案。这里反过来：**保留空气、保留方位**，
// 只取贴身一圈（远处态势仍由 voxels 那份去重集合负责，两者互补）。
//
// 方位用**绝对轴向**（-x/+x/-z/+z）而非「前后左右」：bot 的行动原语（exploreUntil）只收绝对
// 方向 Vec3(±1,0,0)，观察若按朝向给相对方位，LLM 每步都要自己做一次坐标换算，易错。
//
// y 分三层：head=y+1（头部所在，撞头/能否站起）、feet=y+0（脚所在，能否水平走过去）、

class NearbyGrid extends Observation {
    constructor(bot) {
        super(bot);
        this.name = "nearbyGrid";
    }

    observe() {
        const pos = this.bot.entity.position.floored();
        const layers = { head: 1, feet: 0, below: -1 };
        const out = {};
        for (const [layerName, dy] of Object.entries(layers)) {
            const cells = {};
            for (let dx = -1; dx <= 1; dx++) {
                for (let dz = -1; dz <= 1; dz++) {
                    // 中心格：feet/head 是 bot 自身（跳过 2 格 —— bot 高约1.8格占两层，故总数 27-2=25）；below 的中心格是
                    // **脚下支撑方块**，最关键的一格，必须保留。
                    if (dx === 0 && dz === 0 && layerName !== "below") continue;
                    const b = this.bot.blockAt(pos.offset(dx, dy, dz));
                    // 读不到（区块未加载/超出视距）记 unknown，与「确实是空气」区分开 ——
                    // 把未知当空气会让 bot 以为能走过去，那是会摔死人的误报。
                    cells[cellKey(dx, dz)] = b ? b.name : "unknown";
                }
            }
            out[layerName] = cells;
        }
        return out;
    }
}

// 方位键：中心用 "center"，其余用绝对轴向组合（如 "-x", "+x+z"）。
// 顺序固定 x 在前 z 在后，Python 侧渲染时按固定顺序取，保证每步观察文本布局稳定。
function cellKey(dx, dz) {
    if (dx === 0 && dz === 0) return "center";
    const xs = dx === 0 ? "" : (dx < 0 ? "-x" : "+x");
    const zs = dz === 0 ? "" : (dz < 0 ? "-z" : "+z");
    return xs + zs;
}

// ───────────────────── 视觉层 ─────────────────────
//
// 「bot 朝向的方向看到了什么」。与 nearbyGrid（贴身 25 格，全知）互补：那个回答「脚边能不能
// 走」，这个回答「**远处那边有什么、值不值得走过去**」。原有观察缺这一环 —— bot 只能靠
// exploreUntil 盲目走一段再看。
//
// **真·遮挡**：用 mineflayer 的 bot.world.raycast 从眼睛位置投射射线，命中第一个实心方块即停
// —— 被山挡住的树看不到，符合"视觉"语义（而非 nearbyGrid 那种穿墙全知）。
// 533 条射线的耗时相比每步几十秒的墙钟可忽略，故成本不是约束，**文本长度才是**。
//
// 张角/距离/采样密度：水平 ±60°、垂直 ±30°、48 格。
// 采样步长 3°(水平)/5°(垂直) → 41×13=533 条射线，够密到不漏树、不至于慢。
const VIEW_H_DEG = 60, VIEW_V_DEG = 30, VIEW_RANGE = 48;
const VIEW_H_STEP = 3, VIEW_V_STEP = 5;

class Vision extends Observation {
    constructor(bot) {
        super(bot);
        this.name = "vision";
    }

    observe() {
        const bot = this.bot;
        const Vec3 = require("vec3").Vec3;
        const eye = bot.entity.position.offset(0, bot.entity.eyeHeight, 0);
        const yaw0 = bot.entity.yaw;
        // 9 个扇区：水平(左/中/右) × 垂直(上/平/下)。每区聚合命中的方块，避免逐条列射线。
        const sectors = {};
        for (let vDeg = -VIEW_V_DEG; vDeg <= VIEW_V_DEG; vDeg += VIEW_V_STEP) {
            for (let hDeg = -VIEW_H_DEG; hDeg <= VIEW_H_DEG; hDeg += VIEW_H_STEP) {
                // MC 的 yaw：0=-z(南)，逆时针增。hDeg>0 取右侧。
                const yaw = yaw0 - (hDeg * Math.PI) / 180;
                const pitch = (vDeg * Math.PI) / 180;
                const dir = new Vec3(
                    -Math.sin(yaw) * Math.cos(pitch),
                    Math.sin(pitch),
                    -Math.cos(yaw) * Math.cos(pitch),
                );
                // ★ 扇区按**射线的绝对世界方向**归类，不用「左/中/右」：
                // 网格用绝对轴向、视野用相对朝向时两套观察对不上，模型得自己拿 yaw 做换算，
                // 而 yaw 常是斜的（bot 从不主动 look，朝向是寻路/挖掘的副作用），换算必错 ——
                // 那正是"视野里偏左那棵树"与推算坐标对不上、走过去歪掉的原因。
                // 现在直接报 `-x平` / `+z上` 这种键，与 nearbyGrid、exploreUntil 的 Vec3 同一坐标系。
                const key = sectorKey(yaw, vDeg);
                if (!sectors[key]) sectors[key] = { blocks: {}, sky: 0, total: 0 };
                const s = sectors[key];
                s.total++;
                const hit = bot.world.raycast(eye, dir.normalize(), VIEW_RANGE);
                if (!hit) {
                    // 没命中 = 这条射线一路到 48 格都没撞到东西 = 开阔/天空
                    s.sky++;
                    continue;
                }
                const dist = eye.distanceTo(hit.position);
                const rec = s.blocks[hit.name] || (s.blocks[hit.name] = {
                    n: 0, min: Infinity, max: 0, pos: null,
                });
                rec.n++;
                if (dist < rec.min) {
                    rec.min = dist;
                    // 记最近那次命中的坐标：LLM 要走过去/挖它时直接可用
                    rec.pos = { x: hit.position.x, y: hit.position.y, z: hit.position.z };
                }
                if (dist > rec.max) rec.max = dist;
            }
        }
        // 数值取整，压缩 JSON 体积（0.1 格的精度对决策无意义）
        for (const s of Object.values(sectors)) {
            for (const r of Object.values(s.blocks)) {
                r.min = Math.round(r.min * 10) / 10;
                r.max = Math.round(r.max * 10) / 10;
            }
        }
        return { yaw: yaw0, ...facingInfo(yaw0), range: VIEW_RANGE, sectors };
    }
}

// 扇区键：`<绝对轴向><垂直层>`，如 "-x平" / "+z上" / "-z下"。
//
// 水平方向取**这条射线朝的绝对轴向**（离哪个轴最近就归给它），而非相对 bot 的左/中/右。
// 于是同一个键在任何朝向下都指同一个世界方向，可直接对应 exploreUntil 的 Vec3 与 nearbyGrid 的方位。
// 注：视野只有 ±60°，所以一次最多覆盖 2~3 个轴向（背后的轴向不会出现在结果里 —— 那是盲区，
// 符合"视觉"语义）。
function sectorKey(yaw, vDeg) {
    // 由射线 yaw 反推它最接近哪个绝对轴向（与 facingInfo 同一套映射）
    const deg = (((yaw * 180) / Math.PI) % 360 + 360) % 360;
    const axes = [{ n: "-z", at: 0 }, { n: "+x", at: 90 }, { n: "+z", at: 180 }, { n: "-x", at: 270 }];
    let h = axes[0].n, best = 999;
    for (const a of axes) {
        const d = Math.min(Math.abs(deg - a.at), 360 - Math.abs(deg - a.at));
        if (d < best) { best = d; h = a.n; }
    }
    const v = vDeg < -VIEW_V_DEG / 3 ? "下" : (vDeg > VIEW_V_DEG / 3 ? "上" : "平");
    return h + v;
}

// bot 朝向 → 最接近的绝对轴向名 + **偏离该轴向多少度**。
//
// ⚠️ 只报轴向名是错的：yaw 是连续的，四舍五入到最近的正方向**误差最大 44°**。而 bot 从不
// 主动 bot.look —— 它的 yaw 完全是 pathfinder 寻路 / bot.dig 挖掘的副作用，停下来时基本都是斜的。
// 后果：观察说"朝向 -x"，模型据此用 new Vec3(-1,0,0) 行动，但实际朝向偏了几十度 ——
// 视野里"偏左那棵树"与它推算的坐标对不上，走过去就是歪的。
// 现在**把真实角度一并报出**，让模型自己知道"我其实歪着"，需要对齐时可以 bot.look。
function facingInfo(yaw) {
    // MC 的 yaw：0 = -z（南），逆时针增（+x 在 90°）。归一到 [0,360)。
    const deg = (((yaw * 180) / Math.PI) % 360 + 360) % 360;
    const axes = [
        { name: "-z", at: 0 }, { name: "+x", at: 90 },
        { name: "+z", at: 180 }, { name: "-x", at: 270 },
    ];
    let best = axes[0], bestDiff = 999;
    for (const a of axes) {
        // 环形最小夹角
        const d = Math.min(Math.abs(deg - a.at), 360 - Math.abs(deg - a.at));
        if (d < bestDiff) { bestDiff = d; best = a; }
    }
    return { axis: best.name, yawDeg: Math.round(deg), offAxisDeg: Math.round(bestDiff) };
}


function getSurroundingBlocks(bot, x_distance, y_distance, z_distance) {
    const surroundingBlocks = new Set();

    for (let x = -x_distance; x <= x_distance; x++) {
        for (let y = -y_distance; y <= y_distance; y++) {
            for (let z = -z_distance; z <= z_distance; z++) {
                const block = bot.blockAt(bot.entity.position.offset(x, y, z));
                if (block && block.type !== 0) {
                    surroundingBlocks.add(block.name);
                }
            }
        }
    }
    // console.log(surroundingBlocks);
    return surroundingBlocks;
}

function getInventoryItems(bot) {
    const items = new Set();
    bot.inventory.items().forEach((item) => {
        if (item) items.add(item.name);
    });
    return items;
}

module.exports = { Voxels, NearbyGrid, Vision };
