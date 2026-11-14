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
  - [4. Recetas paso a paso](#4-recetas-paso-a-paso)
- [API REST](#-api-rest)
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
├── requirements-dev.txt        # requirements.txt + httpx y pyftpdlib (solo tests)
├── README.md                   # Esta guía (accesible también desde la pestaña Web y 'toolbox readme')
├── .github/workflows/
│   ├── ci.yml                  # bash -n, shellcheck, py_compile y tests unitarios en cada push/PR a main
│   └── docker-publish.yml      # Construye y publica alebupal/toolbox:latest (amd64 + arm64)
├── app/
│   ├── __init__.py
│   ├── main.py                 # API REST, explorador /host, endpoint /api/readme y estáticos
│   ├── cli.py                  # CLI unificado (mover/subeftp en Docker): habla con la API o, si no responde, con SQLite
│   ├── models.py               # Esquemas Pydantic
│   ├── config.py               # settings.json (permisos 600) y validación de rutas
│   ├── db.py                   # Persistencia SQLite (queue.db) de colas y logs
│   ├── mover_worker.py         # Motor local: mv mismo disco, rsync .mover-partial, verificación
│   ├── ftp_worker.py           # Motor FTP/FTPS: curl, reanudación -C -, límite KB/s, listado remoto
│   ├── services.py             # Lógica común Web/CLI: listado ordenado, búsqueda, rutas relativas, juego completo
│   ├── trash.py                # Papelera (.toolbox-trash por punto de montaje): borrar, restaurar, vaciar, caducidad
│   ├── switch_utils.py         # Insignias Switch, auto-orden Base→UPD→DLC y escáner LAN
│   ├── switch_backup.py        # Backup de la microSD por FTP (sin carpetas de juegos)
│   └── worker_manager.py       # Hilos en segundo plano independientes (Mover y FTP)
├── static/
│   ├── index.html              # Interfaz web en español
│   ├── style.css               # Tema oscuro adaptable con doble panel y visor de README
│   └── app.js                  # Control de explorador doble panel, colas, ajustes, logs y Markdown
├── scripts/
│   ├── toolbox.sh              # Wrapper CLI unificado (disponible como 'toolbox' en el contenedor)
│   ├── mover.sh                # Script Bash completo de copia/movimiento (disponible como 'mover')
│   ├── subeftp.sh              # Script Bash completo de subida FTP/Switch (disponible como 'subeftp')
│   ├── mover.conf.example      # Plantilla de configuración CLI para mover.sh (modo --legacy)
│   └── subeftp.conf.example    # Plantilla de configuración CLI para subeftp.sh (modo --legacy)
└── tests/
    ├── test_core.py            # Suite de tests unitarios (unittest)
    └── test_backup.py          # Tests del backup de la microSD (servidor FTP local con pyftpdlib)
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
- **Automática (GitHub Actions):** el workflow `.github/workflows/docker-publish.yml` construye y sube `alebupal/toolbox:latest` (`linux/amd64` y `linux/arm64`) en cada `push` a `main` usando los secrets del repositorio `DOCKERHUB_USERNAME` y `DOCKERHUB_TOKEN`. Por separado, `.github/workflows/ci.yml` valida cada `push`/PR a `main` (ver [Desarrollo y Tests](#-desarrollo-y-tests)).

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
| `CONFIG_DIR` | `/config` | Carpeta donde se guardan `settings.json` y `queue.db`. |
| `MOVER_DATA_DIR` | `/config/cli/mover` | Estado y configuración de `mover --legacy`. |
| `SUBEFTP_DATA_DIR` | `/config/cli/ftp` | Estado y configuración de `subeftp --legacy`. |
| `MOVER_FORZAR_RSYNC` | *(vacío)* | `1` = usa `rsync` aunque origen y destino estén en el mismo disco (útil para pruebas). |
| `TOOLBOX_UNIFIED` | *(vacío)* | `1` = fuerza el modo unificado Web + SSH al ejecutar los scripts fuera del contenedor. |

### Modo de red (`--network host`)

1. **Auto-descubrimiento de Nintendo Switch en LAN**: permite inspeccionar las interfaces activas del servidor (todas salvo `lo`, `docker*`, `br-*`, `veth*`, `virbr*`, `tun*`, `tap*`, `wg*` y `tailscale*`) y escanear su subred `/24`.
2. **Modo FTP activo** además del pasivo sin conflictos de puertos dinámicos de datos.

---

## 🌐 Uso de la Interfaz Web

Arriba están siempre las dos tarjetas de estado (**💾 Mover / Copiar** y **📡 FTP / Switch**) con el archivo en curso, porcentaje, velocidad, `ETA`, el progreso global de la tanda (`2/5 · 1,2/5,5 GB · ETA total`) y `Preparando…` mientras el worker prepara el siguiente archivo. En móvil se resumen en una franja (`● Mover 42 % · ○ FTP parado`) que se despliega al tocarla.

- Todo se ejecuta **en segundo plano dentro del servidor** (`/config/queue.db`): si cierras el navegador, la copia o la subida **sigue** hasta terminar.
- Puedes volver a abrir la Web (o consultar por SSH con `toolbox web estado`) para ver el progreso o el resultado.
- **Tema** automático (según el sistema), claro u oscuro: botón de la cabecera, menú **Más** en móvil o *Ajustes*. Se recuerda en el navegador.

### 1. 📁 Explorar (doble panel `Origen ⇄ Destino`)
- **Modo**: **💾 Copiar / Mover local** o **📡 Subir por FTP / Switch**, y la acción (**📋 Copiar** o **✂️ Mover**).
- **Ruta**: migas de pan clicables; al pulsar sobre ellas (o en `✎`) se convierten en un campo para escribir la ruta (`Enter` para ir, `Esc` para cancelar). Botones rápidos (`Raíz servidor`, `/mnt`, `/media`…) y `⬆` para subir.
- **Panel Origen**:
  - Columnas **Nombre**, **Modificado** y **Tamaño**: pulsa la cabecera para ordenar (en móvil, selector *Ordenar*). El orden se recuerda.
  - **Filtro** en la carpeta actual; con **En subcarpetas** busca en todo el árbol (todas las palabras, en cualquier orden) y los resultados se pueden seleccionar y enviar a la cola igual que en una carpeta.
  - Por fila: enviar (`Copiar →` / `Mover →` / `Subir →`), tamaño de carpeta bajo demanda (`📏`), renombrar (`✏️`) y, en modo FTP, **🎮 juego completo**. En móvil, las acciones secundarias están en el menú `⋯` de cada fila.
  - Sobre la selección: **➕ A la cola**, **⚡ Enviar e iniciar**, **🎮 Solo Switch** y **🎮 Juego completo** (modo FTP) y **🗑 Borrar…**.
- **🎮 Juego completo** (modo FTP): encola base, actualizaciones y DLC de la carpeta o selección **en orden** (`BASE → UPD → DLC`), muestra cada juego detectado y avisa si falta el juego base de alguna actualización o DLC. Desde el mismo aviso puedes iniciar la subida.
- **🗑 Borrar…**: un diálogo muestra qué se borra y cuánto ocupa. Por defecto **se mueve a la papelera** y el aviso inferior ofrece **Deshacer**; puedes marcar *Eliminar definitivamente* para saltártela. Se mantiene la protección frente al borrado de carpetas críticas del sistema.
- **Panel Destino**: contenido de la carpeta local o remota (atajos `/`, `/install`, `/nsp`, `/switch`), espacio libre, columnas **ordenables por nombre, fecha de modificación y tamaño** (pulsa la cabecera; el orden se recuerda y es independiente del de Origen; en FTP solo por nombre), `+ Carpeta` y **★ Favorito** para guardar la carpeta actual como destino favorito. Con **Destino automático (ajustes)** se usa la ruta por defecto de *Ajustes*.

### 2. 📋 Colas
- **Cola Mover** y **Cola FTP**, con filtro `Solo activos` (`PEND`, `REAN`, `PAUS`, `PROC`) y un resumen con el progreso global y el espacio libre (disco destino o **SD de la Switch**; si el servidor FTP no lo permite verás *espacio no disponible*).
- Cada elemento muestra el **nombre** en negrita y debajo `carpeta origen → carpeta destino` (la ruta completa en el *tooltip* y con el botón `⧉` para copiarla). El elemento en curso tiene su propia barra de progreso y solo ofrece **⏹ Detener**.
- **Reordenar**: arrastra el asa `⠿` (ratón o dedo), usa las flechas del teclado sobre el asa o los botones `⤒ ↑ ↓`. Además: pausar/reanudar, cambiar destino (`📂`), alternar `Copiar ⇄ Mover` y quitar de la cola (`✕`). En móvil todo está en el menú `⋯` de cada elemento, con texto.
- **Acciones de la cola** (en móvil, en `⋯ Acciones de la cola`): cambiar destino de los pendientes, `↕ Auto-ordenar` (en FTP `BASE → UPD → DLC → NRO`), reintentar errores, limpiar `OK` / `ERR` y vaciar pendientes (con confirmación).
- **Espacio total** (cola Mover): compara lo pendiente con el espacio libre del destino (descontando los `mv` instantáneos en el mismo disco) y avisa con `⚠️ Espacio insuficiente`.

### 3. 🕘 Historial
- Cada copia, movimiento o subida terminada (aunque limpies la cola): fecha, destino, tamaño, **duración**, **velocidad media** y estado. Filtrable por *Mover / Copiar* o *FTP*, con *Cargar más* y *Borrar historial…*.

### 4. 🗑 Papelera
- Lo borrado desde Explorar queda aquí durante los días configurados en *Ajustes* (`7` por defecto) y después se elimina solo. El botón **Desactivar papelera** (o la casilla *Usar papelera* de Ajustes) hace que los borrados sean definitivos: el diálogo de borrar lo avisa y no ofrece Deshacer. Lo que ya estaba en la papelera se conserva hasta caducar. Puedes **restaurar** a su ruta original, eliminar definitivamente lo seleccionado o **vaciar** la papelera.

### 5. ⚙️ Ajustes
- **Los cambios se guardan solos** (indicador *Guardando… / ✓ Guardado*).
- **Mover / Copiar**: origen inicial, destino por defecto, operación, conflictos (`omitir`, `sobrescribir`, `renombrar`), verificación (`tamano`, `checksum`, `no`), **límite en MB/s** (`0` = sin límite), reintentos y espera, y **carpetas favoritas** como lista (añadir con el selector o con *★ Favorito* del explorador, quitar con `✕`).
- **FTP / Switch**, agrupado en **Conexión** (IP, puerto, usuario, contraseña, pasivo, FTPS/TLS) y **Comportamiento** (carpeta local inicial, carpeta remota, límite en MB/s, reintentos, estructura de carpetas, verificar tamaño final). Incluye **🔍 Buscar Switch** y **🔌 Probar conexión**.
- **💾 Backup de la microSD**: copia por FTP los **ficheros básicos de la SD de la Switch** (configuración, homebrew, partidas guardadas) a una carpeta del servidor, **sin juegos ni carpetas pesadas** (ver [detalles](#backup-de-la-microsd)). Ajustes: carpeta de backups, carpeta remota a copiar (`/` = toda la SD), carpetas excluidas (una por línea), extensiones excluidas e *Incremental*. **▶ Hacer backup** lo lanza en segundo plano con barra de progreso (fase *Analizando la SD* y después *Copiando*), **⏹ Detener** lo para y al terminar se muestra el resumen (ficheros, tamaño, carpetas excluidas, errores) y queda en el **Historial** como `BACKUP SD`. No se puede lanzar mientras la cola FTP sube (ni al revés).
- **Papelera y apariencia**: usar papelera (sí/no), días en la papelera y tema.

### 6. 📜 Registro y 📖 Guía
- **Registro**: eventos persistentes en SQLite (últimos 5.000), filtrables por módulo (`Todos`, `Mover`, `FTP`, `Sistema`).
- **Guía**: este `README.md` dentro de la web, con enlaces internos y botón **Copiar** en cada bloque de comandos.

### 📱 Móvil y accesibilidad
- **Barra de navegación inferior** (`<= 768px`) con *Explorar*, *Colas* (con contador), *Ajustes* y *Más* (Historial, Papelera, Registro, Guía y tema).
- **Conmutador `📂 Origen ⇄ 🎯 Destino`** (`<= 960px`) y **barra de selección fija** abajo al marcar archivos.
- Filas y tarjetas sin scroll horizontal, **objetivos táctiles de al menos 44 px**, campos a `16px` (sin auto-zoom) y diálogos como hoja inferior.
- Diálogos propios accesibles (sin `confirm`/`prompt` del navegador), navegación completa con teclado, foco visible y estados vacíos o de error con una acción para continuar.

---

## 💻 Guía detallada de comandos SSH / CLI

Dentro del contenedor hay tres comandos globales: `toolbox`, `mover` y `subeftp` (enlaces a `scripts/toolbox.sh`, `scripts/mover.sh` y `scripts/subeftp.sh`). Se lanzan por SSH con `docker exec -it toolbox <comando>` o desde `cron` con `docker exec toolbox <comando>` (sin `-it`).

### Índice de comandos

- [Cómo funcionan los comandos (Web, SQLite y `--legacy`)](#cómo-funcionan-los-comandos-web-sqlite-y---legacy)
- [Referencia rápida](#referencia-rápida)
- [1. `toolbox`](#1-comando-unificado-toolbox-scriptstoolboxsh): menú, `estado`, `readme`, `web estado|cola|iniciar|parar|logs|historial|papelera|espacio|buscar-switch|backup`
- [2. `mover`](#2-mover--copiar-local-mover-scriptsmoversh): añadir, `--iniciar`, `--parar`, `--estado`, `--pendientes`, `--listar`, `--buscar`, `--tamano`, `--renombrar`, `--cambiar-destino`, `--borrar`, `--papelera`, `--restaurar`, `--vaciar-papelera`, `--papelera-activar`, `--papelera-desactivar`, `--historial`, `--limpiar-historial`, `--log`, menú TUI
- [3. `subeftp`](#3-ftp-y-nintendo-switch-subeftp-scriptssubeftpsh): añadir, `--subir`, `--parar`, `--juego`, `--espacio`, `--backup`, `--probar`, `--buscar-switch` y las mismas opciones de consulta, papelera e historial que `mover`, menú TUI
- [4. Recetas paso a paso](#4-recetas-paso-a-paso)

### Cómo funcionan los comandos (Web, SQLite y `--legacy`)

- **Modo unificado (por defecto dentro del contenedor).** `mover` y `subeftp` ejecutan `python3 -m app.cli`. Primero preguntan a la web local (`http://127.0.0.1:$PORT/api/health`, espera máxima 1,5 s):
  - **Si la web responde**, todo va por su API REST: lo que hagas por SSH aparece al instante en la web y al revés.
  - **Si no responde**, leen y escriben directamente `/config/queue.db` (SQLite) y `/config/settings.json`. Añadir, consultar, borrar, papelera, historial, listar y buscar funcionan igual; **arrancar o detener una cola necesita la web** (`--iniciar`, `--subir`, `--parar` y la tecla `5` del menú fallan con `Error: El servidor web de Toolbox no está respondiendo…`).
- **Rutas.** Dentro del contenedor el disco del servidor está en `/host`, así que las rutas son `/host/...` (ej. `/host/mnt/peliculas`). Las relativas se resuelven contra la carpeta actual. Nada puede salir de `DATA_ROOT`: una ruta fuera da `Error: … La ruta está fuera de DATA_ROOT (/host)`.
- **IDs y números de fila.** Por línea de comandos, `--cambiar-destino` usa el **ID** de la cola (el `[#12]` que se ve en `--estado`) y `--restaurar` / `--vaciar-papelera` usan el ID de la papelera (`#3` en `--papelera`). En los menús TUI se trabaja con **números de fila** (`1`, `3-5`, `1,4`).
- **Modo clásico `--legacy`.** Con `--legacy` (o fuera de Docker) `mover.sh` y `subeftp.sh` usan sus scripts Bash aislados con `cola.txt` en `/config/cli/...` y no comparten nada con la web. Lo marcado *(solo --legacy)* solo existe en ese modo; lo marcado *(no disponible en --legacy)* solo existe en el modo unificado.
- **Códigos de salida.** `0` = correcto; `1` = error, uso incorrecto, opción desconocida (`Opción no reconocida: --x`), ruta inexistente o error de la API (se imprime `Error: HTTP 400: <motivo>`). Cada opción indica abajo sus casos concretos.
- **Formato.** Los tamaños se muestran en B/KB/MB/GB/TB (base 1024) y las fechas en hora local del contenedor (`TZ`).

### Referencia rápida

| Quiero… | Comando |
|---|---|
| Abrir el menú general | `toolbox` |
| Mover una descarga a la biblioteca y empezar ya | `mover -m -i -d /host/mnt/multimedia/Peliculas "/host/home/usuario/Descargas/Peli (2024).mkv"` |
| Copiar una carpeta entera al destino por defecto | `mover -c -i "/host/home/usuario/Descargas/Serie S01"` |
| Ver qué se está copiando, el progreso global y la cola | `mover --estado` |
| Ver la carpeta de descargas, lo más nuevo primero | `mover --listar /host/home/usuario/Descargas --orden fecha --desc` |
| Buscar un fichero por nombre en todas las subcarpetas | `mover --buscar "dune 2160p" /host/home/usuario/Descargas` |
| Subir un juego completo (base + update + DLC) a la Switch | `subeftp --juego "/host/home/usuario/Juegos/Zelda" -d /install -s` |
| Saber si cabe en la SD | `subeftp --espacio` |
| Copiar la microSD al servidor sin los juegos | `subeftp --backup -d /host/mnt/backups/switch` |
| Ver lo que ya se ha copiado/subido | `mover --historial` · `subeftp --historial` |
| Borrar (a la papelera) y recuperar | `mover --borrar RUTA` → `mover --papelera` → `mover --restaurar ID` |
| Borrar sin papelera | `mover --borrar -y --permanente RUTA` |
| Activar / desactivar la papelera | `mover --papelera-desactivar` · `mover --papelera-activar` |
| Arrancar / parar una cola | `mover --iniciar` · `subeftp --subir` · `mover --parar` · `subeftp --parar` |
| Ver los últimos eventos | `mover --log 50` · `toolbox web logs sistema 20` |
| Consultar la web desde SSH | `toolbox web estado` · `toolbox web cola ftp` |

---

### 1. Comando unificado `toolbox` (`scripts/toolbox.sh`)

```text
toolbox [mover|ftp|subeftp|web|estado|readme|-h] [ARGUMENTOS...]
```

| Comando | Qué hace | Salida / código |
|---|---|---|
| `toolbox` | Abre el menú interactivo (ver abajo). | `0` al salir. |
| `toolbox mover [ARGS...]` | Ejecuta `mover.sh` con los mismos argumentos (ver [sección 2](#2-mover--copiar-local-mover-scriptsmoversh)). | Código de `mover`. |
| `toolbox ftp [ARGS...]` · `toolbox subeftp [ARGS...]` | Ejecuta `subeftp.sh` con los mismos argumentos (ver [sección 3](#3-ftp-y-nintendo-switch-subeftp-scriptssubeftpsh)). | Código de `subeftp`. |
| `toolbox estado` · `toolbox status` | En el contenedor: `mover --estado` seguido de `subeftp --estado` (resumen + cola completa). Fuera de Docker: estado de los scripts clásicos y, si la web responde, `toolbox web estado`. | `0`. |
| `toolbox readme` · `guia` · `docs` | Muestra este README (con `less -R` si hay terminal). | `1` si no encuentra el fichero. |
| `toolbox web SUBCOMANDO` | Consulta o controla la web por su API (`curl` a `http://127.0.0.1:$PORT`). Ver tabla siguiente. | `1` si la web no responde (`Web no accesible en …`) o el subcomando no existe. |
| `toolbox -h` · `--help` · `help` | Ayuda resumida. | `0`. |
| `toolbox <otro>` | `Subcomando desconocido: <otro>`. | `1`. |

#### Subcomandos `toolbox web`

| Subcomando | Argumentos (por defecto) | Qué muestra o hace |
|---|---|---|
| `web estado` · `status` | — | Por cola: estado del worker, pendientes (en bytes), pausados, OK, ERR; si está en marcha, `Preparando <fichero>` o `<fichero>: % \| velocidad \| ETA` y la línea `Global: hechos/total archivo(s) \| bytes hechos/total`; en FTP, el espacio libre de la SD si se conoce. |
| `web cola` · `queue` | `[mover\|ftp]` (`mover`) | Toda la cola de la web: `[ESTADO] [OPERACIÓN] [BASE/UPD/DLC] origen -> destino \| ERR: motivo`. |
| `web iniciar` · `start` | `<mover\|ftp>` (obligatorio) | Arranca el worker de esa cola. Imprime la respuesta JSON de la API (`{"started":true,…}`). |
| `web parar` · `stop` | `<mover\|ftp>` (obligatorio) | Pide la parada limpia (el fichero en curso queda `REAN`). Imprime JSON. |
| `web logs` · `log` | `[todos\|mover\|ftp\|sistema] [N]` (`todos 40`) | Últimos N eventos del registro, del más antiguo al más reciente. Los borrados, renombrados y la papelera son de tipo `sistema`. |
| `web historial` · `history` | `[todos\|mover\|ftp] [N]` (`todos 30`) | Últimas N transferencias terminadas (OK o ERR) con tamaño, duración y error. |
| `web papelera` · `trash` | — | Elementos de la papelera con su ID. Para restaurar o vaciar usa `toolbox mover --restaurar ID` / `--vaciar-papelera`. |
| `web espacio` · `space` | — | Consulta el espacio libre de la SD (ver [`subeftp --espacio`](#subeftp---espacio)). |
| `web buscar-switch` · `scan` | — | Escanea la LAN desde el servidor y lista `IP:puerto -> banner`. |
| `web backup` | `[estado\|iniciar\|parar]` (`estado`) | Progreso del backup de la microSD o resultado del último; `iniciar` usa la carpeta de backups de Configuración. |

Ejemplo de `toolbox web estado` con una copia en marcha:

```text
  [MOVER] EN EJECUCION | Pendientes: 2 (4026531840 B) | Pausados: 0 | OK: 5 | ERR: 0
     -> Dune Parte Dos (2024) 2160p.mkv: 42.0% | 29.47MB/s | ETA 0:00:51
     Global: 5/7 archivo(s) | 9663676416/13690208256 B
  [FTP] DETENIDO | Pendientes: 3 (18647875584 B) | Pausados: 0 | OK: 0 | ERR: 0
     Espacio libre en la SD: 41875931136 B
```

#### Menú interactivo `toolbox`

| Tecla | Acción |
|---|---|
| `1` | Abre el menú de `mover` (igual que ejecutar `mover`). |
| `2` | Abre el menú de `subeftp`. |
| `3` | Estado combinado (`toolbox estado`). |
| `4` | Últimos 30 eventos de la web (`toolbox web logs todos 30`). |
| `5` | Lee este README. |
| `6` | Ayuda rápida (`toolbox --help`). |
| `7` | Historial de transferencias (`toolbox web historial todos 30`). |
| `8` | Papelera (`toolbox web papelera`) con recordatorio de cómo restaurar o vaciar. |
| `0` · `q` · `Q` | Salir. |

```bash
docker exec -it toolbox toolbox                 # menú
docker exec -it toolbox toolbox web estado      # progreso de lo que lanzaste en la web
docker exec -it toolbox toolbox web cola ftp    # cola FTP de la web
docker exec -it toolbox toolbox web logs sistema 20
docker exec -it toolbox toolbox web historial ftp 10
docker exec -it toolbox toolbox readme
```

---

### 2. Mover / Copiar local `mover` (`scripts/mover.sh`)

Copia o mueve ficheros dentro del servidor. En el mismo disco, «mover» es un `mv` instantáneo; entre discos usa `rsync` con parciales en `.mover-partial`, reanudación y verificación (ver [detalles técnicos](#-detalles-técnicos-mover--copiar-local)).

```text
mover                                   # menú TUI
mover [-c|-m] [-d DESTINO] [-i] RUTA... # añadir a la cola
mover --OPCIÓN [ARGUMENTOS]             # consultas y acciones (una por llamada)
```

#### Añadir ficheros o carpetas a la cola

| Opción | Valor | Por defecto | Efecto |
|---|---|---|---|
| `RUTA...` | ficheros o carpetas | — | Se añaden a la cola compartida. Las **carpetas** se recorren enteras (sin ficheros ocultos ni enlaces simbólicos). |
| `-c`, `--copiar` | — | operación de Configuración (`copiar`) | Copia y conserva el origen. |
| `-m`, `--mover` | — | operación de Configuración | Mueve: borra el origen tras verificar la copia. Al mover una carpeta completa, borra después sus subcarpetas vacías. |
| `-d RUTA`, `--destino RUTA`, `--destino=RUTA` | carpeta local | destino de Configuración | Destino explícito: los ficheros van a `RUTA/<fichero>` y las carpetas a `RUTA/<carpeta>/<subrutas>`. Sin `-d`, se usa el destino de Configuración y, si *Mantener estructura* está activo (por defecto), las carpetas conservan su estructura. |
| `-i`, `--iniciar`, `-s`, `--ahora`, `--subir`, `--start`, `--now` | — | preguntar | Arranca la cola en cuanto se añade, sin preguntar. Sin esta opción: si hay terminal pregunta `¿Iniciar en segundo plano ahora? [S/n]`; en `cron` (sin terminal) **no** arranca. |
| `--anadir` | — | — | Se ignora; sirve para dejar explícito que lo que sigue son rutas. |
| `--` | — | — | Todo lo que va detrás se trata como ruta, aunque empiece por `-`. |

- **Salida:** `Añadidos a la cola compartida (mover): N (omitidos: M)`. Se omiten los ficheros cuyo destino ya está en la cola sin terminar. Si una ruta no existe, avisa por stderr (`Aviso: no existe o no es accesible: …`).
- **Código de salida:** `0` si se procesó la petición (aunque se hayan omitido todos); `1` si ninguna ruta es válida (`No se encontraron archivos válidos para añadir a la cola.`), si hay una opción desconocida o si `-d` no lleva ruta (`Falta la ruta tras -d`).

```bash
# Mover una película a la biblioteca y empezar ya
docker exec -it toolbox mover -m -i -d /host/mnt/multimedia/Peliculas "/host/home/usuario/Descargas/Oppenheimer (2023).mkv"
# Copiar una temporada completa al destino por defecto (mantiene carpetas)
docker exec -it toolbox mover -c -i "/host/home/usuario/Descargas/The Bear S03"
# Desde cron: encolar sin preguntar y arrancar
docker exec toolbox mover -m -i -d /host/mnt/multimedia/Series /host/home/usuario/Descargas/completas
```

#### Arrancar y parar

| Opción | Efecto | Salida | Código |
|---|---|---|---|
| `--iniciar` · `-i` (solas) | Arranca el worker de la web con lo que haya pendiente. | `Worker mover iniciado en segundo plano.` | `0`; `1` si la web no responde. |
| `--parar` | Parada limpia: el fichero en curso queda `REAN` y se reanuda en la siguiente ejecución. | `Detención solicitada para mover.` | `0`; `1` si la web no responde. |

#### `--estado` y `--pendientes`

`--estado` muestra el resumen y **toda** la cola; `--pendientes`, solo lo que queda por hacer (`PEND`, `REAN`, `PAUS`, `PROC`). Las rutas de la cola se muestran relativas a la carpeta de origen/destino configurada (o a `/host`) para que se lean mejor. *(La estimación de tiempo total de `--pendientes` es solo --legacy.)*

```text
$ mover --estado
=== MOVER / COPIAR (Cola compartida Web + SSH) ===
  Estado: EN EJECUCIÓN | Pendientes: 2 (3.8 GB) | Pausados: 0 | OK: 5 | ERR: 0 | Libre dest: 167.9 GB
  Fase: TRANSFIRIENDO Dune Parte Dos (2024)/Dune Parte Dos (2024) 2160p.mkv -> 42.0% (1.0 GB/2.3 GB) | 29.5 MB/s | ETA 0:47
  Global: 5/7 archivo(s) · 9.0 GB/12.8 GB · ETA total 2:11
    1. [#12] [PROC] [COPIAR] Dune Parte Dos (2024)/Dune Parte Dos (2024) 2160p.mkv -> Dune Parte Dos (2024)/Dune Parte Dos (2024) 2160p.mkv (2.3 GB)
    2. [#13] [PEND] [MOVER] Oppenheimer (2023).mkv -> Peliculas/Oppenheimer (2023).mkv (1.5 GB)
```

- `Fase: PREPARANDO <fichero>` aparece mientras se prepara la copia (comprobaciones, espacio, inicio de `rsync`) y todavía no hay progreso.
- `Global` cuenta los ficheros terminados en esta ejecución frente al total (terminados + pendientes) y estima el tiempo restante con la velocidad media.
- Si lo pendiente no cabe en el destino aparece `⚠ AVISO ESPACIO: Se requieren X pero solo quedan Y libres…`.
- Código de salida: `0`.

#### `--listar [RUTA] [--orden nombre|fecha|tamano] [--desc]` *(no disponible en --legacy)*

Lista una carpeta con fecha de modificación y tamaño. Sin `RUTA`, lista la carpeta de origen de Configuración.

| Opción | Valores | Por defecto | Efecto |
|---|---|---|---|
| `--orden X` · `--orden=X` | `nombre`, `fecha`, `tamano` (también `name`, `mtime`, `size`, `tamaño`) | `nombre` | Criterio de orden. Las carpetas van siempre primero; con `tamano` se ordenan por nombre (no se calcula su tamaño). |
| `--desc` | — | ascendente | Invierte el orden (con `fecha`: lo más reciente primero). |

```text
$ mover --listar /host/home/usuario/Descargas --orden fecha --desc
/host/home/usuario/Descargas  (orden: mtime desc)
    1) 2026-11-08 12:00  [DIR ]           Juegos/
    2) 2026-11-08 12:00  [DIR ]           Dune Parte Dos (2024)/
    3) 2026-11-11 09:02      1.5 GB  Oppenheimer (2023).mkv
    4) 2026-10-28 18:40    700.0 MB  La Sociedad de la Nieve (2023).mkv
```

No muestra ficheros ocultos ni la papelera. Código: `0`; `1` con un valor de `--orden` no válido (`Uso: --orden nombre|fecha|tamano`) u otra opción desconocida.

#### `--buscar TEXTO [RUTA]` *(no disponible en --legacy)*

Busca en `RUTA` (por defecto, la carpeta de origen de Configuración) y en todas sus subcarpetas los ficheros **y carpetas** cuyo nombre contiene **todas las palabras** de `TEXTO`, en cualquier orden, sin distinguir mayúsculas ni acentos (`pelicula acida` encuentra `La Película Ácida.mkv`). No sigue enlaces simbólicos, salta ficheros ocultos, la papelera y `/host/proc|sys|dev|run`. Se detiene a los 300 resultados o a los 5 segundos y lo indica.

```text
$ mover --buscar "sociedad nieve" /host/home/usuario/Descargas
Búsqueda de 'sociedad nieve' en /host/home/usuario/Descargas: 1 resultado(s)
    1) 2026-10-28 18:40    700.0 MB  La Sociedad de la Nieve (2023).mkv
```

Si hay más resultados de los que se muestran aparece `(resultados truncados: afina la búsqueda o elige una carpeta más concreta)`. Código: `0` con resultados, `1` sin resultados o sin `TEXTO`. Para añadir resultados a la cola, usa `?TEXTO` en el explorador del menú.

#### `--tamano RUTA`

Suma el tamaño de una carpeta (o fichero) sin seguir enlaces simbólicos.

```text
$ mover --tamano /host/home/usuario/Descargas/Juegos
/host/home/usuario/Descargas/Juegos: 17.4 GB (3 archivo(s))
```

Código: `0`; `1` si falta la ruta o no existe.

#### `--renombrar RUTA NUEVO_NOMBRE`

Renombra en la misma carpeta. No permite renombrar la raíz ni las carpetas de sistema de primer nivel (`/host/etc`, `/host/home`…), ni sobrescribir un nombre existente (`Error: HTTP 400: Ya existe 'X' en esa carpeta`). Salida: `Renombrado a: <ruta nueva>`. Código: `0` / `1`.

#### `--cambiar-destino ID[,ID...] RUTA`

Cambia la carpeta destino de elementos de la cola que aún no han terminado (no toca los `PROC` ni los `OK`). Los IDs son los `[#N]` de `--estado` (admite `#`). Si el elemento venía de una carpeta añadida entera, conserva su subruta dentro de la carpeta nueva. *(Indicar `todos` no está soportado.)*

```text
$ mover --cambiar-destino 13,14 /host/mnt/multimedia/Peliculas
Destino actualizado en 2 elemento(s).
```

#### Papelera: `--borrar`, `--papelera`, `--restaurar`, `--vaciar-papelera`, `--papelera-activar`, `--papelera-desactivar` *(papelera no disponible en --legacy)*

Por defecto, borrar **mueve a la papelera**: `<punto de montaje>/.toolbox-trash/<ID>/` del mismo disco, así que es instantáneo y no ocupa espacio extra. Los elementos se conservan `trash_days` días (ajuste de Configuración, por defecto `7`; con `0` se borra directamente) y luego se eliminan solos (la purga corre cada hora en la web y al consultar la papelera).

La papelera se puede **activar o desactivar** (ajuste `trash_enabled`, activada por defecto) desde *Ajustes* en la web, con el botón de la vista Papelera, con `--papelera-activar` / `--papelera-desactivar` o con la tecla `a` del submenú de papelera. **Desactivada, todo borrado es definitivo** desde la web y desde SSH: `--borrar` avisa por stderr (`Papelera desactivada: el borrado será definitivo.`) y pide `¿Eliminar DEFINITIVAMENTE…?`. Lo que ya estaba en la papelera se conserva, se puede restaurar y caduca normalmente. El cambio es el mismo ajuste para la web y para SSH.

| Opción | Efecto | Salida | Código |
|---|---|---|---|
| `--borrar [-y] [--permanente] RUTA...` | Envía a la papelera. Con terminal y sin `-y`/`--yes` pregunta `¿Enviar a la papelera N elemento(s)? [s/N]` (por defecto **no**). `--permanente` borra sin papelera (pregunta `¿Eliminar DEFINITIVAMENTE…?`). Las opciones pueden ir en cualquier posición. | `Enviados a la papelera: 1 (restaurar con --restaurar 7)` · `Eliminados definitivamente: N` | `0`; `1` si ninguna ruta existe, es una carpeta protegida (`Error: HTTP 400: Directorio del sistema protegido: etc`) o está en un disco donde no se puede crear la papelera (`… Bórralo definitivamente (permanent).`). |
| `--papelera` | Lista la papelera e indica si está activada. | ver ejemplo | `0` |
| `--papelera-activar` | Activa la papelera: los siguientes borrados se podrán restaurar. | `Papelera activada: los borrados se podrán restaurar.` (si `trash_days=0`, avisa de que hay que subir los días) | `0` |
| `--papelera-desactivar` | Desactiva la papelera sin pedir confirmación: los siguientes borrados serán definitivos. No toca lo que ya contiene. | `Papelera desactivada: los próximos borrados serán definitivos (lo que ya contiene se conserva hasta caducar).` | `0` |
| `--restaurar ID[,ID...]` | Devuelve cada elemento a su ruta original (recrea la carpeta si hace falta). Si ya existe algo con ese nombre, restaura como `nombre (restaurado).ext`. | `Restaurado #7 en /host/…/peli.mkv (renombrado: ya existía)` | `0`; `1` si falta el ID o alguno no está en la papelera. |
| `--vaciar-papelera [-y] [ID,...]` | Borra definitivamente toda la papelera o solo esos IDs. Con terminal y sin `-y` pide confirmación. | `Eliminados definitivamente: N` | `0` |

```text
$ mover --papelera
Papelera ACTIVADA (2 elemento(s); se conservan 7 día(s))
  #8           /host/home/usuario/Descargas/La Sociedad de la Nieve (2023).mkv (700.0 MB) · borrado 2026-11-12 08:29 · caduca 2026-11-19 08:29
  #7     [DIR] /host/home/usuario/Descargas/Muestras (12.0 MB) · borrado 2026-11-11 22:10 · caduca 2026-11-18 22:10

$ mover --papelera-desactivar
Papelera desactivada: los próximos borrados serán definitivos (lo que ya contiene se conserva hasta caducar).

$ mover --papelera
Papelera DESACTIVADA: los borrados son definitivos (2 elemento(s) antiguos; activar con --papelera-activar)
  #8           /host/home/usuario/Descargas/La Sociedad de la Nieve (2023).mkv (700.0 MB) · borrado 2026-11-12 08:29 · caduca 2026-11-19 08:29
  …

$ mover --borrar -y /host/home/usuario/Descargas/basura.tmp
Papelera desactivada: el borrado será definitivo.
Eliminados definitivamente: 1
```

#### Historial: `--historial [N]` y `--limpiar-historial` *(no disponible en --legacy)*

Cada fichero que termina (OK o ERR) queda registrado con fecha, tamaño, duración y velocidad media (no se calcula en movimientos instantáneos de menos de 1 s). «Limpiar OK» en la cola **no** borra el historial.

```text
$ mover --historial 10
  2026-11-12 08:31  [MOVER] [OK ] Dune Parte Dos (2024) 2160p.mkv (2.3 GB) · 1:22 · 28.7 MB/s
  2026-11-12 08:29  [MOVER] [OK ] Oppenheimer (2023).mkv (1.5 GB) · 0:00 · --
  2026-11-11 23:02  [MOVER] [ERR] Muestra.mkv (12.0 MB) · 0:03 · -- | Tamaño final distinto al origen
  Mostrando 3 de 3 registro(s)
```

`N` por defecto `30`. `--limpiar-historial` borra el historial de `mover` (`Registros de historial eliminados: N`). Código: `0`.

#### Otras opciones

| Opción | Efecto |
|---|---|
| `--log [N]` | Últimos `N` (def. `40`) eventos de tipo `mover` del registro compartido. Los borrados y renombrados son de tipo `sistema`: consúltalos con `toolbox web logs sistema`. Con `--legacy` lee `mover.log`. |
| `--legacy [...]` | Usa el script Bash clásico (cola propia en `/config/cli/mover`). |
| `-h`, `--help`, `help` | Ayuda integrada. |

#### Menú interactivo TUI (`mover`)

Al ejecutar `mover` (o `toolbox` → `1`) aparece el resumen de estado y el menú. Tras cada acción pide `Pulsa Enter para continuar...`.

| Tecla | Acción |
|---|---|
| `1` | **Explorar y añadir a la cola** (ver comandos del explorador). |
| `2` | **Gestionar toda la cola**. |
| `3` · `p` | **Solo pendientes**. |
| `4` | **Progreso en vivo**: repite el resumen cada 1,5 s (fase, %, velocidad, ETA y global). `Ctrl+C` vuelve. |
| `5` · `s` | **Iniciar / detener** el worker (necesita la web). |
| `6` | **Editar configuración**: carpeta origen, destino por defecto, operación y límite KB/s (Enter conserva el valor). *(El asistente completo con conflictos, verificación y favoritos es solo --legacy; en el modo unificado esos ajustes están en la web.)* |
| `7` | **Ver configuración actual** (JSON). |
| `8` | **Registro**: últimos 40 eventos de `mover`. |
| `9` · `h` · `?` | **Ayuda**. |
| `i` | **Historial**: últimas 30 transferencias y opción de vaciarlo. |
| `t` | **Papelera** (submenú, ver abajo). |
| `0` · `q` | **Salir** (el worker sigue en segundo plano). |

**Explorador (opción `1`).** La cabecera muestra la carpeta, la operación (`COPIAR`/`MOVER`), el destino, el orden y el filtro. Cada fila lleva número, fecha de modificación, tamaño, insignia Switch y el estado en cola si ya está encolado (`(PEND)`, `(OK)`…).

| Comando | Acción |
|---|---|
| `N` | Si es carpeta, entra; si es fichero, lo añade a la cola. |
| `1,3,5-8` | Añade esos **ficheros** (las carpetas de la selección se ignoran; usa `r N`). |
| `t` | Añade todos los ficheros visibles (no las carpetas). |
| `r N` | Añade la carpeta `N` entera, con subcarpetas. |
| `?TEXTO` | **Busca** recursivamente desde la carpeta actual (como `--buscar`), lista los resultados y pregunta `Números a añadir a la cola (ej: 1,3-5; Enter = ninguno)`; las carpetas elegidas se añaden enteras. *(no disponible en --legacy)* |
| `orden nombre\|fecha\|tamano [desc]` | Cambia el orden del listado (`orden fecha desc` = lo más reciente arriba). *(no disponible en --legacy)* |
| `tam N` · `s N` | Tamaño total y nº de ficheros de `N`. |
| `ren N` · `rn N` | Renombra `N` (pide el nombre nuevo). |
| `del N[,M-K]` · `rm …` | Envía a la **papelera** los elementos indicados (confirmación, por defecto no). Con la papelera desactivada pregunta `¿Eliminar DEFINITIVAMENTE…? (papelera desactivada)`. |
| `o` | Alterna la operación entre **COPIAR** y **MOVER** para lo que añadas a continuación. |
| `d` | Pide la carpeta destino local para lo siguiente que añadas. *(El navegador de carpetas para elegirla es solo --legacy.)* |
| `f1`, `f2`… | Destino favorito *(solo --legacy)*. |
| `a` | Vuelve al destino automático de Configuración. |
| `/TEXTO` · `*` | Filtra la carpeta actual por nombre · quita el filtro. |
| `..` | Sube a la carpeta superior. |
| `v` · Enter | Vuelve al menú. |

**Gestión de cola (opciones `2` y `3`).** Los números son de fila de la lista mostrada (admiten listas y rangos: `1,3-5`).

| Comando | Acción |
|---|---|
| `t` | Alterna entre **todos** y **solo pendientes**. |
| `ord` | Ordena automáticamente (activos primero, luego errores y terminados; dentro, por ruta de origen). |
| `/TEXTO` · `*` · `/` | Filtra por ruta de origen, estado (`/err`) o ID · quita el filtro. |
| `b N[,M-K]` | Quita elementos de la cola (no los que están en `PROC`). |
| `p N[,M-K]` | Pausa / reanuda. |
| `o N[,M-K]` | Alterna COPIAR/MOVER en elementos sin terminar. |
| `d N[,M-K]` | Cambia su carpeta destino (la pide). |
| `sub N` · `baj N` · `top N` | Sube una posición, baja una, o pone primero (admiten listas). |
| `m N POS` | Mover a la fila `POS` *(solo --legacy)*. |
| `#ID` | Referirse a un elemento por ID *(solo --legacy)*. |
| `c` · `f` · `e` · `x` | Limpia completados · limpia errores · reintenta errores · vacía pendientes y pausados (pide confirmación). |
| `v` · Enter | Vuelve. |

**Papelera (tecla `t`).** Lista la papelera (indicando si está activada) y acepta: `r ID[,ID]` restaurar · `b ID[,ID]` borrar definitivamente (confirma) · `x` vaciar todo (confirma) · `a` activar/desactivar la papelera (desactivar pide confirmación) · `v`/Enter volver. Con la papelera vacía solo se ofrecen `a` y `v`. Aquí se usan los **IDs** de la papelera (`#7`), no números de fila.

**Etiquetas y estados.** Operación: `[COPIAR]` / `[MOVER]` (`[COP ]` / `[MOV ]` *(solo --legacy)*). Estados: `PEND` pendiente · `REAN` reanudable tras un corte o parada · `PAUS` pausado · `PROC` en curso · `OK` terminado · `ERR` error (reintentable con `e`).

---

### 3. FTP y Nintendo Switch `subeftp` (`scripts/subeftp.sh`)

Sube ficheros por FTP/FTPS con `curl` (reanudación `-C -`, límite de velocidad, reintentos) con utilidades para la Nintendo Switch (DBI, `ftpd`, `sys-ftpd`).

```text
subeftp                                     # menú TUI
subeftp [-j] [-d DESTINO] [-s] RUTA...      # añadir a la cola FTP
subeftp --juego RUTA... [-d DESTINO] [-s]   # juego completo en orden
subeftp --backup [-d CARPETA] [-n] [-f]     # backup de la microSD sin juegos
subeftp --OPCIÓN [ARGUMENTOS]               # consultas y acciones (una por llamada)
```

#### Añadir a la cola FTP

| Opción | Valor | Por defecto | Efecto |
|---|---|---|---|
| `RUTA...` | ficheros o carpetas locales | — | Se añaden a la cola FTP. Las carpetas se suben a `DESTINO/<carpeta>/<subrutas>`. Tras añadir, la cola FTP se reordena sola por juego (Base → Update → DLC). |
| `-d RUTA`, `--destino RUTA`, `--destino=RUTA` | carpeta remota | carpeta remota de Configuración | Destino en el servidor FTP (ej. `-d /install`). |
| `-j`, `--solo-switch` | — | todo | Solo `.nsp`, `.nsz`, `.xci`, `.xcz`, `.nro`. |
| `-s`, `--subir`, `--ahora`, `--start`, `--now`, `-i`, `--iniciar` | — | preguntar | Arranca la subida en cuanto se añade, sin preguntar (sin terminal y sin esta opción no arranca). |
| `--anadir` · `--` | — | — | Igual que en `mover`. |

Salida: `Añadidos a la cola compartida (ftp): N (omitidos: M)`. Códigos: igual que en `mover`.

| Opción | Efecto | Salida | Código |
|---|---|---|---|
| `--subir` (sola) | Arranca la cola FTP. | `Worker ftp iniciado en segundo plano.` | `0`; `1` sin web. |
| `--parar` | Parada limpia: corta `curl` y deja el fichero `REAN` para reanudar con `-C -`. | `Detención solicitada para ftp.` | `0`; `1` sin web. |

#### `subeftp --juego RUTA... [-d DESTINO] [-s]` *(no disponible en --legacy)*

Encola **juegos completos** en el orden que necesita DBI: **Base → Update (de menor a mayor versión) → DLC**.

1. Recorre cada `RUTA` (carpetas completas o ficheros) y toma solo ficheros de Switch.
2. Agrupa por juego usando el *title id* del nombre (`[0100F2C0115B6000]`): las updates (`…800`) y los DLC (`…1xxx`) se asocian a su base. Sin *title id*, agrupa por el nombre limpio. Los `.nro` van en un grupo «Homebrew».
3. Avisa si hay updates o DLC **sin el juego base** en la selección y omite lo que ya está en la cola sin terminar (`Ya estaba en la cola: …`).
4. Sube todos los ficheros a `DESTINO/<fichero>` (estructura plana, como espera DBI). `DESTINO` por defecto: carpeta remota de Configuración.

| Opción | Efecto |
|---|---|
| `-d RUTA`, `--destino RUTA`, `--destino=RUTA` | Carpeta remota. |
| `-s`, `--subir`, `--ahora`, `-i`, `--iniciar` | Arranca la subida sin preguntar (si no, pregunta con terminal; sin terminal no arranca). |

```text
$ subeftp --juego "/host/home/usuario/Juegos/Zelda TOTK" -d /install
  • The Legend of Zelda Tears of the Kingdom [0100F2C0115B6000]: Base ✓ · 1 update(s) · 1 DLC
Añadidos a la cola FTP: 3 archivo(s) en orden Base → Update → DLC
¿Iniciar la subida ahora? [S/n]:
```

Con una update suelta aparecería `⚠ 'Mario Kart 8 Deluxe': hay 1 update(s) y 0 DLC sin el juego base en la selección`. Código: `0` si se añadió algo; `1` si no se añadió nada (sin ficheros de Switch, todo ya encolado o rutas inexistentes).

#### `subeftp --espacio`

Pregunta al servidor FTP cuánto espacio libre queda (la SD de la Switch) probando, en orden, `AVBL <carpeta remota>`, `AVBL`, `SITE FREE` y `SITE DF`. Muchos servidores de la Switch no implementan ninguno: entonces se informa sin error.

```text
$ subeftp --espacio
Espacio libre en la SD: 39.0 GB (método AVBL). Pendiente: 17.4 GB → cabe.
$ subeftp --espacio        # servidor sin soporte
El servidor FTP no informa del espacio libre. Pendiente de subir: 17.4 GB
```

- **Comprobación automática:** antes de subir cada fichero, la cola consulta el espacio; si se conoce y el fichero no cabe, ese elemento termina en `ERR` con `Espacio insuficiente en la SD (libre X, necesita Y)` y la cola sigue con el siguiente. Si el servidor no lo soporta, no se vuelve a preguntar en esa ejecución.
- El último valor conocido aparece en `--estado` (`Espacio libre en la SD: …`, con `⚠ NO CABE lo pendiente` si procede) y en la web.
- Código: `0`; `1` si no se pudo conectar (`(No se pudo consultar el espacio: …)`) o no hay servidor configurado.

#### `subeftp --backup [-d CARPETA] [-n | --completo] [-x PATRÓN]... [-f]` *(no disponible en --legacy)*

Descarga la microSD de la Switch a `CARPETA` (por defecto, *Carpeta de backups* de Configuración) **excluyendo los juegos y las carpetas pesadas** (capturas, emuMMC, copias de la NAND, ROMs) y los ficheros `.nsp`/`.nsz`/`.xci`/`.xcz`/`.nca`; ver [exclusiones](#backup-de-la-microsd). En la Switch, abre **DBI → servidor FTP en modo «explorar SD»** (o `ftpd` / `sys-ftpd`): así la raíz del FTP es la SD. No uses el modo de instalar juegos de DBI.

| Opción | Efecto |
|---|---|
| `-d RUTA`, `--destino RUTA`, `--destino=RUTA` | Carpeta local donde guardar el backup. |
| `-n`, `--incremental` | Actualiza siempre `CARPETA/switch-sd/`: solo descarga lo nuevo o con otro tamaño y reanuda los `.part` a medias. |
| `--completo` | Crea una carpeta nueva `CARPETA/switch-sd-AAAAMMDD-HHMMSS/` (por defecto, salvo que *Incremental* esté activo en Configuración). |
| `-x PATRÓN`, `--excluir PATRÓN` | Excluye además esa carpeta solo en esta ejecución (repetible, ej. `-x /emuMMC`). |
| `-f`, `--seguir`, `--esperar` | Espera a que termine mostrando el progreso (útil en `cron`, el código de salida refleja el resultado). Con terminal se sigue siempre; `Ctrl+C` deja de mirar y el backup **continúa** en la web. |

- **Con la web activa** el backup corre en segundo plano en la web (se ve en *Ajustes → Backup de la microSD*). **Sin web** se hace en primer plano y `Ctrl+C` lo detiene limpiamente.
- `subeftp --backup-estado`: progreso o resultado del último backup. `subeftp --backup-parar`: detiene el backup en marcha.
- Salida: `✓ Backup completado: N/N fichero(s), X GB`, la carpeta, las carpetas de juegos excluidas y los errores. Código: `0` si todo fue bien; `1` con errores, detenido, sin carpeta de backups, cola FTP subiendo (`HTTP 409`) o FTP inaccesible.

```bash
docker exec -it toolbox subeftp --backup -d /host/mnt/backups/switch          # carpeta nueva con fecha
docker exec toolbox subeftp --backup -n -f -d /host/mnt/backups/switch        # cron: incremental y esperar
```

#### `--estado`, `--pendientes`, `--probar`, `--buscar-switch`

- `--estado` / `--pendientes`: como en `mover`, más la línea del espacio de la SD. Ejemplo:

  ```text
  === FTP / SWITCH (Cola compartida Web + SSH) ===
    Estado: DETENIDO | Pendientes: 3 (17.4 GB) | Pausados: 0 | OK: 0 | ERR: 0
    Espacio libre en la SD: desconocido (consulta con: subeftp --espacio)
      1. [#3] [PEND] [SUBIR] [BASE] Juegos/Zelda TOTK/The Legend of Zelda Tears of the Kingdom [0100F2C0115B6000][v0].nsp -> The Legend of Zelda Tears of the Kingdom [0100F2C0115B6000][v0].nsp (16.0 GB)
  ```

  *(El tiempo estimado según el límite de velocidad de `--pendientes` es solo --legacy.)*
- `--probar`: prueba la conexión y la carpeta remota. Salida `Conexión correcta con IP:puerto.` (código `0`) o el error (código `1`). *(Ofrecer buscar la Switch si falla es solo --legacy.)*
- `--buscar-switch`: escanea las subredes /24 de la LAN y lista `IP:puerto (banner)`. Prueba el puerto de Configuración y el 5000; si el configurado es 21 o 5000, prueba 5000, 2121 y 21. Con terminal pregunta si guardar la primera en Configuración. Código `0` si encuentra alguna; `1` si no.

#### Opciones compartidas con `mover`

`--listar`, `--buscar`, `--tamano`, `--renombrar`, `--cambiar-destino` (aquí con carpeta **remota**), `--borrar`, `--papelera`, `--restaurar`, `--vaciar-papelera`, `--historial [N]`, `--limpiar-historial` (borra el historial FTP), `--log [N]` (eventos `ftp`; con `--legacy` lee `subeftp.log`), `--legacy` y `-h` funcionan igual que en [`mover`](#2-mover--copiar-local-mover-scriptsmoversh), sobre la carpeta local de Configuración de FTP. La papelera es la misma para los dos comandos.

#### Menú interactivo TUI (`subeftp`)

| Tecla | Acción |
|---|---|
| `1` | Explorar y añadir a la cola. |
| `2` | Gestionar toda la cola. |
| `3` · `p` | Solo pendientes. |
| `4` | Progreso en vivo (`Ctrl+C` vuelve). |
| `5` · `s` | Iniciar / detener la subida (necesita la web). *(Detectar y limpiar un `curl` huérfano es solo --legacy.)* |
| `6` | Probar conexión FTP. |
| `7` | Buscar la IP de la Switch en la red (`b` como atajo *(solo --legacy)*). |
| `8` | Editar configuración FTP: IP, puerto, usuario, carpeta local, carpeta remota, límite KB/s y contraseña (Enter conserva). |
| `9` | Registro: últimos 40 eventos `ftp`. |
| `e` | Espacio libre en la SD (`--espacio`). |
| `b` | Backup de la microSD: muestra exclusiones, pide carpeta e incremental y sigue el progreso (si ya hay uno en marcha, ofrece detenerlo). |
| `i` | Historial (y opción de vaciarlo). |
| `t` | Papelera (mismo submenú que en `mover`). |
| `h` · `?` | Ayuda. |
| `0` · `q` | Salir. |

**Explorador:** los mismos comandos que en `mover` (`N`, `1,3-5`, `t`, `r N`, `?TEXTO`, `orden …`, `tam N`, `ren N`, `del …`, `a`, `/TEXTO`, `*`, `..`, `v`) más:

| Comando | Acción |
|---|---|
| `j` | Añade solo los ficheros de Switch visibles. |
| `jc N[,M-K]` | **Juego completo**: encola las carpetas o ficheros indicados como `--juego` (Base → Update → DLC, con avisos). *(no disponible en --legacy)* |
| `d` | Pide la carpeta remota destino. *(El selector remoto con atajos `inst` → `/install`, `nsp` → `/nsp`, `sw` → `/switch`, `sd` → `/`, `n NOMBRE` y `e RUTA` es solo --legacy.)* |

**Cola:** los mismos comandos que en `mover` salvo `o` (no aplica a FTP); `ord` **auto-ordena por juego** (`[BASE]` → `[UPD]` → `[DLC]` → `[NRO]`).

**Insignias y estados.** Switch: `[BASE]`, `[UPD]` (*title id* acabado en `800` o `[vNNNN]`), `[DLC]`, `[NRO]`. Estados: `PEND`, `REAN`, `PAUS`, `PROC` (subiendo; `SUB` *(solo --legacy)*), `OK`, `ERR`.

---

### 4. Recetas paso a paso

**Mover las descargas terminadas a la biblioteca (TUI).**
1. `docker exec -it toolbox mover` → `1` (Explorar). Se abre la carpeta de origen de Configuración.
2. `orden fecha desc` para ver arriba lo último descargado.
3. `o` hasta que la cabecera diga `Operación: MOVER`; `d` y escribe `/host/mnt/multimedia/Peliculas`.
4. Añade con `3` (un fichero), `1,4-6` (varios) o `r 2` (una carpeta entera); `v` para volver.
5. `5` arranca; `4` muestra el progreso (`Ctrl+C` para salir). Al terminar, `i` muestra el historial.

**Lo mismo en una línea (o desde cron).**

```bash
docker exec toolbox mover -m -i -d /host/mnt/multimedia/Peliculas "/host/home/usuario/Descargas/Oppenheimer (2023).mkv"
docker exec toolbox mover --estado
```

**Subir un juego completo a la Switch.**

```bash
docker exec -it toolbox subeftp --buscar-switch        # primera vez: localizar y guardar IP/puerto
docker exec -it toolbox subeftp --probar
docker exec -it toolbox subeftp --espacio              # ¿cabe en la SD?
docker exec -it toolbox subeftp --juego "/host/home/usuario/Juegos/Zelda TOTK" -d /install -s
docker exec -it toolbox subeftp --estado               # fase, % y progreso global
```

En el menú: `subeftp` → `1` → entra en la carpeta de juegos → `jc 3` (la carpeta del juego) → `5` para empezar.

**Buscar algo y añadirlo.** `mover` → `1` → `?dune 2160p` → aparecen los resultados numerados → escribe `1` → se añade (si es una carpeta, entera). Sin menú: `mover --buscar "dune 2160p" /host/home/usuario/Descargas` y luego `mover -c -i "<ruta mostrada>"`.

**Recuperar algo borrado.**

```bash
docker exec -it toolbox mover --borrar "/host/home/usuario/Descargas/Muestras"   # pide confirmación
docker exec -it toolbox mover --papelera                                       # anota el ID (#7)
docker exec -it toolbox mover --restaurar 7
```

**Revisar lo que se ha hecho.** `mover --historial 50`, `subeftp --historial`, `toolbox web historial todos 100`; los errores incluyen el motivo. Para el detalle, `mover --log 100` o `toolbox web logs sistema 30` (borrados, renombrados y papelera).

---

## 🔌 API REST

La web y los comandos SSH usan la misma API en `http://127.0.0.1:$PORT` (sin autenticación: ver [la advertencia de seguridad](#-publicar-con-nginx-proxy-manager-y-tinyauth)). Todas las rutas locales deben estar dentro de `DATA_ROOT`; si no, la API responde `400`.

| Método y ruta | Parámetros / cuerpo | Respuesta |
|---|---|---|
| `GET /api/health` | — | `{"status":"ok"}` |
| `GET /api/status` | — | `workers.mover` y `workers.ftp`: `running`, `stopping`, `counts`, `pending_bytes`, `phase` (`idle`·`preparing`·`transferring`), `current` (`id`, `name`, `rel_src`, `bytes_done`, `total_bytes`, `speed_bps`, `eta_s`, `percent`) o `null`, `aggregate` (`total_items`, `done_items`, `total_bytes`, `done_bytes`, `eta_s`), `progress` (formato anterior). Mover añade `space_check`; FTP añade `remote_free_bytes` (último valor conocido o `null`) y `remote_space`. |
| `GET /api/fs/list` | `path`, `kind=mover\|ftp`, `show_hidden`, `sort=name\|mtime\|size` (`name`), `order=asc\|desc` (`asc`) | `path`, `parent`, `data_root`, `bookmarks`, `disk`, `sort`, `order`, `dirs[]` (`name`, `path`, `is_dir`, `mtime`, `size:null`), `files[]` (`name`, `path`, `size`, `size_bytes`, `mtime`, `switch_type`, `queue_status`). Carpetas siempre primero. |
| `GET /api/fs/search` | `q` (obligatorio), `path` (origen de Configuración), `kind`, `limit` (300, máx. 1000) | `{path, query, results[] (name, path, rel, is_dir, size, mtime, switch_type), truncated}` |
| `GET /api/fs/dir-size` | `path` | `{path, size_bytes, files_count}` |
| `POST /api/fs/mkdir` · `/api/fs/rename` | `{parent, name}` · `{path, new_name}` | `{path}` |
| `POST /api/fs/delete` | `{paths[], permanent:false}` | `{trashed[] (id, original_path), deleted}`. `400` si es una ruta protegida o no se puede usar la papelera en ese disco (reintentar con `permanent:true`). |
| `GET /api/trash` | — | `{items[] (id, original_path, name, trashed_at, expires_at, size, is_dir), trash_days, trash_enabled}` |
| `POST /api/trash/restore` | `{ids[]}` | `{restored[] (id, path, renamed)}` |
| `POST /api/trash/empty` | `{ids[]}` o `{}` (todo) | `{removed}` |
| `GET /api/queue` | `kind`, `only_pending` | `{items[]}` con todos los campos de la cola más `rel_src` y `rel_dest`. |
| `POST /api/queue/add` | `{kind, paths[], recursive, operation, dest_dir, only_switch, base_browse_dir}` | `{added, skipped, items[]}` |
| `POST /api/queue/{kind}/start` · `/stop` · `/auto-order` · `/retry-errors` · `/clear?scope=completed\|errors\|pending` | — | Acciones sobre la cola. |
| `POST /api/queue/item/{id}/pause` · `/toggle-op` · `/move {direction}` · `DELETE /api/queue/item/{id}` | — | Acciones sobre un elemento. |
| `POST /api/queue/change-dest` | `{item_ids[], dest_dir}` | `{updated}` |
| `GET /api/history` · `DELETE /api/history` | `kind=todos\|mover\|ftp`, `limit` (100), `offset` | `{items[] (id, kind, op, src, dest, size, status, started_at, finished_at, duration_s, avg_bps, error), total}` · `{removed}` |
| `GET /api/ftp/backup` | — | `{running, stopping, progress, last}`. `progress`/`last`: `phase` (`scanning`·`downloading`·`done`), `target`, `files_total`, `files_done`, `files_skipped`, `files_failed`, `bytes_total`, `bytes_done`, `current`, `excluded_dirs[]`, `excluded_files`, `errors[]`, `status` (`OK`·`ERR`·`STOP`) y `error`. También va en `GET /api/status` como `backup`. |
| `POST /api/ftp/backup/start` · `/stop` | `{dest_dir?, incremental?, extra_exclude?[]}` · — | Estado del backup. `400` sin carpeta de backups; `409` si la cola FTP está subiendo (y `POST /api/queue/ftp/start` da `409` durante un backup). |
| `POST /api/ftp/add-game` | `{paths[], dest_dir?}` | `{added, items[], warnings[], groups[] (title_id, name, base, upd, dlc)}` |
| `GET /api/ftp/space` | — | `{supported, free_bytes, pending_bytes, enough, method, error?}` |
| `POST /api/ftp/test` · `GET /api/ftp/dirs?path=` · `POST /api/ftp/scan-switch` | — | Prueba de conexión, carpetas remotas y escáner LAN. |
| `GET /api/settings` · `PUT /api/settings` | Ajustes (velocidades en KB/s; `mover_favorites` como lista, texto JSON o una ruta por línea; `trash_days` 0-365; `trash_enabled` true/false) | Ajustes sin la contraseña FTP. |
| `GET /api/logs` · `DELETE /api/logs` | `kind=todos\|mover\|ftp\|sistema`, `limit` | `{logs[]}` |

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
6. **Papelera:** borrar desde la web o con `--borrar` hace un `rename` a `<punto de montaje>/.toolbox-trash/<ID>/<nombre>` (sin salir de `DATA_ROOT`), así que es instantáneo y no copia datos. Si el destino de la papelera está en otro sistema de ficheros o es de solo lectura, el borrado se rechaza y hay que pedirlo como definitivo. La carpeta `.toolbox-trash` no aparece en el explorador ni en las búsquedas.
7. **Historial:** cada elemento que termina en `OK` o `ERR` se guarda en la tabla `history` (hasta 20.000 registros) con inicio, fin, duración y velocidad media.

---

## 🎮 Detalles técnicos: FTP y Nintendo Switch (DBI / ftpd)

- **Transferencia segura con `curl`:** credenciales en archivo temporal con permisos `600` (nunca expuestas en `ps`), soporte `--ftp-create-dirs`, `--limit-rate`, `--ssl-reqd` (FTPS explícito), `--disable-epsv` (pasivo) o `--ftp-port -` (activo).
- **Reanudación y reintentos:** comprueba el tamaño en el servidor; si coincide lo marca `OK`; si es menor por un corte reanuda con `-C -`. Errores temporales reintentan con espera exponencial (`espera × 3^intento`, máximo `600 s`), mientras que errores de credenciales/permisos (`9`, `67`, `69`, `78`) fallan sin bucle infinito.
- **Nintendo Switch (`DBI` / `ftpd` / `sys-ftpd`):**
  - Desactiva **Verificar tamaño final** cuando uses **DBI**, ya que DBI instala los paquetes al vuelo sin guardar el archivo en el destino.
  - **Espacio en la SD:** antes de cada subida se consulta el espacio libre (`AVBL`, `SITE FREE`, `SITE DF`). Si el fichero no cabe, el elemento termina en `ERR` sin intentar la subida; si el servidor no lo soporta, no se vuelve a preguntar en esa ejecución.
  - **Juego completo:** `--juego` / `jc` / *Subir juego completo* agrupan por *title id* (`base = tid & ~0x1FFF`; `…800` = update, bit `0x1000` = DLC) y encolan Base → Update → DLC en una carpeta remota plana.
  - <a id="backup-de-la-microsd"></a>**Backup de la microSD:** en la Switch, inicia el FTP de **DBI en modo «explorar SD»** (no el de instalar juegos; también valen `ftpd` / `sys-ftpd`), de modo que `/` es la raíz de la SD. Recorre el árbol con `MLSD` (o `LIST` si el servidor no lo soporta) usando una sola conexión, salta las carpetas excluidas y descarga el resto con `RETR` a `fichero.part`, que se renombra al terminar (se conserva la fecha si el servidor la da). Antes de copiar comprueba que cabe en el disco local. Si el servidor no admite `REST`, el fichero se reintenta desde cero. Usa los reintentos y la espera de *FTP*, pero **no** el límite de velocidad (es de subida).
    - **Exclusiones por defecto** (solo se guardan los ficheros básicos: configuración, homebrew y partidas guardadas). Todas se pueden cambiar en *Ajustes* (o con `8` en el menú de `subeftp`; escribe `defecto` para volver a ellas) y la web tiene **↺ Restaurar exclusiones por defecto**:

      | Patrón | Qué es |
      |---|---|
      | `Nintendo/Contents` | Juegos, updates y DLC instalados (sin `/` inicial: a cualquier nivel, también `emuMMC/…/Nintendo/Contents`). |
      | `Nintendo/Album` | Capturas y vídeos. |
      | `/emuMMC` | emuNAND en ficheros (decenas de GB). |
      | `/backup` | Copias de la NAND hechas con Hekate (~30 GB). |
      | `/install`, `/nsp`, `/xci`, `/games`, `/juegos` | Carpetas habituales de juegos sin instalar (solo en la raíz). |
      | `/roms` | ROMs de emuladores. |
      | `.nsp`, `.nsz`, `.xci`, `.xcz`, `.nca` | Ficheros de juego estén donde estén. |

      Los patrones admiten `*` y `?` y no distinguen mayúsculas. Se conserva lo demás: `atmosphere`, `bootloader`, `switch`, `config`, `Nintendo/save`, guardados de JKSV/Checkpoint… Para incluir algo excluido (p. ej. las capturas), quita su línea.
    - Ajustes en `settings.json`: `ftp_backup_dir`, `ftp_backup_remote_root`, `ftp_backup_incremental`, `ftp_backup_exclude`, `ftp_backup_exclude_ext`.
  - El **buscador de Switch en red** escanea en lotes concurrentes de 50 sockets con timeout de `1,8 s` sobre interfaces físicas reales, probando por defecto los puertos `5000`, `2121` y `21` y leyendo el banner FTP `220`.

---

## 🔒 Publicar con Nginx Proxy Manager y Tinyauth

Al usar `--network host`, puedes exponer la web de forma segura tras HTTPS con Nginx Proxy Manager y Tinyauth:

1. En **Nginx Proxy Manager**, crea un Proxy Host apuntando a `http://<IP-del-servidor>:8090` (activa **Websockets Support**).
2. En la pestaña **Advanced**, pega el bloque de `tinyauth` documentado en el repositorio `docker` (`docker/README.md`).

> ⚠️ **La web no tiene login propio.** Con `--network host`, Uvicorn escucha en `0.0.0.0:8090`, es decir, en todas las interfaces del servidor, y el contenedor tiene acceso de `root` a todo `/` (montado en `/host`). Tinyauth solo protege el acceso por el dominio publicado en NPM: cualquiera en la LAN o en la VPN puede entrar directamente por `http://<IP>:8090` y crear, renombrar o borrar archivos. Cierra el puerto `8090` en el firewall (o limita su origen) y accede siempre a través de NPM.

---

## 💾 Persistencia y copias de seguridad

Todo el estado persistente del contenedor vive en `/config` (mapeado a `./config` en `docker/toolbox/`):

| Ruta dentro de `/config` | Contenido |
|---|---|
| `/config/settings.json` | Configuración unificada Web + SSH (permisos `600`, incluye contraseña FTP). |
| `/config/queue.db` | Base de datos SQLite compartida (Web + SSH): colas (`queue_items`), registro (`logs`, últimos 5.000), historial de transferencias (`history`, últimos 20.000) y papelera (`trash`). |
| `/config/cli/mover/` | Solo utilizado si ejecutas `mover --legacy` o fuera de Docker. |
| `/config/cli/ftp/` | Solo utilizado si ejecutas `subeftp --legacy` o fuera de Docker. |

Además, los elementos de la papelera viven en `.toolbox-trash/` en la raíz de cada punto de montaje del servidor (por ejemplo `/mnt/disco1/.toolbox-trash/`) hasta que caducan (`trash_days`, por defecto 7 días) o se vacían.

---

## 🧪 Desarrollo y Tests

```bash
cd toolbox

# Validación Bash y ShellCheck (si está instalado)
bash -n entrypoint.sh scripts/*.sh
shellcheck -S warning scripts/*.sh

# Dependencias de desarrollo (FastAPI, Uvicorn, Pydantic + httpx para TestClient y pyftpdlib para el backup)
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt

# Compilación, sintaxis JS y tests unitarios Python
python3 -m py_compile app/*.py tests/*.py
python3 -m unittest discover -s tests -v
node --check static/app.js
```

La CI (`.github/workflows/ci.yml`) ejecuta en cada `push`/PR a `main` (con Python 3.12 y `requirements-dev.txt`) el `bash -n`, `shellcheck -S warning scripts/*.sh`, `py_compile` y los tests unitarios; **no** ejecuta `node --check`, así que conviene lanzarlo en local al tocar `static/app.js`.
