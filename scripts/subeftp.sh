#!/usr/bin/env bash
#
# subeftp.sh - Subida de ficheros a FTP con cola, límite de ancho de banda
#              y subida en segundo plano. Requiere: bash, curl, flock, awk.
#
# Todo se guarda en la misma carpeta que este script:
#   subeftp.conf   configuración (servidor, puerto, cuenta, límite, carpetas)
#   cola.txt       cola de subidas
#   subeftp.log    registro de todo lo que ocurre
#   worker.pid     PID de la subida en segundo plano (si está activa)
#   worker.lock    bloqueo que impide dos subidas en segundo plano a la vez (lo
#                  heredan los curl de subida: mientras uno siga vivo no arranca otra)
#   curl.pid       PID e inicio del curl que está subiendo (para detectar uno huérfano)
#   orden_manual   marca: la cola se ha reordenado a mano (no auto-ordenar al añadir)
#   progreso.txt   progreso del fichero que se está subiendo
#
# Uso:
#   ./subeftp.sh               menú interactivo
#   ./subeftp.sh --subir       lanza la subida en segundo plano y sale
#   ./subeftp.sh --estado      muestra el estado de la cola y sale
#   ./subeftp.sh --pendientes  lista los archivos pendientes de subir y sale
#   ./subeftp.sh --parar       detiene la subida en segundo plano
#   ./subeftp.sh --help        muestra la ayuda y opciones disponibles

set -uo pipefail

SCRIPT="$(readlink -f "$0")"
BASE="${SUBEFTP_DATA_DIR:-$(dirname "$SCRIPT")}"
mkdir -p "$BASE"
CONF="$BASE/subeftp.conf"
COLA="$BASE/cola.txt"
LOCK="$BASE/cola.lock"
PIDF="$BASE/worker.pid"
LOG="$BASE/subeftp.log"
PROG="$BASE/progreso.txt"
WLOCK="$BASE/worker.lock"
CURLF="$BASE/curl.pid"
ORDEN_MANUAL="$BASE/orden_manual"

# Formato de cola.txt (una línea por fichero, campos separados por TAB):
#   id  estado  reanudar(0/1)  tamaño  fecha_fin  ruta_local  ruta_remota  error  firma_local
# Los campos vacíos se guardan como "-". firma_local ("tamaño:mtime" del fichero local
# cuando se encoló o empezó a subirse) puede faltar en colas de versiones anteriores.

CAMPOS=(SERVIDOR PUERTO USUARIO PASSWORD LIMITE_KBPS CARPETA_LOCAL CARPETA_REMOTA ESTRUCTURA TLS PASIVO
        REINTENTOS ESPERA_REINTENTO VERIFICAR_TAMANO)

SERVIDOR="" PUERTO="21" USUARIO="" PASSWORD="" LIMITE_KBPS="500"
CARPETA_LOCAL="$HOME" CARPETA_REMOTA="/" ESTRUCTURA="s" TLS="n" PASIVO="s"
REINTENTOS="3" ESPERA_REINTENTO="10" VERIFICAR_TAMANO="s"

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
    # Descontar margen y resto de columnas (#, ID, ESTADO, TIPO, TAMAÑO, DESTINO ~ 52 chars)
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

# Detecta el tipo de archivo Switch (BASE, UPD, DLC, NRO) y devuelve insignia coloreada de 6 chars visibles
insignia_tipo_switch() {  # insignia_tipo_switch "ruta_o_nombre"
    local nom="${1##*/}" nom_low ext tid
    nom_low="${nom,,}"
    ext="${nom_low##*.}"

    if [[ $ext == "nro" ]]; then
        printf '%s[NRO ]%s' "$C_GREEN" "$C_RESET"
        return
    fi
    if [[ ! $ext =~ ^(nsp|nsz|xci|xcz)$ ]]; then
        printf '      '
        return
    fi

    tid=""
    if [[ $nom_low =~ \[0100([0-9a-f]{12})\] ]]; then
        tid="0100${BASH_REMATCH[1]}"
    fi

    if [[ $nom_low =~ \[dlc || $nom_low =~ [^a-z]dlc[^a-z] ]]; then
        printf '%s[DLC ]%s' "$C_MAGENTA" "$C_RESET"
    elif [[ -n $tid ]]; then
        if [[ $tid == *800 ]]; then
            printf '%s[UPD ]%s' "$C_YELLOW" "$C_RESET"
        elif [[ $tid == *000 ]]; then
            if [[ $nom_low =~ \[upd|update || $nom_low =~ \[v[1-9][0-9]*\] ]]; then
                printf '%s[UPD ]%s' "$C_YELLOW" "$C_RESET"
            else
                printf '%s[BASE]%s' "$C_CYAN" "$C_RESET"
            fi
        else
            printf '%s[DLC ]%s' "$C_MAGENTA" "$C_RESET"
        fi
    elif [[ $nom_low =~ \[upd|update|patch || $nom_low =~ \[v[1-9][0-9]*\] ]]; then
        printf '%s[UPD ]%s' "$C_YELLOW" "$C_RESET"
    else
        printf '%s[BASE]%s' "$C_CYAN" "$C_RESET"
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
        subiendo)
            ETIQ_ESTADO="SUB "; COLOR_ESTADO="$C_BLUE" ;;
        completado)
            ETIQ_ESTADO=" OK "; COLOR_ESTADO="$C_GREEN" ;;
        error)
            ETIQ_ESTADO="ERR "; COLOR_ESTADO="$C_RED" ;;
        *)
            ETIQ_ESTADO="$estado"; COLOR_ESTADO="$C_RESET" ;;
    esac
}

