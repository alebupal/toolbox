# 🧰 Toolbox (Utilidades del Servidor: Mover/Copiar + FTP/Switch)

Proyecto modular en Docker que reúne en **un único repositorio y contenedor** tus utilidades de servidor (actualmente **Mover/Copiar local** y **FTP / Nintendo Switch**, preparado para seguir incorporando nuevas utilidades):

1. **Interfaz Web moderna** (FastAPI + HTML/CSS/JS) con vista **doble panel Origen ⇄ Destino**, colas persistentes en segundo plano, barra de progreso en vivo, escáner LAN de Switch, visor de registros y visor integrado de esta documentación.
2. **Scripts CLI/SSH integrados** (`toolbox`, `mover` / `mover.sh` y `subeftp` / `subeftp.sh`) dentro del propio contenedor y del repositorio, para trabajar indistintamente desde el navegador, por SSH (`docker exec -it toolbox ...`) o mediante tareas `cron`.

> **Regla de mantenimiento (Paridad Web ⇄ SSH/CLI):** toda funcionalidad, ajuste o mejora nueva que se incorpore en Toolbox debe quedar implementada tanto en la **interfaz Web** como en los **scripts CLI/SSH** (`scripts/`), manteniendo ambos modos siempre equivalentes.

---

## 📋 Índice

