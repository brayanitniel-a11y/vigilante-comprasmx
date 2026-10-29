# Vigilante de anexos y actas de ComprasMX (Telegram)

Le mandas por Telegram el **número de procedimiento** y el bot, cada pocos minutos, lo busca en la
**consulta pública de ComprasMX**, abre solo las secciones **Anexos** y **Actas**, y te avisa cuando
aparece algo nuevo. Con el mismo chat agregas y quitas procedimientos.

Comandos: `/agregar <numero> [alias]` · `/quitar <# | alias | numero>` · `/lista` · `/revisar` · `/ayuda`
(también puedes mandar solo el número, por ejemplo `IO-89-L47-830124970-N-14-2026`).

## Cómo está armado

| Archivo | Para qué sirve |
|---|---|
| `bot.py` | Telegram, lista de procedimientos, avisos. Modos `once`, `loop` y `probe`. |
| `fetcher.py` | Lee ComprasMX con un navegador (Playwright). **Es lo único que depende del sitio.** |
| `.github/workflows/vigilante.yml` | Hace que GitHub ejecute el bot cada 5 minutos. |
| `test_bot.py` | Pruebas de la lógica con un sitio simulado. |

Ahorro de recursos: un solo navegador por ciclo (sin imágenes), una búsqueda por número, solo se abren
Anexos/Actas, y solo te avisa de lo nuevo.

---

## Parte 1. Crear el bot de Telegram (5 min)

1. En Telegram busca **@BotFather** → `/newbot` → pon nombre y usuario (debe terminar en `bot`).
2. BotFather te da un **token** (`123456:ABC...`). Es como una contraseña: no lo compartas ni lo subas a GitHub.
3. Busca tu bot nuevo y mándale `/start` (sin esto no puede escribirte).
4. Obtén tu **chat id**: escríbele a **@userinfobot**; te responde con un número (`Id`). Anótalo.

## Parte 2. Subir el proyecto a GitHub (10 min)

Para la prueba conviene un repositorio **público**: Actions es gratis e ilimitado en repos públicos.
En repos privados el plan gratis solo da 2,000 minutos al mes y este bot, cada 5 minutos, los gasta en
unos pocos días. Tranquilo: **tu token no queda expuesto** (va en Secrets) y **tu lista de procedimientos
tampoco** (se guarda en la caché de Actions, no en el repositorio).

1. En github.com: **+ → New repository**. Nombre: `vigilante-comprasmx`. Elige **Public**. Crear.
2. **Add file → Upload files** y arrastra: `bot.py`, `fetcher.py`, `requirements.txt`, `test_bot.py`,
   `.gitignore`. Pulsa **Commit changes**.
3. El workflow va en una carpeta oculta, así que créalo a mano: **Add file → Create new file**, en el
   nombre escribe exactamente `.github/workflows/vigilante.yml` (al teclear las `/` se crean las carpetas),
   pega dentro el contenido del archivo `vigilante.yml` y **Commit changes**.

## Parte 3. Guardar los secretos

En el repo: **Settings → Secrets and variables → Actions → New repository secret**. Crea dos:

- `TELEGRAM_TOKEN` = el token de BotFather
- `ALLOWED_CHAT_IDS` = tu chat id (si son varias personas, sepáralos con coma)

Solo los chats de esa lista pueden usar el bot.

## Parte 4. Calibrar la lectura de ComprasMX (importante)

El bot "ve" el sitio como una persona: busca el campo *Número de procedimiento*, el botón *Buscar*, abre
el procedimiento y lee la tabla **ANEXOS** (tipo de documento y *Total de archivos*; cada vez que sube el
total de una fila, te avisa). **Esta calibración ya se probó con un procedimiento real y funciona**; repítela
solo si ComprasMX cambia su diseño o el bot empieza a fallar.

Atajo: al subir a la rama `calibrar` un archivo `probe.txt` con un número, el workflow corre la prueba solo y
publica el resultado en la rama `calibracion-resultado` (así se puede revisar sin descargar nada).

1. Repo → pestaña **Actions** → si pide habilitar workflows, acéptalo.
2. A la izquierda **Vigilante ComprasMX** → botón **Run workflow**.
3. En el cuadro *probe* escribe un número de procedimiento real (que ya tenga anexos) → **Run workflow**.
4. Espera 2–3 min, abre la corrida y entra al paso **Calibracion**. Al final debe mostrar algo así:
   ```
   6 anexos/actas en LA-07-110-007000999-T-730-2026:
     - [ANEXOS] ANEXO TÉCNICO (90 archivos) |  | https://...
   ```
5. Si falla o sale vacío: en la parte baja de la corrida, sección **Artifacts**, descarga **debug**
   (capturas `.png` y páginas `.html` de cada paso). **Mándamelas** (o descríbeme qué pasó) y ajusto los
   textos de `fetcher.py`. Es normal que haga falta un ajuste la primera vez.
6. Prueba también con un número que no exista: debe decir `NO ENCONTRADO`.

Si aparece una sección de actas en un procedimiento, el bot la detecta (secciones cuyo título incluya acta, junta, fallo o apertura), igual que las filas nuevas de la tabla ANEXOS.

