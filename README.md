# 🧰 Toolbox (Utilidades del Servidor: Mover/Copiar + FTP/Switch)

Proyecto modular en Docker que reúne en **un único repositorio y contenedor** tus utilidades de servidor (empezando por **Mover/Copiar** y **FTP/Switch**, preparado para ir incorporando más herramientas):

1. **Interfaz Web moderna** (FastAPI + HTML/CSS/JS) para explorar todo el servidor, gestionar colas de copia/movimiento local y subidas FTP/FTPS con progreso en vivo, buscar la Nintendo Switch en la red local y consultar registros.
2. **Scripts CLI integrados** (`toolbox`, `mover.sh` y `subeftp.sh`) dentro del propio contenedor y del repositorio, para poder operarlos indistintamente desde navegador, terminal interactiva (`docker exec`), línea de comandos o tareas `cron`.

---

## 📋 Índice

- [Estructura del proyecto](#-estructura-del-proyecto)
- [Puesta en marcha con Docker](#-puesta-en-marcha-con-docker)
- [Variables de entorno y permisos](#-variables-de-entorno-y-permisos)
- [Uso de la Interfaz Web](#-uso-de-la-interfaz-web)
- [Uso por Terminal / CLI integrado (`toolbox`)](#-uso-por-terminal--cli-integrado-toolbox)
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
├── app/
│   ├── main.py                 # API REST, explorador /host y estáticos
│   ├── models.py               # Esquemas Pydantic
│   ├── config.py               # settings.json (permisos 600) y validación de rutas
│   ├── db.py                   # Persistencia SQLite (queue.db) de colas y logs
│   ├── mover_worker.py         # Motor local: mv mismo disco, rsync .mover-partial, verificación
│   ├── ftp_worker.py           # Motor FTP/FTPS: curl, reanudación -C -, límite KB/s, listado remoto
│   ├── switch_utils.py         # Insignias Switch, auto-orden Base→UPD→DLC y escáner LAN
│   └── worker_manager.py       # Hilos en segundo plano independientes (Mover y FTP)
├── static/
│   ├── index.html              # Interfaz web en español
│   ├── style.css               # Tema oscuro adaptable
│   └── app.js                  # Control de explorador, colas, modal FTP y estado en vivo
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

La imagen se publica en Docker Hub como **`alebupal/toolbox:latest`** y el despliegue está preparado en `docker/toolbox/config.sh` (`MODE="run"` compatible con `update.sh`, igual que `tvspy`).

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
- **Automática (GitHub Actions):** el workflow `.github/workflows/docker-publish.yml` construye y sube `alebupal/toolbox:latest` (`linux/amd64` y `linux/arm64`) en cada `push` a `main` en cuanto añadas en **Settings → Secrets and variables → Actions** los secrets:
  - `DOCKERHUB_USERNAME` (tu usuario, ej. `alebupal`)
  - `DOCKERHUB_TOKEN` (Access Token de Docker Hub)

### Cómo funciona el acceso a todo el servidor

- El contenedor monta el sistema de ficheros raíz del servidor (`/`) en **`/host`** dentro del contenedor.
- Así puedes navegar desde la web (y desde los scripts CLI dentro del contenedor) por `/host/home/...`, `/host/mnt/...`, `/host/media/...`, discos externos o carpetas de descargas de otros servicios.
- Como todo el servidor cuelga de un mismo árbol `/host`, cuando mueves un archivo dentro de la misma partición/disco real del servidor, el sistema detecta que comparten dispositivo (`st_dev`) y realiza un **`mv` instantáneo** sin copiar datos. Si están en discos distintos, usa **`rsync`** con reanudación y verificación.
- Por seguridad en recorridos recursivos desde `/host`, se omiten automáticamente los pseudo-sistemas del kernel (`/host/proc`, `/host/sys`, `/host/dev`, `/host/run`).

---

## ⚙️ Variables de entorno y permisos

En `docker/toolbox/config.sh` (o mediante `-e` en `docker run`) puedes ajustar las variables del contenedor:

| Variable | Valor por defecto | Descripción |
|---|---|---|
| `PORT` | `8090` | Puerto HTTP en el que escucha la interfaz web. |
| `DATA_ROOT` | `/host` | Raíz permitida dentro del contenedor para la API web. |
| `PUID` / `PGID` | `0` / `0` | Usuario con el que corre el proceso. `0:0` (`root`) permite leer, copiar y mover archivos en **cualquier directorio del servidor**; puedes poner `1000:1000` si solo vas a operar en carpetas de tu usuario. |
| `TZ` | `Europe/Madrid` | Zona horaria para fechas en logs. |

### Modo de red (`--network host`)

El contenedor utiliza `--network host` por defecto porque permite:
1. **Auto-descubrimiento de Nintendo Switch en LAN**: el contenedor ve directamente las interfaces físicas del servidor (`eth*`, `en*`, `wl*`) y puede escanear la subred `/24` local.
2. **Modo FTP activo** además del modo pasivo, sin problemas de mapeo dinámico de puertos de datos.

---

## 🌐 Uso de la Interfaz Web

La aplicación dispone de 4 pestañas principales y una barra superior con el estado en vivo de ambos motores (**Cola Mover / Copiar** y **Cola FTP / Switch**):

### 1. 📁 Explorar
- Cambia rápidamente el destino del añadido entre **Para Mover / Copiar** y **Para Subir FTP**.
- **Accesos rápidos**: botones a `Raíz servidor (/host)`, `Home`, `/mnt`, `/media` y tus carpetas favoritas configuradas.
- **Filtrado y selección**: filtra por nombre en tiempo real, muestra u oculta ficheros ocultos, marca archivos o carpetas con checkboxes y usa:
  - `+ Añadir` en un archivo concreto.
  - `+ Recursivo` para encolar una carpeta completa manteniendo su estructura.
  - `➕ Añadir seleccionados` para lotes.
  - `🎮 Añadir juegos Switch` (en modo FTP) para filtrar y encolar solo `.nsp`, `.nsz`, `.xci`, `.xcz` y `.nro`.
- **Selector de destino**:
  - **Automático**: usa la carpeta destino por defecto configurada (recreando o no subcarpetas según configuración).
  - **Elegido**: abre un explorador modal (de carpetas locales en Mover, o de carpetas remotas del servidor FTP en modo FTP, con atajos `/`, `/install`, `/nsp`, `/switch`).

### 2. 📋 Colas
- Vistas independientes para **Cola Mover** y **Cola FTP**, con filtro `Solo activos` (`PEND`, `REAN`, `PAUS`, `PROC`).
- **Controles por elemento**: subir (`↑`), bajar (`↓`), poner primero (`⤒`), pausar/reanudar (`⏯`), alternar `Copiar ⇄ Mover` (en Mover) y quitar (`✕`).
- **Controles globales**:
  - `▶ Iniciar` / `⏹ Detener` (la parada detiene limpiamente `rsync` o `curl` dejando el ítem como `REAN` para continuar exactamente donde se quedó).
  - `↕ Auto-ordenar`: en FTP agrupa y ordena por juego (`BASE → UPD → DLC → NRO`); en Mover ordena por ruta.
  - `↻ Reintentar errores`, `🧹 Limpiar OK`, `🧹 Limpiar ERR`, `🗑 Vaciar pendientes`.

### 3. ⚙️ Configuración
- **Mover / Copiar**: carpeta origen inicial, destino por defecto, operación por defecto, política de conflictos (`omitir`, `sobrescribir`, `renombrar`), verificación (`tamaño`, `SHA-256`, `no`), límite KB/s, reintentos, espera exponencial y lista de favoritos.
- **FTP / Switch**: servidor/IP, puerto, usuario, contraseña (no se vuelve a mostrar en claro tras guardarse), carpeta remota, estructura, modo pasivo, FTPS/TLS, verificación de tamaño final, límite KB/s y reintentos.
- Incluye botones directos para **🔌 Probar conexión** y **🔍 Buscar Switch** (escanea tu red LAN mostrando las IPs detectadas con su banner FTP `220` y permite aplicarla con un clic).

### 4. 📜 Registro
- Visor de eventos almacenados en SQLite filtrable por módulo (`Todos`, `Mover`, `FTP`, `Sistema`).

---

## 💻 Uso por Terminal / CLI integrado (`toolbox`)

Dentro del contenedor (y también en la carpeta `scripts/` del repositorio) están disponibles los scripts originales integrados bajo el comando unificado **`toolbox`** (además de los alias **`mover`** y **`subeftp`**):

### Menú interactivo unificado

```bash
docker exec -it toolbox toolbox
```

Abre un menú para entrar al gestor interactivo de `mover.sh`, al de `subeftp.sh`, ver el estado combinado o consultar la ayuda.

### Subcomandos directos

```bash
# Ver ayuda unificada
docker exec -it toolbox toolbox --help

# Ver estado de ambas colas CLI y de la web
docker exec -it toolbox toolbox estado

# Usar mover.sh directamente (recuerda que el servidor está en /host dentro del contenedor)
docker exec -it toolbox toolbox mover -m -i /host/home/usuario/pelicula.mkv
docker exec -it toolbox toolbox mover --estado
docker exec -it toolbox toolbox mover --iniciar
docker exec -it toolbox toolbox mover --parar

# Usar subeftp.sh directamente
docker exec -it toolbox toolbox ftp -s /host/home/usuario/juego.nsp
docker exec -it toolbox toolbox ftp --estado
docker exec -it toolbox toolbox ftp --subir
docker exec -it toolbox toolbox ftp --parar

# Alias directos dentro del contenedor
docker exec -it toolbox mover --help
docker exec -it toolbox subeftp --help
```

### Persistencia del modo CLI dentro del contenedor

Cuando ejecutas `toolbox`, `mover` o `subeftp` dentro del contenedor, sus archivos de configuración, colas y logs (`mover.conf`, `subeftp.conf`, `cola.txt`, `*.log`) se guardan automáticamente en:

- `/config/cli/mover/` (variable `MOVER_DATA_DIR`)
- `/config/cli/ftp/` (variable `SUBEFTP_DATA_DIR`)

De este modo permanecen en tu volumen persistente `/config` aunque actualices o recrees la imagen.

> **Nota importante sobre web vs CLI:** la interfaz web utiliza la base de datos SQLite (`/config/queue.db` + `/config/settings.json`) mientras que los scripts CLI mantienen sus propios ficheros clásicos (`/config/cli/...`). Ambos conviven dentro del mismo contenedor y pueden trabajar sobre todo `/host`.

### Uso fuera de Docker (en el host directamente)

También puedes ejecutar `./scripts/toolbox.sh`, `./scripts/mover.sh` o `./scripts/subeftp.sh` directamente en tu máquina Linux fuera de Docker: si no existen `MOVER_DATA_DIR` ni `SUBEFTP_DATA_DIR`, cada script guarda sus ficheros en su propia carpeta, igual que siempre.

---

## 🔄 Detalles técnicos: Mover / Copiar local

1. **Comprobación de firma de origen:** guarda `tamaño:mtime_ns` al encolar; si el archivo cambió antes de procesarse o reanudarse, descarta parciales antiguos y vuelve a empezar.
2. **Movimiento instantáneo en mismo disco:** compara `st_dev` entre el archivo origen y la carpeta destino; si coinciden, ejecuta `os.replace()` (`mv` atómico).
3. **Copia segura entre discos con `rsync`:**
   - Verifica antes espacio libre disponible (`os.statvfs`).
   - Copia a `<destino>/.mover-partial/<archivo>` con `--partial --append-verify` y `--bwlimit` opcional.
   - Nunca sobrescribe el nombre final a medias: solo renombra al destino definitivo tras superar la verificación (`tamaño` o `SHA-256`; al mover siempre se verifica al menos el tamaño).
4. **Políticas de conflicto:**
   - `omitir`: si copiar y mismo tamaño → `OK`; si mover y mismo tamaño → compara contenido byte a byte antes de borrar el origen; si distinto tamaño → `ERR` conservando el origen.
   - `sobrescribir`: reemplaza atómicamente tras verificar la copia en `.mover-partial`.
   - `renombrar`: guarda como `nombre (1).ext`, `nombre (2).ext`, etc.
5. **Limpieza al mover carpetas completas:** al completar el último archivo de una carpeta añadida recursivamente, borra las subcarpetas vacías del origen (`rmdir`, sin borrar nada que no se moviera).

---

## 🎮 Detalles técnicos: FTP y Nintendo Switch (DBI / ftpd)

- **Transferencia con `curl`:** credenciales pasadas mediante archivo temporal con permisos `600` (nunca expuestas en argumentos visibles `ps`), soporte `--ftp-create-dirs`, `--limit-rate`, `--ssl-reqd` (FTPS explícito), `--disable-epsv` (pasivo) o `--ftp-port -` (activo).
- **Reanudación y reintentos:** comprueba si el fichero ya existe en el servidor; si tiene el mismo tamaño lo omite (`OK`), si es menor y procede de un corte reanuda con `-C -`. Los errores de red reintentan con espera exponencial (`espera × 3^intento`, tope `600 s`), mientras que errores de autenticación o acceso denegado (`curl` códigos `9`, `67`, `69`, `78`) fallan sin reintentar en bucle.
- **Nintendo Switch (DBI / `ftpd` / `sys-ftpd`):**
  - Desactiva **Verificar tamaño final** cuando uses DBI (ya que DBI instala los juegos al vuelo y no conserva el archivo en destino).
  - Las insignias `[BASE]`, `[UPD]`, `[DLC]`, `[NRO]` se detectan automáticamente y la cola se ordena en orden `Base → Update → DLC`.
  - El **buscador de Switch en red** escanea en lotes concurrentes de 50 sockets con timeout de `1,8 s` (ideal para el Wi-Fi de la consola) sobre interfaces físicas reales, probando por defecto los puertos `5000`, `2121` y `21` y leyendo el banner FTP `220`.

---

## 🔒 Publicar con Nginx Proxy Manager y Tinyauth

Al usar `--network host`, puedes exponer la web de forma segura tras HTTPS con Nginx Proxy Manager y Tinyauth:

1. En **Nginx Proxy Manager**, crea un Proxy Host apuntando a `http://<IP-del-servidor>:8090` (activa **Websockets Support**).
2. En la pestaña **Advanced**, pega el bloque de `tinyauth` documentado en [docker/README.md](../docker/README.md#L292-L335).

---

## 💾 Persistencia y copias de seguridad

Todo el estado persistente del contenedor vive en el volumen `/config` (mapeado a `./config` en `docker/toolbox/`):

| Ruta dentro de `/config` | Contenido |
|---|---|
| `/config/settings.json` | Configuración de la interfaz web (permisos `600`, incluye contraseña FTP). |
| `/config/queue.db` | Base de datos SQLite con las colas web y los logs. |
| `/config/cli/mover/` | Configuración, cola y logs de `mover.sh` cuando se usa por CLI. |
| `/config/cli/ftp/` | Configuración, cola y logs de `subeftp.sh` cuando se usa por CLI. |

---

## 🧪 Desarrollo y Tests

Para ejecutar las comprobaciones de sintaxis, ShellCheck y tests unitarios localmente:

```bash
cd toolbox

# Validación Bash y ShellCheck (si está instalado)
bash -n entrypoint.sh scripts/*.sh
shellcheck -S warning scripts/*.sh

# Compilación y tests Python (no requiere dependencias externas en el host)
python3 -m py_compile app/*.py tests/*.py
python3 -m unittest discover -s tests -v
```
