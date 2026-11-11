#!/usr/bin/env bash
#
# mover.sh - Copia o mueve ficheros de una carpeta local a otras, con cola,
#            límite de ancho de banda y procesamiento en segundo plano.
#            Requiere: bash, rsync, flock, awk, df, stat.
#
# Todo se guarda en la misma carpeta que este script:
#   mover.conf     configuración (carpetas, operación, límite, conflictos, favoritos)
#   cola.txt       cola de transferencias
#   mover.log      registro de todo lo que ocurre
#   worker.pid     PID del proceso en segundo plano (si está activo)
#   worker.lock    bloqueo que impide dos procesos en segundo plano a la vez
#   progreso.txt   progreso del fichero que se está copiando
#
# Uso:
#   ./mover.sh               menú interactivo
#   ./mover.sh --iniciar     lanza el procesamiento en segundo plano y sale
#   ./mover.sh --estado      muestra el estado de la cola y sale
#   ./mover.sh --pendientes  lista los elementos pendientes y sale
#   ./mover.sh --parar       detiene el procesamiento en segundo plano
#   ./mover.sh --help        muestra la ayuda y opciones disponibles

set -uo pipefail

SCRIPT="$(readlink -f "$0")"
ROOT_DIR="$(dirname "$(dirname "$SCRIPT")")"

# En el contenedor Toolbox (o con TOOLBOX_UNIFIED=1) compartir cola SQLite/API con la Web,
# salvo que se pase --legacy para usar el modo Bash clásico con cola.txt.
_use_legacy=0
_filtered_args=()
for _a in "$@"; do
    if [[ $_a == "--legacy" ]]; then
        _use_legacy=1
    else
        _filtered_args+=("$_a")
    fi
done