- [Estructura del proyecto](#-estructura-del-proyecto)
- [Puesta en marcha con Docker](#-puesta-en-marcha-con-docker)
- [Variables de entorno y permisos](#-variables-de-entorno-y-permisos)
- [Uso de la Interfaz Web](#-uso-de-la-interfaz-web)
- [Guía detallada de comandos SSH / CLI](#-guía-detallada-de-comandos-ssh--cli)
  - [1. Comando unificado `toolbox` (`scripts/toolbox.sh`)](#1-comando-unificado-toolbox-scriptstoolboxsh)
  - [2. Mover / Copiar local `mover` (`scripts/mover.sh`)](#2-mover--copiar-local-mover-scriptsmoversh)
  - [3. FTP y Nintendo Switch `subeftp` (`scripts/subeftp.sh`)](#3-ftp-y-nintendo-switch-subeftp-scriptssubeftpsh)
- [Detalles técnicos: Mover / Copiar local](#-detalles-técnicos-mover--copiar-local)
- [Detalles técnicos: FTP y Nintendo Switch (DBI / ftpd)](#-detalles-técnicos-ftp-y-nintendo-switch-dbi--ftpd)
- [Publicar con Nginx Proxy Manager y Tinyauth](#-publicar-con-nginx-proxy-manager-y-tinyauth)
- [Persistencia y copias de seguridad](#-persistencia-y-copias-de-seguridad)
- [Desarrollo y Tests](#-desarrollo-y-tests)

---

## 📁 Estructura del proyecto

```text
toolbox/
├── Dockerfile                  # Imagen Python 3.12 + rsync, curl, iproute2, gawk, flock, gosu
├── entrypoint.sh               # Prepara /config (/config/cli/*) y arranca Uvicorn
├── requirements.txt            # FastAPI, Uvicorn y Pydantic
├── README.md                   # Esta guía (accesible también desde la pestaña Web y 'toolbox readme')
├── app/
│   ├── main.py                 # API REST, explorador /host, endpoint /api/readme y estáticos
│   ├── models.py               # Esquemas Pydantic
│   ├── config.py               # settings.json (permisos 600) y validación de rutas
│   ├── db.py                   # Persistencia SQLite (queue.db) de colas y logs
│   ├── mover_worker.py         # Motor local: mv mismo disco, rsync .mover-partial, verificación
│   ├── ftp_worker.py           # Motor FTP/FTPS: curl, reanudación -C -, límite KB/s, listado remoto
│   ├── switch_utils.py         # Insignias Switch, auto-orden Base→UPD→DLC y escáner LAN
│   └── worker_manager.py       # Hilos en segundo plano independientes (Mover y FTP)
├── static/
│   ├── index.html              # Interfaz web en español
│   ├── style.css               # Tema oscuro adaptable con doble panel y visor de README
│   └── app.js                  # Control de explorador doble panel, colas, ajustes, logs y Markdown
├── scripts/
│   ├── toolbox.sh              # Wrapper CLI unificado (disponible como 'toolbox' en el contenedor)
│   ├── mover.sh                # Script Bash completo de copia/movimiento (disponible como 'mover')
│   ├── subeftp.sh              # Script Bash completo de subida FTP/Switch (disponible como 'subeftp')
│   └── mover.conf.example      # Plantilla de configuración CLI para mover.sh
└── tests/
    └── test_core.py            # Suite de tests unitarios (unittest)
```

---

## 🐳 Puesta en marcha con Docker

La imagen se publica en Docker Hub como **`alebupal/toolbox:latest`** y el despliegue está preparado en `docker/toolbox/config.sh` (`MODE="run"` compatible con `update.sh`).

### Despliegue con `update.sh` (recomendado)

```bash
cd /ruta/a/docker
./update.sh toolbox
```

### Despliegue manual con `docker run`

```bash
docker run -d \
  --name toolbox \
  --network host \
  --restart unless-stopped \
  -e TZ=Europe/Madrid \
  -e PORT=8090 \
  -e DATA_ROOT=/host \
  -e PUID=0 \
  -e PGID=0 \
  -v /ruta/a/toolbox/config:/config \
  -v /:/host \
  alebupal/toolbox:latest
```

Una vez levantado, abre en tu navegador:

```text
http://<IP-de-tu-servidor>:8090
```

### Publicar la imagen en Docker Hub

- **Manual (desde el código fuente):**
  ```bash
  cd /ruta/a/toolbox
  docker build -t alebupal/toolbox:latest .
  docker push alebupal/toolbox:latest
  ```
- **Automática (GitHub Actions):** el workflow `.github/workflows/docker-publish.yml` construye y sube `alebupal/toolbox:latest` (`linux/amd64` y `linux/arm64`) en cada `push` a `main` usando los secrets del repositorio `DOCKERHUB_USERNAME` y `DOCKERHUB_TOKEN`.

### Cómo funciona el acceso a todo el servidor

- El contenedor monta la raíz real del servidor (`/`) en **`/host`** dentro del contenedor.
- Desde la Web y desde los scripts ejecutados dentro del contenedor (`docker exec`), las rutas reales del servidor empiezan por `/host/...` (por ejemplo `/home/usuario/Descargas` pasa a ser `/host/home/usuario/Descargas`).
- Como todo cuelga del mismo árbol `/host`, al mover dentro del mismo disco/partición física del servidor se detecta que comparten dispositivo (`st_dev`) y realiza un **`mv` instantáneo** sin copiar bytes. Entre discos distintos utiliza **`rsync`** con reanudación y verificación.
- Por seguridad, en recorridos recursivos desde `/host` se omiten automáticamente los pseudo-sistemas del kernel (`/host/proc`, `/host/sys`, `/host/dev`, `/host/run`).

---

## ⚙️ Variables de entorno y permisos

| Variable | Valor por defecto | Descripción |
|---|---|---|
| `PORT` | `8090` | Puerto HTTP en el que escucha la interfaz web y la API local. |
| `DATA_ROOT` | `/host` | Raíz permitida dentro del contenedor para explorar archivos desde la Web. |
| `PUID` / `PGID` | `0` / `0` | Usuario con el que corre el proceso. `0:0` (`root`) permite operar en todo el servidor; puedes usar `1000:1000` para limitarlo a tu usuario. |
| `TZ` | `Europe/Madrid` | Zona horaria para fechas en los registros. |

### Modo de red (`--network host`)

1. **Auto-descubrimiento de Nintendo Switch en LAN**: permite inspeccionar las interfaces físicas (`eth*`, `en*`, `wl*`) y escanear su subred `/24`.
2. **Modo FTP activo** además del pasivo sin conflictos de puertos dinámicos de datos.

---

## 🌐 Uso de la Interfaz Web

En la parte superior aparecen siempre las dos tarjetas de estado (**💾 Cola Mover / Copiar** y **📡 Cola FTP / Switch**):

- Todo se ejecuta **en segundo plano dentro del servidor** (`/config/queue.db`). Si lanzas una copia/movimiento/subida y cierras la ventana del navegador, el proceso **continúa trabajando** hasta terminar.
- En cualquier momento puedes volver a abrir la Web (o consultar por SSH con `toolbox web estado`) para ver el porcentaje (`%`), archivo en curso, velocidad, `ETA` o revisar el resultado final.

### 1. 📁 Explorar (Doble panel `Origen ⇄ Destino`)
- **Selector de modo superior**: alterna entre **💾 Copiar / Mover local** y **📡 Subir por FTP / Switch**, además de elegir rápidamente la acción (**📋 Copiar** o **✂️ Mover**).
- **Panel izquierdo (`📂 Origen (Servidor)`)**:
  - Navega con **migas de pan clicables**, botones rápidos (`Raíz servidor`, `Home`, `/mnt`, `/media`), barra de ruta directa y filtro en vivo.
  - Puedes crear carpetas (`+ Carpeta`), mostrar u ocultar ocultos, y seleccionar múltiples archivos o carpetas.
  - Botones de envío:
    - **➕ A la cola →**: añade a la cola sin arrancar todavía.
    - **⚡ Enviar e iniciar** (*Copiar ahora* / *Mover ahora* / *Subir ahora*): añade a la cola hacia la carpeta abierta a la derecha y arranca el worker al instante en segundo plano.
    - **🎮 Solo Switch** (en modo FTP): filtra y encola solo `.nsp`, `.nsz`, `.xci`, `.xcz` y `.nro`.
- **Panel derecho (`🎯 Destino`)**:
  - Muestra en tiempo real el contenido de la carpeta destino (carpeta local del servidor o carpeta remota FTP con atajos `/`, `/install`, `/nsp`, `/switch`).
  - Permite navegar, ver el espacio libre en disco, crear nuevas subcarpetas (`+ Carpeta`) y ver claramente el indicador **Destino activo**.
  - Si marcas **Usar destino automático (config)**, ignora la carpeta abierta a la derecha y usa la ruta por defecto configurada en *Configuración*.

### 2. 📋 Colas
- Vista independiente para **Cola Mover** y **Cola FTP**, con filtro `Solo activos` (`PEND`, `REAN`, `PAUS`, `PROC`).
- **Controles por elemento**: subir (`↑`), bajar (`↓`), poner primero (`⤒`), pausar/reanudar (`⏯`), alternar `Copiar ⇄ Mover` (en Mover) y eliminar (`✕`).
- **Controles globales**: `▶ Iniciar` / `⏹ Detener` (parada limpia dejando el ítem como `REAN` para reanudar exactamente donde quedó), `↕ Auto-ordenar` (en FTP ordena `BASE → UPD → DLC → NRO`), `↻ Reintentar errores`, `🧹 Limpiar OK`, `🧹 Limpiar ERR`, `🗑 Vaciar pendientes`.

### 3. ⚙️ Configuración
- **Mover / Copiar**: origen inicial, destino por defecto, operación por defecto, política de conflictos (`omitir`, `sobrescribir`, `renombrar`), verificación (`tamano`, `checksum`, `no`), límite KB/s, reintentos, espera exponencial y rutas favoritas (con botón `📂` selector de carpetas).
- **FTP / Switch**: IP/host, puerto, usuario, contraseña (oculta tras guardarse), carpeta local inicial, carpeta remota por defecto, modo pasivo, FTPS/TLS, verificación de tamaño final, límite KB/s y reintentos. Incluye **🔍 Buscar Switch** y **🔌 Probar conexión**.

### 4. 📜 Registro
- Historial persistente en SQLite (últimos 5.000 eventos) filtrable por módulo (`Todos`, `Mover`, `FTP`, `Sistema`), ideal para confirmar cuándo terminó una tarea ejecutada en segundo plano.

### 5. 📖 Guía / README
- Renderiza este mismo `README.md` directamente dentro de la interfaz web, con índice clicable y botones **Copiar** en cada bloque de comandos.

---

## 💻 Guía detallada de comandos SSH / CLI

Todos los scripts están instalados dentro del contenedor como comandos globales (`toolbox`, `mover`, `subeftp`) y también se encuentran en `scripts/` para ejecutarlos directamente en Linux fuera de Docker.

> **Colas y configuración compartidas Web ⇄ SSH:**
> - **Dentro del contenedor Docker (`docker exec -it toolbox ...`)**: `toolbox`, `mover` y `subeftp` se conectan automáticamente al mismo motor Web (`/config/queue.db` + `/config/settings.json`). Todo lo que añadas, inicies, pauses, reordenes o configures por **SSH** aparece al instante en la **Web** y viceversa.
> - **Rutas en Docker**: el disco del servidor está montado en `/host`, así que debes pasar rutas `/host/...` (ej. `/host/mnt/peliculas`).
> - **Modo clásico aislado (`--legacy` o fuera de Docker)**: si ejecutas `./scripts/mover.sh` o `./scripts/subeftp.sh` en otro servidor Linux sin contenedor, o si pasas el flag `--legacy` dentro del contenedor, usan sus ficheros independientes clásicos (`cola.txt` en `/config/cli/...`).

---

### 1. Comando unificado `toolbox` (`scripts/toolbox.sh`)

Es el punto de entrada principal por SSH.

#### Subcomandos disponibles

| Comando | Descripción |
|---|---|
| `toolbox` | Abre el menú interactivo unificado (acceso a `mover.sh`, `subeftp.sh`, estado combinado CLI + Web, logs de la Web, lectura de este README y ayuda). |
| `toolbox mover [OPCIONES]` | Reenvía exactamente los argumentos a `mover.sh` (ver [sección 2](#2-mover--copiar-local-mover-scriptsmoversh)). |
| `toolbox ftp [OPCIONES]` | Reenvía exactamente los argumentos a `subeftp.sh` (ver [sección 3](#3-ftp-y-nintendo-switch-subeftp-scriptssubeftpsh)). Alias: `toolbox subeftp`. |
| `toolbox estado` | Muestra un resumen unificado de las colas CLI (`mover` + `ftp`) y del estado/progreso en vivo del servicio Web. Alias: `toolbox status`. |
| `toolbox readme` | Muestra este `README.md` en la terminal (paginado con `less -R` si es TTY interactiva). Alias: `toolbox guia`, `toolbox docs`. |
| `toolbox web estado` | Muestra el estado de las colas de la Web (`mover` y `ftp`), conteos (`PEND/PAUS/OK/ERR`), archivo en progreso, `%`, velocidad y `ETA`. |
| `toolbox web cola [mover\|ftp]` | Lista todos los elementos de la cola Web (SQLite) con su estado, operación y rutas origen `→` destino. Por defecto `mover`. |
| `toolbox web iniciar <mover\|ftp>` | Inicia en segundo plano el worker Web indicado (`mover` o `ftp`). |
| `toolbox web parar <mover\|ftp>` | Solicita la parada limpia del worker Web indicado. |
| `toolbox web logs [tipo] [N]` | Muestra los últimos `N` logs (def. `40`) de la Web. `tipo` puede ser `todos`, `mover`, `ftp` o `sistema`. |
| `toolbox web buscar-switch` | Lanza el escaneo LAN del servidor Web para localizar la IP y puerto FTP de tu Nintendo Switch. |
| `toolbox --help` | Muestra la ayuda resumida en pantalla (`-h`, `help`). |

#### Ejemplos por SSH con Docker

```bash
# Abrir menú interactivo unificado
docker exec -it toolbox toolbox

# Ver progreso en vivo de lo que lanzaste desde la Web
docker exec -it toolbox toolbox web estado

# Listar los ítems de la cola Mover de la Web y ver los últimos 30 logs
docker exec -it toolbox toolbox web cola mover
docker exec -it toolbox toolbox web logs mover 30

# Leer el README en terminal
docker exec -it toolbox toolbox readme
```

---

### 2. Mover / Copiar local `mover` (`scripts/mover.sh`)

Gestiona colas locales de copia o movimiento con `rsync` (con parciales `.mover-partial` y reanudación) o `mv` atómico en el mismo disco.

#### Opciones y flags de línea de comandos (CLI)

| Opción / Flag | Descripción |
|---|---|
| `mover` (sin argumentos) | Abre la interfaz interactiva en terminal (TUI). |
| `-c`, `--copiar` | Marca la operación de los archivos que se añaden como **copiar** (mantiene origen). |
| `-m`, `--mover` | Marca la operación de los archivos que se añaden como **mover** (borra origen tras verificar). |
| `-d RUTA`, `--destino RUTA`, `--destino=RUTA` | Fija una carpeta destino concreta (si se omite, usa el destino automático configurado). |
| `-i`, `--iniciar` (o `-s`, `--ahora`) | Si se pasa junto a ficheros/carpetas, los añade y **arranca de inmediato** sin preguntar. Si se pasa solo (`mover --iniciar`), arranca los pendientes en cola. |
| `--anadir RUTA...` | Explícito para añadir ficheros o carpetas (útil si algún nombre empieza por `-`). También puedes usar `--` antes de las rutas. |
| `--estado` | Muestra el estado del worker en segundo plano y lista la cola completa. |
| `--pendientes` | Lista únicamente los archivos pendientes/pausados y estima el tiempo total. |
| `--parar` | Detiene de forma limpia el worker en segundo plano (el archivo actual queda en `REAN` para reanudar luego). |
| `--log [N]` | Muestra las últimas `N` líneas del registro `mover.log` (por defecto `40`). |
| `-h`, `--help`, `help` | Muestra la ayuda integrada del script y sale. |

#### Ejemplos de uso rápido por SSH

```bash
# Mover una película a una carpeta concreta y empezar al momento
docker exec -it toolbox mover -m -i -d /host/mnt/peliculas /host/home/usuario/Descargas/Peli.mkv

# Copiar una carpeta entera (mantiene subcarpetas) usando el destino por defecto
docker exec -it toolbox mover -c -i /host/home/usuario/Descargas/Serie

# Consultar estado, ver últimos 50 logs o parar limpiamente el worker CLI
docker exec -it toolbox mover --estado
docker exec -it toolbox mover --log 50
docker exec -it toolbox mover --parar
```

#### Comandos del menú interactivo TUI (`mover`)

Al ejecutar `mover` (o `toolbox` → `1`) aparece el menú principal:

- **Teclas del menú principal**:
  - `1`: **Explorar y añadir a la cola**
  - `2`: **Ver / gestionar la cola** (todos los elementos)
  - `3` (o `p`): **Ver solo archivos pendientes**
  - `4`: **Ver progreso en vivo** (barra, velocidad, ETA del archivo actual)
  - `5` (o `s`): **▶ Iniciar / ⏹ Detener proceso en 2º plano**
  - `6`: **Editar configuración** (asistente para origen, destino, operación, límites, conflictos, favoritos...)
  - `7`: **Ver configuración y espacio libre**
  - `8`: **Ver registro de actividad (log)**
  - `9` (o `h`, `?`): **Ayuda**
  - `0` (o `q`): **Salir** (si el worker está activo, sigue en segundo plano)

- **Comandos dentro del Explorador (opción `1`)**:
  - `N`: si es carpeta, entra en ella; si es archivo, lo añade a la cola.
  - `1,3,5-8`: añade varios archivos a la vez por lista o rangos.
  - `t`: añade todos los archivos visibles en la carpeta actual.
  - `r N`: añade recursivamente la carpeta `N` con todas sus subcarpetas.
  - `o`: alterna la operación de lo que vas a añadir entre **COPIAR** y **MOVER**.
  - `d`: abre el navegador local para elegir una carpeta destino puntual.
  - `f1`, `f2`...: selecciona directamente una de tus carpetas destino favoritas.
  - `a`: vuelve a usar el destino automático configurado.
  - `/TEXTO` / `*`: filtra por nombre (`/texto`) o limpia el filtro (`*`).
  - `..` / `v`: sube a la carpeta superior (`..`) o vuelve al menú (`v`).

- **Comandos dentro de Gestión de Cola (opción `2` o `3`)**:
  - Puedes referirte a elementos por **número de fila** (`1`, `2`, `3-6`) o por **ID** precedido de `#` (`#105`).
  - `t`: alterna entre ver **TODOS** o **SOLO PENDIENTES**.
  - `ord`: ordena la cola automáticamente por estado y ruta de origen.
  - `/TEXTO` / `*` o `/`: filtra por nombre, ID, estado u operación (ej: `/mover`, `/err`), o quita el filtro.
  - `b N[,M-K]`: elimina elementos de la cola.
  - `sub N` / `baj N` / `top N` / `m N POS`: reordena prioridades (subir 1 posición, bajar 1, poner primero o mover a la fila `POS`).
  - `p N[,M-K]`: pausa o reanuda elementos pendientes.
  - `o N[,M-K]`: alterna entre **COPIAR** y **MOVER** en elementos aún no terminados.
  - `d N[,M-K]`: cambia la carpeta destino de esos elementos.
  - `c` / `f` / `e` / `x`: limpia completados (`c`), limpia fallidos (`f`), reintenta errores (`e`) o vacía la cola (`x`).

- **Etiquetas y estados en `mover`**:
  - Operación: `[COP ]` (copia conservando origen) / `[MOV ]` (mueve borrando origen tras verificar).
  - Estados: `PEND` (pendiente), `REAN` (reanudable tras corte), `PAUS` (pausado), `PROC` (copiando/moviendo ahora), `OK` (completado) y `ERR` (error).

---

### 3. FTP y Nintendo Switch `subeftp` (`scripts/subeftp.sh`)

Gestiona subidas FTP/FTPS con `curl` (reanudación automática `-C -`, límite KB/s, reintentos exponenciales) y herramientas específicas para **Nintendo Switch (DBI / `ftpd` / `sys-ftpd`)**.

#### Opciones y flags de línea de comandos (CLI)

| Opción / Flag | Descripción |
|---|---|
| `subeftp` (sin argumentos) | Abre la interfaz interactiva en terminal (TUI). |
| `-s`, `--subir`, `--ahora`, `--start`, `--now` | Junto a rutas: añade y **arranca la subida de inmediato** sin preguntar. Solo (`subeftp --subir`): arranca la cola pendiente. |
| `-d RUTA`, `--destino RUTA`, `--destino=RUTA` | Especifica la carpeta remota destino en el servidor FTP (ej: `-d /install`). |
| `-j`, `--solo-switch` | Al añadir rutas/carpetas por CLI, filtra e incluye únicamente archivos de Switch (`.nsp`, `.nsz`, `.xci`, `.xcz`, `.nro`). |
| `--anadir RUTA...` | Explícito para añadir archivos o carpetas (o usa `--` antes de las rutas). |
| `--estado` | Muestra el estado del worker FTP y la cola de subidas. |
| `--pendientes` | Lista archivos pendientes y calcula tiempo estimado según la velocidad límite configurada. |
| `--probar` | Prueba la conexión FTP configurada (si falla, ofrece buscar la Switch en la LAN). |
| `--buscar-switch` | Escanea las subredes LAN buscando una Nintendo Switch con servidor FTP activo y permite guardar su IP/puerto. |
| `--parar` | Detiene de forma limpia la subida en segundo plano (mata `curl` dejando el archivo listo para reanudar con `-C -`). |
| `--log [N]` | Muestra las últimas `N` líneas del registro `subeftp.log` (por defecto `40`). |
| `-h`, `--help`, `help` | Muestra la ayuda integrada y sale. |

#### Ejemplos de uso rápido por SSH

```bash
# Buscar la IP de la Nintendo Switch en tu red local
docker exec -it toolbox subeftp --buscar-switch

# Subir todos los juegos Switch de una carpeta hacia /install y empezar ya
docker exec -it toolbox subeftp -s -j -d /install /host/home/usuario/Descargas/Switch

# Probar conexión FTP, ver cola/estado o parar limpiamente
docker exec -it toolbox subeftp --probar
docker exec -it toolbox subeftp --estado
docker exec -it toolbox subeftp --parar
```

#### Comandos del menú interactivo TUI (`subeftp`)

Al ejecutar `subeftp` (o `toolbox` → `2`):

- **Teclas del menú principal**:
  - `1`: **Explorar y añadir a la cola**
  - `2`: **Ver / gestionar la cola**
  - `3` (o `p`): **Ver solo archivos pendientes**
  - `4`: **Ver progreso en vivo**
  - `5` (o `s`): **▶ Iniciar / ⏹ Detener subida en 2º plano** (también detecta y limpia un `curl` huérfano si existiera)
  - `6`: **Probar conexión FTP**
  - `7` (o `b`): **Buscar IP de Switch en la red**
  - `8`: **Editar configuración FTP**
  - `9`: **Ver registro de actividad (log)**
  - `h` (o `?`): **Ayuda** · `0` (o `q`): **Salir**

- **Comandos dentro del Explorador (opción `1`)**:
  - `N`: entra en la carpeta `N` o añade el archivo `N`.
  - `1,3,5-8`: añade múltiples archivos por lista o rangos.
  - `j`: añade **solo juegos Switch** (`.nsp`, `.nsz`, `.xci`, `.xcz`, `.nro`) de la vista actual.
  - `t`: añade todos los archivos visibles de la vista actual.
  - `r N`: añade recursivamente la carpeta `N` con sus subcarpetas.
  - `d`: abre el selector remoto FTP (con atajos `inst` → `/install`, `nsp` → `/nsp`, `sw` → `/switch`, `sd` → `/`, `n NOMBRE` para nueva subcarpeta y `e RUTA` para ruta manual).
  - `/TEXTO` / `*`: filtra por nombre o quita el filtro.
  - `..` / `v`: sube una carpeta o vuelve al menú.

- **Comandos dentro de Gestión de Cola (opción `2` o `3`)**:
  - `t`: alterna entre **TODOS** o **SOLO PENDIENTES**.
  - `ord`: **auto-ordena juegos Switch** agrupando por juego y ordenando `Juego Base ➔ Update ➔ DLC` (`[BASE]`, `[UPD]`, `[DLC]`, `[NRO]`).
  - `/TEXTO` / `*` o `/`: busca por nombre, ID o estado (ej: `/mario`, `/pend`).
  - `b N[,M-K]`, `sub N`, `baj N`, `top N`, `m N POS`, `p N[,M-K]`, `d N[,M-K]`, `c`, `f`, `e`, `x`: idénticos a `mover` (para reordenar, pausar, cambiar carpeta remota, limpiar o reintentar).

- **Insignias y estados en `subeftp`**:
  - Switch: `[BASE]` (ID terminando en `000`), `[UPD]` (`800` / `vXXXX`), `[DLC]`, `[NRO]`.
  - Estados: `PEND`, `REAN`, `PAUS`, `SUB` (subiendo actualmente), `OK`, `ERR`.

---

## 🔄 Detalles técnicos: Mover / Copiar local

1. **Comprobación de firma de origen:** guarda `tamaño:mtime_ns` al encolar; si el archivo cambió antes de procesarse o reanudarse, descarta parciales antiguos y vuelve a empezar.
2. **Movimiento instantáneo en mismo disco:** compara `st_dev` entre el archivo origen y la carpeta destino; si coinciden, ejecuta `os.replace()` (`mv` atómico).
3. **Copia segura entre discos con `rsync`:**
   - Verifica espacio libre disponible antes de copiar (`os.statvfs`).
   - Copia a `<destino>/.mover-partial/<archivo>` con `--partial --append-verify` y `--bwlimit` opcional.
   - Nunca deja un archivo final corrupto a medias: solo renombra al destino definitivo tras superar la verificación (`tamaño` o `SHA-256`; al mover siempre se verifica al menos el tamaño).
4. **Políticas de conflicto:**
   - `omitir`: si es copia con mismo tamaño → `OK`; si es mover con mismo tamaño → compara byte a byte antes de borrar origen; si tiene distinto tamaño → `ERR` conservando el origen.
   - `sobrescribir`: reemplaza atómicamente tras verificar la copia en `.mover-partial`.
   - `renombrar`: guarda como `nombre (1).ext`, `nombre (2).ext`, etc.
5. **Limpieza al mover carpetas completas:** al completar el último archivo de una carpeta añadida recursivamente, borra las subcarpetas vacías del origen (`rmdir`, sin borrar nada ajeno).

---

## 🎮 Detalles técnicos: FTP y Nintendo Switch (DBI / ftpd)

- **Transferencia segura con `curl`:** credenciales en archivo temporal con permisos `600` (nunca expuestas en `ps`), soporte `--ftp-create-dirs`, `--limit-rate`, `--ssl-reqd` (FTPS explícito), `--disable-epsv` (pasivo) o `--ftp-port -` (activo).
- **Reanudación y reintentos:** comprueba el tamaño en el servidor; si coincide lo marca `OK`; si es menor por un corte reanuda con `-C -`. Errores temporales reintentan con espera exponencial (`espera × 3^intento`, máximo `600 s`), mientras que errores de credenciales/permisos (`9`, `67`, `69`, `78`) fallan sin bucle infinito.
- **Nintendo Switch (`DBI` / `ftpd` / `sys-ftpd`):**
  - Desactiva **Verificar tamaño final** cuando uses **DBI**, ya que DBI instala los paquetes al vuelo sin guardar el archivo en el destino.
  - El **buscador de Switch en red** escanea en lotes concurrentes de 50 sockets con timeout de `1,8 s` sobre interfaces físicas reales, probando por defecto los puertos `5000`, `2121` y `21` y leyendo el banner FTP `220`.

---

## 🔒 Publicar con Nginx Proxy Manager y Tinyauth

Al usar `--network host`, puedes exponer la web de forma segura tras HTTPS con Nginx Proxy Manager y Tinyauth:

1. En **Nginx Proxy Manager**, crea un Proxy Host apuntando a `http://<IP-del-servidor>:8090` (activa **Websockets Support**).
2. En la pestaña **Advanced**, pega el bloque de `tinyauth` documentado en el repositorio `docker` (`docker/README.md`).

---

## 💾 Persistencia y copias de seguridad

Todo el estado persistente del contenedor vive en `/config` (mapeado a `./config` en `docker/toolbox/`):

| Ruta dentro de `/config` | Contenido |
|---|---|
| `/config/settings.json` | Configuración unificada Web + SSH (permisos `600`, incluye contraseña FTP). |
| `/config/queue.db` | Base de datos SQLite compartida (Web + SSH) con las colas y los registros de actividad. |
| `/config/cli/mover/` | Solo utilizado si ejecutas `mover --legacy` o fuera de Docker. |
| `/config/cli/ftp/` | Solo utilizado si ejecutas `subeftp --legacy` o fuera de Docker. |

---

## 🧪 Desarrollo y Tests

```bash
cd toolbox

# Validación Bash y ShellCheck (si está instalado)
bash -n entrypoint.sh scripts/*.sh
shellcheck -S warning scripts/*.sh

# Compilación, sintaxis JS y tests unitarios Python
python3 -m py_compile app/*.py tests/*.py
python3 -m unittest discover -s tests -v
node --check static/app.js
```
