const { Observation } = require("./base");

class Inventory extends Observation {
    constructor(bot) {
        super(bot);
        this.name = "inventory";
    }

    observe() {
        return listItems(this.bot);
    }
}

function listItems(bot) {
    const items = getInventoryItems(bot);
    return items.reduce(itemToDict, {});
}

function getInventoryItems(bot) {
    // ★ **只读 `bot.inventory`，绝不用 `bot.currentWindow`**（2026-08-14 修）。
    //
    // 原先是 `bot.currentWindow || bot.inventory`（Voyager 的写法）。打开箱子/工作台/熔炉时
    // `currentWindow` 是**那个容器的窗口**，`items()` 返回的是**容器格子里的东西**——
    // 于是「库存」这一栏会把箱子里的物品说成是 bot 自己的。
    //
    // 更坏的是它与**别的字段和全部原语**都不同源，产生自相矛盾的观察：
    //   · `inventoryUsed`（见 lib/skillLoader.js）永远数 `bot.inventory.slots[9..45]`；
    //   · craftItem/placeItem/mineBlock… 全部读 `bot.inventory`。
    // 实测 2026-08-13 那轮 alice day01 出现过 `库存(0/36)：stickx5, cobblestonex6,
    // wooden_pickaxex1, …` —— 槽位数 0 却列出 7 种物品（物理上不可能）。Agent 自己写了
    // 一步去查 `bot.inventory.items()`，拿到「总计 0 种物品」，随后连烧几步试图
    // 「刷新库存同步」，最后在 updateplan 里判定「系统库存同步失效」并把整条科技路线
    // 改成保守求生。全轮 22 条观察处于这种不可能状态（alice d01/d02、carol d14-18、bob d30）。
    //
    // 判据：**观察必须与原语执行时看到的世界一致**，否则 Agent 按观察做的决策在原语层
    // 必然失败，而它无从得知原因。箱子内容有专门的通道（观察里的「附近箱子」+ useChest），
    // 不该借「库存」这一栏顶替。
    return bot.inventory.items();
}

function itemToDict(acc, cur) {
    if (cur.name && cur.count) {
        //if both name and count property are defined
        if (acc[cur.name]) {
            //if the item is already in the dict
            acc[cur.name] += cur.count;
        } else {
            //if the item is not in the dict
            acc[cur.name] = cur.count;
        }
    }
    return acc;
}

//export modules
module.exports = Inventory;
