#!/bin/bash
# Версионное развёртывание комплекта agent-control.
#
# 🔴 Куплено аварией 06.08.2026: файлы копировались по одному прямо в рабочий
# каталог. Один патч успел обрезать cp.py — пустой файл уехал на сервер, служба
# перезапускалась 25 раз. При двух и более модулях появляется вторая беда: они
# могут оказаться РАЗНЫХ версий, потому что копируются не одновременно.
#
# Поэтому: комплект кладётся в каталог версии, там проверяется целиком
# (компиляция, импорт, тесты на временной базе и временном порте), и только
# затем одним переключением ссылки становится действующим. Не прошло проверку —
# ссылка не двигается. После переключения health-check; не поднялось — возврат.
set -euo pipefail

ROOT=/opt/agent-control
RELEASES=$ROOT/releases
SRC=${1:?укажи каталог с новым комплектом}
TAG=${2:-$(date -u +%Y%m%dT%H%M%SZ)}
DST=$RELEASES/$TAG
MODULES="cp.py contracts.py gc.py ctl.py safe_write.py"
TESTS="test_cp.py test_gc.py"

echo "=== развёртывание $TAG ==="
mkdir -p "$RELEASES"
rm -rf "$DST"
mkdir -p "$DST"
for f in $MODULES $TESTS; do
  [ -f "$SRC/$f" ] || { echo "нет файла $f"; exit 1; }
  # 🔴 Пустой или подозрительно короткий файл дальше не пускаем
  sz=$(stat -c%s "$SRC/$f")
  [ "$sz" -ge 500 ] || { echo "файл $f подозрительно мал ($sz Б)"; exit 1; }
  cp "$SRC/$f" "$DST/$f"
done
cp "$SRC/zones.txt" "$DST/zones.txt" 2>/dev/null || true

echo "--- компиляция ---"
python3 -m py_compile $(for f in $MODULES $TESTS; do echo "$DST/$f"; done)

echo "--- импорт модулей ---"
( cd "$DST" && python3 -c "
import importlib.util, sys
for name in ['cp', 'contracts', 'gc', 'safe_write']:
    spec = importlib.util.spec_from_file_location(name, name + '.py')
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
print('импорт всех модулей прошёл')
" )

echo "--- проверки на ВРЕМЕННОЙ базе и порте ---"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"; kill %1 2>/dev/null || true' EXIT
cp "$ROOT/api.key" "$TMP/api.key"
mkdir -p "$TMP/keys" && cp "$ROOT"/keys/*.key "$TMP/keys/" 2>/dev/null || true
cp "$ROOT/state/disk.json" "$TMP/" 2>/dev/null || true
mkdir -p "$TMP/state" && cp "$ROOT/state/disk.json" "$TMP/state/" 2>/dev/null || true
CP_ROOT="$TMP" CP_DB="$TMP/cp.db" CP_PORT=8019 CP_SOCKET="$TMP/cp.sock" \
  python3 "$DST/cp.py" > "$TMP/cp.log" 2>&1 &
sleep 3
CP_API=http://127.0.0.1:8019 CP_KEYFILE="$TMP/api.key" CP_SOCKET="$TMP/cp.sock" \
  python3 - <<PY
import json, urllib.request, sys
key = open("$TMP/api.key").read().strip()
r = urllib.request.Request("http://127.0.0.1:8019/status", data=b"{}",
                           headers={"Content-Type": "application/json",
                                    "X-Api-Key": key})
print("временный экземпляр отвечает:", bool(json.loads(urllib.request.urlopen(r, timeout=10).read())))
PY
kill %1 2>/dev/null || true
echo "проверочный экземпляр поднимался на отдельной базе и порте — боевая не тронута"

echo "--- переключение ---"
PREV=$(readlink -f "$ROOT/current" 2>/dev/null || echo "")
ln -sfn "$DST" "$ROOT/current.new" && mv -Tf "$ROOT/current.new" "$ROOT/current"
for f in $MODULES $TESTS; do ln -sfn "$ROOT/current/$f" "$ROOT/$f"; done
systemctl restart agent-cp
sleep 3
if ! systemctl is-active --quiet agent-cp; then
  echo "🔴 служба не поднялась — ВОЗВРАТ на $PREV"
  [ -n "$PREV" ] && ln -sfn "$PREV" "$ROOT/current" && systemctl restart agent-cp
  exit 1
fi
echo "готово: current → $DST"
ls -l "$ROOT/current"
