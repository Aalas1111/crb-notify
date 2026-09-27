#!/usr/bin/env bash
# 把生产机更新到上游 —— 唯一被允许的更新方式。
#
# 按顺序：取 flock → 确认工作区干净 → 快进 → 在**临时 HOME** 里跑测试 →
# 对齐单元 → 重启 → 验收 → 记 ops.log。任何一步失败就停，不改任何东西。
#
# 用法（注意 `./`：sudo 的 PATH 里通常没有当前目录）：
#   sudo ./scripts/deploy.sh
#
# 下面几条「坑」都是实测换来的，**别删**（详见每处的注释）。

set -euo pipefail

REPO="${REPO:-/opt/crb-notify}"
STATE="${STATE:-/var/lib/crb-notify}"
LOG="$STATE/ops.log"
LOCK="$STATE/.deploy.lock"
WHO="${WHO:-$(whoami)@$(hostname -s)}"
UNIT=crb-notify.service
PORT="${CRBN_PORT:-8788}"

say() { printf '\n== %s\n' "$*"; }
die() { printf '✗ %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "需要 root（要 install 单元、restart systemd）"

say "取部署锁（$LOCK）"
mkdir -p "$STATE"
# 这个脚本以 root 跑，但服务以 yuque 起、要在 $STATE 里建 workspace/ ——
# 不 chown 的话服务一启动就 PermissionError。实测踩到。
chown yuque:yuque "$STATE"
mkdir -p "$STATE/.uv-cache"
chown yuque:yuque "$STATE/.uv-cache"
exec 9>"$LOCK"
flock -n 9 || die "另一个部署正在进行（$LOCK 被占用）。"

cd "$REPO"
[ -f deploy/crb-notify.service ] || die "$REPO 看起来不是这个项目的检出"

# ⚠️ 所有 git 操作**以仓库所有者（yuque）的身份**跑。脚本本身是 root 执行的，
# 但检出归 yuque、部署密钥也在 /home/yuque/.ssh —— root 直接跑 git 会先撞
# dubious ownership、再撞 Host key verification failed，于是 fetch 失败、
# 脚本退回「按当前 HEAD 继续」，**静默部署旧 commit**（实测踩到）。
GIT=(sudo -u yuque git -C "$REPO")

say "工作区必须干净（跟踪的文件）"
if [ -n "$("${GIT[@]}" status --porcelain --untracked-files=no)" ]; then
  "${GIT[@]}" status --short --untracked-files=no >&2
  die "有未提交的改动：先提交或 stash。部署只走「上游里有的 commit」。"
fi

say "快进到上游"
# 脚本自己也可能在这次快进里被更新。bash 边读边执行，已读进内存的部分不会跟着变 ——
# 于是「改了 deploy.sh」的那次部署会用旧脚本跑完剩下半段（实测踩到两次）。
SELF_BEFORE="$(md5sum "$0" | cut -d' ' -f1)"
if timeout 45 "${GIT[@]}" fetch origin; then
  "${GIT[@]}" merge --ff-only origin/main
else
  echo "⚠ 取不到 origin（网络问题？）—— 跳过快进，按当前 HEAD 继续"
fi
SELF_AFTER="$(md5sum "$0" | cut -d' ' -f1)"
if [ "$SELF_BEFORE" != "$SELF_AFTER" ]; then
  say "deploy.sh 自己刚被更新 —— 用新版重跑一遍（否则后半段还是旧逻辑）"
  exec 9>&-            # 先放锁：重跑的那个要自己取锁
  exec "$0" "$@"
fi
COMMIT="$("${GIT[@]}" rev-parse --short HEAD)"
SUBJECT="$("${GIT[@]}" log -1 --pretty=%s)"

say "测试（HOME 关进临时目录）"
SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT
HOME="$SANDBOX" PYTHONPATH=src .venv/bin/python -m pytest -q

say "systemd 单元与仓库对齐"
install -m 644 "deploy/$UNIT" /etc/systemd/system/
if [ -d "/etc/systemd/system/${UNIT}.d" ]; then
  echo "⚠ 还有 drop-in：$(ls "/etc/systemd/system/${UNIT}.d")—— 它会覆盖单元里的设置，确认后删掉"
fi
systemctl daemon-reload

say "重启单元"
systemctl enable "$UNIT" >/dev/null
systemctl restart "$UNIT"
sleep 6

say "验收"
RC=0
state="$(systemctl is-active "$UNIT" || true)"     # `|| true`：没起来时它会返回非 0，
printf '%-24s %s\n' "$UNIT" "$state"              # 裸着写会被 set -e 直接杀掉脚本
[ "$state" = "active" ] || RC=1

# ⚠️ 不要写成 `journalctl … | grep -q "…"`：pipefail 下 grep 一找到就退出、
# journalctl 吃 SIGPIPE(141)，管道整体非 0 → 条件判为假 → 间歇性误报「启动行没有」。
START="$(journalctl -u "$UNIT" --since "-3min" --no-pager 2>/dev/null | grep "接收入口已开" || true)"
if [ -n "$START" ]; then
  printf '%-24s %s\n' "启动行" "有"
else
  printf '%-24s %s\n' "启动行" "没有（journalctl -u $UNIT -n 50 看原因）"
  RC=1
fi

if [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/healthz")" = "200" ]; then
  printf '%-24s %s\n' "/healthz" "ok"
else
  printf '%-24s %s\n' "/healthz" "没反应"
  RC=1
fi

TB="$(journalctl -u "$UNIT" --since "-3min" --no-pager 2>/dev/null | grep "Traceback" || true)"
if [ -n "$TB" ]; then
  echo "⚠ 日志里有 Traceback —— 看 journalctl -u $UNIT -n 80" >&2
  RC=1
fi

printf '%s deploy %s %s %s\n' "$(date -Is)" "$WHO" "$COMMIT" "$SUBJECT" | tee -a "$LOG"
if [ "$RC" = "0" ]; then
  say "✓ 部署完成：$COMMIT $SUBJECT"
else
  say "✗ 部署不完整：$COMMIT $SUBJECT（上面有没过的检查）"
fi
exit "$RC"
