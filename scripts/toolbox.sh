#!/usr/bin/env bash
#
# toolbox.sh - Punto de entrada CLI unificado para las utilidades del servidor
#
# Uso:
#   toolbox                   Menú interactivo unificado
#   toolbox mover [OPCIONES]  Ejecuta mover.sh con sus argumentos
#   toolbox ftp [OPCIONES]    Ejecuta subeftp.sh con sus argumentos
#   toolbox estado            Resumen del estado de ambos módulos CLI y de la web
#   toolbox --help            Muestra esta ayuda

set -uo pipefail

SCRIPT="$(readlink -f "$0")"
SCRIPTS_DIR="$(dirname "$SCRIPT")"
MOVER_SH="$SCRIPTS_DIR/mover.sh"
FTP_SH="$SCRIPTS_DIR/subeftp.sh"

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

mostrar_ayuda() {
    cat << 'EOF'
toolbox - CLI unificado para utilidades del servidor (Mover/Copiar y FTP/Switch)

Uso:
  toolbox                       Abre el menú interactivo unificado
  toolbox mover [OPCIONES...]   Ejecuta mover.sh (ej: toolbox mover --iniciar)
  toolbox ftp [OPCIONES...]     Ejecuta subeftp.sh (ej: toolbox ftp --subir)
  toolbox subeftp [OPCIONES...] Alias de 'toolbox ftp'
  toolbox estado                Muestra el estado de las colas CLI y del servicio web
  toolbox -h | --help           Muestra esta ayuda

Ejemplos rápidos dentro del contenedor:
  docker exec -it toolbox toolbox
  docker exec -it toolbox toolbox mover -m -i /host/home/usuario/peli.mkv
  docker exec -it toolbox toolbox ftp -s /host/home/usuario/juego.nsp
  docker exec -it toolbox toolbox estado
EOF
}

mostrar_estado_global() {
    printf '%s=== Estado CLI: Mover / Copiar ===%s\n' "$C_BOLD$C_CYAN" "$C_RESET"
    "$MOVER_SH" --estado || true
    echo
    printf '%s=== Estado CLI: FTP / Switch ===%s\n' "$C_BOLD$C_CYAN" "$C_RESET"
    "$FTP_SH" --estado || true
    echo
    if command -v curl > /dev/null 2>&1; then
        local port="${PORT:-8090}"
        printf '%s=== Servicio Web (http://127.0.0.1:%s) ===%s\n' "$C_BOLD$C_CYAN" "$port" "$C_RESET"
        if curl -fsS --max-time 2 "http://127.0.0.1:${port}/api/health" > /dev/null 2>&1; then
            printf '  %s● Web activa%s\n' "$C_GREEN" "$C_RESET"
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
        printf '  %s3)%s 📊 Ver estado de colas CLI y web\n' "$C_BOLD" "$C_RESET"
        printf '  %s4)%s ❓ Ayuda de comandos CLI\n' "$C_BOLD" "$C_RESET"
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
                mostrar_ayuda
                echo
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
    estado | status)
        mostrar_estado_global
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

