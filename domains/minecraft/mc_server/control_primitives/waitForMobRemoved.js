function waitForMobRemoved(bot, entity, timeoutMs = 30000) {
    return new Promise((resolve, reject) => {
        let success = false;
        let droppedItem = null;
        // Set up timeout（timeoutMs 已是墙钟毫秒，由 killMob 从游戏刻换算而来）
        const timeoutId = setTimeout(() => {
            success = false;
            bot.pvp.stop();
        }, timeoutMs);

        // Function to handle entityRemoved event
        function onEntityGone(e) {
            if (e === entity) {
                success = true;
                clearTimeout(timeoutId);
                bot.chat(`Killed ${entity.name}!`);
                bot.pvp.stop();
            }
        }

        function onItemDrop(item) {
            if (entity.position.distanceTo(item.position) <= 1) {
                droppedItem = item;
            }
        }

        function onStoppedAttacking() {
            clearTimeout(timeoutId);
            bot.removeListener("entityGone", onEntityGone);
            bot.removeListener("stoppedAttacking", onStoppedAttacking);
            bot.removeListener("itemDrop", onItemDrop);
            if (!success) reject(new Error(`Failed to kill ${entity.name}.`));
            else resolve(droppedItem);
        }

        // Listen for entityRemoved event
        bot.on("entityGone", onEntityGone);
        bot.on("stoppedAttacking", onStoppedAttacking);
        bot.on("itemDrop", onItemDrop);
    });
}


function waitForMobShot(bot, entity, timeoutMs = 30000) {
    return new Promise((resolve, reject) => {
        let success = false;
        let droppedItem = null;
        // Set up timeout（timeoutMs 已是墙钟毫秒，由 killMob 从游戏刻换算而来）
        const timeoutId = setTimeout(() => {
            success = false;
            bot.hawkEye.stop();
        }, timeoutMs);

        // Function to handle entityRemoved event
        function onEntityGone(e) {
            if (e === entity) {
                success = true;
                clearTimeout(timeoutId);
                bot.chat(`Shot ${entity.name}!`);
                bot.hawkEye.stop();
            }
        }

        function onItemDrop(item) {
            if (entity.position.distanceTo(item.position) <= 1) {
                droppedItem = item;
            }
        }

        function onAutoShotStopped() {
            clearTimeout(timeoutId);
            bot.removeListener("entityGone", onEntityGone);
            bot.removeListener("auto_shot_stopped", onAutoShotStopped);
            bot.removeListener("itemDrop", onItemDrop);
            if (!success) reject(new Error(`Failed to shoot ${entity.name}.`));
            else resolve(droppedItem);
        }

        // Listen for entityRemoved event
        bot.on("entityGone", onEntityGone);
        bot.on("auto_shot_stopped", onAutoShotStopped);
        bot.on("itemDrop", onItemDrop);
    });
}
