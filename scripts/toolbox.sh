#!/usr/bin/env bash
#
# toolbox.sh - Punto de entrada CLI unificado para las utilidades del servidor
#
# Uso:
#   toolbox                         Menú interactivo unificado
#   toolbox mover [OPCIONES...]     Ejecuta mover.sh con sus argumentos
#   toolbox ftp [OPCIONES...]       Ejecuta subeftp.sh con sus argumentos
#   toolbox web <accion> [ARGS...]  Controla y consulta las colas/logs de la Web por SSH
#   toolbox estado                  Resumen del estado CLI y de la interfaz Web
#   toolbox readme                  Muestra la guía completa (README.md) en terminal
#   toolbox --help                  Muestra esta ayuda

set -uo pipefail

SCRIPT="$(readlink -f "$0")"
SCRIPTS_DIR="$(dirname "$SCRIPT")"
ROOT_DIR="$(dirname "$SCRIPTS_DIR")"
MOVER_SH="$SCRIPTS_DIR/mover.sh"
FTP_SH="$SCRIPTS_DIR/subeftp.sh"
README_MD="$ROOT_DIR/README.md"

# En contenedor Docker (/config existe) persistir estado CLI bajo /config/cli
if [[ -z ${MOVER_DATA_DIR:-} && -d /config && $SCRIPTS_DIR == /app/* ]]; then
    export MOVER_DATA_DIR="/config/cli/mover"
fi
if [[ -z ${SUBEFTP_DATA_DIR:-} && -d /config && $SCRIPTS_DIR == /app/* ]]; then
    export SUBEFTP_DATA_DIR="/config/cli/ftp"
fi

if [[ -t 1 ]]; then
    C_RESET=$'\033[0m' C_BOLD=$'\033[1m' C_DIM=$'\033[2m'
    C_CYAN=$'\033[36m' C_GREEN=$'\033[32m' C_YELLOW=$'\033[33m'
else
    C_RESET="" C_BOLD="" C_DIM="" C_CYAN="" C_GREEN="" C_YELLOW=""
fi

WEB_BASE="http://127.0.0.1:${PORT:-8090}"

mostrar_ayuda() {
    cat << 'EOF'
toolbox - CLI unificado para utilidades del servidor (Mover/Copiar y FTP/Switch)

Uso:
  toolbox                           Abre el menú interactivo unificado
  toolbox mover [OPCIONES...]       Ejecuta mover.sh (ej: toolbox mover --iniciar)
  toolbox ftp [OPCIONES...]         Ejecuta subeftp.sh (ej: toolbox ftp --subir)
  toolbox subeftp [OPCIONES...]     Alias de 'toolbox ftp'
  toolbox web <subcomando>          Consulta o controla la Web/SQLite desde SSH:
      web estado                    Estado y progreso en vivo de Mover y FTP web
      web cola [mover|ftp]          Lista la cola actual de la web
      web iniciar <mover|ftp>       Arranca el worker web de mover o ftp
      web parar <mover|ftp>         Detiene limpiamente el worker web
      web logs [tipo] [N]           Últimos N logs (todos|mover|ftp|sistema)
      web historial [tipo] [N]      Últimas N transferencias terminadas (todos|mover|ftp)
      web papelera                  Lista la papelera (restaurar: toolbox mover --restaurar ID)
      web espacio                   Espacio libre en la SD de la Switch (si el FTP lo soporta)
      web buscar-switch             Escanea la LAN buscando Switch desde el servidor
      web backup [iniciar|parar]    Estado, inicio o parada del backup de la microSD (sin juegos)
  toolbox estado                    Resumen combinado de CLI + Web
  toolbox readme                    Muestra el README completo en terminal
  toolbox -h | --help               Muestra esta ayuda

Ejemplos rápidos dentro del contenedor:
  docker exec -it toolbox toolbox
  docker exec -it toolbox toolbox mover -m -i -d /host/mnt/destino /host/home/peli.mkv
  docker exec -it toolbox toolbox ftp -s -j -d /install /host/home/juegos/
  docker exec -it toolbox toolbox ftp --buscar-switch
  docker exec -it toolbox toolbox ftp --juego /host/home/juegos/Zelda -d /install -s
  docker exec -it toolbox toolbox ftp --backup -d /host/mnt/backups/switch
  docker exec -it toolbox toolbox mover --buscar "dune" /host/home/Descargas
  docker exec -it toolbox toolbox mover --listar /host/home/Descargas --orden fecha --desc
  docker exec -it toolbox toolbox mover --papelera
  docker exec -it toolbox toolbox web historial ftp 20
  docker exec -it toolbox toolbox web estado
  docker exec -it toolbox toolbox web logs mover 30
  docker exec -it toolbox toolbox readme
EOF
}

_web_api() {
    local method="$1" endpoint="$2" body="${3:-}"
    command -v curl > /dev/null 2>&1 || { echo "Error: falta curl." >&2; return 1; }
    if [[ $method == "GET" ]]; then
        curl -fsS --max-time 15 "${WEB_BASE}${endpoint}"
    elif [[ -n $body ]]; then
        curl -fsS --max-time 30 -X "$method" -H "Content-Type: application/json" -d "$body" "${WEB_BASE}${endpoint}"
    else
        curl -fsS --max-time 30 -X "$method" "${WEB_BASE}${endpoint}"
    fi
}

web_cmd() {
    local sub="${1:-estado}"
    shift || true
    case "$sub" in
        estado | status)
            local json
            json=$(_web_api GET "/api/status") || { echo "No se pudo conectar con la Web en $WEB_BASE"; return 1; }
            printf '%s' "$json" | python3 -c "$(cat << 'PY'
import json, sys
d = json.load(sys.stdin)
for kind in ("mover", "ftp"):
    w = d["workers"][kind]
    c = w.get("counts", {})
    pend = c.get("PEND", 0) + c.get("REAN", 0) + c.get("PROC", 0)
    st = "DETENIENDO" if w.get("stopping") else ("EN EJECUCION" if w.get("running") else "DETENIDO")
    print(f"  [{kind.upper()}] {st} | Pendientes: {pend} ({w.get('pending_bytes', 0)} B) | Pausados: {c.get('PAUS', 0)} | OK: {c.get('OK', 0)} | ERR: {c.get('ERR', 0)}")
    if kind == "ftp" and w.get("remote_free_bytes") is not None:
        print(f"     Espacio libre en la SD: {w['remote_free_bytes']} B")
    p = w.get("progress") or {}
    if w.get("running") and w.get("phase") == "preparing":
        print(f"     -> Preparando {p.get('filename', '')}")
    elif w.get("running") and p.get("filename"):
        print(f"     -> {p['filename']}: {p.get('percent', 0):.1f}% | {p.get('speed', '--')} | ETA {p.get('eta', '--')}")
    a = w.get("aggregate") or {}
    if w.get("running") and a.get("total_items"):
        print(f"     Global: {a.get('done_items', 0)}/{a['total_items']} archivo(s) | {a.get('done_bytes', 0)}/{a.get('total_bytes', 0)} B")
PY
)"
            ;;
        cola | queue)
            local kind="${1:-mover}" json
            [[ $kind == mover || $kind == ftp ]] || { echo "Uso: toolbox web cola [mover|ftp]"; return 1; }
            json=$(_web_api GET "/api/queue?kind=${kind}&only_pending=false") || { echo "Web no accesible en $WEB_BASE"; return 1; }
            printf '%s' "$json" | python3 -c "$(cat << 'PY'
import json, sys
items = json.load(sys.stdin).get("items", [])
if not items:
    print("  (cola vacía)")
for idx, it in enumerate(items, 1):
    sw = f"[{it['switch_type']}] " if it.get("switch_type") else ""
    err = f" | ERR: {it['error']}" if it.get("error") else ""
    print(f"  {idx:3d}. [{it['status']}] [{it['operation'].upper()}] {sw}{it['source_path']} -> {it['dest_path']}{err}")
PY
)"
            ;;
        iniciar | start)
            local kind="${1:-}"
            [[ $kind == mover || $kind == ftp ]] || { echo "Uso: toolbox web iniciar <mover|ftp>"; return 1; }
            _web_api POST "/api/queue/${kind}/start" && echo
            ;;
        parar | stop)
            local kind="${1:-}"
            [[ $kind == mover || $kind == ftp ]] || { echo "Uso: toolbox web parar <mover|ftp>"; return 1; }
            _web_api POST "/api/queue/${kind}/stop" && echo
            ;;
        logs | log)
            local kind="${1:-todos}" limit="${2:-40}" json
            json=$(_web_api GET "/api/logs?kind=${kind}&limit=${limit}") || { echo "Web no accesible en $WEB_BASE"; return 1; }
            printf '%s' "$json" | python3 -c "$(cat << 'PY'
import datetime, json, sys
logs = json.load(sys.stdin).get("logs", [])
if not logs:
    print("  (sin registros)")
for l in reversed(logs):
    ts = datetime.datetime.fromtimestamp(l["ts"]).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{l['kind'].upper()}] [{l['level']}] {l['message']}")
PY
)"
            ;;
        historial | history)
            local kind="${1:-todos}" limit="${2:-30}" json
            [[ $kind == todos || $kind == mover || $kind == ftp ]] || { echo "Uso: toolbox web historial [todos|mover|ftp] [N]"; return 1; }
            [[ $limit =~ ^[0-9]+$ ]] || { echo "Uso: toolbox web historial [todos|mover|ftp] [N]"; return 1; }
            json=$(_web_api GET "/api/history?kind=${kind}&limit=${limit}") || { echo "Web no accesible en $WEB_BASE"; return 1; }
            printf '%s' "$json" | python3 -c "$(cat << 'PY'
import datetime, json, os, sys
d = json.load(sys.stdin)
items = d.get("items", [])
if not items:
    print("  (historial vacío)")
for h in items:
    ts = datetime.datetime.fromtimestamp(h["finished_at"]).strftime("%Y-%m-%d %H:%M")
    dur = h.get("duration_s") or 0
    err = f" | {h['error']}" if h.get("error") else ""
    print(f"  {ts} [{h['kind'].upper()}] [{h['status']}] {os.path.basename(h['src'])} ({h['size']} B, {dur:.0f} s){err}")
print(f"  Mostrando {len(items)} de {d.get('total', len(items))}")
PY
)"
            ;;
        papelera | trash)
            local json
            json=$(_web_api GET "/api/trash") || { echo "Web no accesible en $WEB_BASE"; return 1; }
            printf '%s' "$json" | python3 -c "$(cat << 'PY'
import datetime, json, sys
d = json.load(sys.stdin)
items = d.get("items", [])
if d.get("trash_enabled", True):
    print(f"Papelera ACTIVADA ({len(items)} elemento(s); se conservan {d.get('trash_days')} día(s))")
else:
    print(f"Papelera DESACTIVADA: los borrados son definitivos ({len(items)} elemento(s) antiguos)")
for t in items:
    ts = datetime.datetime.fromtimestamp(t["trashed_at"]).strftime("%Y-%m-%d %H:%M")
    print(f"  #{t['id']} {t['original_path']} ({t['size']} B) borrado {ts}")
PY
)"
            ;;
        espacio | space)
            local json
            json=$(_web_api GET "/api/ftp/space") || { echo "Web no accesible en $WEB_BASE"; return 1; }
            printf '%s' "$json" | python3 -c "$(cat << 'PY'
import json, sys
d = json.load(sys.stdin)
if not d.get("supported"):
    print("El servidor FTP no informa del espacio libre." + (f" ({d['error']})" if d.get("error") else ""))
else:
    estado = "cabe" if d.get("enough") else "NO CABE"
    print(f"Libre en la SD: {d['free_bytes']} B | Pendiente: {d['pending_bytes']} B -> {estado}")
PY
)"
            ;;
        buscar-switch | scan)
            local json
            json=$(_web_api POST "/api/ftp/scan-switch" "{}") || { echo "Web no accesible en $WEB_BASE"; return 1; }
            printf '%s' "$json" | python3 -c "$(cat << 'PY'
import json, sys
res = json.load(sys.stdin)
found = res.get("found", [])
if not found:
    print("No se encontró ninguna Switch FTP en subredes:", ", ".join(res.get("prefixes", [])))
for hit in found:
    print(f"  {hit['ip']}:{hit['port']} -> {hit.get('banner') or 'puerto abierto'}")
PY
)"
            ;;
        backup)
            local json
            case "${1:-}" in
                iniciar | start) json=$(_web_api POST "/api/ftp/backup/start" "{}") || { echo "No se pudo iniciar el backup (¿web caída, cola FTP subiendo o sin carpeta de backups?)."; return 1; } ;;
                parar | stop) json=$(_web_api POST "/api/ftp/backup/stop") || { echo "Web no accesible en $WEB_BASE"; return 1; } ;;
                "" | estado) json=$(_web_api GET "/api/ftp/backup") || { echo "Web no accesible en $WEB_BASE"; return 1; } ;;
                *) echo "Uso: toolbox web backup [estado|iniciar|parar]" >&2; return 1 ;;
            esac
            printf '%s' "$json" | python3 -c "$(cat << 'PY'
import json, sys
d = json.load(sys.stdin)
p = d.get("progress") if d.get("running") else None
if p:
    print(f"Backup EN MARCHA ({p.get('phase')}) -> {p.get('target')}")
    print(f"  {p.get('files_done', 0)}/{p.get('files_total', 0)} fichero(s) | {p.get('bytes_done', 0)}/{p.get('bytes_total', 0)} B | {p.get('current', '')}")
elif d.get("last"):
    l = d["last"]
    print(f"Último backup: {l.get('status')} | {l.get('files_done', 0)}/{l.get('files_total', 0)} fichero(s) | {l.get('bytes_done', 0)} B -> {l.get('target')}")
    if l.get("error"):
        print(f"  Motivo: {l['error']}")
else:
    print("Sin backups desde que arrancó la web.")
PY
)"
            ;;
        *)
            echo "Subcomando web desconocido: $sub" >&2
            echo "Opciones: estado | cola [mover|ftp] | iniciar <mover|ftp> | parar <mover|ftp> | logs [tipo] [N] | historial [tipo] [N] | papelera | espacio | buscar-switch | backup [estado|iniciar|parar]" >&2
            return 1
            ;;
    esac
}

mostrar_readme() {
    if [[ ! -f $README_MD ]]; then
        echo "No se encontró $README_MD" >&2
        return 1
    fi
    if [[ -t 1 ]] && command -v less > /dev/null 2>&1; then
        less -R "$README_MD"
    else
        cat "$README_MD"
    fi
}

mostrar_estado_global() {
    if [[ ${TOOLBOX_UNIFIED:-} == "1" ]] || [[ -d /config && $SCRIPTS_DIR == /app/* ]]; then
        PYTHONPATH="$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}" python3 -m app.cli mover --estado 2>/dev/null || true
        echo
        PYTHONPATH="$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}" python3 -m app.cli ftp --estado 2>/dev/null || true
        echo
        return 0
    fi
    printf '%s=== Estado CLI: Mover / Copiar ===%s\n' "$C_BOLD$C_CYAN" "$C_RESET"
    "$MOVER_SH" --estado || true
    echo
    printf '%s=== Estado CLI: FTP / Switch ===%s\n' "$C_BOLD$C_CYAN" "$C_RESET"
    "$FTP_SH" --estado || true
    echo
    if command -v curl > /dev/null 2>&1; then
        printf '%s=== Servicio Web (%s) ===%s\n' "$C_BOLD$C_CYAN" "$WEB_BASE" "$C_RESET"
        if curl -fsS --max-time 2 "${WEB_BASE}/api/health" > /dev/null 2>&1; then
            printf '  %s● Web activa%s\n' "$C_GREEN" "$C_RESET"
            web_cmd estado || true
        else
            printf '  %s○ Web no accesible en este momento%s\n' "$C_YELLOW" "$C_RESET"
        fi
    fi
}

menu_interactivo() {
    local op=""
    while :; do
        clear 2>/dev/null || true
        printf '%s╭──────────────────────────────────────────────────────╮%s\n' "$C_CYAN" "$C_RESET"
        printf '%s│%s   %-50s %s│%s\n' "$C_CYAN" "$C_RESET" "toolbox · Utilidades CLI (Mover + FTP/Switch)" "$C_CYAN" "$C_RESET"
        printf '%s╰──────────────────────────────────────────────────────╯%s\n' "$C_CYAN" "$C_RESET"
        echo
        printf '  %s1)%s 💾 Gestor Mover / Copiar local (mover.sh)\n' "$C_BOLD" "$C_RESET"
        printf '  %s2)%s 📡 Gestor FTP / Nintendo Switch (subeftp.sh)\n' "$C_BOLD" "$C_RESET"
        printf '  %s3)%s 📊 Ver estado combinado (CLI + Web)\n' "$C_BOLD" "$C_RESET"
        printf '  %s4)%s 📜 Ver últimos logs de la Web\n' "$C_BOLD" "$C_RESET"
        printf '  %s5)%s 📖 Leer guía completa (README)\n' "$C_BOLD" "$C_RESET"
        printf '  %s6)%s ❓ Ayuda rápida de comandos CLI\n' "$C_BOLD" "$C_RESET"
        printf '  %s7)%s 🕘 Historial de transferencias (Web)\n' "$C_BOLD" "$C_RESET"
        printf '  %s8)%s 🗑  Papelera (Web)\n' "$C_BOLD" "$C_RESET"
        printf '  %s0)%s 🚪 Salir\n' "$C_BOLD" "$C_RESET"
        echo
        printf '  %sDirectorio datos:%s %s\n' "$C_DIM" "$C_RESET" "${DATA_ROOT:-$PWD}"
        echo
        read -r -p "  Elige una opción: " op || return 0
        case "$op" in
            1) "$MOVER_SH" ;;
            2) "$FTP_SH" ;;
            3)
                echo
                mostrar_estado_global
                echo
                read -r -p "  Pulsa Enter para continuar..." _ || true
                ;;
            4)
                echo
                web_cmd logs todos 30 || true
                echo
                read -r -p "  Pulsa Enter para continuar..." _ || true
                ;;
            5) mostrar_readme ;;
            6)
                echo
                mostrar_ayuda
                echo
                read -r -p "  Pulsa Enter para continuar..." _ || true
                ;;
            7)
                echo
                web_cmd historial todos 30 || true
                echo
                read -r -p "  Pulsa Enter para continuar..." _ || true
                ;;
            8)
                echo
                web_cmd papelera || true
                echo
                printf '  Restaurar: toolbox mover --restaurar ID  ·  Vaciar: toolbox mover --vaciar-papelera\n'
                printf '  Activar/desactivar: toolbox mover --papelera-activar | --papelera-desactivar\n'
                read -r -p "  Pulsa Enter para continuar..." _ || true
                ;;
            0 | q | Q) return 0 ;;
            *) ;;
        esac
    done
}

case "${1:-}" in
    "")
        menu_interactivo
        ;;
    mover)
        shift
        exec "$MOVER_SH" "$@"
        ;;
    ftp | subeftp)
        shift
        exec "$FTP_SH" "$@"
        ;;
    web)
        shift
        web_cmd "$@"
        ;;
    estado | status)
        mostrar_estado_global
        ;;
    readme | guia | docs)
        mostrar_readme
        ;;
    -h | --help | help)
        mostrar_ayuda
        ;;
    *)
        echo "Subcomando desconocido: $1" >&2
        echo "Usa 'toolbox --help' para ver las opciones disponibles." >&2
        exit 1
        ;;
esac
