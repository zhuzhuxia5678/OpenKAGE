#!/usr/bin/env bash
# 起 Minecraft 世界 + Voyager mineflayer Express 服务器，**供手工调试单步用**。
# 用法：
#   bash start_servers.sh            # 起 MC server(后台) + mineflayer server(前台)
#   bash start_servers.sh --mc-only  # 只起 MC server
#   MINEFLAYER_PORT=3001 bash start_servers.sh   # 换 mineflayer 端口
set -euo pipefail

MC_DIR="${MC_DIR:-/path/to/mc_probe/server121}"
MC_PORT="${MC_PORT:-25567}"
# MC 1.21 需 Java 21（系统默认 java 是 8，会起不来）
JAVA_BIN="${JAVA_BIN:-/usr/lib/jvm/java-21-openjdk/bin/java}"
MINEFLAYER_PORT="${MINEFLAYER_PORT:-3000}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { echo "[start_servers] $*"; }

# 起服前强制 allow-flight=true（幂等，只改这一项，其余原样保留）。
# dave flying 踢人事故（2026-08-20）：pauseBot() 反思期关 bot.physicsEnabled 让 bot 悬空，
# allow-flight=false（vanilla 默认）会把这判成飞行外挂踢人 → 重连不恢复 chunk → 半连接空转退场。
# bot 是受控的 mineflayer 实体不是人类玩家，开 true 让悬空副作用合法。worker 目录由
# worker_pool.provision_workers 同款强制；这里管手工起的那台模板 server。allow-flight 是
# server 启动时读一次的配置，必须改在起服之前。
_ensure_allow_flight() {
  local props="$1/server.properties"
  [ -f "$props" ] || return 0
  if grep -qE '^allow-flight=true' "$props"; then
    return 0
  fi
  if grep -qE '^allow-flight=' "$props"; then
    sed -i -E 's/^allow-flight=.*/allow-flight=true/' "$props"
  else
    printf 'allow-flight=true\n' >> "$props"
  fi
  log "已把 $props 的 allow-flight 改为 true（防 bot 反思期悬空被 flying 踢）"
}

# 1) MC server：若 MC_PORT 未监听就后台起
if ! (exec 3<>"/dev/tcp/127.0.0.1/${MC_PORT}") 2>/dev/null; then
  _ensure_allow_flight "$MC_DIR"
  log "起 MC server @ ${MC_DIR} (port ${MC_PORT}) ..."
  # ★ 6G + GC 日志（2026-09-06）：与 env.py 托管路径（_MC_JVM_FLAGS）保持一致。手工起服只调堆
  #   + GC 日志，不加 OOM 诊断参数（手工调试场景，崩溃可直接看终端）。GC 日志为确诊 server
  #   主线程 36.9s 卡顿是否 STW full GC（见 env.py _MC_JVM_FLAGS 注释）。
  ( cd "$MC_DIR" && nohup "$JAVA_BIN" -Xmx6G \
      -Xlog:gc*:file=gc.log:time,uptime,level,tags:filecount=5,filesize=10M \
      -jar fabric-server-launch.jar nogui \
      > "$MC_DIR/openkage_server.log" 2>&1 & )
  log "等待 MC server 就绪（最多 90s）..."
  for i in $(seq 1 90); do
    if (exec 3<>"/dev/tcp/127.0.0.1/${MC_PORT}") 2>/dev/null; then
      log "MC server 已就绪（${i}s）"; break
    fi
    sleep 1
  done
else
  log "MC server 已在 :${MC_PORT} 运行，跳过"
fi

if [[ "${1:-}" == "--mc-only" ]]; then
  log "仅起 MC server，完成"; exit 0
fi

# 2) mineflayer Express 服务器（前台，Ctrl-C 停）
log "起 mineflayer 服务器（本目录 index.js）@ :${MINEFLAYER_PORT} ..."
cd "$HERE"
exec node index.js "${MINEFLAYER_PORT}"