if ((!_use_legacy)) && [[ ${1:-} != "--worker" ]] && [[ -f "$ROOT_DIR/app/cli.py" ]] && command -v python3 > /dev/null 2>&1; then
    if [[ ${TOOLBOX_UNIFIED:-} == "1" ]] || [[ -d /config && $SCRIPT == /app/* ]]; then
        export PYTHONPATH="$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}"
        exec python3 -m app.cli mover "${_filtered_args[@]}"
    fi
fi
if ((${#_filtered_args[@]})); then
    set -- "${_filtered_args[@]}"
else
    set --
fi

BASE="${MOVER_DATA_DIR:-$(dirname "$SCRIPT")}"
mkdir -p "$BASE"
CONF="$BASE/mover.conf"
COLA="$BASE/cola.txt"
LOCK="$BASE/cola.lock"
PIDF="$BASE/worker.pid"
LOG="$BASE/mover.log"
PROG="$BASE/progreso.txt"
WLOCK="$BASE/worker.lock"

# Carpeta (dentro de la carpeta destino) donde se escriben los ficheros a medio copiar
PARCIAL=".mover-partial"

# Formato de cola.txt (una línea por fichero, campos separados por TAB):
#   id  estado  reanudar(0/1)  tamaño  fecha_fin  origen  destino  mensaje  firma  op  raiz
# Los campos vacíos se guardan como "-". firma = "tamaño:mtime" del origen cuando se encoló
# o empezó a procesarse. op = copiar|mover. raiz = carpeta añadida con "r N" (al mover,
# sus subcarpetas vacías se eliminan al terminar; nunca se sale de ella).
# Estados: pendiente, pausado, procesando, completado, error.

CAMPOS=(CARPETA_ORIGEN CARPETA_DESTINO ESTRUCTURA OPERACION LIMITE_KBPS REINTENTOS
        ESPERA_REINTENTO VERIFICAR CONFLICTO)

CARPETA_ORIGEN="$HOME" CARPETA_DESTINO="" ESTRUCTURA="s" OPERACION="copiar" LIMITE_KBPS="0"
REINTENTOS="3" ESPERA_REINTENTO="10" VERIFICAR="tamano" CONFLICTO="omitir"
FAVORITOS=()

ORIGEN="menu"

# --------------------------------------------------------------------------
# Utilidades y Diseño Visual
# --------------------------------------------------------------------------

if [[ -t 1 ]]; then
    C_RESET=$'\033[0m'
    C_BOLD=$'\033[1m'
    C_DIM=$'\033[2m'
    C_GREEN=$'\033[32m'
    C_BLUE=$'\033[34m'
    C_YELLOW=$'\033[33m'
    C_MAGENTA=$'\033[35m'
    C_RED=$'\033[31m'
    C_CYAN=$'\033[36m'
else
    C_RESET="" C_BOLD="" C_DIM="" C_GREEN="" C_BLUE="" C_YELLOW="" C_MAGENTA="" C_RED="" C_CYAN=""
fi

log() {
    if [[ -f $LOG ]] && (( $(stat -c %s "$LOG" 2>/dev/null || echo 0) > 5242880 )); then
        # Rotar bajo bloqueo y volviendo a comprobar el tamaño: si otro proceso
        # (menú o worker) ya lo ha rotado, no se pisa el .1 recién creado.
        (
            flock -n 7 || exit 0
            (( $(stat -c %s "$LOG" 2>/dev/null || echo 0) > 5242880 )) && mv -f "$LOG" "$LOG.1"
        ) 7>> "$LOG" 2>/dev/null || true
    fi
    printf '%(%Y-%m-%d %H:%M:%S)T [%s pid=%d] %s\n' -1 "$ORIGEN" "$BASHPID" "$*" >> "$LOG"
}

tam() {
    numfmt --to=iec-i --suffix=B --format='%.1f' "${1:-0}" 2>/dev/null || echo "${1}B"
}

barra_progreso() {  # barra_progreso porcentaje [ancho] [color]
    local pct="${1:-0}" ancho="${2:-26}" color="${3:-$C_GREEN}" completado restante barra esp
    pct="${pct%.*}"
    [[ $pct =~ ^[0-9]+$ ]] || pct=0
    ((pct > 100)) && pct=100
    ((pct < 0)) && pct=0

    completado=$((pct * ancho / 100))
    restante=$((ancho - completado))

    printf -v barra "%*s" "$completado" ""
    barra="${barra// /█}"
    printf -v esp "%*s" "$restante" ""
    esp="${esp// /░}"

    printf '[%s%s%s%s%s] %3d%%' "$color" "$barra" "$C_DIM" "$esp" "$C_RESET" "$pct"
}

acortar_nombre() {  # acortar_nombre "cadena" [max_longitud]
    local s="$1" max="${2:-62}"
    if ((${#s} > max)); then
        local p1=$(( (max - 3) * 58 / 100 ))
        local p2=$(( max - 3 - p1 ))
        printf '%s...%s' "${s:0:p1}" "${s: -p2}"
    else
        printf '%s' "$s"
    fi
}

ancho_terminal() {
    local cols
    cols=$(tput cols 2>/dev/null || echo 120)
    [[ $cols =~ ^[0-9]+$ ]] || cols=120
    ((cols < 88)) && cols=88
    echo "$cols"
}

ancho_columna_fichero() {
    local cols
    cols=$(ancho_terminal)
    # Descontar margen y resto de columnas (#, ID, ESTADO, OP, TAMAÑO, DESTINO ~ 52 chars)
    local w=$((cols - 52))
    ((w < 56)) && w=56
    echo "$w"
}

repetir_char() {  # repetir_char "─" N
    local ch="$1" n="${2:-80}" s
    ((n <= 0)) && return
    printf -v s "%*s" "$n" ""
    printf '%s' "${s// /$ch}"
}

separador_tabla() {
    local ancho="${1:-105}"
    printf '  %s%s%s\n' "$C_DIM" "$(repetir_char "─" "$ancho")" "$C_RESET"
}

caja_cabecera() {  # caja_cabecera "TÍTULO" [ancho_interior]
    local titulo="$1" w="${2:-84}" len_t pad_izq pad_der
    len_t=${#titulo}
    pad_izq=$(( (w - len_t) / 2 ))
    pad_der=$(( w - len_t - pad_izq ))
    ((pad_izq < 1)) && pad_izq=1
    ((pad_der < 1)) && pad_der=1
    printf '  %s╭%s╮%s\n' "$C_CYAN" "$(repetir_char "─" "$w")" "$C_RESET"
    printf '  %s│%s%*s%s%s%s%*s%s│%s\n' \
        "$C_CYAN" "$C_RESET" "$pad_izq" "" "$C_BOLD" "$titulo" "$C_RESET" "$pad_der" "" "$C_CYAN" "$C_RESET"
    printf '  %s╰%s╯%s\n' "$C_CYAN" "$(repetir_char "─" "$w")" "$C_RESET"
}

# Insignia de 6 caracteres visibles con la operación del elemento
insignia_op() {  # insignia_op copiar|mover
    if [[ $1 == mover ]]; then
        printf '%s[MOV ]%s' "$C_MAGENTA" "$C_RESET"
    else
        printf '%s[COP ]%s' "$C_CYAN" "$C_RESET"
    fi
}

texto_op() {  # texto_op copiar|mover -> COPIAR / MOVER coloreado
    if [[ $1 == mover ]]; then
        printf '%sMOVER%s' "$C_BOLD$C_MAGENTA" "$C_RESET"
    else
        printf '%sCOPIAR%s' "$C_BOLD$C_CYAN" "$C_RESET"
    fi
}

# Devuelve etiqueta y color ANSI para un estado de la cola
info_estado_cola() {  # info_estado_cola estado reanudar -> ETIQ_ESTADO, COLOR_ESTADO
    local estado="$1" reanudar="${2:-0}"
    case "$estado" in
        pendiente)
            if [[ $reanudar == 1 ]]; then
                ETIQ_ESTADO="REAN"; COLOR_ESTADO="$C_CYAN"
            else
                ETIQ_ESTADO="PEND"; COLOR_ESTADO="$C_YELLOW"
            fi ;;
        pausado)
            ETIQ_ESTADO="PAUS"; COLOR_ESTADO="$C_MAGENTA" ;;
        procesando)
            ETIQ_ESTADO="PROC"; COLOR_ESTADO="$C_BLUE" ;;
        completado)
            ETIQ_ESTADO=" OK "; COLOR_ESTADO="$C_GREEN" ;;
        error)
            ETIQ_ESTADO="ERR "; COLOR_ESTADO="$C_RED" ;;
        *)
            ETIQ_ESTADO="$estado"; COLOR_ESTADO="$C_RESET" ;;
    esac
}

parsear_velocidad_bps() {  # "1.23MB/s" / "512.00kB/s" -> bytes por segundo
    local v="${1:-0}"
    v="${v// /}"
    v="${v%/s}"
    v="${v%[bB]}"
    if [[ $v =~ ^([0-9]+(\.[0-9]+)?)([kKmMgG]?)$ ]]; then
        local n="${BASH_REMATCH[1]}"
        local u="${BASH_REMATCH[3],,}"
        case "$u" in
            k) awk -v n="$n" 'BEGIN { printf "%.0f", n * 1024 }' ;;
            m) awk -v n="$n" 'BEGIN { printf "%.0f", n * 1048576 }' ;;
            g) awk -v n="$n" 'BEGIN { printf "%.0f", n * 1073741824 }' ;;
            *) awk -v n="$n" 'BEGIN { printf "%.0f", n }' ;;
        esac
    else
        echo 0
    fi
}

# Lee la última línea de progreso de rsync (--info=progress2, LC_ALL=C) en progreso.txt:
#   bytes  porcentaje  velocidad  restante [(xfr#N, to-chk=...)]
# progreso.txt.w guarda "inicio_epoch" del fichero en curso.
# Establece PRG_PCT PRG_BYTES PRG_ENVIADO PRG_VEL PRG_T_RESTANTE PRG_T_PASADO.
# Devuelve 1 si todavía no hay datos.
leer_progreso() {
    local ult="" ini="" ahora
    PRG_PCT="" PRG_BYTES="" PRG_ENVIADO="" PRG_VEL="" PRG_T_RESTANTE="" PRG_T_PASADO=""
    [[ -f $PROG ]] && ult=$(tr '\r' '\n' < "$PROG" 2>/dev/null | grep -E '^ *[0-9,]+ +[0-9]+%' | tail -1)
    [[ -n $ult ]] || return 1
    read -r PRG_BYTES PRG_PCT PRG_VEL PRG_T_RESTANTE _ <<< "$ult"
    PRG_BYTES="${PRG_BYTES//,/}"
    PRG_PCT="${PRG_PCT%\%}"
    [[ $PRG_BYTES =~ ^[0-9]+$ ]] || PRG_BYTES=0
    PRG_ENVIADO="$(tam "$PRG_BYTES")"
    read -r ini _ < "$PROG.w" 2>/dev/null
    if [[ $ini =~ ^[0-9]+$ ]]; then
        printf -v ahora '%(%s)T' -1
        PRG_T_PASADO="$(formatear_tiempo $((ahora - ini)))"
    fi
    return 0
}

formatear_tiempo() {  # segundos -> "Xh Ym Zs" o "Ym Zs" o "Xs"
    local s="${1:-0}"
    s="${s%.*}"
    [[ $s =~ ^[0-9]+$ ]] || { echo "?"; return; }
    if ((s >= 3600)); then
        printf '%dh %02dm %02ds' $((s / 3600)) $(( (s % 3600) / 60 )) $((s % 60))
    elif ((s >= 60)); then
        printf '%dm %02ds' $((s / 60)) $((s % 60))
    else
        printf '%ds' "$s"
    fi
}

# Calcula métricas globales y tiempo restante total de la cola.
# Establece:
#   COLA_BYTES_REST, COLA_FICH_REST, COLA_BPS, COLA_TIEMPO_EST
#   COLA_BYTES_TOT, COLA_BYTES_HECHOS, COLA_PCT_GLOBAL
calcular_tiempo_cola() {  # calcular_tiempo_cola [vel_sugerida] [pct_actual]
    local vel_sugerida="${1:-}" pct_sub="${2:-0}"
    touch "$COLA"

    local datos_cola
    datos_cola=$(awk -F'\t' '
        BEGIN { pend_c=0; pend_b=0; sub_c=0; sub_b=0; ok_c=0; ok_b=0 }
        $2 == "pendiente"  { pend_c++; pend_b += $4 }
        $2 == "procesando" { sub_c++;  sub_b += $4 }
        $2 == "completado" { ok_c++;   ok_b += $4 }
        END { printf "%d|%s|%d|%s|%d|%s", pend_c, pend_b, sub_c, sub_b, ok_c, ok_b }
    ' "$COLA" 2>/dev/null)

    local p_cnt p_bytes s_cnt s_bytes ok_cnt ok_bytes
    IFS='|' read -r p_cnt p_bytes s_cnt s_bytes ok_cnt ok_bytes <<< "$datos_cola"
    p_cnt=${p_cnt:-0}; p_bytes=${p_bytes:-0}; s_cnt=${s_cnt:-0}; s_bytes=${s_bytes:-0}
    ok_cnt=${ok_cnt:-0}; ok_bytes=${ok_bytes:-0}

    pct_sub="${pct_sub%.*}"
    [[ $pct_sub =~ ^[0-9]+$ ]] || pct_sub=0

    local s_restante=$s_bytes s_hecho=0
    if ((s_cnt > 0 && pct_sub > 0)); then
        s_hecho=$(( s_bytes * pct_sub / 100 ))
        s_restante=$(( s_bytes - s_hecho ))
        ((s_restante < 0)) && s_restante=0
    fi

    COLA_BYTES_REST=$(( p_bytes + s_restante ))
    COLA_FICH_REST=$(( p_cnt + s_cnt ))
    COLA_BYTES_HECHOS=$(( ok_bytes + s_hecho ))
    COLA_BYTES_TOT=$(( ok_bytes + p_bytes + s_bytes ))
    COLA_PCT_GLOBAL=0
    if ((COLA_BYTES_TOT > 0)); then
        COLA_PCT_GLOBAL=$(( COLA_BYTES_HECHOS * 100 / COLA_BYTES_TOT ))
        ((COLA_PCT_GLOBAL > 100)) && COLA_PCT_GLOBAL=100
    fi

    COLA_BPS=0
    COLA_TIEMPO_EST=""

    if [[ -n $vel_sugerida && $vel_sugerida != "?" ]]; then
        COLA_BPS=$(parsear_velocidad_bps "$vel_sugerida")
    fi

    if ((COLA_BPS <= 0 && LIMITE_KBPS > 0)); then
        COLA_BPS=$(( LIMITE_KBPS * 1024 ))
    fi

    if ((COLA_BPS > 0 && COLA_BYTES_REST > 0)); then
        local segs=$(( COLA_BYTES_REST / COLA_BPS ))
        COLA_TIEMPO_EST="$(formatear_tiempo "$segs")"
    fi
}

pausa() {
    read -rp $'\n  Pulsa Intro para continuar...' _ || true
}

preguntar() {  # preguntar "texto" "defecto" -> RESP
    local texto="$1" defecto="${2:-}"
    read -rep "  $texto${defecto:+ [$defecto]}: " RESP || RESP=""
    RESP="${RESP:-$defecto}"
}

si_no() {  # si_no "texto" s|n
    local defecto="${2:-s}" r
    read -rp "  $1 ($([[ $defecto == s ]] && echo S/n || echo s/N)): " r || r=""
    r="${r:-$defecto}"
    [[ ${r,,} == s* || ${r,,} == y* ]]
}

# "1,3,5-8" -> SELECCION=(1 3 5 6 7 8), limitado a 1..max
parsear_seleccion() {
    local texto="${1// /}" max="$2" trozo a b n
    local -a trozos=()
    SELECCION=()
    IFS=',' read -ra trozos <<< "$texto"
    for trozo in "${trozos[@]}"; do
        [[ -z $trozo ]] && continue
        if [[ $trozo =~ ^([0-9]+)-([0-9]+)$ ]]; then
            a=${BASH_REMATCH[1]} b=${BASH_REMATCH[2]}
        elif [[ $trozo =~ ^[0-9]+$ ]]; then
            a=$trozo b=$trozo
        else
            return 1
        fi
        for ((n = 10#$a; n <= 10#$b; n++)); do
            ((n >= 1 && n <= max)) && [[ " ${SELECCION[*]} " != *" $n "* ]] && SELECCION+=("$n")
        done
    done
    return 0
}

# Resuelve una selección de la cola hecha por número de fila (1,3-5) o por #ID (#102,#105)
# Devuelve los IDs reales en IDS_RESUELTOS=("102" "105" ...)
resolver_seleccion_cola() {  # resolver_seleccion_cola "expresion"
    local expr="${1#"${1%%[! ]*}"}"
    expr="${expr%"${expr##*[! ]}"}"
    IDS_RESUELTOS=()
    [[ -s $COLA ]] || return 1

    local -a mapa_pos=()
    local -A existe_id=()
    local id rest
    while IFS=$'\t' read -r id rest; do
        mapa_pos+=("$id")
        existe_id["$id"]=1
    done < "$COLA"

    local total_filas=${#mapa_pos[@]}
    ((total_filas == 0)) && return 1

    # Si empieza por #, interpretamos los números directamente como IDs internos
    if [[ $expr == \#* ]]; then
        expr="${expr//#/}"
        local max_id
        max_id=$(awk -F'\t' 'BEGIN { m = 0 } $1 > m { m = $1 } END { print m }' "$COLA")
        parsear_seleccion "$expr" "$max_id" || return 1
        local n
        for n in "${SELECCION[@]}"; do
            [[ -n ${existe_id[$n]:-} ]] && IDS_RESUELTOS+=("$n")
        done
        ((${#IDS_RESUELTOS[@]} > 0))
        return
    fi

    # Por defecto interpreta los números como número de fila # (1..total_filas)
    if parsear_seleccion "$expr" "$total_filas" && ((${#SELECCION[@]} > 0)); then
        local pos
        for pos in "${SELECCION[@]}"; do
            IDS_RESUELTOS+=("${mapa_pos[pos - 1]}")
        done
        return 0
    fi

    # Fallback: si escribió un ID alto (ej: 105 cuando hay 10 filas), comprobar si existe como ID
    if [[ $expr =~ ^[0-9]+$ ]] && [[ -n ${existe_id[$expr]:-} ]]; then
        IDS_RESUELTOS+=("$expr")
        return 0
    fi
    return 1
}

# --------------------------------------------------------------------------
# Rutas locales
# --------------------------------------------------------------------------

ruta_absoluta() {  # ruta_absoluta RUTA (admite ~ y rutas que aún no existen)
    local r="${1/#\~/$HOME}"
    [[ -z $r ]] && return 1
    readlink -m -- "$r"
}

# Carpeta existente más cercana (la propia ruta o su antecesor más próximo)
dir_existente() {  # dir_existente RUTA
    local d="$1"
    while [[ ! -d $d && $d != / && -n $d ]]; do d="$(dirname -- "$d")"; done
    printf '%s' "${d:-/}"
}

espacio_libre() {  # espacio_libre RUTA -> bytes libres en el sistema de ficheros de RUTA
    df -P -B1 --output=avail -- "$(dir_existente "$1")" 2>/dev/null | awk 'NR == 2 { print $1 }'
}

# ¿origen y carpeta destino están en el mismo sistema de ficheros? (mv instantáneo)
# MOVER_FORZAR_RSYNC=1 fuerza la ruta lenta (rsync + borrado), útil para pruebas.
mismo_fs() {  # mismo_fs origen carpeta_destino
    [[ ${MOVER_FORZAR_RSYNC:-0} == 1 ]] && return 1
    local a b
    a=$(stat -c %d -- "$1" 2>/dev/null) || return 1
    b=$(stat -c %d -- "$(dir_existente "$2")" 2>/dev/null) || return 1
    [[ $a == "$b" ]]
}

# "a/peli.mkv" -> "a/peli (1).mkv", "a/peli (2).mkv"... el primero que no exista
nombre_libre() {  # nombre_libre RUTA
    local ruta="$1" dir nom base ext n=1 cand
    dir="${ruta%/*}"; nom="${ruta##*/}"
    if [[ $nom == ?*.* ]]; then base="${nom%.*}"; ext=".${nom##*.}"; else base="$nom"; ext=""; fi
    while :; do
        cand="$dir/$base ($n)$ext"
        [[ -e $cand || -L $cand || -e $dir/$PARCIAL/${cand##*/} ]] || { printf '%s' "$cand"; return; }
        ((n++))
    done
}

ruta_parcial() {  # ruta_parcial destino -> <carpeta destino>/.mover-partial/<nombre>
    printf '%s/%s/%s' "${1%/*}" "$PARCIAL" "${1##*/}"
}

# Destino automático para lo que se añada desde la carpeta local DIR
destino_auto() {  # destino_auto DIR
    local dir="$1" rel
    if [[ $ESTRUCTURA == s && ( $dir == "$CARPETA_ORIGEN" || $dir == "$CARPETA_ORIGEN"/* ) ]]; then
        rel="${dir#"$CARPETA_ORIGEN"}"
        printf '%s%s' "${CARPETA_DESTINO%/}" "$rel"
    else
        printf '%s' "$CARPETA_DESTINO"
    fi
}

# Elimina subcarpetas que hayan quedado vacías en el origen tras mover, subiendo desde
# la carpeta del fichero hasta RAIZ (incluida). Solo rmdir: nunca borra nada que no esté vacío.
limpiar_dirs_origen() {  # limpiar_dirs_origen fichero_origen raiz
    local d raiz="${2%/}"
    [[ -z $raiz || $raiz == - || $raiz == / ]] && return 0
    d="$(dirname -- "$1")"
    while [[ $d == "$raiz" || $d == "$raiz"/* ]]; do
        rmdir -- "$d" 2>/dev/null || break
        log "Carpeta vacía eliminada del origen: $d"
        [[ $d == "$raiz" ]] && break
        d="$(dirname -- "$d")"
    done
}

# Al terminar el último elemento de una carpeta movida con "r N": las subcarpetas que ya
# estaban vacías en el origen se recrean en el destino y se eliminan del origen (rmdir).
cerrar_raiz() {  # cerrar_raiz id origen destino_final raiz
    local id="$1" origen="$2" destino="$3" raiz="${4%/}" rel dest_raiz d
    [[ -z $raiz || $raiz == - || $raiz == / || ! -d $raiz ]] && return 0
    [[ $origen == "$raiz"/* ]] || return 0
    # ¿Quedan otros elementos de esta carpeta sin terminar?
    R="$raiz" awk -F'\t' -v id="$id" '$1 != id && $11 == ENVIRON["R"] && $2 != "completado" { e = 1 } END { exit !e }' "$COLA" && return 0
    rel="${origen#"$raiz"/}"
    [[ $destino == */"$rel" ]] || return 0
    dest_raiz="${destino%/"$rel"}"
    while IFS= read -r -d '' d; do
        [[ $d == "$raiz" ]] && continue
        mkdir -p -- "$dest_raiz/${d#"$raiz"/}" 2>/dev/null || continue
        if rmdir -- "$d" 2>/dev/null; then
            log "Carpeta vacía trasladada: $d -> $dest_raiz/${d#"$raiz"/}"
            limpiar_dirs_origen "$d" "$raiz"
        fi
    done < <(find "$raiz" -depth -mindepth 1 -type d -empty -print0 2>/dev/null)
    limpiar_dirs_origen "$raiz/." "$raiz"
}

# --------------------------------------------------------------------------
# Configuración
# --------------------------------------------------------------------------

cargar_conf() {
    # shellcheck source=/dev/null
    [[ -f $CONF ]] && source "$CONF"
    [[ -n $CARPETA_ORIGEN ]] && CARPETA_ORIGEN="${CARPETA_ORIGEN%/}"
    [[ -z $CARPETA_ORIGEN ]] && CARPETA_ORIGEN="/"
    [[ $CARPETA_DESTINO != / ]] && CARPETA_DESTINO="${CARPETA_DESTINO%/}"
    [[ $LIMITE_KBPS =~ ^[0-9]+$ ]] || LIMITE_KBPS=0
    [[ $REINTENTOS =~ ^[0-9]+$ ]] || REINTENTOS=3
    [[ $ESPERA_REINTENTO =~ ^[0-9]+$ ]] || ESPERA_REINTENTO=10
    [[ $OPERACION == mover ]] || OPERACION=copiar
    [[ $VERIFICAR =~ ^(tamano|checksum|no)$ ]] || VERIFICAR=tamano
    [[ $CONFLICTO =~ ^(omitir|sobrescribir|renombrar)$ ]] || CONFLICTO=omitir
    return 0
}

guardar_conf() {
    local k f
    (
        umask 077
        {
            echo "# Configuración de mover.sh (se puede editar desde el menú, opción 6)"
            for k in "${CAMPOS[@]}"; do printf '%s=%q\n' "$k" "${!k}"; done
            printf 'FAVORITOS=('
            for f in "${FAVORITOS[@]}"; do printf ' %q' "$f"; done
            printf ' )\n'
        } > "$CONF"
    )
    chmod 600 "$CONF"
}

conf_completa() {
    [[ -d $CARPETA_ORIGEN && -n $CARPETA_DESTINO ]]
}

texto_verificar() {
    case "$VERIFICAR" in
        checksum) echo "checksum (compara el contenido byte a byte)" ;;
        no) echo "no (al mover se comprueba igualmente el tamaño)" ;;
        *) echo "tamano (compara el tamaño)" ;;
    esac
}

mostrar_conf() {
    local libre
    echo
    caja_cabecera "Configuración actual" 76
    printf '  %-30s %s\n' "Carpeta origen (explorar)" "$CARPETA_ORIGEN" \
        "Carpeta destino por defecto" "$CARPETA_DESTINO" \
        "Mantener estructura carpetas" "$ESTRUCTURA" \
        "Operación por defecto" "$OPERACION" \
        "Límite (KB/s, 0=sin límite)" "$LIMITE_KBPS" \
        "Reintentos por fichero" "$REINTENTOS" \
        "Espera 1er reintento (s)" "$ESPERA_REINTENTO" \
        "Verificar al terminar" "$(texto_verificar)" \
        "Si el destino ya existe" "$CONFLICTO"
    if ((${#FAVORITOS[@]})); then
        local i
        echo "  Destinos favoritos:"
        for i in "${!FAVORITOS[@]}"; do printf '    f%-3d %s\n' $((i + 1)) "${FAVORITOS[i]}"; done
    else
        echo "  Destinos favoritos:            (ninguno)"
    fi
    if [[ -n $CARPETA_DESTINO ]]; then
        libre=$(espacio_libre "$CARPETA_DESTINO")
        [[ $libre =~ ^[0-9]+$ ]] && printf '  %-30s %s (en %s)\n' "Espacio libre en destino" "$(tam "$libre")" "$(dir_existente "$CARPETA_DESTINO")"
    fi
}

editar_favoritos() {
    local op n r i
    while :; do
        echo
        echo "  Destinos favoritos (atajos f1, f2... en el explorador y al elegir destino):"
        ((${#FAVORITOS[@]} == 0)) && echo "    (ninguno)"
        for i in "${!FAVORITOS[@]}"; do printf '    f%-3d %s\n' $((i + 1)) "${FAVORITOS[i]}"; done
        echo "  [a RUTA] añadir  ·  [b N] borrar  ·  [Intro] terminar"
        read -rep "  Favoritos: " op || op=""
        case "$op" in
            "") return ;;
            "a "*)
                r=$(ruta_absoluta "${op#a }") || continue
                FAVORITOS+=("$r")
                [[ -d $r ]] || echo "    Aviso: $r no existe todavía (se creará al usarla)." ;;
            "b "*)
                n="${op#b }"; n="${n#f}"
                if [[ $n =~ ^[0-9]+$ ]] && ((n >= 1 && n <= ${#FAVORITOS[@]})); then
                    unset 'FAVORITOS[n - 1]'
                    FAVORITOS=("${FAVORITOS[@]}")
                else
                    echo "    Número no válido."
                fi ;;
            *) echo "    Opción no válida." ;;
        esac
    done
}

asistente_conf() {
    echo
    caja_cabecera "Asistente de Configuración" 76
    echo "  (Pulsa Intro para mantener el valor actual entre corchetes)"
    echo
    while :; do
        preguntar "Carpeta de origen a explorar" "$CARPETA_ORIGEN"
        RESP="${RESP/#\~/$HOME}"
        [[ -d $RESP ]] && break
        echo "    La carpeta $RESP no existe."
    done
    CARPETA_ORIGEN="$(readlink -f -- "$RESP")"
    while :; do
        preguntar "Carpeta de destino por defecto" "$CARPETA_DESTINO"
        [[ -n $RESP ]] && break
        echo "    El destino es obligatorio."
    done
    CARPETA_DESTINO="$(ruta_absoluta "$RESP")"
    [[ -d $CARPETA_DESTINO ]] || echo "    Aviso: $CARPETA_DESTINO no existe todavía; se creará al procesar."
    echo "    Mantener estructura: s = recrear en el destino las subcarpetas del origen;"
    echo "                         n = dejar todo en la carpeta destino"
    while :; do preguntar "Mantener estructura de carpetas (s/n)" "$ESTRUCTURA"; [[ $RESP =~ ^[sSnN]$ ]] && break; echo "    Responde s o n."; done
    ESTRUCTURA="${RESP,,}"
    while :; do preguntar "Operación por defecto (copiar/mover)" "$OPERACION"; [[ ${RESP,,} =~ ^(c|copiar|m|mover)$ ]] && break; echo "    Responde copiar o mover."; done
    [[ ${RESP,,} == m* ]] && OPERACION=mover || OPERACION=copiar
    while :; do preguntar "Límite de velocidad en KB/s (0 = sin límite)" "$LIMITE_KBPS"; [[ $RESP =~ ^[0-9]+$ ]] && break; echo "    Debe ser un número."; done
    LIMITE_KBPS="$RESP"
    while :; do preguntar "Reintentos automáticos si falla la copia (0 = ninguno)" "$REINTENTOS"; [[ $RESP =~ ^[0-9]+$ ]] && break; echo "    Debe ser un número."; done
    REINTENTOS="$RESP"
    while :; do preguntar "Segundos de espera antes del 1er reintento" "$ESPERA_REINTENTO"; [[ $RESP =~ ^[0-9]+$ ]] && break; echo "    Debe ser un número."; done
    ESPERA_REINTENTO="$RESP"
    echo "    Verificar: tamano   = comparar tamaño al terminar (rápido);"
    echo "               checksum = comparar el contenido completo (lento, más seguro);"
    echo "               no       = no comprobar (al mover se comprueba el tamaño igualmente)"
    while :; do preguntar "Verificación (tamano/checksum/no)" "$VERIFICAR"; [[ $RESP =~ ^(tamano|checksum|no)$ ]] && break; echo "    Responde tamano, checksum o no."; done
    VERIFICAR="$RESP"
    echo "    Si el fichero ya existe en el destino:"
    echo "      omitir       = no tocarlo (OK si tiene el mismo tamaño, error si es distinto)"
    echo "      sobrescribir = reemplazarlo"
    echo "      renombrar    = guardar como 'nombre (1).ext', 'nombre (2).ext'..."
    while :; do preguntar "Política de conflicto (omitir/sobrescribir/renombrar)" "$CONFLICTO"; [[ $RESP =~ ^(omitir|sobrescribir|renombrar)$ ]] && break; echo "    Responde omitir, sobrescribir o renombrar."; done
    CONFLICTO="$RESP"
    if si_no "¿Editar los destinos favoritos (${#FAVORITOS[@]} ahora)?" "$( ((${#FAVORITOS[@]})) && echo n || echo s)"; then
        editar_favoritos
    fi

    guardar_conf
    log "Configuración guardada (origen=$CARPETA_ORIGEN destino=$CARPETA_DESTINO estructura=$ESTRUCTURA operacion=$OPERACION limite=${LIMITE_KBPS}KB/s reintentos=$REINTENTOS espera=${ESPERA_REINTENTO}s verificar=$VERIFICAR conflicto=$CONFLICTO favoritos=${#FAVORITOS[@]})"
    echo
    printf '  %s✓ Configuración guardada en %s%s\n' "$C_GREEN" "$CONF" "$C_RESET"
}

# --------------------------------------------------------------------------
# Elegir carpeta de destino (local)
# --------------------------------------------------------------------------

linea_favoritos() {
    local i s=""
    ((${#FAVORITOS[@]})) || return 0
    for i in "${!FAVORITOS[@]}"; do
        s+="${s:+  ·  }${C_BOLD}f$((i + 1))${C_RESET} ${FAVORITOS[i]##*/}"
    done
    printf '   %sFavoritos:%s %s\n' "$C_BOLD" "$C_RESET" "$s"
}

favorito() {  # favorito "fN" -> ruta (código 1 si no existe ese favorito)
    local n="${1#f}"
    [[ $n =~ ^[0-9]+$ ]] && ((n >= 1 && n <= ${#FAVORITOS[@]})) || return 1
    printf '%s' "${FAVORITOS[n - 1]}"
}

elegir_carpeta_destino() {  # elegir_carpeta_destino /carpeta/inicial [permitir_automatico]
    local actual automatico="${2:-}" op n i p libre
    actual="$(ruta_absoluta "${1:-/}")" || actual="/"
    local -a carpetas=()
    while :; do
        clear 2>/dev/null
        caja_cabecera "Carpeta de destino: $actual" 84
        linea_favoritos
        carpetas=()
        if [[ -d $actual ]]; then
            mapfile -d '' carpetas < <(find "$actual" -mindepth 1 -maxdepth 1 \( -type d -o -xtype d \) ! -name '.*' -print0 2>/dev/null | sort -z)
            libre=$(espacio_libre "$actual")
            [[ $libre =~ ^[0-9]+$ ]] && printf '   %sEspacio libre:%s %s\n' "$C_BOLD" "$C_RESET" "$(tam "$libre")"
            echo
            ((${#carpetas[@]} == 0)) && echo "  (sin subcarpetas)"
            for i in "${!carpetas[@]}"; do
                p="${carpetas[i]}"
                printf '  %4d) %s[DIR ]%s %s%s/%s\n' $((i + 1)) "$C_BOLD$C_BLUE" "$C_RESET" "$C_BOLD$C_BLUE" "${p##*/}" "$C_RESET"
            done
        else
            echo
            echo "  Esta carpeta no existe todavía: se creará al procesar el primer fichero."
        fi
        echo
        echo "  Opciones:"
        echo "    N          entrar en la carpeta N          ..       carpeta superior"
        echo "    u          USAR esta carpeta como destino  v        cancelar"
        echo "    n NOMBRE   nueva subcarpeta (se crea al    e RUTA   escribir ruta (ej: e /ruta/x)  "
        echo "               procesar)                       f1, f2.. ir a un destino favorito"
        [[ -n $automatico ]] && echo "    a          volver a destino automático"
        echo
        read -rep "  Opción: " op || op="v"
        case "$op" in
            v | q | "") return 1 ;;
            u)
                [[ -d $actual ]] || echo "  Nota: $actual aún no existe; se creará al procesar."
                ELEGIDA="$actual"; return 0 ;;
            a) [[ -n $automatico ]] && return 2 ;;
            ..) actual="$(dirname -- "$actual")" ;;
            f[0-9]*)
                if p=$(favorito "$op"); then actual="$p"; else echo "  No existe el favorito $op."; pausa; fi ;;
            "n "* | "e "*)
                n="${op:2}"; n="${n#"${n%%[! ]*}"}"
                [[ $n != / ]] && n="${n%/}"
                [[ -z $n ]] && continue
                if [[ $op == e* ]]; then
                    if [[ $n == /* || $n == \~* ]]; then
                        actual="$(ruta_absoluta "$n")"
                    else
                        actual="$(ruta_absoluta "$actual/$n")"
                    fi
                elif [[ $n == */* || $n == . || $n == .. ]]; then
                    echo "  El nombre no puede contener '/'. Usa 'e RUTA' para rutas completas."; pausa
                else
                    actual="${actual%/}/$n"
                fi ;;
            *)
                if [[ $op =~ ^[0-9]+$ ]] && ((op >= 1 && op <= ${#carpetas[@]})); then
                    actual="${carpetas[op - 1]}"
                else
                    echo "  Opción no válida."; pausa
                fi ;;
        esac
        [[ -z $actual ]] && actual="/"
    done
}

# --------------------------------------------------------------------------
# Cola (cola.txt protegido con flock, compartido entre menú y worker)
# --------------------------------------------------------------------------

con_bloqueo() {  # con_bloqueo comando args...
    (
        flock 9
        touch "$COLA"
        "$@"
    ) 9> "$LOCK"
}

# Reescribe cola.txt con la salida de "comando args..." (que lee la cola por stdin).
# Llamar siempre bajo con_bloqueo.
_reescribir_cola() {  # _reescribir_cola comando args...
    "$@" < "$COLA" > "$COLA.tmp" && mv -f "$COLA.tmp" "$COLA" && return 0
    rm -f "$COLA.tmp"
    return 1
}

# Los textos se pasan a awk por ENVIRON: con -v, awk interpretaría las barras invertidas.
_cola_set() {  # _cola_set id estado reanudar [mensaje]
    local fin="-"
    [[ $2 == completado || $2 == error ]] && fin="$(date +%FT%T)"
    MSG_COLA="${4:--}" _reescribir_cola awk -F'\t' -v OFS='\t' -v id="$1" -v e="$2" -v r="$3" -v f="$fin" \
        '$1 == id { $2 = e; $3 = r; $8 = ENVIRON["MSG_COLA"]; if (f != "-") $5 = f } { print }'
}
cola_set() { con_bloqueo _cola_set "$@"; }

_cola_set_firma() {  # _cola_set_firma id tamaño firma
    _reescribir_cola awk -F'\t' -v OFS='\t' -v id="$1" -v t="$2" -v fl="$3" \
        '$1 == id { $4 = t; $9 = fl } { print }'
}

_cola_set_destino() {  # _cola_set_destino id nuevo_destino
    DESTINO="$2" _reescribir_cola awk -F'\t' -v OFS='\t' -v id="$1" \
        '$1 == id { $7 = ENVIRON["DESTINO"] } { print }'
}

firma_local() {  # firma_local fichero -> "tamaño:mtime"
    stat -L -c '%s:%Y' -- "$1" 2>/dev/null
}

_tomar_siguiente() {
    local linea
    linea=$(awk -F'\t' '$2 == "pendiente" { print; exit }' "$COLA")
    [[ -z $linea ]] && return 1
    _cola_set "${linea%%$'\t'*}" procesando "$(cut -f3 <<< "$linea")" -
    printf '%s\n' "$linea"
}

# Añade ficheros al final de la cola (en el orden recibido; no se reordena lo existente).
#   op      copiar | mover
#   raiz    carpeta añadida con "r N" (o "-"): al mover, sus subcarpetas vacías se eliminan
#   destino carpeta destino
#   base    carpeta local de referencia: el destino es destino/<ruta relativa a base>
_anadir() {  # _anadir op raiz destino base ficheros...
    local op="$1" raiz="${2:--}" destino="${3%/}" base="${4%/}" f rel dest id n=0 omitidos_ok=0 total_args tamano
    shift 4
    total_args=$#
    id=$(awk -F'\t' 'BEGIN { m = 0 } $1 > m { m = $1 } END { print m + 1 }' "$COLA")
    for f in "$@"; do
        if [[ $f == *$'\t'* || $f == *$'\n'* || $destino == *$'\t'* || $destino == *$'\n'* ]]; then
            echo "  Omitido (nombre con tabuladores o saltos de línea): $f"
            continue
        fi
        if L="$f" awk -F'\t' '$6 == ENVIRON["L"] && ($2 == "pendiente" || $2 == "procesando" || $2 == "pausado") { e = 1 } END { exit !e }' "$COLA"; then
            echo "  Ya está en la cola: ${f#"$CARPETA_ORIGEN"/}"
            continue
        fi
        if ((total_args > 1)) && L="$f" awk -F'\t' '$6 == ENVIRON["L"] && $2 == "completado" { e = 1 } END { exit !e }' "$COLA"; then
            ((omitidos_ok++))
            continue
        fi
        if [[ $f == "$base"/* ]]; then rel="${f#"$base"/}"; else rel="${f##*/}"; fi
        dest="$destino/$rel"
        if [[ $dest == "$f" ]]; then
            echo "  Omitido: el destino es el propio fichero de origen ($f)"
            continue
        fi
        if R="$dest" awk -F'\t' '$7 == ENVIRON["R"] && ($2 == "pendiente" || $2 == "procesando" || $2 == "pausado") { e = 1 } END { exit !e }' "$COLA"; then
            echo "  Omitido: otro elemento de la cola ya va a $dest (${f#"$CARPETA_ORIGEN"/})"
            log "No añadido $f: el destino $dest ya lo usa otro elemento de la cola"
            continue
        fi
        tamano=$(stat -L -c %s -- "$f" 2>/dev/null) || { echo "  Omitido (no se puede leer): $f"; continue; }
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
            "$id" pendiente 0 "$tamano" - "$f" "$dest" - "$(firma_local "$f")" "$op" "$raiz" >> "$COLA"
        log "Añadido a la cola #$id ($op): $f -> $dest ($(tam "$tamano"))"
        ((id++, n++))
    done
    local nota=""
    ((omitidos_ok > 0)) && nota=" [$omitidos_ok omitido(s) por estar ya completados]"
    if ((n > 0)); then
        echo "  $n fichero(s) añadido(s) al final de la cola para $([[ $op == mover ]] && echo MOVER || echo COPIAR).$nota"
    else
        echo "  0 ficheros añadidos.$nota"
    fi
}
anadir_a_cola() { con_bloqueo _anadir "$@"; }

# Renderizador unificado de tablas para cola completa, pendientes y vista de progreso
# Modos: "todos" | "pendientes" | "progreso"
imprimir_tabla_cola() {  # imprimir_tabla_cola [modo] [filtro_texto] [max_filas]
    local modo="${1:-todos}" filtro="${2:-}" max_filas="${3:-0}"
    touch "$COLA"

    if [[ ! -s $COLA ]]; then
        echo "  (la cola está vacía)"
        return
    fi
    if [[ $modo == "pendientes" ]] && ! grep -qE $'\t(pendiente|procesando|pausado)\t' "$COLA" 2>/dev/null; then
        echo "  (no hay elementos pendientes ni en curso)"
        return
    fi

    local w_fich w_tot
    w_fich=$(ancho_columna_fichero)
    w_tot=$((w_fich + 48))

    if [[ -n $filtro ]]; then
        printf '  %sFiltro activo: "%s"%s (escribe %s*%s o %s/%s para ver todos)\n' \
            "$C_BOLD$C_CYAN" "$filtro" "$C_RESET" "$C_BOLD" "$C_RESET" "$C_BOLD" "$C_RESET"
    fi

    printf "  %s%-3s %-5s %-6s %-6s %-${w_fich}s %10s  %s%s\n" \
        "$C_BOLD" "#" "ID" "ESTADO" "OP" "FICHERO" "TAMAÑO" "DESTINO" "$C_RESET"
    separador_tabla "$w_tot"

    local id estado reanudar tamano origen_f destino_f mensaje op pos=0 mostrados=0 total_bytes=0
    local nom nom_completo dest filtro_low="${filtro,,}"
    while IFS=$'\t' read -r id estado reanudar tamano _ origen_f destino_f mensaje _ op _; do
        ((pos++))

        if [[ $modo == "pendientes" ]]; then
            [[ $estado == pendiente || $estado == procesando || $estado == pausado ]] || continue
        elif [[ $modo == "progreso" ]]; then
            [[ $estado == pendiente || $estado == pausado ]] || continue
        fi

        info_estado_cola "$estado" "$reanudar"
        nom_completo="${origen_f##*/}"
        dest="${destino_f%/*}"
        dest="${dest:-/}"

        if [[ -n $filtro_low ]]; then
            local texto_busq="$pos #$id $estado $ETIQ_ESTADO ${op:-copiar} $nom_completo $dest $mensaje"
            [[ ${texto_busq,,} == *"$filtro_low"* ]] || continue
        fi

        ((mostrados++))
        ((total_bytes += tamano))
        if ((max_filas > 0 && mostrados > max_filas)); then
            continue
        fi

        nom="$(acortar_nombre "$nom_completo" "$w_fich")"
        local pad_nom=$((w_fich - ${#nom}))
        ((pad_nom < 0)) && pad_nom=0

        printf "  %2d  #%-4s %s[%s]%s %s %s%*s %10s  %s\n" \
            "$pos" "$id" "$COLOR_ESTADO" "$ETIQ_ESTADO" "$C_RESET" "$(insignia_op "${op:-copiar}")" "$nom" "$pad_nom" "" "$(tam "$tamano")" "$dest"

        if [[ $modo != "progreso" && -n $mensaje && $mensaje != - ]]; then
            if [[ $estado == error ]]; then
                printf '       %s└─> Fallo: %s%s\n' "$C_RED" "$mensaje" "$C_RESET"
            else
                printf '       %s└─> %s%s\n' "$C_DIM" "$mensaje" "$C_RESET"
            fi
        fi
    done < "$COLA"

    if [[ -n $filtro && $mostrados -eq 0 ]]; then
        printf '  %s(ningún elemento coincide con la búsqueda "%s")%s\n' "$C_DIM" "$filtro" "$C_RESET"
    fi

    separador_tabla "$w_tot"

    if ((max_filas > 0 && mostrados > max_filas)); then
        printf '  %s... y %d elemento(s) más en cola (ver todos en opción 2)%s\n' \
            "$C_DIM" $((mostrados - max_filas)) "$C_RESET"
    elif [[ $modo == "pendientes" ]]; then
        echo "  Total pendientes / en curso: $mostrados fichero(s), $(tam "$total_bytes")"
    elif [[ -n $filtro ]]; then
        printf '  Mostrando %d de %d elemento(s) en la cola (%s).\n' "$mostrados" "$pos" "$(tam "$total_bytes")"
    fi
}

imprimir_cola() {
    imprimir_tabla_cola "todos" "${1:-}"
}

imprimir_pendientes() {
    imprimir_tabla_cola "pendientes" "${1:-}"
    local ult_vel="" ult_pct=0
    if leer_progreso; then
        ult_vel="${PRG_VEL:-}"
        ult_pct="${PRG_PCT:-0}"
    fi
    calcular_tiempo_cola "$ult_vel" "$ult_pct"
    if [[ -n $COLA_TIEMPO_EST ]]; then
        local origen_vel=""
        if [[ -n $ult_vel && $ult_vel != "?" ]]; then
            origen_vel="a $ult_vel"
        elif ((LIMITE_KBPS > 0)); then
            origen_vel="con límite de ${LIMITE_KBPS} KB/s"
        fi
        printf '  Tiempo restante estimado de la cola: %s%s%s%s\n' \
            "$C_BOLD$C_GREEN" "$COLA_TIEMPO_EST" "$C_RESET" "${origen_vel:+ ($origen_vel)}"
    fi
}

resumen_estado() {
    touch "$COLA"
    local cuentas pid
    cuentas=$(awk -F'\t' '{ c[$2]++ } END { s = ""; for (k in c) s = s (s ? ", " : "") c[k] " " k; print s }' "$COLA")
    if pid=$(worker_activo); then pid="${C_GREEN}ACTIVO (PID $pid)${C_RESET}"; else pid="${C_DIM}DETENIDO${C_RESET}"; fi
    printf '  Cola: %s | Proceso en 2º plano: %s\n' "${cuentas:-vacía}" "$pid"
}

panel_resumen_menu() {
    touch "$COLA"
    local pid estado_proceso w_panel=84
    if pid=$(worker_activo); then
        estado_proceso="${C_BOLD}${C_GREEN}● ACTIVO (PID $pid)${C_RESET}"
    else
        estado_proceso="${C_YELLOW}○ DETENIDO${C_RESET}"
    fi

    local datos
    datos=$(awk -F'\t' '
        BEGIN { pend_c=0; pend_b=0; paus_c=0; err_c=0; ok_c=0; ok_b=0; sub_nom=""; sub_b=0; sub_op="" }
        $2 == "pendiente" { pend_c++; pend_b += $4 }
        $2 == "pausado"   { paus_c++ }
        $2 == "error"     { err_c++ }
        $2 == "completado"{ ok_c++; ok_b += $4 }
        $2 == "procesando" {
            n = split($6, p, "/")
            sub_nom = p[n]
            sub_b = $4
            sub_op = $10
        }
        END {
            gsub(/\|/, "/", sub_nom)
            printf "%d|%s|%d|%d|%d|%s|%s|%s|%s", pend_c, pend_b, paus_c, err_c, ok_c, ok_b, sub_b, sub_op, sub_nom
        }
    ' "$COLA" 2>/dev/null)

    local pend_c pend_b paus_c err_c ok_c ok_b sub_nom sub_b sub_op
    IFS='|' read -r pend_c pend_b paus_c err_c ok_c ok_b sub_b sub_op sub_nom <<< "$datos"
    pend_c=${pend_c:-0}; pend_b=${pend_b:-0}; paus_c=${paus_c:-0}; err_c=${err_c:-0}; ok_c=${ok_c:-0}; ok_b=${ok_b:-0}

    caja_cabecera "mover · Copiar y mover ficheros en segundo plano" "$w_panel"
    printf '   %sOrigen:%s %s   %sDestino:%s %s\n' \
        "$C_BOLD" "$C_RESET" "$(acortar_nombre "$CARPETA_ORIGEN" 30)" "$C_BOLD" "$C_RESET" "$(acortar_nombre "$CARPETA_DESTINO" 30)"
    printf '   %sPor defecto:%s %s   %sLímite:%s %s   %sConflicto:%s %s   %sWorker:%s %s\n' \
        "$C_BOLD" "$C_RESET" "$(texto_op "$OPERACION")" \
        "$C_BOLD" "$C_RESET" "$([[ $LIMITE_KBPS == 0 ]] && echo 'Sin límite' || echo "${LIMITE_KBPS} KB/s")" \
        "$C_BOLD" "$C_RESET" "$CONFLICTO" \
        "$C_BOLD" "$C_RESET" "$estado_proceso"
    separador_tabla "$((w_panel + 2))"

    local ult_vel="" ult_pct=0
    if [[ -n ${sub_nom:-} ]]; then
        printf '   %s>> EN CURSO:%s %s %s%s%s (%s)\n' \
            "$C_BOLD$C_BLUE" "$C_RESET" "$(insignia_op "${sub_op:-copiar}")" "$C_BOLD" "$(acortar_nombre "$sub_nom" 56)" "$C_RESET" "$(tam "$sub_b")"
        if leer_progreso; then
            ult_vel="${PRG_VEL:-}"
            ult_pct="${PRG_PCT:-0}"
            printf '      Archivo : %s %s / %s | %s | Falta: %s\n' \
                "$(barra_progreso "$PRG_PCT" 22 "$C_GREEN")" "${PRG_ENVIADO:-0}" "$(tam "$sub_b")" "${PRG_VEL:-?}" "${PRG_T_RESTANTE:-?}"
        else
            printf '      Archivo : %s (preparando...)\n' "$(barra_progreso 0 22 "$C_GREEN")"
        fi
    fi

    calcular_tiempo_cola "$ult_vel" "$ult_pct"

    if ((COLA_BYTES_TOT > 0)); then
        printf '      Global  : %s (%s de %s)%s\n' \
            "$(barra_progreso "$COLA_PCT_GLOBAL" 22 "$C_CYAN")" \
            "$(tam "$COLA_BYTES_HECHOS")" "$(tam "$COLA_BYTES_TOT")" \
            "${COLA_TIEMPO_EST:+ | Cola: ~${C_BOLD}${C_GREEN}$COLA_TIEMPO_EST${C_RESET}}"
        separador_tabla "$((w_panel + 2))"
    fi

    printf '   %sCola:%s %s%d%s pendientes (%s)  ·  %s%d%s completados (%s)' \
        "$C_BOLD" "$C_RESET" \
        "$C_YELLOW" "$pend_c" "$C_RESET" "$(tam "$pend_b")" \
        "$C_GREEN" "$ok_c" "$C_RESET" "$(tam "$ok_b")"
    ((paus_c > 0)) && printf '  ·  %s%d en pausa%s' "$C_MAGENTA" "$paus_c" "$C_RESET"
    ((err_c > 0))  && printf '  ·  %s%d con error%s' "$C_RED" "$err_c" "$C_RESET"
    echo
    separador_tabla "$((w_panel + 2))"
}

# --------------------------------------------------------------------------
# Worker (procesamiento en segundo plano)
# --------------------------------------------------------------------------

worker_activo() {
    local pid
    pid=$(cat "$PIDF" 2>/dev/null) || return 1
    [[ $pid =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null \
        && tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q -- '--worker' \
        && echo "$pid"
}

lanzar_worker() {
    local pid
    if pid=$(worker_activo); then
        echo "  El proceso en segundo plano ya está en marcha (PID $pid)."
        return
    fi
    recuperar_huerfanos
    if ! grep -q $'\tpendiente\t' "$COLA" 2>/dev/null; then
        if grep -q $'\tprocesando\t' "$COLA" 2>/dev/null; then
            echo "  Una copia de un proceso anterior todavía está terminando; inténtalo de nuevo en unos segundos."
        else
            echo "  No hay elementos pendientes en la cola."
        fi
        return
    fi
    setsid nohup "$SCRIPT" --worker < /dev/null > /dev/null 2>&1 &
    log "Lanzado el proceso en segundo plano (PID $!)"
    printf '  %s▶ Proceso en segundo plano iniciado (PID %s).%s Puedes cerrar este programa.\n' "$C_GREEN" "$!" "$C_RESET"
}

detener_worker() {
    local pid
    if ! pid=$(worker_activo); then
        echo "  No hay ningún proceso en segundo plano activo."
        return
    fi
    kill -TERM "$pid"
    log "Solicitada parada del proceso en segundo plano (PID $pid)"
    printf '  %s⏹ Señal de parada enviada al PID %s.%s El fichero en curso se reanudará al volver a iniciar.\n' "$C_YELLOW" "$pid" "$C_RESET"
}

_recuperar_procesando() {
    _reescribir_cola awk -F'\t' -v OFS='\t' '$2 == "procesando" { $2 = "pendiente"; $3 = 1 } { print }'
}

# Filas "procesando" que dejó un worker muerto sin limpiar (kill -9, OOM, apagado):
# si nadie tiene worker.lock, ningún worker vivo las está procesando -> vuelven a pendiente.
recuperar_huerfanos() {
    (
        flock -n 8 || exit 0
        grep -q $'\tprocesando\t' "$COLA" 2>/dev/null || exit 0
        con_bloqueo _recuperar_procesando
        log "Recuperadas filas 'procesando' huérfanas de un proceso interrumpido"
    ) 8> "$WLOCK"
}

# Códigos de rsync que no se arreglan reintentando: sintaxis, protocolo, selección de
# ficheros, E/S (p. ej. disco lleno), permisos, el origen desapareció...
ERRORES_DEFINITIVOS=" 1 2 3 4 6 11 13 14 23 24 "

esperar() {
    local i
    for ((i = 0; i < $1 && !PARAR; i++)); do sleep 1; done
}

# Copia ORIGEN sobre el fichero temporal TMP (nunca sobre el nombre final). Si TMP ya
# tiene un trozo de una copia anterior, rsync continúa desde ahí (--append-verify
# comprueba al final que todo el fichero coincide y, si no, lo vuelve a copiar entero).
# rsync hereda worker.lock (fd 8): si el worker muere de golpe, no puede arrancar otro
# que escriba en el mismo temporal mientras ese rsync siga vivo.
rsync_una_vez() {  # rsync_una_vez origen tmp
    local -a args=(--inplace --append-verify -t -L --info=progress2 --outbuf=N)
    local ahora
    ((LIMITE_KBPS > 0)) && args+=(--bwlimit="$LIMITE_KBPS")
    : > "$PROG"
    printf -v ahora '%(%s)T' -1
    echo "$ahora" > "$PROG.w"
    LC_ALL=C rsync "${args[@]}" -- "$1" "$2" > "$PROG" 2> "$PROG.err" &
    HIJO_PID=$!
    # Si --parar llegó antes de conocer el PID, la trap no pudo matar a rsync
    ((PARAR)) && kill -TERM "$HIJO_PID" 2>/dev/null
    wait "$HIJO_PID"; RC=$?
    while kill -0 "$HIJO_PID" 2>/dev/null; do wait "$HIJO_PID"; RC=$?; done
    HIJO_PID=""
    MSG=""
    if ((RC != 0)); then
        MSG=$(grep -v '^ *$' "$PROG.err" 2>/dev/null | grep -v 'received SIGUSR1' | tail -1)
        MSG="${MSG:-rsync terminó con código $RC}"
        MSG="${MSG//$'\t'/ }"
    fi
}

# Coloca SRC (temporal ya verificado, o el origen si se mueve en el mismo disco) en su
# nombre final según la política de conflicto. Establece FINAL. Devuelve 1 con MSG si no puede.
colocar_final() {  # colocar_final src destino
    local src="$1"
    FINAL="$2"
    if [[ -e $FINAL || -L $FINAL ]]; then
        case "$CONFLICTO" in
            sobrescribir)
                if [[ -d $FINAL && ! -L $FINAL ]]; then
                    MSG="en el destino hay una carpeta con ese nombre"; return 1
                fi
                log "Sobrescribiendo $FINAL"
                mv -f -T -- "$src" "$FINAL" 2>/dev/null || { MSG="no se pudo escribir $FINAL"; return 1; } ;;
            renombrar)
                FINAL="$(nombre_libre "$FINAL")"
                log "El destino ya existe; se guarda como $FINAL"
                mv -n -T -- "$src" "$FINAL" 2>/dev/null ;;
            *)
                MSG="apareció un fichero con el mismo nombre en el destino durante la copia"; return 1 ;;
        esac
    else
        mv -n -T -- "$src" "$FINAL" 2>/dev/null
    fi
    # mv -n no avisa si no ha movido: se comprueba que el origen ya no está
    if [[ -e $src || -L $src ]]; then
        MSG="no se pudo colocar el fichero en $FINAL"; return 1
    fi
    return 0
}

worker() {
    ORIGEN="worker"
    cargar_conf
    # worker.lock se mantiene (fd 8) mientras viva el worker: impide dos a la vez
    exec 8> "$WLOCK"
    if ! flock -n 8; then
        log "Ya hay otro proceso en segundo plano activo; saliendo"
        exit 0
    fi
    echo $$ > "$PIDF"
    PARAR=0 HIJO_PID=""
    trap 'PARAR=1; [[ -n $HIJO_PID ]] && kill -TERM "$HIJO_PID" 2>/dev/null' TERM INT
    trap '[[ $(cat "$PIDF" 2>/dev/null) == "$$" ]] && rm -f "$PIDF"; rm -f "$PROG" "$PROG.w" "$PROG.err"' EXIT

    con_bloqueo _recuperar_procesando

    log "Proceso en segundo plano iniciado (límite ${LIMITE_KBPS} KB/s, $REINTENTOS reintentos, verificar=$VERIFICAR, conflicto=$CONFLICTO)"
    local hechos=0 errores=0 omitidos=0 linea id reanudar origen destino firma firma_act op raiz
    local origen_tam dest_tam tmp tmp_tam destdir intento espera espera_max final detalle k
    local necesita libre nota t_ini t_fin
    while ((!PARAR)); do
        linea=$(con_bloqueo _tomar_siguiente) || break
        IFS=$'\t' read -r id _ reanudar _ _ origen destino _ firma op raiz <<< "$linea"
        op="${op:-copiar}" raiz="${raiz:--}"
        destdir="${destino%/*}"; destdir="${destdir:-/}"
        tmp="$(ruta_parcial "$destino")"

        if [[ ! -f $origen || ! -r $origen ]]; then
            log "ERROR #$id: no se puede leer $origen"
            cola_set "$id" error 0 "no existe o no se puede leer el origen"
            ((errores++))
            continue
        fi
        origen_tam=$(stat -L -c %s -- "$origen")

        # Si el origen ha cambiado desde que se encoló o empezó a copiarse, el trozo
        # copiado es de otra versión: se descarta y se empieza desde cero.
        firma_act=$(firma_local "$origen")
        if [[ -n $firma && $firma != - && $firma != "$firma_act" ]]; then
            if [[ -e $tmp ]]; then
                log "Aviso #$id: $origen ha cambiado desde la copia anterior; se copia desde el principio"
                rm -f -- "$tmp"
            fi
            reanudar=0
        fi
        [[ $firma != "$firma_act" ]] && con_bloqueo _cola_set_firma "$id" "$origen_tam" "$firma_act"
        firma="$firma_act"

        if [[ $origen -ef $destino ]]; then
            log "ERROR #$id: origen y destino son el mismo fichero ($origen)"
            cola_set "$id" error 0 "origen y destino son el mismo fichero"
            ((errores++)); continue
        fi

        # Conflicto: el nombre final ya existe (los trozos propios van en .mover-partial,
        # así que esto nunca es una copia nuestra a medias).
        if [[ -d $destino && ! -L $destino && $CONFLICTO != renombrar ]]; then
            log "ERROR #$id: en el destino hay una carpeta llamada como el fichero ($destino)"
            cola_set "$id" error 0 "en el destino hay una carpeta con ese nombre"
            ((errores++)); continue
        fi
        if [[ $CONFLICTO == omitir ]] && [[ -e $destino || -L $destino ]]; then
            dest_tam=$(stat -L -c %s -- "$destino" 2>/dev/null || echo -1)
            if [[ $dest_tam != "$origen_tam" ]]; then
                log "ERROR #$id: $destino ya existe con otro tamaño ($dest_tam bytes, origen $origen_tam bytes)"
                cola_set "$id" error 0 "ya existe en el destino con otro tamaño ($(tam "$dest_tam"))"
                ((errores++)); continue
            fi
            rm -f -- "$tmp"; rmdir -- "$destdir/$PARCIAL" 2>/dev/null
            if [[ $op == copiar ]]; then
                cola_set "$id" completado 0 "ya existía en el destino con el mismo tamaño"
                log "Omitido #$id $destino: ya existe con el mismo tamaño ($(tam "$origen_tam"))"
            elif cmp -s -- "$origen" "$destino"; then
                if rm -f -- "$origen"; then
                    limpiar_dirs_origen "$origen" "$raiz"; cerrar_raiz "$id" "$origen" "$destino" "$raiz"
                    cola_set "$id" completado 0 "ya existía idéntico en el destino; origen eliminado"
                    log "Omitido #$id $destino: ya existía idéntico; origen $origen eliminado"
                else
                    cola_set "$id" completado 0 "ya existía idéntico en el destino; no se pudo borrar el origen"
                    log "Aviso #$id: ya existía idéntico en destino pero no se pudo borrar $origen"
                fi
            else
                log "ERROR #$id: $destino ya existe con el mismo tamaño pero distinto contenido; origen conservado"
                cola_set "$id" error 0 "ya existe en el destino con el mismo tamaño pero distinto contenido (origen conservado)"
                ((errores++)); continue
            fi
            ((omitidos++)); continue
        fi

        ((PARAR)) && { cola_set "$id" pendiente "$reanudar" -; break; }

        if ! mkdir -p -- "$destdir" 2>/dev/null; then
            log "ERROR #$id: no se puede crear la carpeta destino $destdir"
            cola_set "$id" error 0 "no se puede crear la carpeta destino $destdir"
            ((errores++)); continue
        fi

        # Mover dentro del mismo sistema de ficheros: rename instantáneo, sin copiar datos
        if [[ $op == mover ]] && mismo_fs "$origen" "$destdir"; then
            if colocar_final "$origen" "$destino"; then
                [[ $FINAL != "$destino" ]] && con_bloqueo _cola_set_destino "$id" "$FINAL"
                rm -f -- "$tmp"; rmdir -- "$destdir/$PARCIAL" 2>/dev/null
                limpiar_dirs_origen "$origen" "$raiz"; cerrar_raiz "$id" "$origen" "$FINAL" "$raiz"
                nota="movido (mismo disco)"
                [[ $FINAL != "$destino" ]] && nota+="; guardado como ${FINAL##*/}"
                cola_set "$id" completado 0 "$nota"
                log "Completado #$id: movido $origen -> $FINAL (mismo sistema de ficheros)"
                ((hechos++))
            else
                log "ERROR #$id moviendo $origen: $MSG"
                cola_set "$id" error 0 "$MSG"
                ((errores++))
            fi
            continue
        fi

        # Copia con rsync a un temporal en <destino>/.mover-partial
        if ! mkdir -p -- "$destdir/$PARCIAL" 2>/dev/null; then
            log "ERROR #$id: no se puede escribir en $destdir"
            cola_set "$id" error 0 "no se puede escribir en la carpeta destino $destdir"
            ((errores++)); continue
        fi
        tmp_tam=$(stat -c %s -- "$tmp" 2>/dev/null || echo 0)
        if ((tmp_tam > origen_tam)); then rm -f -- "$tmp"; tmp_tam=0; fi
        ((tmp_tam > 0)) && reanudar=1 || reanudar=0

        necesita=$((origen_tam - tmp_tam))
        libre=$(espacio_libre "$destdir")
        if [[ $libre =~ ^[0-9]+$ ]] && ((necesita > libre)); then
            MSG="sin espacio en el destino: hacen falta $(tam "$necesita") y hay $(tam "$libre") libres"
            log "ERROR #$id: $MSG ($destdir)"
            rmdir -- "$destdir/$PARCIAL" 2>/dev/null
            cola_set "$id" error "$reanudar" "$MSG"
            ((errores++)); continue
        fi

        intento=0 final=""
        printf -v t_ini '%(%s)T' -1
        while :; do
            detalle=""
            [[ $reanudar == 1 ]] && detalle+=", reanudando desde $(tam "$(stat -c %s -- "$tmp" 2>/dev/null || echo 0)")"
            ((intento)) && detalle+=" [reintento $intento/$REINTENTOS]"
            # --parar pudo llegar mientras se preparaba el elemento
            ((PARAR)) && { final="parado"; break; }
            log "$([[ $op == mover ]] && echo Moviendo || echo Copiando) #$id $origen -> $destino ($(tam "$origen_tam"))$detalle"
            rsync_una_vez "$origen" "$tmp"

            if ((PARAR)); then
                [[ -s $tmp ]] && reanudar=1 || reanudar=0
                final="parado"; break
            fi

            if ((RC == 0)); then
                [[ -n ${MOVER_PRUEBA_TRAS_COPIA:-} ]] && "$MOVER_PRUEBA_TRAS_COPIA" "$tmp"
                tmp_tam=$(stat -c %s -- "$tmp" 2>/dev/null || echo -1)
                if [[ $(firma_local "$origen") != "$firma" ]]; then
                    RC=-1 MSG="el origen cambió durante la copia"
                    rm -f -- "$tmp"
                    firma=$(firma_local "$origen"); origen_tam=$(stat -L -c %s -- "$origen" 2>/dev/null || echo 0)
                    con_bloqueo _cola_set_firma "$id" "$origen_tam" "$firma"
                elif [[ ($VERIFICAR != no || $op == mover) && $tmp_tam != "$origen_tam" ]]; then
                    RC=-1 MSG="verificación fallida: tamaño copiado ($tmp_tam bytes) distinto del origen ($origen_tam bytes)"
                    rm -f -- "$tmp"
                elif [[ $VERIFICAR == checksum ]] && ! cmp -s -- "$origen" "$tmp"; then
                    RC=-1 MSG="verificación fallida: el contenido copiado no coincide con el origen"
                    rm -f -- "$tmp"
                else
                    chmod --reference="$origen" -- "$tmp" 2>/dev/null
                    if colocar_final "$tmp" "$destino"; then
                        final="ok"
                    else
                        final="error"
                    fi
                    break
                fi
                reanudar=0
            else
                [[ -s $tmp ]] && reanudar=1 || reanudar=0
            fi

            if [[ $ERRORES_DEFINITIVOS == *" $RC "* ]] || ((intento >= REINTENTOS)); then
                final="error"; break
            fi
            ((intento++))
            # Espera triplicada en cada reintento, con tope (600 s o la espera inicial si es mayor)
            espera_max=$((ESPERA_REINTENTO > 600 ? ESPERA_REINTENTO : 600))
            espera=$ESPERA_REINTENTO
            for ((k = 1; k < intento && espera < espera_max; k++)); do espera=$((espera * 3)); done
            ((espera > espera_max)) && espera=$espera_max
            log "Fallo en #$id: $MSG. Reintento $intento/$REINTENTOS en ${espera}s"
            esperar "$espera"
            if ((PARAR)); then final="parado"; break; fi
        done

        case $final in
            ok)
                printf -v t_fin '%(%s)T' -1
                [[ $FINAL != "$destino" ]] && con_bloqueo _cola_set_destino "$id" "$FINAL"
                rmdir -- "$destdir/$PARCIAL" 2>/dev/null
                nota="-"
                [[ $FINAL != "$destino" ]] && nota="guardado como ${FINAL##*/}"
                if [[ $op == mover ]]; then
                    # Solo se llega aquí con la copia verificada (al menos el tamaño)
                    if rm -f -- "$origen" 2>/dev/null && [[ ! -e $origen ]]; then
                        limpiar_dirs_origen "$origen" "$raiz"; cerrar_raiz "$id" "$origen" "$FINAL" "$raiz"
                        nota="${nota/#-/}"; nota="movido entre discos${nota:+; $nota}"
                    else
                        nota="copiado, pero NO se pudo borrar el origen"
                        log "Aviso #$id: copiado a $FINAL pero no se pudo borrar $origen"
                    fi
                fi
                cola_set "$id" completado 0 "$nota"
                log "Completado #$id ($op) $FINAL: $(tam "$origen_tam") en $(formatear_tiempo $((t_fin - t_ini)))"
                ((hechos++)) ;;
            parado)
                cola_set "$id" pendiente "$reanudar" -
                log "Procesamiento de #$id interrumpido; queda $([[ $reanudar == 1 ]] && echo 'pendiente de reanudar' || echo 'pendiente')" ;;
            error)
                rmdir -- "$destdir/$PARCIAL" 2>/dev/null
                cola_set "$id" error "$reanudar" "$MSG"
                log "ERROR procesando #$id $origen: $MSG$([[ $op == mover ]] && echo ' (origen conservado)')"
                ((errores++)) ;;
        esac
        [[ $final == parado ]] && break
    done
    rm -f "$PROG" "$PROG.w" "$PROG.err"
    log "Proceso en segundo plano finalizado: $hechos procesados, $omitidos ya existían, $errores con error"
}

# --------------------------------------------------------------------------
# Interfaz Interactiva (Explorador, Cola, Progreso y Menú)
# --------------------------------------------------------------------------

navegar() {
    local raiz actual op n rel i p destino destino_fijo="" rc filtro="" op_actual="$OPERACION"
    raiz="$(readlink -f -- "$CARPETA_ORIGEN")"
    actual="$raiz"
    while :; do
        clear 2>/dev/null
        local -a dirs_raw=() ficheros_raw=() dirs=() ficheros=() todos=()
        local -A en_cola=() ya_completado=()
        while IFS= read -r p; do en_cola["$p"]=1; done \
            < <(awk -F'\t' '$2 == "pendiente" || $2 == "procesando" || $2 == "pausado" { print $6 }' "$COLA" 2>/dev/null)
        while IFS= read -r p; do ya_completado["$p"]=1; done \
            < <(awk -F'\t' '$2 == "completado" { print $6 }' "$COLA" 2>/dev/null)
        mapfile -d '' dirs_raw < <(find "$actual" -mindepth 1 -maxdepth 1 \( -type d -o -xtype d \) ! -name '.*' -print0 2>/dev/null | sort -z)
        mapfile -d '' ficheros_raw < <(find "$actual" -mindepth 1 -maxdepth 1 \( -type f -o -xtype f \) ! -name '.*' -print0 2>/dev/null | sort -z)

        local filtro_low="${filtro,,}"
        for p in "${dirs_raw[@]}" "${ficheros_raw[@]}"; do
            if [[ -z $filtro_low || ${p##*/} == *"$filtro"* || ${p,,} == *"$filtro_low"* ]]; then
                if [[ -d $p ]]; then dirs+=("$p"); else ficheros+=("$p"); fi
            fi
        done
        local -a lista=("${dirs[@]}" "${ficheros[@]}")

        rel="${actual#"$raiz"}"
        if [[ -n $destino_fijo ]]; then
            destino="$destino_fijo"
        else
            destino="$(destino_auto "$actual")"
        fi

        caja_cabecera "Explorador de Archivos · $raiz$rel" 88
        printf '   %sDestino:%s %s (%s)\n' "$C_BOLD" "$C_RESET" "$destino" "$([[ -n $destino_fijo ]] && echo 'elegido manualmente' || echo 'automático')"
        printf '   %sOperación:%s %s  %s(o: alternar copiar/mover)%s\n' "$C_BOLD" "$C_RESET" "$(texto_op "$op_actual")" "$C_DIM" "$C_RESET"
        linea_favoritos
        if [[ -n $filtro ]]; then
            printf '   %sFiltro activo: "%s"%s (escribe %s*%s o %s/%s para quitar el filtro)\n' \
                "$C_BOLD$C_CYAN" "$filtro" "$C_RESET" "$C_BOLD" "$C_RESET" "$C_BOLD" "$C_RESET"
        fi
        separador_tabla 90

        ((${#lista[@]} == 0)) && echo "  (ningún archivo o carpeta coincide)"
        local marca w_nom
        w_nom=$(ancho_columna_fichero)
        for i in "${!lista[@]}"; do
            p="${lista[i]}"
            if [[ -d $p ]]; then
                printf '  %4d)  %s[DIR ]%s %s%s/%s\n' \
                    $((i + 1)) "$C_BOLD$C_BLUE" "$C_RESET" "$C_BOLD$C_BLUE" "${p##*/}" "$C_RESET"
            else
                marca=" "
                if [[ -n ${en_cola[$p]:-} ]]; then
                    marca="${C_YELLOW}*${C_RESET}"
                elif [[ -n ${ya_completado[$p]:-} ]]; then
                    marca="${C_GREEN}✓${C_RESET}"
                fi
                printf '  %4d)%s        %s  %s(%s)%s\n' \
                    $((i + 1)) "$marca" "$(acortar_nombre "${p##*/}" "$w_nom")" \
                    "$C_DIM" "$(tam "$(stat -L -c %s -- "$p" 2>/dev/null || echo 0)")" "$C_RESET"
            fi
        done
        separador_tabla 90
        printf '  Leyenda: %s*%s en cola  |  %s✓%s ya completado (se omite en lotes)\n' \
            "$C_YELLOW" "$C_RESET" "$C_GREEN" "$C_RESET"
        echo "  Comandos:"
        echo "    • Seleccionar : N (abrir carpeta / añadir archivo) | 1,3,5-8 (añadir varios) | r N (añadir carpeta N recursiva)"
        echo "    • Lotes       : t (añadir todos los archivos de esta vista) | o (alternar COPIAR / MOVER)"
        echo "    • Destino     : d (elegir carpeta destino) | f1, f2.. (destino favorito) | a (destino automático)"
        echo "    • Buscar/Ruta : /TEXTO (filtrar por nombre) | * (quitar filtro) | .. (subir) | v (volver)"
        echo
        read -rep "  Opción: " op || op="v"

        case "$op" in
            v | q | "") return ;;
            ..)
                filtro=""
                [[ $actual != "$raiz" ]] && actual="$(dirname -- "$actual")"
                continue ;;
            "/" | "*" | "todos")
                filtro=""
                continue ;;
            "/"*)
                filtro="${op#/}"
                continue ;;
            "buscar "*)
                filtro="${op#buscar }"
                continue ;;
            o)
                [[ $op_actual == copiar ]] && op_actual=mover || op_actual=copiar
                continue ;;
            a)
                destino_fijo=""
                continue ;;
            f[0-9]*)
                if p=$(favorito "$op"); then
                    destino_fijo="$p"
                    log "Destino favorito elegido en el explorador: $destino_fijo"
                else
                    echo "  No existe el favorito $op."; pausa
                fi
                continue ;;
            d)
                elegir_carpeta_destino "$destino" si; rc=$?
                if ((rc == 0)); then
                    destino_fijo="$ELEGIDA"
                    log "Destino elegido en el explorador: $destino_fijo"
                elif ((rc == 2)); then
                    destino_fijo=""
                fi
                continue ;;
            t)
                if ((${#ficheros[@]})); then anadir_a_cola "$op_actual" - "$destino" "$actual" "${ficheros[@]}"; else echo "  No hay ficheros en esta vista."; fi
                pausa; continue ;;
            "r "*)
                n="${op#r }"
                if [[ $n =~ ^[0-9]+$ ]] && ((n >= 1 && n <= ${#dirs[@]})); then
                    mapfile -d '' todos < <(find "${dirs[n - 1]}" -mindepth 1 -name '.*' -prune -o \( -type f -o -xtype f \) -print0 2>/dev/null | sort -z)
                    if ((${#todos[@]} == 0)); then
                        echo "  La carpeta no contiene ficheros."
                    elif si_no "¿$([[ $op_actual == mover ]] && echo MOVER || echo COPIAR) ${#todos[@]} fichero(s) de ${dirs[n - 1]##*/}/ a ${destino%/}/${dirs[n - 1]##*/}/?" s; then
                        anadir_a_cola "$op_actual" "${dirs[n - 1]}" "$destino" "$actual" "${todos[@]}"
                    fi
                else
                    echo "  Número de carpeta no válido."
                fi
                pausa; continue ;;
        esac

        if ! parsear_seleccion "$op" "${#lista[@]}"; then
            echo "  Opción no válida."; pausa; continue
        fi
        ((${#SELECCION[@]} == 0)) && continue
        if ((${#SELECCION[@]} == 1)) && [[ -d ${lista[SELECCION[0] - 1]} ]]; then
            actual="${lista[SELECCION[0] - 1]}"
            filtro=""
            continue
        fi
        local -a elegidos=()
        for n in "${SELECCION[@]}"; do [[ -f ${lista[n - 1]} ]] && elegidos+=("${lista[n - 1]}"); done
        if ((${#elegidos[@]} == 0)); then
            echo "  No has seleccionado ningún fichero (usa 'r N' para añadir una carpeta)."
        elif ((${#elegidos[@]} == 1)) && [[ -n ${ya_completado[${elegidos[0]}]:-} ]]; then
            if si_no "El fichero '${elegidos[0]##*/}' ya figura como completado [OK]. ¿Volver a añadirlo?" n; then
                anadir_a_cola "$op_actual" - "$destino" "$actual" "${elegidos[@]}"
            fi
        else
            anadir_a_cola "$op_actual" - "$destino" "$actual" "${elegidos[@]}"
        fi
        pausa
    done
}

_quitar_ids() {  # _quitar_ids "101 103 105"
    local ids=" $1 " antes despues
    antes=$(wc -l < "$COLA")
    _reescribir_cola awk -F'\t' -v ids="$ids" '
        index(ids, " " $1 " ") && $2 == "procesando" { print "  #" $1 " se está procesando: detén el proceso antes de quitarlo." > "/dev/stderr"; print; next }
        index(ids, " " $1 " ") { next }
        { print }'
    despues=$(wc -l < "$COLA")
    log "Quitados de la cola: $1 ($((antes - despues)) elementos)"
    echo "  $((antes - despues)) elemento(s) quitado(s)."
}

_filtrar_cola() {  # _filtrar_cola 'expresión awk que decide qué se queda' descripción
    local antes despues
    antes=$(wc -l < "$COLA")
    _reescribir_cola awk -F'\t' "$1"
    despues=$(wc -l < "$COLA")
    log "$2 ($((antes - despues)) elementos)"
    echo "  $((antes - despues)) elemento(s) eliminado(s)."
}

_cambiar_destino() {  # _cambiar_destino "101 103" /carpeta/destino
    local antes
    # Las filas cuyo nuevo destino ya use otra fila activa (o otra de la selección) no se cambian.
    # Se lee la cola dos veces: la 1ª pasada reúne los destinos ocupados por filas que no cambian.
    DESTINO="${2%/}" _reescribir_cola awk -F'\t' -v OFS='\t' -v ids=" $1 " '
        function cambia() { return index(ids, " " $1 " ") && ($2 == "pendiente" || $2 == "pausado" || $2 == "error") }
        NR == FNR { if (!cambia() && ($2 == "pendiente" || $2 == "procesando" || $2 == "pausado")) ocupado[$7] = 1; next }
        cambia() {
            n = split($7, p, "/"); nuevo = ENVIRON["DESTINO"] "/" p[n]
            if (nuevo == $6) {
                print "  #" $1 " no se cambia: el destino sería el propio origen" > "/dev/stderr"
            } else if (nuevo != $7 && (nuevo in ocupado)) {
                print "  #" $1 " no se cambia: otro elemento de la cola ya va a " nuevo > "/dev/stderr"
            } else {
                $7 = nuevo; $3 = 0
            }
            ocupado[$7] = 1
        }
        { print }' "$COLA" -
    antes=$(DESTINO="${2%/}" awk -F'\t' -v ids=" $1 " '
        index(ids, " " $1 " ") && ($2 == "pendiente" || $2 == "pausado" || $2 == "error") {
            n = split($7, p, "/"); if ($7 == ENVIRON["DESTINO"] "/" p[n]) c++
        }
        END { print c + 0 }' "$COLA")
    log "Cambiado el destino de $1 a $2 ($antes elementos)"
    echo "  $antes elemento(s) irán a $2 (solo se cambian pendientes, pausados o con error)."
}

_cambiar_operacion() {  # _cambiar_operacion "101 103"  (alterna copiar <-> mover)
    _reescribir_cola awk -F'\t' -v OFS='\t' -v ids=" $1 " '
        index(ids, " " $1 " ") && ($2 == "pendiente" || $2 == "pausado" || $2 == "error") {
            $10 = ($10 == "mover") ? "copiar" : "mover"
            print "  #" $1 " -> " $10 > "/dev/stderr"
        }
        index(ids, " " $1 " ") && ($2 == "procesando" || $2 == "completado") {
            print "  #" $1 " no se cambia (" $2 ")" > "/dev/stderr"
        }
        { print }'
    log "Alternada la operación copiar/mover de: $1"
}

_toggle_pausa_ids() {  # _toggle_pausa_ids "101 103"
    local ids=" $1 "
    _reescribir_cola awk -F'\t' -v OFS='\t' -v ids="$ids" '
        index(ids, " " $1 " ") && $2 == "procesando" {
            print "  #" $1 " se está procesando: detén el proceso en segundo plano para pausarlo." > "/dev/stderr"
            print; next
        }
        index(ids, " " $1 " ") && $2 == "pendiente" { $2 = "pausado"; print; next }
        index(ids, " " $1 " ") && $2 == "pausado" { $2 = "pendiente"; print; next }
        { print }'
    log "Cambiado estado de pausa para: $1"
    echo "  Estado de pausa actualizado para la selección."
}

_reintentar_errores() {
    local n
    n=$(awk -F'\t' '$2 == "error"' "$COLA" | wc -l)
    _reescribir_cola awk -F'\t' -v OFS='\t' '$2 == "error" { $2 = "pendiente"; $8 = "-" } { print }'
    log "Marcados $n elementos con error para reintentar"
    echo "  $n elemento(s) vuelven a pendiente."
}

_mover_elemento() {  # _mover_elemento ID TARGET_POS
    local id="$1" target_pos="$2"
    _reescribir_cola awk -F'\t' -v id="$id" -v target_pos="$target_pos" '
        {
            total++
            lineas[total] = $0
            if ($1 == id) origen = total
        }
        END {
            if (!origen) {
                print "  ERROR: Elemento #" id " no encontrado en la cola." > "/dev/stderr"
                for (i=1; i<=total; i++) print lineas[i]
                exit 1
            }
            split(lineas[origen], co, "\t")
            if (co[2] == "procesando") {
                print "  No se puede mover un elemento que ya se está procesando." > "/dev/stderr"
                for (i=1; i<=total; i++) print lineas[i]
                exit 1
            }
            split(lineas[1], c1, "\t")
            min_pos = (c1[2] == "procesando") ? 2 : 1
            if (target_pos < min_pos) target_pos = min_pos
            if (target_pos > total) target_pos = total

            elem = lineas[origen]
            idx = 0
            for (i=1; i<=total; i++) {
                if (i != origen) {
                    idx++
                    temp[idx] = lineas[i]
                }
            }
            for (i=1; i<=total; i++) {
                if (i < target_pos) {
                    print temp[i]
                } else if (i == target_pos) {
                    print elem
                } else {
                    print temp[i-1]
                }
            }
        }
    ' || return 1
    log "Reordenada cola: movido #$id a posicion $target_pos"
    echo "  Elemento #$id colocado en la fila $target_pos."
}
mover_elemento_cola() { con_bloqueo _mover_elemento "$@"; }

# Ordena la cola: en curso, pendientes/pausados, errores y completados; dentro de cada
# grupo, por ruta de origen (orden estable).
_ordenar_cola() {
    [[ -s $COLA ]] || { echo "  La cola está vacía."; return; }
    _reescribir_cola awk -F'\t' '
        {
            if ($2 == "procesando") p = 0
            else if ($2 == "pendiente" || $2 == "pausado") p = 1
            else if ($2 == "error") p = 2
            else p = 3
            printf "%d\t%s\t%08d\t%s\n", p, $6, NR, $0
        }' | sort -t$'\t' -k1,1n -k2,2 -k3,3n | cut -f4-
    log "Cola ordenada por ruta de origen"
    echo "  Cola ordenada por estado y ruta de origen."
}

menu_cola() {  # menu_cola [modo_inicial: todos|pendientes]
    local op modo="${1:-todos}" filtro=""
    while :; do
        clear 2>/dev/null
        local titulo_cola="GESTIÓN DE LA COLA (Vista: TODOS)"
        [[ $modo == "pendientes" ]] && titulo_cola="GESTIÓN DE LA COLA (Vista: SOLO PENDIENTES)"
        caja_cabecera "$titulo_cola" 88

        if [[ $modo == "pendientes" ]]; then
            imprimir_pendientes "$filtro"
        else
            imprimir_cola "$filtro"
        fi

        echo
        echo "  Comandos (usa el nº de fila # como 1,3-5 o el ID con # como #105):"
        printf '    • Vista/Buscar : [%st%s] Alternar Todos/Pendientes | [%s/TEXTO%s] Buscar | [%s*%s] Quitar filtro\n' \
            "$C_BOLD$C_CYAN" "$C_RESET" "$C_BOLD$C_CYAN" "$C_RESET" "$C_BOLD$C_CYAN" "$C_RESET"
        printf '    • Prioridad    : [%sord%s] Ordenar por ruta | [%ssub N%s] Subir | [%sbaj N%s] Bajar | [%stop N%s] Al inicio | [%sm N POS%s] Mover\n' \
            "$C_BOLD$C_CYAN" "$C_RESET" "$C_BOLD$C_CYAN" "$C_RESET" "$C_BOLD$C_CYAN" "$C_RESET" "$C_BOLD$C_CYAN" "$C_RESET" "$C_BOLD$C_CYAN" "$C_RESET"
        printf '    • Elementos    : [%sp N%s] Pausar/Reanudar | [%sd N%s] Cambiar destino | [%so N%s] Copiar⇄Mover | [%sb N%s] Quitar de la cola\n' \
            "$C_BOLD$C_CYAN" "$C_RESET" "$C_BOLD$C_CYAN" "$C_RESET" "$C_BOLD$C_CYAN" "$C_RESET" "$C_BOLD$C_CYAN" "$C_RESET"
        printf '    • Limpieza     : [%sc%s] Limpiar completados | [%sf%s] Limpiar errores | [%se%s] Reintentar errores | [%sx%s] Vaciar | [%sv%s] Volver\n' \
            "$C_BOLD$C_CYAN" "$C_RESET" "$C_BOLD$C_CYAN" "$C_RESET" "$C_BOLD$C_CYAN" "$C_RESET" "$C_BOLD$C_CYAN" "$C_RESET" "$C_BOLD$C_CYAN" "$C_RESET"
        separador_tabla 90
        read -rep "  Opción: " op || op="v"
        case "$op" in
            v | q | "") return ;;
            t | tab | pend)
                if [[ $modo == "todos" ]]; then modo="pendientes"; else modo="todos"; fi
                continue ;;
            ord | ordenar)
                con_bloqueo _ordenar_cola ;;
            "/" | "*" | "todos")
                filtro=""
                continue ;;
            "/"*)
                filtro="${op#/}"
                continue ;;
            "buscar "*)
                filtro="${op#buscar }"
                continue ;;
            "b "*)
                if resolver_seleccion_cola "${op#b }"; then
                    con_bloqueo _quitar_ids "${IDS_RESUELTOS[*]}"
                else
                    echo "  Selección no válida."
                fi ;;
            "sub "* | "ar "*)
                if resolver_seleccion_cola "${op#* }" && ((${#IDS_RESUELTOS[@]} == 1)); then
                    local n_id="${IDS_RESUELTOS[0]}" cur_pos
                    cur_pos=$(awk -F'\t' -v id="$n_id" '$1 == id { print NR; exit }' "$COLA")
                    if [[ -n $cur_pos ]] && ((cur_pos > 1)); then
                        mover_elemento_cola "$n_id" $((cur_pos - 1))
                    else
                        echo "  El elemento #$n_id ya está en la primera posición posible."
                    fi
                else
                    echo "  Indica una única fila o #ID válido."
                fi ;;
            "baj "* | "ab "*)
                if resolver_seleccion_cola "${op#* }" && ((${#IDS_RESUELTOS[@]} == 1)); then
                    local n_id="${IDS_RESUELTOS[0]}" cur_pos total_l
                    cur_pos=$(awk -F'\t' -v id="$n_id" '$1 == id { print NR; exit }' "$COLA")
                    total_l=$(wc -l < "$COLA")
                    if [[ -n $cur_pos ]] && ((cur_pos < total_l)); then
                        mover_elemento_cola "$n_id" $((cur_pos + 1))
                    else
                        echo "  El elemento #$n_id ya está en la última posición."
                    fi
                else
                    echo "  Indica una única fila o #ID válido."
                fi ;;
            "top "*)
                if resolver_seleccion_cola "${op#top }" && ((${#IDS_RESUELTOS[@]} == 1)); then
                    mover_elemento_cola "${IDS_RESUELTOS[0]}" 1
                else
                    echo "  Indica una única fila o #ID válido."
                fi ;;
            "m "*)
                local args_m="${op#m }"
                local orig_m="${args_m%% *}"
                local target_p="${args_m##* }"
                if [[ $target_p =~ ^[0-9]+$ ]] && resolver_seleccion_cola "$orig_m" && ((${#IDS_RESUELTOS[@]} == 1)); then
                    mover_elemento_cola "${IDS_RESUELTOS[0]}" "$target_p"
                else
                    echo "  Uso: m FILA POSICIÓN  (ejemplo: m 5 2)"
                fi ;;
            "p "*)
                if resolver_seleccion_cola "${op#p }"; then
                    con_bloqueo _toggle_pausa_ids "${IDS_RESUELTOS[*]}"
                else
                    echo "  Selección no válida."
                fi ;;
            "o "*)
                if resolver_seleccion_cola "${op#o }"; then
                    con_bloqueo _cambiar_operacion "${IDS_RESUELTOS[*]}"
                else
                    echo "  Selección no válida."
                fi ;;
            "d "*)
                if resolver_seleccion_cola "${op#d }"; then
                    if elegir_carpeta_destino "$CARPETA_DESTINO"; then
                        con_bloqueo _cambiar_destino "${IDS_RESUELTOS[*]}" "$ELEGIDA"
                    else
                        continue
                    fi
                else
                    echo "  Selección no válida."
                fi ;;
            c) con_bloqueo _filtrar_cola '$2 != "completado"' "Limpiados completados de la cola" ;;
            f) con_bloqueo _filtrar_cola '$2 != "error"' "Limpiados fallidos de la cola" ;;
            e) con_bloqueo _reintentar_errores ;;
            x) si_no "¿Seguro que quieres vaciar la cola?" n \
                   && con_bloqueo _filtrar_cola '$2 == "procesando"' "Cola vaciada" ;;
            *) echo "  Opción no válida." ;;
        esac
        pausa
    done
}

ver_progreso() {
    local linea frame
    printf '\033[?25l'
    trap 'VOLVER=1; printf "\033[?25h"' INT
    VOLVER=0
    clear 2>/dev/null
    while ((!VOLVER)); do
        frame=$(
            caja_cabecera "Progreso en Tiempo Real" 88
            resumen_estado
            echo

            linea=$(awk -F'\t' '$2 == "procesando"' "$COLA" 2>/dev/null)
            if [[ -n $linea ]]; then
                local id tam_f origen_f destino_f op_f
                IFS=$'\t' read -r id _ _ tam_f _ origen_f destino_f _ _ op_f _ <<< "$linea"

                printf '  %s>> EN CURSO #%s%s : %s %s%s%s (%s)\n' \
                    "$C_BOLD$C_BLUE" "$id" "$C_RESET" "$(insignia_op "${op_f:-copiar}")" "$C_BOLD" "${origen_f##*/}" "$C_RESET" "$(tam "$tam_f")"
                printf '     Origen       : %s\n' "${origen_f%/*}"
                printf '     Destino      : %s\n' "$destino_f"

                if leer_progreso; then
                    calcular_tiempo_cola "${PRG_VEL:-}" "${PRG_PCT:-0}"

                    printf '     Archivo act. : %s (%s de %s)\n' \
                        "$(barra_progreso "$PRG_PCT" 30 "$C_GREEN")" "${PRG_ENVIADO:-0}" "$(tam "$tam_f")"
                    printf '     Cola global  : %s (%s de %s)\n' \
                        "$(barra_progreso "$COLA_PCT_GLOBAL" 30 "$C_CYAN")" "$(tam "$COLA_BYTES_HECHOS")" "$(tam "$COLA_BYTES_TOT")"
                    printf '     Velocidad    : %s\n' "${PRG_VEL:-?}"
                    printf '     Tiempo arch. : transcurrido %s | restante %s\n' \
                        "${PRG_T_PASADO:-?}" "${PRG_T_RESTANTE:-?}"
                    if [[ -n $COLA_TIEMPO_EST ]]; then
                        printf '     Tiempo cola  : restante est. %s%s%s (%d arch, %s restantes)\n' \
                            "$C_BOLD$C_GREEN" "$COLA_TIEMPO_EST" "$C_RESET" "$COLA_FICH_REST" "$(tam "$COLA_BYTES_REST")"
                    fi
                else
                    calcular_tiempo_cola "" 0
                    printf '     Archivo act. : %s (preparando o moviendo...)\n' "$(barra_progreso 0 30 "$C_GREEN")"
                    if ((COLA_BYTES_TOT > 0)); then
                        printf '     Cola global  : %s (%s de %s)\n' \
                            "$(barra_progreso "$COLA_PCT_GLOBAL" 30 "$C_CYAN")" "$(tam "$COLA_BYTES_HECHOS")" "$(tam "$COLA_BYTES_TOT")"
                    fi
                    if [[ -n $COLA_TIEMPO_EST ]]; then
                        printf '     Tiempo cola  : restante est. ~ %s (%s restantes)\n' \
                            "$COLA_TIEMPO_EST" "$(tam "$COLA_BYTES_REST")"
                    fi
                fi
            else
                calcular_tiempo_cola "" 0
                echo "  (no se está procesando ningún archivo en este momento)"
                if ((COLA_BYTES_TOT > 0)); then
                    printf '  Cola global : %s (%s de %s)\n' \
                        "$(barra_progreso "$COLA_PCT_GLOBAL" 30 "$C_CYAN")" "$(tam "$COLA_BYTES_HECHOS")" "$(tam "$COLA_BYTES_TOT")"
                fi
                if [[ -n $COLA_TIEMPO_EST ]]; then
                    printf '  Tiempo estimado cola restante: %s%s%s (%d arch, %s)\n' \
                        "$C_BOLD$C_GREEN" "$COLA_TIEMPO_EST" "$C_RESET" "$COLA_FICH_REST" "$(tam "$COLA_BYTES_REST")"
                fi
            fi

            local pend_total
            pend_total=$(awk -F'\t' '$2 == "pendiente" || $2 == "pausado"' "$COLA" 2>/dev/null | wc -l)
            if ((pend_total > 0)); then
                echo
                if [[ -n $COLA_TIEMPO_EST ]]; then
                    printf '  %sSiguientes archivos en cola (%d pendientes) — Tiempo restante cola: %s%s%s (~%s)%s\n' \
                        "$C_BOLD" "$pend_total" "$C_GREEN" "$COLA_TIEMPO_EST" "$C_RESET$C_BOLD" "$(tam "$COLA_BYTES_REST")" "$C_RESET"
                else
                    printf '  %sSiguientes archivos en cola (%d pendientes):%s\n' "$C_BOLD" "$pend_total" "$C_RESET"
                fi
                imprimir_tabla_cola "progreso" "" 10
            fi

            worker_activo > /dev/null || { echo; echo "  El proceso en segundo plano está detenido (pulsa 's' para iniciarlo)."; }
            echo
            printf '  %sControles en vivo:%s [%ss%s] Iniciar/Parar | [%sc%s] Limpiar completados | [%sv%s/%sq%s] Volver\n' \
                "$C_BOLD" "$C_RESET" "$C_BOLD$C_CYAN" "$C_RESET" "$C_BOLD$C_CYAN" "$C_RESET" "$C_BOLD$C_CYAN" "$C_RESET" "$C_BOLD$C_CYAN" "$C_RESET"
        )
        printf '\033[H%s\033[K\033[J\n' "${frame//$'\n'/$'\033[K\n'}"
        local tecla="" rc_t
        read -rsn 1 -t 1 tecla; rc_t=$?
        # Sin teclado (fin de la entrada, no un simple tiempo agotado): no quedarse en bucle
        ((rc_t != 0 && rc_t <= 128)) && [[ -z $tecla ]] && VOLVER=1
        case "${tecla,,}" in
            v | q)
                VOLVER=1 ;;
            s)
                if worker_activo > /dev/null; then
                    detener_worker > /dev/null 2>&1
                else
                    lanzar_worker > /dev/null 2>&1
                fi ;;
            c)
                con_bloqueo _filtrar_cola '$2 != "completado"' "Limpiados completados desde progreso en vivo" > /dev/null 2>&1 ;;
        esac
    done
    printf '\033[?25h'
    trap - INT
    clear 2>/dev/null
}

ver_log() {
    clear 2>/dev/null
    caja_cabecera "Últimas 40 líneas de $LOG" 88
    echo
    if [[ -f $LOG ]]; then tail -n 40 "$LOG"; else echo "  (sin registros todavía)"; fi
    pausa
}

mostrar_ayuda() {
    clear 2>/dev/null
    caja_cabecera "mover · Guía y Ayuda de Uso" 88
    cat << 'EOF'

  USO BÁSICO:
    ./mover.sh [OPCIONES] [FICHEROS O CARPETAS...]

  EJEMPLOS DE LÍNEA DE COMANDOS:
    ./mover.sh                            Inicia la interfaz interactiva (TUI)
    ./mover.sh peli.mkv                   Añade peli.mkv (operación por defecto) y pregunta si arrancar
    ./mover.sh -m -i peli.mkv             Añade peli.mkv para MOVER y arranca de inmediato
    ./mover.sh -c -d /ruta/destino *.mkv  Añade para COPIAR a /ruta/destino
    ./mover.sh --anadir ~/Descargas/Serie Añade la carpeta (manteniendo sus subcarpetas)
    ./mover.sh --iniciar                  Arranca el procesamiento de los pendientes
    ./mover.sh --parar                    Detiene de forma limpia el proceso en segundo plano
    ./mover.sh --estado                   Muestra el estado del proceso y la cola
    ./mover.sh --pendientes               Lista los pendientes y el tiempo estimado
    ./mover.sh --log [N]                  Muestra las últimas N líneas del registro (def. 40)
    -c, --copiar / -m, --mover            Operación para lo que se añade
    -d, --destino RUTA                    Carpeta destino (si no, la automática)
    -i, --iniciar                         Con ficheros: añade y arranca sin preguntar
    -h, --help                            Muestra esta ayuda y sale

  EXPLORADOR DE CARPETAS (Opción 1):
    N                  Entra en la carpeta N o añade el fichero N a la cola
    1,3,5-8            Añade varios ficheros a la vez (por lista o rangos)
    t                  Añade todos los ficheros de la vista actual
    r N                Añade la carpeta N con todas sus subcarpetas
    o                  Alterna la operación para lo que añadas: COPIAR / MOVER
    d                  Elige la carpeta destino (navegador local)
    f1, f2...          Usa un destino favorito  |  a  vuelve al destino automático
    /TEXTO             Filtra por nombre (y * para quitar el filtro)
    ..                 Sube a la carpeta superior
    v                  Vuelve al menú principal

  GESTIÓN DE LA COLA (Opción 2):
    Usa directamente el número de fila # (ej: 1, 2, 3-6) o el ID precedido de # (ej: #105):
    t                  Alterna entre ver TODOS o SOLO PENDIENTES
    ord                Ordena la cola por estado y ruta de origen
    /TEXTO             Busca por nombre, ID, estado u operación (ej: /mover, /err)
    * o /              Quita el filtro activo
    b N[,M-K]          Quita elementos de la cola
    sub N / baj N      Sube o baja 1 posición el elemento de la fila N
    top N              Pone el elemento de la fila N al principio
    m N POS            Mueve el elemento de la fila N a la fila POS (ej: m 5 2)
    p N[,M-K]          Pausa o reanuda elementos
    o N[,M-K]          Alterna COPIAR / MOVER (pendientes, pausados o con error)
    d N[,M-K]          Cambia la carpeta destino
    c / f / e / x      Limpia completados (c), fallidos (f), reintenta errores (e) o vacía (x)

  OPERACIONES Y ESTADOS:
    [COP ] / [MOV ]                       Copiar (el origen se queda) o Mover (se borra al terminar)
    PEND / REAN / PAUS / PROC / OK / ERR  Estado del elemento en la cola
EOF
}

fila_menu_2col() {  # fila_menu_2col num_izq txt_izq col_izq num_der txt_der
    local n1="$1" t1="$2" c1="${3:-}" n2="$4" t2="$5" pad
    pad=$((38 - ${#t1}))
    ((pad < 2)) && pad=2
    printf '   %s%s)%s %s%s%s%*s%s%s)%s %s\n' \
        "$C_BOLD$C_CYAN" "$n1" "$C_RESET" \
        "$c1" "$t1" "${c1:+$C_RESET}" \
        "$pad" "" \
        "$C_BOLD$C_CYAN" "$n2" "$C_RESET" \
        "$t2"
}

menu() {
    local op rc w_pid frame col_sub txt_sub
    worker_activo > /dev/null || recuperar_huerfanos
    clear 2>/dev/null
    while :; do
        w_pid=$(worker_activo 2>/dev/null) || w_pid=""
        frame=$(
            panel_resumen_menu
            if [[ -n $w_pid ]]; then
                col_sub="$C_YELLOW"
                txt_sub="⏹ Detener proceso en 2º plano"
            else
                col_sub="$C_GREEN"
                txt_sub="▶ Iniciar proceso en 2º plano"
            fi
            fila_menu_2col "1" "Explorar y añadir a la cola"  ""         "6" "Editar configuración"
            fila_menu_2col "2" "Ver / gestionar la cola"      ""         "7" "Ver configuración y espacio libre"
            fila_menu_2col "3" "Ver solo archivos pendientes" ""         "8" "Ver registro de actividad (log)"
            fila_menu_2col "4" "Ver progreso en vivo"         ""         "9" "Ayuda"
            fila_menu_2col "5" "$txt_sub"                     "$col_sub" "0" "Salir del programa  ${C_DIM}[h: Ayuda]${C_RESET}"
            separador_tabla 86
        )
        printf '\033[H%s\033[K\033[J\n' "${frame//$'\n'/$'\033[K\n'}"
        if [[ -n $w_pid ]]; then
            read -n 1 -t 2 -rp "  Opción (0-9 / Intro refresca): " op
            rc=$?
            if ((rc > 128)); then continue; fi
            ((rc != 0)) && op=0
        else
            read -n 1 -rp "  Opción (0-9): " op || op=0
        fi
        [[ -z ${op:-} ]] && continue
        echo
        case "$op" in
            1) navegar ;;
            2) menu_cola "todos" ;;
            3 | p | P) menu_cola "pendientes" ;;
            4) ver_progreso ;;
            5 | s | S)
                if worker_activo > /dev/null; then
                    detener_worker
                else
                    lanzar_worker
                fi
                pausa ;;
            6) asistente_conf; pausa ;;
            7) mostrar_conf; pausa ;;
            8) ver_log ;;
            9 | h | H | \?) mostrar_ayuda; pausa ;;
            0 | q | Q)
                worker_activo > /dev/null && echo "  El proceso sigue en segundo plano. Vuelve a abrir el programa para ver su estado."
                log "Sesión interactiva finalizada"
                return ;;
        esac
        clear 2>/dev/null
    done
}

anadir_desde_cli() {
    local auto_arrancar=0 op="$OPERACION" destino_cli="" arg f n_encontrados=0 d_abs padre dest
    local -a items=() ficheros=()

    while (($#)); do
        arg="$1"; shift
        case "$arg" in
            --anadir) ;;
            -i | --iniciar | -s | --ahora) auto_arrancar=1 ;;
            -c | --copiar) op=copiar ;;
            -m | --mover) op=mover ;;
            -d | --destino)
                (($#)) || { echo "Falta la ruta tras $arg"; exit 1; }
                destino_cli="$1"; shift ;;
            --destino=*) destino_cli="${arg#--destino=}" ;;
            --) items+=("$@"); break ;;
            -*)
                echo "Opción no reconocida: $arg"
                echo "Usa '$0 --help' para ver la lista de comandos disponibles."
                exit 1 ;;
            *) items+=("$arg") ;;
        esac
    done

    if ! conf_completa; then
        echo "Configuración incompleta. Iniciando asistente..."
        asistente_conf
    fi
    [[ -n $destino_cli ]] && destino_cli="$(ruta_absoluta "$destino_cli")"

    for arg in "${items[@]}"; do
        if [[ -f $arg ]]; then
            f="$(readlink -f -- "$arg")"
            padre="$(dirname -- "$f")"
            dest="${destino_cli:-$(destino_auto "$padre")}"
            anadir_a_cola "$op" - "$dest" "$padre" "$f"
            ((n_encontrados++))
        elif [[ -d $arg ]]; then
            d_abs="$(readlink -f -- "$arg")"
            padre="$(dirname -- "$d_abs")"
            dest="${destino_cli:-$(destino_auto "$padre")}"
            ficheros=()
            mapfile -d '' ficheros < <(find "$d_abs" -mindepth 1 -name '.*' -prune -o \( -type f -o -xtype f \) -print0 2>/dev/null | sort -z)
            if ((${#ficheros[@]})); then
                anadir_a_cola "$op" "$d_abs" "$dest" "$padre" "${ficheros[@]}"
                ((n_encontrados += ${#ficheros[@]}))
            else
                echo "Aviso: la carpeta no contiene ficheros: $arg"
            fi
        else
            echo "Aviso: no existe o no es accesible: $arg"
        fi
    done

    if ((n_encontrados == 0)); then
        echo "No se encontraron archivos válidos para añadir a la cola."
        exit 1
    fi

    echo
    if ((auto_arrancar)) || si_no "¿Deseas iniciar el procesamiento en segundo plano ahora?" s; then
        lanzar_worker
    fi
}

main() {
    local cmd faltan=""
    for cmd in rsync flock awk df stat find sort numfmt cmp setsid; do
        command -v "$cmd" > /dev/null || faltan+=" $cmd"
    done
    if [[ -n $faltan ]]; then
        echo "Faltan comandos:$faltan"
        echo "En Ubuntu/Debian: sudo apt install rsync util-linux gawk coreutils findutils diffutils"
        exit 1
    fi
    touch "$COLA"
    cargar_conf

    case "${1:-}" in
        -h | --help | help) mostrar_ayuda; exit 0 ;;
        --worker) worker; exit ;;
        --estado) resumen_estado; imprimir_cola; exit ;;
        --pendientes) imprimir_pendientes; exit ;;
        --log)
            if [[ -f $LOG ]]; then
                tail -n "${2:-40}" "$LOG"
            else
                echo "(sin registros todavía)"
            fi
            exit 0 ;;
        --parar) detener_worker; exit ;;
        --iniciar | -i)
            if (($# == 1)); then
                lanzar_worker; exit
            fi
            ;;
    esac

    if (($# > 0)); then
        anadir_desde_cli "$@"
        exit 0
    fi

    log "Sesión interactiva iniciada"
    if ! conf_completa; then
        echo "No hay configuración (o está incompleta). Vamos a crearla."
        asistente_conf
    fi
    menu
}

main "$@"