## Parte 5. Ponerlo a trabajar

1. Con la calibración bien, el workflow ya corre solo cada 5 minutos (puede tardar unos minutos en la
   primera ejecución programada).
2. En Telegram mándale al bot: `/agregar IO-89-L47-830124970-N-14-2026 mi-alias`.
3. En la siguiente corrida te contesta "Base lista: N anexos/actas actuales". A partir de ahí, cada
   anexo o acta nueva te llega como mensaje.
4. Para quitarlo: `/quitar 1` (o por alias). `/lista` muestra todo.

Para probar el aviso real: agrega un procedimiento y espera a que publiquen algo, o agrégalo cuando
sepas que van a subir un acta (por ejemplo, el día de la junta de aclaraciones).

### Límites de GitHub Actions (por eso es solo una prueba)

- **No es tiempo real.** El mínimo es cada 5 minutos y GitHub suele retrasar las ejecuciones varios minutos.
- **Tus comandos tardan en contestarse**: se leen al inicio de la siguiente corrida (hasta ~5–10 min).
- Cada corrida instala el navegador (~1 min). En repos públicos es gratis.
- En repos públicos GitHub **desactiva** los workflows programados tras 60 días sin actividad en el
  repositorio. Si pasa, reactívalo en Actions. (Para entonces ya deberías estar en un servidor.)
- La lista de procedimientos vive en una caché de Actions: si GitHub la borrara, tendrías que volver a
  mandar tus `/agregar` (son 10).

## Parte 6. Pasarlo a un servidor (cuando sea estable)

Un servidor responde al instante a tus comandos y revisa en el intervalo que quieras. Basta un VPS
pequeño con Ubuntu (1 GB de RAM, ~4–6 USD al mes) o una Raspberry Pi que no apagues.

**Antes de arrancar el servidor, desactiva el workflow en GitHub** (Actions → Vigilante ComprasMX → ⋯ →
*Disable workflow*). Si los dos corren a la vez se pelean por los mensajes de Telegram.

```bash
# 1. Preparar el sistema
sudo apt update && sudo apt install -y python3-venv git
sudo timedatectl set-timezone America/Mexico_City

# 2. Descargar tu repo y crear el entorno
git clone https://github.com/TU_USUARIO/vigilante-comprasmx.git
cd vigilante-comprasmx
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install --with-deps chromium

# 3. Guardar los secretos en un archivo solo legible por ti
cat > .env <<'EOF'
TELEGRAM_TOKEN=pon_aqui_tu_token
ALLOWED_CHAT_IDS=pon_aqui_tu_chat_id
POLL_SECONDS=120
EOF
chmod 600 .env

# 4. Probar a mano
set -a; . ./.env; set +a
.venv/bin/python bot.py probe IO-89-L47-830124970-N-14-2026
```

Dejarlo corriendo siempre (se reinicia solo si falla o si se reinicia el servidor). Crea
`/etc/systemd/system/vigilante.service` con `sudo nano` y este contenido (cambia `TU_USUARIO`):

```ini
[Unit]
Description=Vigilante ComprasMX
After=network-online.target

[Service]
User=TU_USUARIO
WorkingDirectory=/home/TU_USUARIO/vigilante-comprasmx
EnvironmentFile=/home/TU_USUARIO/vigilante-comprasmx/.env
ExecStart=/home/TU_USUARIO/vigilante-comprasmx/.venv/bin/python bot.py loop
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now vigilante
sudo systemctl status vigilante     # debe decir "active (running)"
journalctl -u vigilante -f          # ver en vivo lo que hace (Ctrl+C para salir)
```

Luego mándale `/agregar` de nuevo a tus procedimientos (la lista de la prueba no se traslada).
El estado queda en `state/state.json`; respáldalo si quieres.

Para actualizar el código después: `git pull && sudo systemctl restart vigilante`.

## Variables que puedes ajustar

| Variable | Por defecto | Qué hace |
|---|---|---|
| `POLL_SECONDS` | 300 | Cada cuántos segundos revisa (solo servidor, de 7 a 21 h) |
| `NIGHT_POLL_SECONDS` | 900 | Intervalo de noche (solo servidor) |
| `TABS` | `Anuncios vigentes,Anuncios en seguimiento,Anuncios concluidos` | Pestañas donde busca el número |
| `ALERT_AFTER_FAILURES` | 6 | Fallos seguidos antes de avisarte que no puede leer |
| `DEBUG` | 0 | `1` guarda capturas en `debug/` |

## Si algo falla

- **"Aún no encuentro el procedimiento"**: revisa que el número esté igual que en ComprasMX; puede no
  estar publicado todavía (el bot sigue intentando).
- **"No puedo leer ... "**: ComprasMX cambió o está caído. Corre la calibración (Parte 4) y compárala.
- **El bot no contesta**: ¿le mandaste `/start`? ¿Tu chat id está en `ALLOWED_CHAT_IDS`? En GitHub, ¿la
  última corrida terminó en verde?
- Usa el sitio con respeto: el bot hace pocas consultas y con pausas. No bajes el intervalo de forma
  agresiva.