parsear_velocidad_bps() {
    local v="${1:-0}"
    v="${v// /}"
    v="${v%/s}"
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

# Lee la última línea del medidor de progreso de curl (progreso.txt). Columnas de curl:
#   % Total  % Received  % Xferd  Dload  Upload  Total  Spent  Left  Current
# Establece PRG_PCT PRG_TOTAL PRG_ENVIADO PRG_VEL_MEDIA PRG_T_TOTAL PRG_T_PASADO
# PRG_T_RESTANTE PRG_VEL_ACTUAL. Devuelve 1 si todavía no hay datos.
leer_progreso() {
    local ult=""
    PRG_PCT="" PRG_TOTAL="" PRG_ENVIADO="" PRG_VEL_MEDIA="" PRG_T_TOTAL="" PRG_T_PASADO="" PRG_T_RESTANTE="" PRG_VEL_ACTUAL=""
    [[ -f $PROG ]] && ult=$(tr '\r' '\n' < "$PROG" 2>/dev/null | grep -E '^ *[0-9]' | tail -1)
    [[ -n $ult ]] || return 1
    read -r PRG_PCT PRG_TOTAL _ _ _ PRG_ENVIADO _ PRG_VEL_MEDIA PRG_T_TOTAL PRG_T_PASADO PRG_T_RESTANTE PRG_VEL_ACTUAL <<< "$ult"
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
        $2 == "subiendo"   { sub_c++;  sub_b += $4 }
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

urlencode() {
    local LC_ALL=C s="$1" o="" c i
    for ((i = 0; i < ${#s}; i++)); do
        c="${s:i:1}"
        case "$c" in
            [a-zA-Z0-9._~/-]) o+="$c" ;;
            *) printf -v c '%%%02X' "'$c"; o+="$c" ;;
        esac
    done
    printf '%s' "$o"
}

# --------------------------------------------------------------------------
# Configuración
# --------------------------------------------------------------------------

cargar_conf() {
    # shellcheck source=/dev/null
    [[ -f $CONF ]] && source "$CONF"
}

guardar_conf() {
    local k
    (
        umask 077
        {
            echo "# Configuración de subeftp (contiene la contraseña: no compartir)"
            for k in "${CAMPOS[@]}"; do printf '%s=%q\n' "$k" "${!k}"; done
        } > "$CONF"
    )
    chmod 600 "$CONF"
}

conf_completa() {
    [[ -n $SERVIDOR && -d $CARPETA_LOCAL ]]
}

mostrar_conf() {
    echo
    caja_cabecera "Configuración actual" 76
    printf '  %-28s %s\n' "Servidor" "$SERVIDOR" "Puerto" "$PUERTO" "Usuario" "$USUARIO" \
        "Contraseña" "$([[ -n $PASSWORD ]] && echo '******' || echo '(vacía)')" \
        "Límite (KB/s, 0=sin límite)" "$LIMITE_KBPS" "Carpeta local" "$CARPETA_LOCAL" \
        "Carpeta remota" "$CARPETA_REMOTA" "Mantener estructura carpetas" "$ESTRUCTURA" \
        "FTPS / TLS" "$TLS" "Modo pasivo" "$PASIVO" \
        "Reintentos por fichero" "$REINTENTOS" "Espera 1er reintento (s)" "$ESPERA_REINTENTO" \
        "Verificar tamaño final" "$([[ $VERIFICAR_TAMANO == n ]] && echo 'n (instalación Switch/DBI)' || echo 's (estándar)')"
}

asistente_conf() {
    echo
    caja_cabecera "Asistente de Configuración FTP" 76
    echo "  (Pulsa Intro para mantener el valor actual entre corchetes)"
    echo
    while :; do preguntar "Servidor FTP" "$SERVIDOR"; [[ -n $RESP ]] && break; echo "    El servidor es obligatorio."; done
    SERVIDOR="$RESP"
    while :; do preguntar "Puerto" "$PUERTO"; [[ $RESP =~ ^[0-9]+$ ]] && break; echo "    Debe ser un número."; done
    PUERTO="$RESP"
    preguntar "Usuario (vacío = anónimo)" "$([[ $USUARIO == anonymous ]] || echo "$USUARIO")"
    USUARIO="${RESP:-anonymous}"
    if [[ $USUARIO == anonymous ]]; then
        PASSWORD="anonymous@"
    else
        local p
        read -rsp "  Contraseña${PASSWORD:+ [Intro = mantener la actual]}: " p || p=""
        echo
        [[ -n $p ]] && PASSWORD="$p"
    fi
    while :; do preguntar "Límite de subida en KB/s (0 = sin límite)" "$LIMITE_KBPS"; [[ $RESP =~ ^[0-9]+$ ]] && break; echo "    Debe ser un número."; done
    LIMITE_KBPS="$RESP"
    while :; do
        preguntar "Carpeta local a explorar" "$CARPETA_LOCAL"
        RESP="${RESP/#\~/$HOME}"
        [[ -d $RESP ]] && break
        echo "    La carpeta $RESP no existe."
    done
    CARPETA_LOCAL="$(readlink -f "$RESP")"
    preguntar "Carpeta remota de destino (relativa al inicio FTP)" "$CARPETA_REMOTA"
    RESP="${RESP#/}"; RESP="${RESP%/}"
    CARPETA_REMOTA="/$RESP"
    echo "    Mantener estructura: s = recrear subcarpetas locales en el servidor;"
    echo "                         n = subir todos los ficheros sueltos a la carpeta destino"
    while :; do preguntar "Mantener estructura de carpetas en el servidor (s/n)" "$ESTRUCTURA"; [[ $RESP =~ ^[sSnN]$ ]] && break; echo "    Responde s o n."; done
    ESTRUCTURA="${RESP,,}"
    while :; do preguntar "Usar FTPS / TLS (s/n)" "$TLS"; [[ $RESP =~ ^[sSnN]$ ]] && break; echo "    Responde s o n."; done
    TLS="${RESP,,}"
    while :; do preguntar "Modo pasivo (s/n)" "$PASIVO"; [[ $RESP =~ ^[sSnN]$ ]] && break; echo "    Responde s o n."; done
    PASIVO="${RESP,,}"
    while :; do preguntar "Reintentos automáticos si falla la red (0 = ninguno)" "$REINTENTOS"; [[ $RESP =~ ^[0-9]+$ ]] && break; echo "    Debe ser un número."; done
    REINTENTOS="$RESP"
    while :; do preguntar "Segundos de espera antes del 1er reintento" "$ESPERA_REINTENTO"; [[ $RESP =~ ^[0-9]+$ ]] && break; echo "    Debe ser un número."; done
    ESPERA_REINTENTO="$RESP"
    echo "    Verificar tamaño final: s = comprobar tamaño en el servidor al terminar;"
    echo "                            n = omitir comprobación (imprescindible en Switch/DBI)"
    while :; do preguntar "Verificar tamaño en el servidor al terminar (s/n)" "$VERIFICAR_TAMANO"; [[ $RESP =~ ^[sSnN]$ ]] && break; echo "    Responde s o n."; done
    VERIFICAR_TAMANO="${RESP,,}"

    guardar_conf
    log "Configuración guardada (servidor=$SERVIDOR puerto=$PUERTO usuario=$USUARIO limite=${LIMITE_KBPS}KB/s local=$CARPETA_LOCAL remota=$CARPETA_REMOTA estructura=$ESTRUCTURA tls=$TLS pasivo=$PASIVO reintentos=$REINTENTOS espera=${ESPERA_REINTENTO}s verificar_tamano=$VERIFICAR_TAMANO)"
    echo
    printf '  %s✓ Configuración guardada en %s%s\n' "$C_GREEN" "$CONF" "$C_RESET"
    if si_no "¿Probar la conexión ahora?" s; then probar_conexion; fi
}

# --------------------------------------------------------------------------
# FTP (curl)
# --------------------------------------------------------------------------

credenciales_curl() {
    local u="${USUARIO:-anonymous}:${PASSWORD:-anonymous@}"
    u="${u//\\/\\\\}"; u="${u//\"/\\\"}"
    printf 'user = "%s"\n' "$u"
}

opciones_conexion() {
    CONN_OPTS=(-sS --globoff --connect-timeout 30 --speed-time 30 --speed-limit 100 --tcp-nodelay --keepalive-time 20)
    [[ $TLS == s ]] && CONN_OPTS+=(--ssl-reqd)
    if [[ $PASIVO == n ]]; then
        CONN_OPTS+=(--ftp-port -)
    else
        CONN_OPTS+=(--disable-epsv)
    fi
}

opciones_curl() {  # como opciones_conexion pero sin -s, para que curl escriba el progreso
    local o
    opciones_conexion
    CURL_OPTS=()
    for o in "${CONN_OPTS[@]}"; do
        [[ $o == -sS || $o == -s || $o == --silent ]] || CURL_OPTS+=("$o")
    done
    CURL_OPTS+=(--ftp-create-dirs)
    ((LIMITE_KBPS > 0)) && CURL_OPTS+=(--limit-rate "${LIMITE_KBPS}K")
}

tam_remoto() {  # tam_remoto /ruta/remota -> tamaño; código 2 si no existe, 1 si no se sabe
    local salida rc
    opciones_conexion
    salida=$(curl "${CONN_OPTS[@]}" --max-time 20 -K <(credenciales_curl) -I "$(url_ftp "$1")" 2>&1); rc=$?
    ((rc == 78)) && return 2
    ((rc == 0)) || return 1
    salida=$(tr -d '\r' <<< "$salida" | sed -n 's/^Content-Length: *//Ip' | head -1)
    [[ $salida =~ ^[0-9]+$ ]] || return 1
    echo "$salida"
}

url_ftp() {  # url_ftp /ruta/remota
    local ruta="${1#/}"
    printf 'ftp://%s:%s/%s' "$SERVIDOR" "$PUERTO" "$(urlencode "$ruta")"
}

# Usa prefijos[], ips_locales[] y encontradas[] de buscar_switch_red (ámbito dinámico)
_escanear_red() {  # _escanear_red puerto "texto del puerto"
    local pto="$1" desc="$2" prefijo i lote ip_enc
    for prefijo in "${prefijos[@]}"; do
        echo "  Escaneando red ${prefijo}.1-254 $desc..."
        while IFS= read -r ip_enc; do
            [[ -z $ip_enc ]] && continue
            [[ " ${ips_locales[*]:-} " == *" $ip_enc "* ]] && continue
            encontradas+=("${pto}|${ip_enc}")
        done < <(
            lote=0
            for i in {1..254}; do
                (
                    timeout 1.8 bash -c '>/dev/tcp/"$1"/"$2"' _ "$prefijo.$i" "$pto" 2>/dev/null \
                        && printf '%s\n' "$prefijo.$i"
                ) &
                lote=$((lote + 1))
                if ((lote >= 50)); then
                    wait
                    lote=0
                fi
            done
            wait
        )
    done
}

_leer_banner_ftp() {  # _leer_banner_ftp ip puerto
    local linea=""
    linea=$(timeout 2 bash -c '
        exec 3<>"/dev/tcp/$1/$2" 2>/dev/null || exit 1
        l=""
        IFS= read -r -t 1.5 l <&3 || true
        exec 3>&- 3<&-
        printf "%s\n" "$l"
    ' _ "$1" "$2" 2>/dev/null) || true
    printf '%s\n' "${linea//$'\r'/}"
}

_anadir_prefijo_unico() {  # _anadir_prefijo_unico ip_o_prefijo (usa prefijos[])
    local val="$1" p=""
    if [[ $val =~ ^([0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3})(\.[0-9]{1,3})?$ ]]; then
        p="${BASH_REMATCH[1]}"
    fi
    [[ -z $p || $p == "127.0.0" ]] && return 0
    [[ " ${prefijos[*]:-} " == *" $p "* ]] || prefijos+=("$p")
}

_ofrecer_encontradas() {  # Usa encontradas[] y devuelve 0 si el usuario aceptó alguna
    local entrada pto ip_enc banner etiqueta
    for entrada in "${encontradas[@]}"; do
        pto="${entrada%%|*}"
        ip_enc="${entrada#*|}"
        banner="$(_leer_banner_ftp "$ip_enc" "$pto")"
        if [[ $banner == 220* ]]; then
            etiqueta=" [FTP: ${banner#220 }]"
        elif [[ -n $banner ]]; then
            etiqueta=" [respuesta: $banner]"
        else
            etiqueta=" [puerto abierto]"
        fi
        printf '  %s¡Dispositivo detectado en %s:%s%s!%s\n' \
            "$C_BOLD$C_GREEN" "$ip_enc" "$pto" "$etiqueta" "$C_RESET"
        if si_no "¿Actualizar la configuración para usar $ip_enc:$pto?" s; then
            SERVIDOR="$ip_enc"
            PUERTO="$pto"
            guardar_conf
            log "Servidor actualizado por auto-descubrimiento a $SERVIDOR:$PUERTO"
            echo "  Configuración actualizada a $SERVIDOR:$PUERTO."
            return 0
        fi
    done
    return 1
}

buscar_switch_red() {
    local pto_conf="${PUERTO:-5000}" ip_tmp pto_scan
    local -a prefijos=() ips_locales=() encontradas=() puertos_principales=() puertos_extra=() host_ips=() extra_lista=()

    # Recoger todas las IPs locales (para no detectarnos a nosotros mismos)
    if command -v ip > /dev/null 2>&1; then
        while IFS= read -r ip_tmp; do
            [[ -n $ip_tmp ]] && ips_locales+=("$ip_tmp")
        done < <(ip -o -4 addr show up 2>/dev/null | awk '{ sub(/\/.*/, "", $4); print $4 }')

        # Subredes de interfaces físicas activas (excluyendo loopback, Docker, bridges y VPN)
        while IFS= read -r ip_tmp; do
            _anadir_prefijo_unico "$ip_tmp"
        done < <(ip -o -4 addr show up 2>/dev/null | awk '
            $2 !~ /^(lo|docker|br-|veth|virbr|tun|tap|wg|tailscale)/ {
                sub(/\/.*/, "", $4); print $4
            }')

        # Ruta por defecto como respaldo si no hubo interfaces físicas reconocidas
        if ((${#prefijos[@]} == 0)); then
            ip_tmp=$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')
            _anadir_prefijo_unico "$ip_tmp"
        fi
    fi

    if ((${#prefijos[@]} == 0)) && command -v hostname > /dev/null 2>&1; then
        read -ra host_ips <<< "$(hostname -I 2>/dev/null || true)"
        for ip_tmp in "${host_ips[@]}"; do
            ips_locales+=("$ip_tmp")
            _anadir_prefijo_unico "$ip_tmp"
        done
    fi

    # Si había una IP previa en SERVIDOR de otra subred, incluirla también
    _anadir_prefijo_unico "$SERVIDOR"

    if ((${#prefijos[@]} == 0)); then
        echo "  No se pudo determinar la subred local automáticamente."
        preguntar "Introduce la subred /24 a escanear (ej: 192.168.1) o Enter para cancelar" ""
        _anadir_prefijo_unico "$RESP"
        ((${#prefijos[@]} == 0)) && return 1
    fi

    # Priorizar el puerto configurado si es específico (distinto de 21 y 5000),
    # y siempre incluir 5000 (puerto por defecto de DBI / ftpd en Switch).
    if [[ $pto_conf =~ ^[0-9]+$ ]] && [[ $pto_conf != "21" && $pto_conf != "5000" ]]; then
        puertos_principales=("$pto_conf" 5000)
    else
        puertos_principales=(5000)
    fi

    for pto_scan in "${puertos_principales[@]}"; do
        encontradas=()
        if [[ $pto_scan == "5000" ]]; then
            _escanear_red "$pto_scan" "en el puerto DBI/Switch ($pto_scan)"
        else
            _escanear_red "$pto_scan" "en el puerto $pto_scan"
        fi
        if ((${#encontradas[@]} > 0)); then
            _ofrecer_encontradas && return 0
        fi
    done

    echo "  No se encontró ninguna Switch en el puerto ${puertos_principales[*]}."
    echo "  Comprueba que la pantalla esté encendida, el servidor FTP de DBI/ftpd iniciado y en la misma red."

    # Puertos alternativos habituales (2121, 21) o puerto manual
    for pto_scan in 2121 21; do
        [[ " ${puertos_principales[*]} " == *" $pto_scan "* ]] || puertos_extra+=("$pto_scan")
    done

    if ((${#puertos_extra[@]} > 0)) && si_no "¿Buscar también en puertos alternativos (${puertos_extra[*]}) o indicar otro?" n; then
        preguntar "Puerto(s) a probar (Enter = ${puertos_extra[*]})" "${puertos_extra[*]}"
        read -ra extra_lista <<< "$RESP"
        for pto_scan in "${extra_lista[@]}"; do
            [[ $pto_scan =~ ^[0-9]+$ ]] || continue
            encontradas=()
            _escanear_red "$pto_scan" "en el puerto $pto_scan"
            if ((${#encontradas[@]} > 0)); then
                _ofrecer_encontradas && return 0
            fi
        done
        echo "  Tampoco se encontró ningún servidor FTP en esos puertos."
    fi
    return 1
}

probar_conexion() {
    local dir="${CARPETA_REMOTA#/}" salida
    opciones_conexion
    local -a opts=("${CONN_OPTS[@]}" --list-only)
    echo "  Conectando a $SERVIDOR:$PUERTO..."
    if salida=$(curl "${opts[@]}" -K <(credenciales_curl) "$(url_ftp "/")" 2>&1); then
        printf '  %s✓ Conexión correcta.%s\n' "$C_GREEN" "$C_RESET"
        log "Prueba de conexión correcta con $SERVIDOR:$PUERTO"
        if [[ -n $dir ]]; then
            if curl "${opts[@]}" -K <(credenciales_curl) "$(url_ftp "/$dir")/" > /dev/null 2>&1; then
                echo "  Carpeta remota $CARPETA_REMOTA accesible."
            else
                echo "  Aviso: la carpeta remota $CARPETA_REMOTA no existe todavía; se creará al subir."
            fi
        fi
    else
        printf '  %s✗ ERROR:%s %s\n' "$C_RED" "$C_RESET" "$salida"
        log "ERROR prueba de conexión con $SERVIDOR:$PUERTO: $salida"
        echo
        if si_no "¿Buscar automáticamente la nueva IP de la Switch en la red local?" s; then
            if buscar_switch_red; then
                echo
                probar_conexion
            fi
        fi
    fi
}

listar_carpetas_remotas() {  # listar_carpetas_remotas /ruta
    local url salida
    opciones_conexion
    local -a opts=("${CONN_OPTS[@]}")
    url="$(url_ftp "$1")"
    [[ $url == */ ]] || url+="/"
    salida=$(curl "${opts[@]}" -K <(credenciales_curl) "$url" 2>&1) || { printf '%s\n' "$salida"; return 1; }
    printf '%s\n' "$salida" | tr -d '\r' | sed -nE \
        -e 's/^d([^ ]+ +){8}//p' \
        -e 's/^([^ ]+ +){2}<DIR> +//p' | grep -vxE '\.\.?' | sort
    return 0
}

elegir_carpeta_remota() {  # elegir_carpeta_remota /carpeta/inicial [permitir_automatico]
    local actual="${1:-/}" automatico="${2:-}" salida op n i existe
    local -a carpetas=()
    while :; do
        clear 2>/dev/null
        caja_cabecera "Carpeta de destino en $SERVIDOR: $actual" 84
        echo
        existe=1
        if salida=$(listar_carpetas_remotas "$actual"); then
            mapfile -t carpetas <<< "$salida"
            [[ -z ${carpetas[0]:-} ]] && carpetas=()
            ((${#carpetas[@]} == 0)) && echo "  (sin subcarpetas)"
            for i in "${!carpetas[@]}"; do
                printf '  %4d) %s[DIR ]%s %s%s/%s\n' $((i + 1)) "$C_BOLD$C_BLUE" "$C_RESET" "$C_BOLD$C_BLUE" "${carpetas[i]}" "$C_RESET"
            done
        else
            existe=0
            carpetas=()
            echo "  No se puede listar esta carpeta: ${salida##*$'\n'}"
            echo "  (si es una carpeta nueva, se creará al subir el primer fichero)"
        fi
        echo
        echo "  Opciones:"
        echo "    N          entrar en la carpeta N          ..       carpeta superior"
        echo "    u          USAR esta carpeta como destino  v        cancelar"
        echo "    inst / nsp ir a /install o /nsp (Switch)   sw / sd  ir a /switch o raíz /"
        echo "    n NOMBRE   crear nueva subcarpeta          e RUTA   escribir ruta (ej: e /backups)"
        [[ -n $automatico ]] && echo "    a          volver a destino automático"
        echo
        read -rep "  Opción: " op || op="v"
        case "$op" in
            v | q | "") return 1 ;;
            u)
                ((existe)) || echo "  Nota: $actual aún no existe; se creará al subir."
                ELEGIDA="$actual"; return 0 ;;
            inst) actual="/install" ;;
            nsp) actual="/nsp" ;;
            sw) actual="/switch" ;;
            sd) actual="/" ;;
            a) [[ -n $automatico ]] && return 2 ;;
            ..) actual="$(dirname "$actual")" ;;
            "n "* | "e "*)
                n="${op:2}"; n="${n#"${n%%[! ]*}"}"; n="${n%/}"
                [[ -z $n ]] && continue
                if [[ $op == e* ]]; then
                    actual="/${n#/}"
                elif [[ $n == */* ]]; then
                    echo "  El nombre no puede contener '/'. Usa 'e RUTA' para rutas completas."; pausa
                else
                    actual="${actual%/}/$n"
                fi ;;
            *)
                if [[ $op =~ ^[0-9]+$ ]] && ((op >= 1 && op <= ${#carpetas[@]})); then
                    actual="${actual%/}/${carpetas[op - 1]}"
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

firma_local() {  # firma_local fichero -> "tamaño:mtime"
    stat -L -c '%s:%Y' -- "$1" 2>/dev/null
}

_tomar_siguiente() {
    local linea
    linea=$(awk -F'\t' '$2 == "pendiente" { print; exit }' "$COLA")
    [[ -z $linea ]] && return 1
    _cola_set "${linea%%$'\t'*}" subiendo "$(cut -f3 <<< "$linea")" -
    printf '%s\n' "$linea"
}

_anadir() {
    local destino="${1%/}" base="$2" f rel remoto id n=0 omitidos_ok=0 total_args tamano filas_antes
    shift 2
    total_args=$#
    filas_antes=$(wc -l < "$COLA")
    # Si no queda nada por subir, el orden manual anterior ya no aplica
    grep -qE $'\t(pendiente|subiendo|pausado)\t' "$COLA" || rm -f "$ORDEN_MANUAL"
    id=$(awk -F'\t' 'BEGIN { m = 0 } $1 > m { m = $1 } END { print m + 1 }' "$COLA")
    for f in "$@"; do
        if [[ $f == *$'\t'* || $f == *$'\n'* ]]; then
            echo "  Omitido (nombre con tabuladores o saltos de línea): $f"
            continue
        fi
        if L="$f" awk -F'\t' '$6 == ENVIRON["L"] && ($2 == "pendiente" || $2 == "subiendo" || $2 == "pausado") { e = 1 } END { exit !e }' "$COLA"; then
            echo "  Ya está en la cola: ${f#"$CARPETA_LOCAL"/}"
            continue
        fi
        if ((total_args > 1)) && L="$f" awk -F'\t' '$6 == ENVIRON["L"] && $2 == "completado" { e = 1 } END { exit !e }' "$COLA"; then
            ((omitidos_ok++))
            continue
        fi
        if [[ $ESTRUCTURA == n || $f != "$base"/* ]]; then rel="${f##*/}"; else rel="${f#"$base"/}"; fi
        remoto="$destino/$rel"
        if R="$remoto" awk -F'\t' '$7 == ENVIRON["R"] && ($2 == "pendiente" || $2 == "subiendo" || $2 == "pausado") { e = 1 } END { exit !e }' "$COLA"; then
            echo "  Omitido: otro fichero de la cola ya se sube a $remoto (${f#"$CARPETA_LOCAL"/})"
            log "No añadido $f: el destino $remoto ya lo usa otro elemento de la cola"
            continue
        fi
        tamano=$(stat -L -c %s -- "$f" 2>/dev/null) || { echo "  Omitido (no se puede leer): $f"; continue; }
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$id" pendiente 0 "$tamano" - "$f" "$remoto" - "$(firma_local "$f")" >> "$COLA"
        log "Añadido a la cola #$id: $f -> $remoto ($(tam "$tamano"))"
        ((id++, n++))
    done
    if ((n > 0)); then
        if [[ -f $ORDEN_MANUAL ]]; then
            # Orden manual: no se toca lo que ya estaba; solo se ordenan los nuevos entre sí.
            _reescribir_cola _ordenar_desde "$filas_antes"
            echo "  $n fichero(s) añadido(s) al final de la cola (se respeta tu orden manual; 'ord' reordena toda la cola).$( ((omitidos_ok > 0)) && echo " [$omitidos_ok omitido(s) por estar ya completados]" )"
        else
            _auto_ordenar_switch > /dev/null
            echo "  $n fichero(s) añadido(s) y ordenados (Base ➔ Update ➔ DLC).$( ((omitidos_ok > 0)) && echo " [$omitidos_ok omitido(s) por estar ya completados]" )"
        fi
    else
        echo "  0 ficheros añadidos.$( ((omitidos_ok > 0)) && echo " [$omitidos_ok omitido(s) porque ya estaban completados en la cola]" )"
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
    if [[ $modo == "pendientes" ]] && ! grep -qE $'\t(pendiente|subiendo|pausado)\t' "$COLA" 2>/dev/null; then
        echo "  (no hay archivos pendientes ni en proceso)"
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
        "$C_BOLD" "#" "ID" "ESTADO" "TIPO" "FICHERO" "TAMAÑO" "DESTINO" "$C_RESET"
    separador_tabla "$w_tot"

    local id estado reanudar tamano local_f remoto error pos=0 mostrados=0 total_bytes=0
    local nom nom_completo dest badge filtro_low="${filtro,,}"
    while IFS=$'\t' read -r id estado reanudar tamano _ local_f remoto error _; do
        ((pos++))

        if [[ $modo == "pendientes" ]]; then
            [[ $estado == pendiente || $estado == subiendo || $estado == pausado ]] || continue
        elif [[ $modo == "progreso" ]]; then
            [[ $estado == pendiente || $estado == pausado ]] || continue
        fi

        info_estado_cola "$estado" "$reanudar"
        nom_completo="${local_f##*/}"
        dest="${remoto%/*}"
        dest="${dest:-/}"

        if [[ -n $filtro_low ]]; then
            local texto_busq="$pos #$id $estado $ETIQ_ESTADO $nom_completo $dest $error"
            [[ ${texto_busq,,} == *"$filtro_low"* ]] || continue
        fi

        ((mostrados++))
        ((total_bytes += tamano))
        if ((max_filas > 0 && mostrados > max_filas)); then
            continue
        fi

        badge="$(insignia_tipo_switch "$nom_completo")"
        nom="$(acortar_nombre "$nom_completo" "$w_fich")"
        local pad_nom=$((w_fich - ${#nom}))
        ((pad_nom < 0)) && pad_nom=0

        printf "  %2d  #%-4s %s[%s]%s %s %s%*s %10s  %s\n" \
            "$pos" "$id" "$COLOR_ESTADO" "$ETIQ_ESTADO" "$C_RESET" "$badge" "$nom" "$pad_nom" "" "$(tam "$tamano")" "$dest"

        if [[ $estado == error && $modo != "progreso" ]]; then
            printf '       %s└─> Fallo: %s%s\n' "$C_RED" "$error" "$C_RESET"
        fi
    done < "$COLA"

    if [[ -n $filtro && $mostrados -eq 0 ]]; then
        printf '  %s(ningún elemento coincide con la búsqueda "%s")%s\n' "$C_DIM" "$filtro" "$C_RESET"
    fi

    separador_tabla "$w_tot"

    if ((max_filas > 0 && mostrados > max_filas)); then
        printf '  %s... y %d archivo(s) más en cola (ver todos en opción 2)%s\n' \
            "$C_DIM" $((mostrados - max_filas)) "$C_RESET"
    elif [[ $modo == "pendientes" ]]; then
        echo "  Total pendientes / en curso: $mostrados archivo(s), $(tam "$total_bytes")"
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
        ult_vel="${PRG_VEL_MEDIA:-${PRG_VEL_ACTUAL:-}}"
        ult_pct="${PRG_PCT:-0}"
    fi
    calcular_tiempo_cola "$ult_vel" "$ult_pct"
    if [[ -n $COLA_TIEMPO_EST ]]; then
        local origen_vel=""
        if [[ -n $ult_vel && $ult_vel != "?" ]]; then
            origen_vel="a $ult_vel/s"
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
    if pid=$(worker_activo); then
        pid="${C_GREEN}ACTIVA (PID $pid)${C_RESET}"
    elif pid=$(curl_huerfano); then
        pid="${C_RED}SIN WORKER: curl huérfano (PID $pid) sigue subiendo${C_RESET}"
    else
        pid="${C_DIM}DETENIDA${C_RESET}"
    fi
    printf '  Cola: %s | Subida en 2º plano: %s\n' "${cuentas:-vacía}" "$pid"
}

panel_resumen_menu() {
    touch "$COLA"
    local pid estado_proceso w_panel=84
    if pid=$(worker_activo); then
        estado_proceso="${C_BOLD}${C_GREEN}● ACTIVA (PID $pid)${C_RESET}"
    elif pid=$(curl_huerfano); then
        estado_proceso="${C_BOLD}${C_RED}⚠ curl huérfano (PID $pid)${C_RESET}"
    else
        estado_proceso="${C_YELLOW}○ DETENIDA${C_RESET}"
    fi

    local datos
    datos=$(awk -F'\t' '
        BEGIN { pend_c=0; pend_b=0; paus_c=0; err_c=0; ok_c=0; ok_b=0; sub_nom=""; sub_b=0 }
        $2 == "pendiente" { pend_c++; pend_b += $4 }
        $2 == "pausado"   { paus_c++ }
        $2 == "error"     { err_c++ }
        $2 == "completado"{ ok_c++; ok_b += $4 }
        $2 == "subiendo"  {
            n = split($6, p, "/")
            sub_nom = p[n]
            sub_b = $4
        }
        END {
            printf "%d|%s|%d|%d|%d|%s|%s|%s", pend_c, pend_b, paus_c, err_c, ok_c, ok_b, sub_nom, sub_b
        }
    ' "$COLA" 2>/dev/null)

    local pend_c pend_b paus_c err_c ok_c ok_b sub_nom sub_b
    IFS='|' read -r pend_c pend_b paus_c err_c ok_c ok_b sub_nom sub_b <<< "$datos"
    pend_c=${pend_c:-0}; pend_b=${pend_b:-0}; paus_c=${paus_c:-0}; err_c=${err_c:-0}; ok_c=${ok_c:-0}; ok_b=${ok_b:-0}

    caja_cabecera "subeftp · Gestor FTP y Nintendo Switch (DBI)" "$w_panel"
    printf '   %sServidor:%s %s@%s:%s %s   %sLímite:%s %s   %sWorker:%s %s\n' \
        "$C_BOLD" "$C_RESET" "${USUARIO:-anonymous}" "$SERVIDOR" "$PUERTO" \
        "$([[ $TLS == s ]] && echo '[TLS]' || echo '[Sin TLS]')" \
        "$C_BOLD" "$C_RESET" "$([[ $LIMITE_KBPS == 0 ]] && echo 'Sin límite' || echo "${LIMITE_KBPS} KB/s")" \
        "$C_BOLD" "$C_RESET" "$estado_proceso"
    separador_tabla "$((w_panel + 2))"

    local ult_vel="" ult_pct=0
    if [[ -n ${sub_nom:-} ]]; then
        printf '   %s>> SUBIENDO:%s %s %s%s%s (%s)\n' \
            "$C_BOLD$C_BLUE" "$C_RESET" "$(insignia_tipo_switch "$sub_nom")" "$C_BOLD" "$(acortar_nombre "$sub_nom" 56)" "$C_RESET" "$(tam "$sub_b")"
        if leer_progreso; then
            ult_vel="${PRG_VEL_MEDIA:-${PRG_VEL_ACTUAL:-}}"
            ult_pct="${PRG_PCT:-0}"
            printf '      Archivo : %s %s / %s | %s/s | Falta: %s\n' \
                "$(barra_progreso "$PRG_PCT" 22 "$C_GREEN")" "${PRG_ENVIADO:-0}" "${PRG_TOTAL:-$(tam "$sub_b")}" "${PRG_VEL_ACTUAL:-${PRG_VEL_MEDIA:-?}}" "${PRG_T_RESTANTE:-?}"
        else
            printf '      Archivo : %s (iniciando transferencia...)\n' "$(barra_progreso 0 22 "$C_GREEN")"
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
# Worker (subida en segundo plano)
# --------------------------------------------------------------------------

worker_activo() {
    local pid
    pid=$(cat "$PIDF" 2>/dev/null) || return 1
    [[ $pid =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null \
        && tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q -- '--worker' \
        && echo "$pid"
}

# Instante de arranque de un proceso (campo 22 de /proc/PID/stat): junto con el PID
# identifica el proceso sin riesgo de confundirlo con otro que reutilice el PID.
inicio_proceso() {  # inicio_proceso pid
    local st
    st=$(cat "/proc/$1/stat" 2>/dev/null) || return 1
    read -ra st <<< "${st##*) }"
    [[ ${st[19]:-} =~ ^[0-9]+$ ]] && echo "${st[19]}"
}

# curl de subida que sigue vivo sin su worker (el worker murió con kill -9, OOM...).
# Solo se da por nuestro si coincide con curl.pid (PID e instante de arranque), es un
# curl y tiene abierto worker.lock. Imprime su PID.
curl_huerfano() {
    local pid ini fd
    worker_activo > /dev/null && return 1
    { read -r pid ini < "$CURLF"; } 2>/dev/null || return 1
    [[ $pid =~ ^[0-9]+$ && -n $ini ]] || return 1
    [[ $(inicio_proceso "$pid") == "$ini" ]] || return 1
    [[ $(cat "/proc/$pid/comm" 2>/dev/null) == curl ]] || return 1
    for fd in "/proc/$pid/fd/"*; do
        if [[ $(readlink "$fd" 2>/dev/null) == "$WLOCK" ]]; then
            echo "$pid"
            return 0
        fi
    done
    return 1
}

wlock_libre() {
    ( flock -n 8 ) 8> "$WLOCK"
}

lanzar_worker() {
    local pid
    if pid=$(worker_activo); then
        echo "  La subida en segundo plano ya está en marcha (PID $pid)."
        return
    fi
    recuperar_huerfanos
    if ! wlock_libre; then
        if pid=$(curl_huerfano); then
            echo "  Una subida anterior (curl PID $pid) sigue viva sin su proceso en segundo plano."
            echo "  Deténla primero (opción 5 del menú o --parar) o espera a que termine."
        else
            echo "  Otra subida anterior todavía está terminando (worker.lock ocupado); inténtalo en unos segundos."
        fi
        return 1
    fi
    if ! grep -q $'\tpendiente\t' "$COLA" 2>/dev/null; then
        echo "  No hay elementos pendientes en la cola."
        return
    fi
    setsid nohup "$SCRIPT" --worker < /dev/null > /dev/null 2>&1 &
    log "Lanzada subida en segundo plano (PID $!)"
    printf '  %s▶ Subida en segundo plano iniciada (PID %s).%s Puedes cerrar este programa.\n' "$C_GREEN" "$!" "$C_RESET"
}

detener_worker() {
    local pid i
    if ! pid=$(worker_activo); then
        if pid=$(curl_huerfano); then
            kill -TERM "$pid" 2>/dev/null
            log "Detenido curl huérfano de una subida interrumpida (PID $pid)"
            for ((i = 0; i < 50; i++)); do
                kill -0 "$pid" 2>/dev/null || break
                sleep 0.1
            done
            if kill -0 "$pid" 2>/dev/null; then
                printf '  %s⏹ Señal de parada enviada al curl huérfano (PID %s);%s todavía está terminando.\n' "$C_YELLOW" "$pid" "$C_RESET"
            else
                recuperar_huerfanos
                printf '  %s⏹ Detenido el curl huérfano (PID %s).%s El fichero se reanudará al volver a iniciar.\n' "$C_YELLOW" "$pid" "$C_RESET"
            fi
            return
        fi
        echo "  No hay ninguna subida en segundo plano activa."
        return
    fi
    kill -TERM "$pid"
    log "Solicitada parada de la subida en segundo plano (PID $pid)"
    printf '  %s⏹ Señal de parada enviada al PID %s.%s El fichero en curso se reanudará al volver a iniciar.\n' "$C_YELLOW" "$pid" "$C_RESET"
}

_recuperar_subiendo() {
    _reescribir_cola awk -F'\t' -v OFS='\t' '$2 == "subiendo" { $2 = "pendiente"; $3 = 1 } { print }'
}

# Filas "subiendo" que dejó un worker muerto sin limpiar (kill -9, OOM, apagado):
# si nadie tiene worker.lock (ni un worker ni un curl huérfano que lo haya heredado),
# nadie las está subiendo -> vuelven a pendiente.
recuperar_huerfanos() {
    (
        flock -n 8 || exit 0
        rm -f "$CURLF"
        grep -q $'\tsubiendo\t' "$COLA" 2>/dev/null || exit 0
        con_bloqueo _recuperar_subiendo
        log "Recuperadas filas 'subiendo' huérfanas de una subida interrumpida"
    ) 8> "$WLOCK"
}

ERRORES_DEFINITIVOS=" 2 3 9 25 26 67 "
ERRORES_REANUDAR=" 31 36 "

esperar() {
    local i
    for ((i = 0; i < $1 && !PARAR; i++)); do sleep 1; done
}

# curl hereda worker.lock (fd 8): si el worker muere de golpe (kill -9), no puede
# arrancar otro que suba el mismo fichero mientras ese curl siga vivo. curl.pid
# permite al menú reconocerlo y detenerlo (ver curl_huerfano).
subir_una_vez() {  # subir_una_vez local remoto reanudar
    local -a args=("${CURL_OPTS[@]}")
    [[ $3 == 1 ]] && args+=(-C -)
    : > "$PROG"
    : > "$PROG.w"
    curl "${args[@]}" -K <(credenciales_curl) -T "$1" "$(url_ftp "$2")" \
        -w '%{size_upload} %{speed_upload} %{time_total}' -o /dev/null > "$PROG.w" 2> "$PROG" &
    CURL_PID=$!
    echo "$CURL_PID $(inicio_proceso "$CURL_PID")" > "$CURLF"
    # Si --parar llegó antes de conocer el PID, la trap no pudo matar a curl
    ((PARAR)) && kill -TERM "$CURL_PID" 2>/dev/null
    wait "$CURL_PID"; RC=$?
    while kill -0 "$CURL_PID" 2>/dev/null; do wait "$CURL_PID"; RC=$?; done
    CURL_PID=""
    rm -f "$CURLF"
    ENVIADOS=0 VELOCIDAD=0 SEGUNDOS="?"
    read -r ENVIADOS VELOCIDAD SEGUNDOS < "$PROG.w" 2> /dev/null
    [[ $ENVIADOS =~ ^[0-9]+$ ]] || ENVIADOS=0
    MSG=""
    if ((RC != 0)); then
        MSG=$(tr '\r' '\n' < "$PROG" | grep -o 'curl: ([0-9]*).*' | tail -1)
        MSG="${MSG:-curl terminó con código $RC}"
        MSG="${MSG//$'\t'/ }"
    fi
}

decidir_reanudar() {
    local r rc
    r=$(tam_remoto "$remoto"); rc=$?
    if ((rc == 0)); then
        remoto_tam="$r"
        ((r > 0 && r < local_tam)) && reanudar=1 || reanudar=0
        return
    fi
    remoto_tam=""
    if ((rc == 2)); then reanudar=0; return; fi
    leer_progreso
    if ((ENVIADOS > 0)) || [[ -n $PRG_ENVIADO && $PRG_ENVIADO != 0 ]]; then reanudar=1; fi
}

worker() {
    ORIGEN="worker"
    cargar_conf
    # worker.lock se mantiene (fd 8) mientras viva el worker: impide dos a la vez
    exec 8> "$WLOCK"
    if ! flock -n 8; then
        log "Ya hay otra subida en segundo plano activa; saliendo"
        exit 0
    fi
    echo $$ > "$PIDF"
    PARAR=0 CURL_PID=""
    trap 'PARAR=1; [[ -n $CURL_PID ]] && kill -TERM "$CURL_PID" 2>/dev/null' TERM INT
    trap '[[ $(cat "$PIDF" 2>/dev/null) == "$$" ]] && rm -f "$PIDF"; rm -f "$PROG" "$PROG.w" "$CURLF"' EXIT

    con_bloqueo _recuperar_subiendo

    log "Subida en segundo plano iniciada (límite ${LIMITE_KBPS} KB/s, $REINTENTOS reintentos)"
    opciones_curl
    local subidos=0 errores=0 omitidos=0 linea id reanudar ruta_local remoto firma firma_act
    local local_tam remoto_tam rc_tam intento espera espera_max final detalle k
    while ((!PARAR)); do
        linea=$(con_bloqueo _tomar_siguiente) || break
        IFS=$'\t' read -r id _ reanudar _ _ ruta_local remoto _ firma <<< "$linea"

        if [[ ! -f $ruta_local || ! -r $ruta_local ]]; then
            log "ERROR #$id: no se puede leer $ruta_local"
            cola_set "$id" error 0 "no se puede leer el fichero local"
            ((errores++))
            continue
        fi
        local_tam=$(stat -L -c %s -- "$ruta_local")

        # Si el fichero local ha cambiado desde que se encoló o empezó a subirse,
        # lo que haya en el servidor es de otra versión: no se puede reanudar.
        firma_act=$(firma_local "$ruta_local")
        if [[ -n $firma && $firma != - && $firma != "$firma_act" ]]; then
            [[ $reanudar == 1 ]] && log "Aviso #$id: $ruta_local ha cambiado desde la subida anterior; se sube desde el principio"
            reanudar=0
        fi
        [[ $firma != "$firma_act" ]] && con_bloqueo _cola_set_firma "$id" "$local_tam" "$firma_act"

        if [[ $VERIFICAR_TAMANO != n ]]; then
            remoto_tam=$(tam_remoto "$remoto"); rc_tam=$?
            ((rc_tam == 0)) || remoto_tam=""
            ((rc_tam == 2)) && reanudar=0   # no existe en el servidor: nada que reanudar
            if [[ $remoto_tam == "$local_tam" ]]; then
                cola_set "$id" completado 0 "ya existía en el servidor con el mismo tamaño"
                log "Omitido #$id $remoto: ya existe en el servidor con el mismo tamaño ($(tam "$local_tam"))"
                ((omitidos++))
                continue
            fi
            if [[ -n $remoto_tam ]] && ((remoto_tam == 0 || remoto_tam > local_tam)); then
                reanudar=0
            fi
        else
            reanudar=0
            remoto_tam=""
        fi

        intento=0 final=""
        while :; do
            detalle=""
            [[ $reanudar == 1 ]] && detalle+=", reanudando${remoto_tam:+ desde $(tam "$remoto_tam")}"
            ((intento)) && detalle+=" [reintento $intento/$REINTENTOS]"
            # --parar pudo llegar mientras se consultaba el servidor o se tomaba la fila
            ((PARAR)) && { final="parado"; break; }
            log "Subiendo #$id $ruta_local -> $remoto ($(tam "$local_tam"))$detalle"
            subir_una_vez "$ruta_local" "$remoto" "$reanudar"

            if ((PARAR)); then
                [[ $VERIFICAR_TAMANO != n ]] && decidir_reanudar || reanudar=0
                final="parado"; break
            fi

            if ((RC != 0)) && [[ $reanudar == 1 && $ERRORES_REANUDAR == *" $RC "* ]]; then
                log "El servidor no admite reanudar #$id ($MSG); se sube desde el principio"
                reanudar=0
                continue
            fi

            if ((RC == 0)); then
                if [[ $VERIFICAR_TAMANO == n ]]; then
                    final="ok"; break
                fi
                remoto_tam=$(tam_remoto "$remoto"); rc_tam=$?
                if ((rc_tam == 0)); then
                    if ((remoto_tam == local_tam)); then
                        final="ok"; break
                    fi
                    RC=-1
                    MSG="tamaño en el servidor ($remoto_tam bytes) distinto del local ($local_tam bytes)"
                    ((remoto_tam > 0 && remoto_tam < local_tam)) && reanudar=1 || reanudar=0
                elif ((rc_tam == 2)); then
                    RC=-1
                    MSG="el fichero no aparece en el servidor tras la subida"
                    remoto_tam="" reanudar=0
                else
                    log "Aviso #$id: el servidor no informa del tamaño; subida sin verificar"
                    final="ok"; break
                fi
            else
                [[ $VERIFICAR_TAMANO != n ]] && decidir_reanudar || reanudar=0
                # curl falló pero el fichero llegó entero (p. ej. se cortó al cerrar): no resubir
                if [[ -n $remoto_tam && $remoto_tam == "$local_tam" ]]; then
                    log "Aviso #$id: curl terminó con error ($MSG) pero el servidor ya tiene el fichero completo"
                    final="ok"; break
                fi
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
                cola_set "$id" completado 0 -
                log "Completado #$id $remoto: $(tam "$ENVIADOS") enviados en ${SEGUNDOS}s ($(tam "${VELOCIDAD%.*}")/s)"
                ((subidos++)) ;;
            parado)
                cola_set "$id" pendiente "$reanudar" -
                log "Subida de #$id interrumpida; queda $([[ $reanudar == 1 ]] && echo 'en pausa para reanudar' || echo 'pendiente')" ;;
            error)
                [[ $MSG == *"curl: (9)"* ]] && MSG+=" -> el servidor no deja crear o entrar en la carpeta de destino. Elige una carpeta existente (cola: d N) o desactiva 'Mantener estructura' en la configuración"
                cola_set "$id" error "$reanudar" "$MSG"
                log "ERROR subiendo #$id $ruta_local: $MSG"
                ((errores++)) ;;
        esac
        [[ $final == parado ]] && break
    done
    rm -f "$PROG" "$PROG.w"
    log "Subida en segundo plano finalizada: $subidos subidos, $omitidos ya existían, $errores con error"
}

# --------------------------------------------------------------------------
# Interfaz Interactiva (Explorador, Cola, Progreso y Menú)
# --------------------------------------------------------------------------

navegar() {
    local raiz actual op n rel i p destino destino_fijo="" rc filtro=""
    raiz="$(readlink -f "$CARPETA_LOCAL")"
    actual="$raiz"
    while :; do
        clear 2>/dev/null
        local -a dirs_raw=() ficheros_raw=() dirs=() ficheros=() todos=()
        local -A en_cola=() ya_completado=()
        while IFS= read -r p; do en_cola["$p"]=1; done \
            < <(awk -F'\t' '$2 == "pendiente" || $2 == "subiendo" || $2 == "pausado" { print $6 }' "$COLA" 2>/dev/null)
        while IFS= read -r p; do ya_completado["$p"]=1; done \
            < <(awk -F'\t' '$2 == "completado" { print $6 }' "$COLA" 2>/dev/null)
        mapfile -d '' dirs_raw < <(find "$actual" -mindepth 1 -maxdepth 1 \( -type d -o -xtype d \) ! -name '.*' -print0 2>/dev/null | sort -z)
        mapfile -d '' ficheros_raw < <(find "$actual" -mindepth 1 -maxdepth 1 \( -type f -o -xtype f \) ! -name '.*' -print0 2>/dev/null | sort -z)

        local filtro_low="${filtro,,}"
        for p in "${dirs_raw[@]}"; do
            if [[ -z $filtro_low || ${p##*/} =~ $filtro_low || ${p,,} == *"$filtro_low"* ]]; then
                dirs+=("$p")
            fi
        done
        for p in "${ficheros_raw[@]}"; do
            if [[ -z $filtro_low || ${p,,} == *"$filtro_low"* ]]; then
                ficheros+=("$p")
            fi
        done
        local -a lista=("${dirs[@]}" "${ficheros[@]}")

        rel="${actual#"$raiz"}"
        if [[ -n $destino_fijo ]]; then
            destino="$destino_fijo"
        elif [[ $ESTRUCTURA == n ]]; then
            destino="$CARPETA_REMOTA"
        else
            destino="${CARPETA_REMOTA%/}$rel"
            destino="${destino:-/}"
        fi

        caja_cabecera "Explorador de Archivos · $raiz$rel" 88
        printf '   %sDestino FTP:%s %s (%s)\n' "$C_BOLD" "$C_RESET" "$destino" "$([[ -n $destino_fijo ]] && echo 'elegido manualmente' || echo 'automático')"
        if [[ -n $filtro ]]; then
            printf '   %sFiltro activo: "%s"%s (escribe %s*%s o %s/%s para quitar el filtro)\n' \
                "$C_BOLD$C_CYAN" "$filtro" "$C_RESET" "$C_BOLD" "$C_RESET" "$C_BOLD" "$C_RESET"
        fi
        separador_tabla 90

        ((${#lista[@]} == 0)) && echo "  (ningún archivo o carpeta coincide)"
        local ext_f tag_f badge marca w_nom
        w_nom=$(ancho_columna_fichero)
        for i in "${!lista[@]}"; do
            p="${lista[i]}"
            if [[ -d $p ]]; then
                printf '  %4d)   %s[DIR ]%s       %s%s/%s\n' \
                    $((i + 1)) "$C_BOLD$C_BLUE" "$C_RESET" "$C_BOLD$C_BLUE" "${p##*/}" "$C_RESET"
            else
                ext_f="${p##*.}"
                ext_f="${ext_f,,}"
                tag_f="      "
                if [[ $ext_f =~ ^(nsp|nsz|xci|xcz|nro)$ ]]; then
                    printf -v tag_f '[%-4s]' "${ext_f^^}"
                fi
                badge="$(insignia_tipo_switch "$p")"
                marca=" "
                if [[ -n ${en_cola[$p]:-} ]]; then
                    marca="${C_YELLOW}*${C_RESET}"
                elif [[ -n ${ya_completado[$p]:-} ]]; then
                    marca="${C_GREEN}✓${C_RESET}"
                fi
                printf '  %4d)%s %s %s %s  %s(%s)%s\n' \
                    $((i + 1)) "$marca" "$tag_f" "$badge" "$(acortar_nombre "${p##*/}" "$w_nom")" \
                    "$C_DIM" "$(tam "$(stat -L -c %s -- "$p" 2>/dev/null || echo 0)")" "$C_RESET"
            fi
        done
        separador_tabla 90
        printf '  Leyenda: %s*%s en cola  |  %s✓%s ya completado (se omite en lotes)  |  %s[BASE]%s Juego  %s[UPD]%s Update  %s[DLC]%s DLC\n' \
            "$C_YELLOW" "$C_RESET" "$C_GREEN" "$C_RESET" "$C_CYAN" "$C_RESET" "$C_YELLOW" "$C_RESET" "$C_MAGENTA" "$C_RESET"
        echo "  Comandos:"
        echo "    • Seleccionar : N (abrir carpeta / añadir archivo) | 1,3,5-8 (añadir varios) | r N (añadir carpeta N recursiva)"
        echo "    • Lotes       : j (añadir solo juegos Switch de esta vista) | t (añadir todos los archivos)"
        echo "    • Buscar/Ruta : /TEXTO (filtrar por nombre) | * (quitar filtro) | d (cambiar destino FTP) | .. (subir) | v (volver)"
        echo
        read -rep "  Opción: " op || op="v"

        case "$op" in
            v | q | "") return ;;
            ..)
                filtro=""
                [[ $actual != "$raiz" ]] && actual="$(dirname "$actual")"
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
            d)
                elegir_carpeta_remota "$destino" si; rc=$?
                if ((rc == 0)); then
                    destino_fijo="$ELEGIDA"
                    log "Destino elegido en el explorador: $destino_fijo"
                elif ((rc == 2)); then
                    destino_fijo=""
                fi
                continue ;;
            j)
                local -a solo_juegos=()
                for p in "${ficheros[@]}"; do
                    ext_f="${p##*.}"
                    ext_f="${ext_f,,}"
                    if [[ $ext_f =~ ^(nsp|nsz|xci|xcz|nro)$ ]]; then
                        solo_juegos+=("$p")
                    fi
                done
                if ((${#solo_juegos[@]})); then
                    anadir_a_cola "$destino" "$actual" "${solo_juegos[@]}"
                else
                    echo "  No se encontraron juegos (.nsp, .nsz, .xci, .xcz, .nro) en esta vista."
                fi
                pausa; continue ;;
            t)
                if ((${#ficheros[@]})); then anadir_a_cola "$destino" "$actual" "${ficheros[@]}"; else echo "  No hay ficheros en esta vista."; fi
                pausa; continue ;;
            "r "*)
                n="${op#r }"
                if [[ $n =~ ^[0-9]+$ ]] && ((n >= 1 && n <= ${#dirs[@]})); then
                    mapfile -d '' todos < <(find "${dirs[n - 1]}" -mindepth 1 -name '.*' -prune -o \( -type f -o -xtype f \) -print0 2>/dev/null | sort -z)
                    if ((${#todos[@]} == 0)); then
                        echo "  La carpeta no contiene ficheros."
                    elif si_no "¿Añadir ${#todos[@]} fichero(s) de ${dirs[n - 1]##*/}/ a ${destino%/}/$([[ $ESTRUCTURA == s ]] && echo "${dirs[n - 1]##*/}/")?" s; then
                        anadir_a_cola "$destino" "$actual" "${todos[@]}"
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
                anadir_a_cola "$destino" "$actual" "${elegidos[@]}"
            fi
        else
            anadir_a_cola "$destino" "$actual" "${elegidos[@]}"
        fi
        pausa
    done
}

_quitar_ids() {  # _quitar_ids "101 103 105"
    local ids=" $1 " antes despues
    antes=$(wc -l < "$COLA")
    _reescribir_cola awk -F'\t' -v ids="$ids" '
        index(ids, " " $1 " ") && $2 == "subiendo" { print "  #" $1 " se está subiendo: detén la subida antes de quitarlo." > "/dev/stderr"; print; next }
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

_cambiar_destino() {  # _cambiar_destino "101 103" /carpeta/remota
    local antes
    # Las filas cuyo nuevo destino ya use otra fila activa (o otra de la selección) no se cambian.
    # Se lee la cola dos veces: la 1ª pasada reúne los destinos ocupados por filas que no cambian.
    DESTINO="${2%/}" _reescribir_cola awk -F'\t' -v OFS='\t' -v ids=" $1 " '
        function cambia() { return index(ids, " " $1 " ") && ($2 == "pendiente" || $2 == "pausado" || $2 == "error") }
        NR == FNR { if (!cambia() && ($2 == "pendiente" || $2 == "subiendo" || $2 == "pausado")) ocupado[$7] = 1; next }
        cambia() {
            n = split($7, p, "/"); nuevo = ENVIRON["DESTINO"] "/" p[n]
            if (nuevo != $7 && (nuevo in ocupado)) {
                print "  #" $1 " no se cambia: otro elemento de la cola ya se sube a " nuevo > "/dev/stderr"
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
    echo "  $antes elemento(s) se subirán a $2 (solo se cambian pendientes, pausados o con error)."
}

_toggle_pausa_ids() {  # _toggle_pausa_ids "101 103"
    local ids=" $1 "
    _reescribir_cola awk -F'\t' -v OFS='\t' -v ids="$ids" '
        index(ids, " " $1 " ") && $2 == "subiendo" {
            print "  #" $1 " se está subiendo: detén la subida en segundo plano para pausarlo." > "/dev/stderr"
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
            if (co[2] == "subiendo") {
                print "  No se puede mover un fichero que ya se está subiendo." > "/dev/stderr"
                for (i=1; i<=total; i++) print lineas[i]
                exit 1
            }
            split(lineas[1], c1, "\t")
            min_pos = (c1[2] == "subiendo") ? 2 : 1
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
    # A partir de aquí, añadir ficheros ya no reordena toda la cola (ver _anadir)
    touch "$ORDEN_MANUAL"
    log "Reordenada cola: movido #$id a posicion $target_pos"
    echo "  Elemento #$id colocado en la fila $target_pos."
}
mover_elemento_cola() { con_bloqueo _mover_elemento "$@"; }

# Filtro stdin -> stdout: ordena filas de la cola por estado y juego (Base -> Update -> DLC)
_ordenar_switch_filtro() {
    awk -F'\t' '
    function clasificar(estado, ruta,    nom, nom_low, juego, tid, tipo, prio_est) {
        if (estado == "subiendo") prio_est = 0
        else if (estado == "pendiente" || estado == "pausado") prio_est = 1
        else if (estado == "error") prio_est = 2
        else prio_est = 3

        nom = ruta
        sub(/^.*\//, "", nom)
        nom_low = tolower(nom)

        juego = nom_low
        sub(/ *[\[\(].*$/, "", juego)
        sub(/\.[^.]+$/, "", juego)

        tid = ""
        if (match(nom_low, /\[0100[0-9a-f]{12}\]/)) {
            tid = substr(nom_low, RSTART+1, 16)
        }

        # 1=Juego Base, 2=Actualización (Update), 3=DLC
        if (nom_low ~ /\[dlc|[^a-z]dlc[^a-z]/) {
            tipo = 3
        } else if (tid != "") {
            if (tid ~ /800$/) {
                tipo = 2
            } else if (tid ~ /000$/) {
                if (nom_low ~ /\[upd|update/ || nom_low ~ /\[v[1-9][0-9]*\]/) tipo = 2
                else tipo = 1
            } else {
                tipo = 3
            }
        } else if (nom_low ~ /\[upd|update|patch/ || nom_low ~ /\[v[1-9][0-9]*\]/) {
            tipo = 2
        } else {
            tipo = 1
        }

        return sprintf("%d|%s|%d|%s|%06d", prio_est, juego, tipo, nom_low, NR)
    }
    {
        clave = clasificar($2, $6)
        print clave "\t" $0
    }
    ' | sort -t'|' -k1,1n -k2,2 -k3,3n -k4,4 -k5,5n | cut -f2-
}

# Deja intactas las N primeras filas de la cola y ordena solo las siguientes
_ordenar_desde() {  # _ordenar_desde N
    awk -v n="$1" 'NR <= n' "$COLA"
    awk -v n="$1" 'NR > n' "$COLA" | _ordenar_switch_filtro
}

_auto_ordenar_switch() {
    [[ -s $COLA ]] || { echo "  La cola está vacía."; return; }
    _reescribir_cola _ordenar_switch_filtro || return 1
    rm -f "$ORDEN_MANUAL"
    log "Cola auto-ordenada (Switch: Base -> Update -> DLC)"
    echo "  Cola ordenada automáticamente: Juego Base ➔ Actualización (Update) ➔ DLCs."
}
auto_ordenar_cola() { con_bloqueo _auto_ordenar_switch; }

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
        printf '    • Prioridad    : [%sord%s] Auto-ordenar Switch | [%ssub N%s] Subir | [%sbaj N%s] Bajar | [%stop N%s] Al inicio | [%sm N POS%s] Mover\n' \
            "$C_BOLD$C_CYAN" "$C_RESET" "$C_BOLD$C_CYAN" "$C_RESET" "$C_BOLD$C_CYAN" "$C_RESET" "$C_BOLD$C_CYAN" "$C_RESET" "$C_BOLD$C_CYAN" "$C_RESET"
        printf '    • Elementos    : [%sp N[,..]%s] Pausar/Reanudar | [%sd N[,..]%s] Cambiar destino | [%sb N[,..]%s] Quitar de la cola\n' \
            "$C_BOLD$C_CYAN" "$C_RESET" "$C_BOLD$C_CYAN" "$C_RESET" "$C_BOLD$C_CYAN" "$C_RESET"
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
                auto_ordenar_cola ;;
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
            "d "*)
                if resolver_seleccion_cola "${op#d }"; then
                    if elegir_carpeta_remota "$CARPETA_REMOTA"; then
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
                   && con_bloqueo _filtrar_cola '$2 == "subiendo"' "Cola vaciada" ;;
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
            caja_cabecera "Progreso de Subida en Tiempo Real" 88
            resumen_estado
            echo

            linea=$(awk -F'\t' '$2 == "subiendo"' "$COLA" 2>/dev/null)
            if [[ -n $linea ]]; then
                local id tam local_f remoto_f
                IFS=$'\t' read -r id _ _ tam _ local_f remoto_f _ <<< "$linea"

                printf '  %s>> SUBIENDO #%s%s : %s %s%s%s (%s)\n' \
                    "$C_BOLD$C_BLUE" "$id" "$C_RESET" "$(insignia_tipo_switch "$local_f")" "$C_BOLD" "${local_f##*/}" "$C_RESET" "$(tam "$tam")"
                printf '     Destino      : %s\n' "$remoto_f"

                if leer_progreso; then
                    calcular_tiempo_cola "${PRG_VEL_MEDIA:-${PRG_VEL_ACTUAL:-}}" "${PRG_PCT:-0}"

                    printf '     Archivo act. : %s (%s de %s)\n' \
                        "$(barra_progreso "$PRG_PCT" 30 "$C_GREEN")" "${PRG_ENVIADO:-0}" "${PRG_TOTAL:-$(tam "$tam")}"
                    printf '     Cola global  : %s (%s de %s)\n' \
                        "$(barra_progreso "$COLA_PCT_GLOBAL" 30 "$C_CYAN")" "$(tam "$COLA_BYTES_HECHOS")" "$(tam "$COLA_BYTES_TOT")"
                    printf '     Velocidad    : %s/s (media: %s/s)\n' \
                        "${PRG_VEL_ACTUAL:-${PRG_VEL_MEDIA:-?}}" "${PRG_VEL_MEDIA:-?}"
                    printf '     Tiempo arch. : transcurrido %s | restante %s | total est. %s\n' \
                        "${PRG_T_PASADO:-?}" "${PRG_T_RESTANTE:-?}" "${PRG_T_TOTAL:-?}"
                    if [[ -n $COLA_TIEMPO_EST ]]; then
                        printf '     Tiempo cola  : restante est. %s%s%s (%d arch, %s restantes)\n' \
                            "$C_BOLD$C_GREEN" "$COLA_TIEMPO_EST" "$C_RESET" "$COLA_FICH_REST" "$(tam "$COLA_BYTES_REST")"
                    fi
                else
                    calcular_tiempo_cola "" 0
                    printf '     Archivo act. : %s (iniciando conexión o transfiriendo...)\n' "$(barra_progreso 0 30 "$C_GREEN")"
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
                echo "  (no hay ningún archivo subiéndose en este momento)"
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

            if ! worker_activo > /dev/null; then
                if curl_huerfano > /dev/null; then
                    echo; echo "  La subida perdió su proceso en segundo plano; su curl sigue vivo (pulsa 's' para detenerlo)."
                else
                    echo; echo "  La subida en segundo plano está detenida (pulsa 's' para iniciarla)."
                fi
            fi
            echo
            printf '  %sControles en vivo:%s [%ss%s] Iniciar/Parar subida | [%so%s] Auto-ordenar Switch | [%sc%s] Limpiar completados | [%sv%s/%sq%s] Volver\n' \
                "$C_BOLD" "$C_RESET" "$C_BOLD$C_CYAN" "$C_RESET" "$C_BOLD$C_CYAN" "$C_RESET" "$C_BOLD$C_CYAN" "$C_RESET" "$C_BOLD$C_CYAN" "$C_RESET" "$C_BOLD$C_CYAN" "$C_RESET"
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
                if worker_activo > /dev/null || curl_huerfano > /dev/null; then
                    detener_worker > /dev/null 2>&1
                else
                    lanzar_worker > /dev/null 2>&1
                fi ;;
            o)
                auto_ordenar_cola > /dev/null 2>&1 ;;
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
    caja_cabecera "subeftp · Guía y Ayuda de Uso" 88
    cat << 'EOF'

  USO BÁSICO:
    ./subeftp.sh [OPCIONES] [FICHEROS...]

  EJEMPLOS DE LÍNEA DE COMANDOS:
    ./subeftp.sh                        Inicia la interfaz interactiva (TUI)
    ./subeftp.sh juego.nsp              Añade juego.nsp a la cola y pregunta si arrancar
    ./subeftp.sh -s juego.nsp           Añade juego.nsp y arranca la subida de inmediato
    ./subeftp.sh -s *.nsp               Añade todos los .nsp y arranca de inmediato
    ./subeftp.sh --anadir ~/Descargas/  Añade todos los ficheros de esa carpeta a la cola
    ./subeftp.sh --subir                Arranca la subida de los pendientes en la cola
    ./subeftp.sh --parar                Detiene de forma limpia la subida en segundo plano
    ./subeftp.sh --estado               Muestra el estado del worker y la cola de subidas
    ./subeftp.sh --pendientes           Lista los ficheros pendientes y el tiempo estimado
    -h, --help                          Muestra esta ayuda detallada y sale

  EXPLORADOR DE CARPETAS (Opción 1):
    N                  Entra en la carpeta N o añade el fichero N a la cola
    1,3,5-8            Añade múltiples ficheros a la vez (por lista o rangos)
    j                  Añade solo juegos de Switch (.nsp, .nsz, .xci, .xcz, .nro)
    t                  Añade todos los ficheros de la vista actual
    r N                Añade recursivamente la carpeta N con sus subcarpetas
    /TEXTO             Filtra archivos y carpetas por nombre (y * para limpiar filtro)
    d                  Elige la carpeta de destino remota en el servidor FTP
    ..                 Sube a la carpeta superior
    v                  Vuelve al menú principal

  GESTIÓN DE LA COLA (Opción 2):
    Usa directamente el número de fila # (ej: 1, 2, 3-6) o el ID precedido de # (ej: #105):
    t                  Alterna entre ver TODOS los archivos o SOLO PENDIENTES
    ord                Auto-ordena juegos Switch (Juego Base ➔ Update ➔ DLC)
    /TEXTO             Busca y filtra por nombre, ID o estado (ej: /mario, /pend)
    * o /              Quita el filtro activo y vuelve a mostrar toda la cola
    b N[,M-K]          Quita elementos de la cola
    sub N / baj N      Sube o baja 1 posición el elemento de la fila N
    top N              Pone el elemento de la fila N al principio (máxima prioridad)
    m N POS            Mueve el elemento de la fila N a la fila POS (ej: m 5 2)
    p N[,M-K]          Pausa o reanuda elementos pendientes individualmente
    d N[,M-K]          Cambia la carpeta remota de destino
    c / f / e / x      Limpia completados (c), fallidos (f), reintenta errores (e) o vacía (x)

  INSIGNIAS Y ESTADOS:
    [BASE] / [UPD] / [DLC] / [NRO]   Tipo de paquete de Nintendo Switch detectado
    PEND / REAN / PAUS / SUB / OK / ERR  Estado del elemento en la cola
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
    local op rc w_pid h_pid frame col_sub txt_sub
    worker_activo > /dev/null || recuperar_huerfanos
    clear 2>/dev/null
    while :; do
        w_pid=$(worker_activo 2>/dev/null) || w_pid=""
        h_pid=""
        [[ -z $w_pid ]] && { h_pid=$(curl_huerfano 2>/dev/null) || h_pid=""; }
        frame=$(
            panel_resumen_menu
            if [[ -n $h_pid ]]; then
                printf '   %s⚠ La subida anterior perdió su proceso en segundo plano, pero su curl (PID %s)%s\n' "$C_RED" "$h_pid" "$C_RESET"
                printf '   %s  sigue subiendo y bloquea nuevas subidas. Usa 5 para detenerlo.%s\n' "$C_RED" "$C_RESET"
                separador_tabla 86
                col_sub="$C_YELLOW"
                txt_sub="⏹ Detener curl huérfano"
            elif [[ -n $w_pid ]]; then
                col_sub="$C_YELLOW"
                txt_sub="⏹ Detener subida en 2º plano"
            else
                col_sub="$C_GREEN"
                txt_sub="▶ Iniciar subida en 2º plano"
            fi
            fila_menu_2col "1" "Explorar y añadir a la cola"  ""         "6" "Probar conexión FTP"
            fila_menu_2col "2" "Ver / gestionar la cola"      ""         "7" "Buscar IP de Switch en la red"
            fila_menu_2col "3" "Ver solo archivos pendientes" ""         "8" "Editar configuración FTP"
            fila_menu_2col "4" "Ver progreso en vivo"         ""         "9" "Ver registro de actividad (log)"
            fila_menu_2col "5" "$txt_sub"                     "$col_sub" "0" "Salir del programa  ${C_DIM}[h: Ayuda]${C_RESET}"
            separador_tabla 86
        )
        printf '\033[H%s\033[K\033[J\n' "${frame//$'\n'/$'\033[K\n'}"
        if [[ -n $w_pid || -n $h_pid ]]; then
            read -n 1 -t 2 -rp "  Opción (0-9 / Intro refresca): " op
            rc=$?
            ((rc > 128)) && continue
            # Fin de la entrada (no un tiempo agotado): salir en vez de quedarse en bucle
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
                elif h_pid=$(curl_huerfano); then
                    if si_no "¿Detener el curl huérfano (PID $h_pid)? El fichero se reanudará después" s; then
                        detener_worker
                    fi
                else
                    lanzar_worker
                fi
                pausa ;;
            6) probar_conexion; pausa ;;
            7 | b | B) buscar_switch_red; pausa ;;
            8) asistente_conf; pausa ;;
            9) ver_log ;;
            h | H | \?) mostrar_ayuda; pausa ;;
            0 | q | Q)
                worker_activo > /dev/null && echo "  La subida sigue en segundo plano. Vuelve a abrir el programa para ver su estado."
                curl_huerfano > /dev/null && echo "  Aviso: sigue vivo un curl huérfano de una subida interrumpida (./subeftp.sh --parar lo detiene)."
                log "Sesión interactiva finalizada"
                return ;;
        esac
        clear 2>/dev/null
    done
}

anadir_desde_cli() {
    local auto_arrancar=0
    local -a archivos=()
    local arg f n_encontrados=0

    for arg in "$@"; do
        case "$arg" in
            --anadir) continue ;;
            -s | --subir | --start | --now | --ahora)
                auto_arrancar=1
                ;;
            -*)
                echo "Opción no reconocida: $arg"
                echo "Usa '$0 --help' para ver la lista de comandos disponibles."
                exit 1
                ;;
            *)
                if [[ -f $arg ]]; then
                    archivos+=("$(readlink -f "$arg")")
                    ((n_encontrados++))
                elif [[ -d $arg ]]; then
                    local d_abs
                    d_abs="$(readlink -f "$arg")"
                    while IFS= read -r -d '' f; do
                        archivos+=("$f")
                        ((n_encontrados++))
                    done < <(find "$d_abs" -type f ! -name '.*' -print0 2>/dev/null | sort -z)
                else
                    echo "Aviso: no existe o no es accesible: $arg"
                fi
                ;;
        esac
    done

    if ((n_encontrados == 0)); then
        echo "No se encontraron archivos válidos para añadir a la cola."
        exit 1
    fi

    if ! conf_completa; then
        echo "Configuración incompleta. Iniciando asistente..."
        asistente_conf
    fi

    anadir_a_cola "$CARPETA_REMOTA" "$CARPETA_LOCAL" "${archivos[@]}"

    if ((auto_arrancar)); then
        echo
        lanzar_worker
    else
        echo
        if si_no "¿Deseas iniciar la subida en segundo plano ahora?" s; then
            lanzar_worker
        fi
    fi
}

main() {
    for cmd in curl flock awk; do
        command -v "$cmd" > /dev/null || { echo "Falta el comando '$cmd'. Instálalo y vuelve a intentarlo."; exit 1; }
    done
    touch "$COLA"
    cargar_conf

    case "${1:-}" in
        -h | --help | help) mostrar_ayuda; exit 0 ;;
        --worker) worker; exit ;;
        --estado) resumen_estado; imprimir_cola; exit ;;
        --pendientes) imprimir_pendientes; exit ;;
        --parar) detener_worker; exit ;;
        --subir)
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
